"""Pay in CELO — the buyer transfers CELO to our own address and we watch for it.

There is no x402 here, on purpose. x402's signed authorization exists so a
third-party facilitator can broadcast and pay gas on the buyer's behalf; we are
the recipient, so we do not need a middleman at all. CELO could not use x402
anyway: its token implements neither EIP-3009 nor EIP-2612 (verified on-chain),
so nothing can be signed over to the facilitator.

Two things this lane must get right, both verified against the live chain:

  * **the transfer has to go through the token contract** (0x471EcE37…). A
    native CELO send emits no log whatsoever — a receipt with zero logs — so a
    wallet's plain "send" could never be detected. The page therefore builds an
    ERC-20 `transfer(payTo, amount)`, which does emit a Transfer event.
  * **the price comes from the chain**, not an API: the same Uniswap v3 pools
    the Arena's swap box uses quote CELO→USDC, so the rate a buyer is charged
    is the rate they could have swapped at. Four fee tiers are quoted and the
    best is taken; a single tier can be ~20% off.

An order is a promise to deliver at a quoted rate: it stores what was asked
for, what it costs in CELO, and (after payment) what was delivered. Orders are
never deleted — a paid order that failed to deliver has to stay visible.

OFF unless CELO_PAY_ENABLED=1.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .. import db
from ..services import oplog
from . import x402

RPC_URLS = ("https://forno.celo.org", "https://celo.drpc.org")
CELO_TOKEN = "0x471ece3750da237f93b8e339c536989b8978a438"   # CELO's own ERC-20 interface
QUOTER = "0x82825d0554fa07f7fc52ab63c961f330fdefa8e8"       # Uniswap QuoterV2 on Celo
USDC = "0xceba9300f2b948710d2653dd7b07f33a8b32118c"
FEE_TIERS = (100, 500, 3000, 10000)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

QUOTE_TTL_S = 600            # how long a quoted CELO amount is honoured
CLAIM_TTL_S = 86400          # a payment can still be claimed a day later
PRICE_CACHE_S = 60
BUFFER_BPS = 200             # quote 2% above spot so a small dip still covers it
TOLERANCE_BPS = 300          # accept a payment within 3% under the quote
PRICE_FLOOR, PRICE_CEIL = 0.0001, 1000.0    # refuse an absurd quote outright
SCAN_BLOCKS = 7200           # ~2h of Celo blocks for the "lost the tab" fallback
MAX_OPEN_PER_PAYER = 5

PRODUCTS = ("chat", "brief", "task_monitor", "task_research")

_schema_ready = False
_price_cache: Dict[str, Any] = {"at": 0.0, "usd": 0.0, "fee": 0}


class Invalid(ValueError):
    """Buyer-safe rejection."""


# ----------------------------------------------------------------- switch --

def enabled() -> bool:
    return (os.environ.get("CELO_PAY_ENABLED", "0").strip().lower() in ("1", "true", "on", "yes")
            and bool(pay_to()))


def pay_to() -> str:
    return x402.pay_to()          # one receiving address for every lane


# -------------------------------------------------------------------- rpc --

RPC_STATS: Dict[str, Dict[str, Any]] = {}      # in-process, for the diagnostics view


def _stat(url: str, method: str, ms: float, ok: bool, err: str = "") -> None:
    k = f"{url}|{method}"
    st = RPC_STATS.setdefault(k, {"url": url, "method": method, "calls": 0, "fails": 0,
                                  "ms_total": 0.0, "last_ms": 0.0, "last_at": 0.0, "last_error": ""})
    st["calls"] += 1
    st["ms_total"] += ms
    st["last_ms"] = round(ms, 1)
    st["last_at"] = time.time()
    if not ok:
        st["fails"] += 1
        st["last_error"] = (err or "")[:160]


def _rpc(method: str, params: list) -> Any:
    """Every Celo RPC call goes through here, so the diagnostics view can show
    which gateway answered, how fast, and what failed."""
    last: Optional[Exception] = None
    for url in RPC_URLS:
        t0 = time.time()
        try:
            r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                      "params": params}, timeout=15.0)
            r.raise_for_status()
            body = r.json() or {}
            if body.get("error"):
                raise RuntimeError(str(body["error"])[:200])
            _stat(url, method, (time.time() - t0) * 1000, True)
            return body.get("result")
        except Exception as e:                    # try the next gateway
            _stat(url, method, (time.time() - t0) * 1000, False, repr(e))
            last = e
    oplog.error("celo.rpc", f"{method}: {last!r}"[:300])
    raise RuntimeError(f"celo rpc failed: {last!r}")


def _a32(a: str) -> str:
    return str(a).lower().replace("0x", "").rjust(64, "0")


def _u32(n: int) -> str:
    return format(int(n), "x").rjust(64, "0")


# ------------------------------------------------------------------ price --

def price_usd() -> Tuple[float, int]:
    """USD per CELO, straight from the Uniswap pools (best of four fee tiers),
    cached for a minute. Returns (usd_per_celo, fee_tier)."""
    now = time.time()
    if _price_cache["usd"] and now - _price_cache["at"] < PRICE_CACHE_S:
        return float(_price_cache["usd"]), int(_price_cache["fee"])
    one = 10 ** 18
    best_out, best_fee = 0, 0
    quotes: Dict[str, float] = {}
    for fee in FEE_TIERS:
        data = ("0xc6a5026a" + _a32(CELO_TOKEN) + _a32(USDC) + _u32(one) + _u32(fee) + _u32(0))
        try:
            res = _rpc("eth_call", [{"to": QUOTER, "data": data}, "latest"])
            out = int(str(res)[2:66], 16)
        except Exception:
            continue
        quotes[str(fee)] = round(out / 10 ** 6, 6)
        if out > best_out:
            best_out, best_fee = out, fee
    usd = best_out / 10 ** 6
    if not (PRICE_FLOOR <= usd <= PRICE_CEIL):
        oplog.error("celo.price", f"refused rate {usd} (tiers: {quotes})", status=503)
        raise Invalid("CELO pricing is unavailable right now — pay in USDC instead")
    _price_cache.update({"at": now, "usd": usd, "fee": best_fee})
    oplog.op("celo.price", params={"usd": round(usd, 6), "fee_tier": best_fee,
                                   "ms": round((time.time() - now) * 1000),
                                   "tiers": quotes})
    return usd, best_fee


def celo_for_usd(usd: float) -> Tuple[int, float, int]:
    """(wei, rate, fee_tier) for a dollar amount, with the buffer applied."""
    rate, fee = price_usd()
    wei = int(float(usd) / rate * (10 ** 18) * (10000 + BUFFER_BPS) / 10000)
    return wei, rate, fee


# ------------------------------------------------------------------ store --

def ensure_schema() -> None:
    """Additive and self-healing (constitution §3)."""
    global _schema_ready
    if _schema_ready:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS celo_orders (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id     TEXT NOT NULL,
        token_hash   TEXT NOT NULL DEFAULT '',
        payer        TEXT NOT NULL DEFAULT '',
        product      TEXT NOT NULL DEFAULT '',
        amount_usd   REAL NOT NULL DEFAULT 0,
        celo_wei     TEXT NOT NULL DEFAULT '0',
        rate_usd     REAL NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT 'awaiting',
        tx           TEXT NOT NULL DEFAULT '',
        paid_wei     TEXT NOT NULL DEFAULT '0',
        paid_at      REAL NOT NULL DEFAULT 0,
        created_at   REAL NOT NULL DEFAULT 0,
        expires_at   REAL NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '',
        result_json  TEXT NOT NULL DEFAULT '',
        error        TEXT NOT NULL DEFAULT ''
    )""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_celo_orders_id ON celo_orders(order_id)")
    # One transfer can only ever pay for one order.
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_celo_orders_tx ON celo_orders(tx) WHERE tx<>''")
    db.execute("CREATE INDEX IF NOT EXISTS idx_celo_orders_payer ON celo_orders(payer, status)")
    existing = {r["name"] for r in db.query_all("PRAGMA table_info(celo_orders)")}
    if "events_json" not in existing:
        try:
            db.execute("ALTER TABLE celo_orders ADD COLUMN events_json TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
    _schema_ready = True


MAX_EVENTS = 40


def events_of(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        v = json.loads(row.get("events_json") or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def note(order_id: str, step: str, detail: str = "", **extra: Any) -> None:
    """Append one line to an order's own trail. This is what a buyer (and we)
    read to answer "what happened to my payment?" — every branch that accepts
    or refuses money writes here, in words that are safe to show."""
    row = db.query_one("SELECT events_json FROM celo_orders WHERE order_id=?", (order_id,))
    if row is None:
        return
    evs = events_of(row)
    evs.append({"ts": round(time.time(), 3), "step": step, "detail": (detail or "")[:300],
                **{k: v for k, v in extra.items() if v is not None}})
    db.execute("UPDATE celo_orders SET events_json=? WHERE order_id=?",
               (json.dumps(evs[-MAX_EVENTS:], default=str), order_id))
    oplog.op("celo." + step, params={"order": order_id, "detail": (detail or "")[:160], **extra})


def _hash(tok: str) -> str:
    return hashlib.sha256((tok or "").encode("utf-8")).hexdigest()


def by_id(order_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM celo_orders WHERE order_id=?", (str(order_id or ""),))


def token_ok(row: Dict[str, Any], token: str) -> bool:
    th = (row or {}).get("token_hash") or ""
    return bool(th) and secrets.compare_digest(th, _hash(token or ""))


def payload_of(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("payload_json") or "{}") or {}
    except ValueError:
        return {}


def result_of(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("result_json") or "{}") or {}
    except ValueError:
        return {}


def create(product: str, payer: str, amount_usd: float,
           payload: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Quote and record an order. The CELO amount is fixed here — that is the
    number the buyer is told to send and the number we hold ourselves to."""
    ensure_schema()
    if product not in PRODUCTS:
        raise Invalid("unknown product")
    payer = (payer or "").strip().lower()
    if not (payer.startswith("0x") and len(payer) == 42):
        raise Invalid("a wallet address is required")
    if not pay_to():
        raise Invalid("this server has no Celo receiving address configured")
    open_n = db.query_one("SELECT COUNT(*) n FROM celo_orders WHERE payer=? AND status='awaiting' "
                          "AND expires_at>?", (payer, time.time())) or {}
    if int(open_n.get("n") or 0) >= MAX_OPEN_PER_PAYER:
        raise Invalid("too many unpaid orders on this wallet — pay or let them expire first")
    wei, rate, _fee = celo_for_usd(amount_usd)
    now = time.time()
    oid = "o_" + uuid.uuid4().hex[:16]
    token = secrets.token_urlsafe(24)
    db.execute(
        "INSERT INTO celo_orders(order_id, token_hash, payer, product, amount_usd, celo_wei, "
        "rate_usd, status, created_at, expires_at, payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (oid, _hash(token), payer, product, float(amount_usd), str(wei), rate, "awaiting",
         now, now + QUOTE_TTL_S, json.dumps(payload or {}, default=str)))
    note(oid, "created", f"{product} · ${float(amount_usd):.4f} = {wei / 10 ** 18:.6f} CELO "
                         f"@ ${rate:.6f}", payer=payer[:10] + "…", celo=round(wei / 10 ** 18, 6))
    return by_id(oid) or {}, token


def view(row: Dict[str, Any], with_result: bool = False) -> Dict[str, Any]:
    wei = int(row.get("celo_wei") or 0)
    out = {
        "order_id": row["order_id"], "product": row["product"], "status": row["status"],
        "amount_usd": round(float(row.get("amount_usd") or 0), 6),
        "celo_wei": str(wei), "celo": round(wei / 10 ** 18, 6),
        "rate_usd": round(float(row.get("rate_usd") or 0), 6),
        "pay_to": pay_to(), "token_contract": CELO_TOKEN,
        "expires_at": float(row.get("expires_at") or 0),
        "created_at": float(row.get("created_at") or 0),
        "tx": row.get("tx") or "",
        "explorer": (x402.CHAIN["explorer_tx"] + row["tx"]) if row.get("tx") else "",
        "paid_at": float(row.get("paid_at") or 0),
        "error": row.get("error") or "",
    }
    if with_result:
        out["result"] = result_of(row)
    return out


# --------------------------------------------------------------- payment --

def _logs_of_receipt(txhash: str) -> Tuple[bool, List[Dict[str, Any]]]:
    rec = _rpc("eth_getTransactionReceipt", [txhash]) or {}
    if not rec:
        return False, []
    ok = str(rec.get("status") or "").lower() == "0x1"
    return ok, rec.get("logs") or []


def _paid_in_logs(logs: List[Dict[str, Any]], payer: str, treasury: str) -> int:
    """Sum of CELO moved payer → treasury in this transaction."""
    want_from = "0x" + payer[2:].rjust(64, "0")
    want_to = "0x" + treasury[2:].rjust(64, "0")
    total = 0
    for lg in logs:
        if (lg.get("address") or "").lower() != CELO_TOKEN:
            continue
        tp = lg.get("topics") or []
        if len(tp) < 3 or str(tp[0]).lower() != TRANSFER_TOPIC:
            continue
        if str(tp[1]).lower() != want_from or str(tp[2]).lower() != want_to:
            continue
        try:
            total += int(str(lg.get("data") or "0x0"), 16)
        except (TypeError, ValueError):
            continue
    return total


def _enough(row: Dict[str, Any], paid_wei: int) -> bool:
    want = int(row.get("celo_wei") or 0)
    return paid_wei >= want * (10000 - TOLERANCE_BPS) // 10000


def attach_tx(row: Dict[str, Any], txhash: str) -> Dict[str, Any]:
    """Verify one transaction against this order and mark it paid. Raises
    Invalid with a buyer-safe reason; never credits on uncertainty."""
    ensure_schema()
    txhash = (txhash or "").strip().lower()
    if not (txhash.startswith("0x") and len(txhash) == 66):
        raise Invalid("that does not look like a transaction hash")
    if row["status"] not in ("awaiting", "paid"):
        return row
    if time.time() > float(row.get("created_at") or 0) + CLAIM_TTL_S:
        raise Invalid("this order is too old to claim — start a new one")
    oid = row["order_id"]
    note(oid, "tx_submitted", txhash)
    dup = db.query_one("SELECT order_id FROM celo_orders WHERE tx=?", (txhash,))
    if dup and dup["order_id"] != oid:
        note(oid, "rejected", f"that transaction already paid order {dup['order_id']}")
        raise Invalid("that transaction already paid for another order")
    ok, logs = _logs_of_receipt(txhash)
    if not logs and not ok:
        note(oid, "pending", "no receipt yet — the transaction is not mined")
        raise Invalid("that transaction is not confirmed yet — try again in a few seconds")
    if not ok:
        note(oid, "rejected", "the transaction reverted on-chain")
        raise Invalid("that transaction failed on-chain — nothing was charged")
    paid = _paid_in_logs(logs, row["payer"], pay_to())
    if paid <= 0:
        note(oid, "rejected", f"no CELO Transfer {row['payer'][:10]}… → {pay_to()[:10]}… in this "
                              f"transaction ({len(logs)} log(s) total). A native send emits no log.")
        raise Invalid("that transaction did not send CELO to the payment address. "
                      "A plain wallet send cannot be detected — use the pay button.")
    want = int(row.get("celo_wei") or 0)
    if not _enough(row, paid):
        note(oid, "rejected", f"paid {paid / 10 ** 18:.6f} CELO, needs "
                              f"{want * (10000 - TOLERANCE_BPS) / 10000 / 10 ** 18:.6f} "
                              f"(quoted {want / 10 ** 18:.6f}, {TOLERANCE_BPS / 100:.0f}% tolerance)")
        raise Invalid("that payment is below the quoted amount")
    note(oid, "verified", f"received {paid / 10 ** 18:.6f} CELO (quoted {want / 10 ** 18:.6f})",
         logs=len(logs))
    db.execute("UPDATE celo_orders SET status='paid', tx=?, paid_wei=?, paid_at=?, error='' "
               "WHERE order_id=? AND status='awaiting'",
               (txhash, str(paid), time.time(), row["order_id"]))
    oplog.op("celo_pay.paid", params={"order": row["order_id"], "product": row["product"],
                                      "usd": row.get("amount_usd"), "tx": txhash[:18]})
    return by_id(row["order_id"]) or row


def find_payment(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fallback for a buyer who closed the tab before the hash came back: look
    for a transfer from this payer to our address in the recent window."""
    ensure_schema()
    treasury = pay_to()
    if not treasury or row["status"] != "awaiting":
        return None
    try:
        latest = int(_rpc("eth_blockNumber", []), 16)
        logs = _rpc("eth_getLogs", [{
            "fromBlock": hex(max(0, latest - SCAN_BLOCKS)), "toBlock": hex(latest),
            "address": CELO_TOKEN,
            "topics": [TRANSFER_TOPIC,
                       "0x" + row["payer"][2:].rjust(64, "0"),
                       "0x" + treasury[2:].rjust(64, "0")],
        }]) or []
    except Exception as e:
        oplog.error("celo_pay.scan", repr(e)[:200], params={"order": row["order_id"]})
        note(row["order_id"], "scan_failed", repr(e)[:160])
        return None
    note(row["order_id"], "scanned", f"{len(logs)} transfer(s) from this wallet in the last "
                                     f"{SCAN_BLOCKS} blocks")
    used = {r["tx"] for r in db.query_all("SELECT tx FROM celo_orders WHERE tx<>''")}
    for lg in logs:
        txh = (lg.get("transactionHash") or "").lower()
        if not txh or txh in used:
            continue
        try:
            amt = int(str(lg.get("data") or "0x0"), 16)
        except (TypeError, ValueError):
            continue
        if _enough(row, amt):
            try:
                return attach_tx(row, txh)
            except Invalid:
                continue
    return None


def store_result(order_id: str, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    note(order_id, "delivered", ", ".join(sorted(k for k in (result or {}) if k != "payment")))
    db.execute("UPDATE celo_orders SET status='delivered', result_json=? WHERE order_id=?",
               (json.dumps(result or {}, default=str), order_id))
    return by_id(order_id)


def store_error(order_id: str, message: str) -> None:
    note(order_id, "deliver_failed", (message or "")[:200])
    db.execute("UPDATE celo_orders SET error=? WHERE order_id=?", ((message or "")[:300], order_id))


def expire_stale(limit: int = 200) -> int:
    """Unpaid orders past their claim window stop being claimable. Rows stay."""
    ensure_schema()
    cur = db.execute("UPDATE celo_orders SET status='expired' WHERE status='awaiting' "
                     "AND created_at < ?", (time.time() - CLAIM_TTL_S,))
    return int(getattr(cur, "rowcount", 0) or 0)


def summary() -> Dict[str, Any]:
    ensure_schema()
    row = db.query_one(
        "SELECT COUNT(*) n, COUNT(DISTINCT payer) payers, COALESCE(SUM(amount_usd),0) usd "
        "FROM celo_orders WHERE status IN ('paid','delivered')") or {}
    return {"orders": int(row.get("n") or 0), "payers": int(row.get("payers") or 0),
            "usd": round(float(row.get("usd") or 0), 4),
            "rate_usd": round(float(_price_cache.get("usd") or 0), 6)}


def recent(limit: int = 30) -> List[Dict[str, Any]]:
    """Paid-in-CELO settlements, in the same shape x402.activity() uses for its
    list. Two lanes pay for the same products, so the public log has to show
    both — otherwise a CELO payment happens on-chain and appears nowhere."""
    ensure_schema()
    rows = db.query_all(
        "SELECT paid_at, created_at, product, amount_usd, tx, payer FROM celo_orders "
        "WHERE status IN ('paid','delivered') AND tx<>'' ORDER BY id DESC LIMIT ?", (int(limit),))
    return [{"ts": float(r["paid_at"] or r["created_at"] or 0), "product": r["product"],
             "amount_usd": round(float(r["amount_usd"] or 0), 4), "asset": "CELO",
             "tx": r["tx"], "payer": (r["payer"] or "").lower()} for r in rows]


def public_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "enabled": enabled(), "pay_to": pay_to(), "token_contract": CELO_TOKEN,
        "quote_ttl_s": QUOTE_TTL_S, "tolerance_bps": TOLERANCE_BPS,
        "buffer_bps": BUFFER_BPS, "symbol": "CELO", "decimals": 18,
    }
    if enabled():
        try:
            cfg["rate_usd"], cfg["fee_tier"] = price_usd()
        except Exception:
            cfg["rate_usd"] = 0.0
    return cfg


# ---------------------------------------------------------- diagnostics --
# Everything below is read-only and exists so a live test can be debugged
# without SSH: which gateway answered, how fast, what each order did, and why
# a payment was refused.

def health() -> Dict[str, Any]:
    """Probe every gateway and the price feed. Safe to call publicly: it says
    nothing that is not already on-chain."""
    gateways = []
    for url in RPC_URLS:
        t0 = time.time()
        try:
            blk = int(_rpc_one(url, "eth_blockNumber", []), 16)
            gateways.append({"url": url, "ok": True, "block": blk,
                             "ms": round((time.time() - t0) * 1000)})
        except Exception as e:
            gateways.append({"url": url, "ok": False, "error": repr(e)[:160],
                             "ms": round((time.time() - t0) * 1000)})
    out: Dict[str, Any] = {"enabled": enabled(), "pay_to": pay_to(), "gateways": gateways,
                           "token_contract": CELO_TOKEN}
    try:
        rate, fee = price_usd()
        out["price"] = {"usd_per_celo": round(rate, 6), "fee_tier": fee,
                        "age_s": round(time.time() - float(_price_cache["at"]), 1)}
    except Exception as e:
        out["price"] = {"error": repr(e)[:160]}
    return out


def _rpc_one(url: str, method: str, params: list) -> Any:
    """One gateway, no failover — the health probe needs per-gateway truth."""
    t0 = time.time()
    try:
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                  "params": params}, timeout=10.0)
        r.raise_for_status()
        body = r.json() or {}
        if body.get("error"):
            raise RuntimeError(str(body["error"])[:200])
        _stat(url, method, (time.time() - t0) * 1000, True)
        return body.get("result")
    except Exception as e:
        _stat(url, method, (time.time() - t0) * 1000, False, repr(e))
        raise


def order_trail(row: Dict[str, Any]) -> Dict[str, Any]:
    """What happened to ONE order, in order. This is what a buyer sees while
    testing — every line was written to be safe to show."""
    return {**view(row), "events": events_of(row),
            "quoted_celo": round(int(row.get("celo_wei") or 0) / 10 ** 18, 6),
            "paid_celo": round(int(row.get("paid_wei") or 0) / 10 ** 18, 6),
            "tolerance_bps": TOLERANCE_BPS}


def recent_orders(limit: int = 20) -> List[Dict[str, Any]]:
    ensure_schema()
    rows = db.query_all("SELECT * FROM celo_orders ORDER BY id DESC LIMIT ?", (int(limit),))
    return [{"order_id": r["order_id"], "product": r["product"], "status": r["status"],
             "payer": (r["payer"] or "")[:10] + "…",
             "usd": round(float(r["amount_usd"] or 0), 4),
             "celo": round(int(r["celo_wei"] or 0) / 10 ** 18, 6),
             "paid_celo": round(int(r["paid_wei"] or 0) / 10 ** 18, 6),
             "tx": r.get("tx") or "", "created_at": float(r.get("created_at") or 0),
             "error": (r.get("error") or "")[:160],
             "events": events_of(r)} for r in rows]


def rpc_stats() -> List[Dict[str, Any]]:
    out = []
    for st in RPC_STATS.values():
        calls = max(1, int(st["calls"]))
        out.append({**{k: st[k] for k in ("url", "method", "calls", "fails", "last_ms",
                                          "last_at", "last_error")},
                    "avg_ms": round(st["ms_total"] / calls, 1)})
    return sorted(out, key=lambda x: (-x["calls"], x["method"]))


def diag(limit: int = 20) -> Dict[str, Any]:
    """The operator's one-stop view while someone is testing the CELO lane."""
    return {"config": public_config(), "health": health(), "rpc": rpc_stats(),
            "summary": summary(), "orders": recent_orders(limit),
            "constants": {"quote_ttl_s": QUOTE_TTL_S, "claim_ttl_s": CLAIM_TTL_S,
                          "buffer_bps": BUFFER_BPS, "tolerance_bps": TOLERANCE_BPS,
                          "scan_blocks": SCAN_BLOCKS,
                          "max_open_per_payer": MAX_OPEN_PER_PAYER}}
