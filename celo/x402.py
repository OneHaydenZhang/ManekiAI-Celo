"""x402 v2 seller on Celo — pay-per-request in USDC (or USA₮), settled by the
Celo facilitator (api.x402.celo.org) or, as an explicit fallback, by our own
registrar wallet (settle.py).

Protocol (specs/x402-specification-v2 + transports-v2/http, verified against
the live facilitator 2026-09-10):

  1. Client calls a paid endpoint → 402 + `PAYMENT-REQUIRED` header
     (base64 JSON PaymentRequired: {x402Version:2, resource, accepts:[…]}).
     `accepts` lists USDC first and USA₮ second — the buyer picks the one it
     holds; both are EIP-3009 stablecoins on Celo.
  2. Client signs an EIP-3009 `TransferWithAuthorization` (EIP-712 domain =
     the token's name()/version(), read from the contracts) and retries with
     `PAYMENT-SIGNATURE` (base64 JSON PaymentPayload).
  3. Server → verify → produces the content → settle → 200 +
     `PAYMENT-RESPONSE` header with the on-chain tx.

Settlement paths (X402_SETTLER):
  facilitator (default)  POST /verify (open) + POST /settle (X-API-Key);
                         the facilitator submits the authorization and pays gas.
  self                   settle.py simulates and broadcasts the authorization
                         from the registrar wallet. Spec-legal, but the hackathon
                         leaderboard may not count builder-broadcast settlements
                         as facilitator settlements — documented fallback only,
                         never chosen automatically.

Ledger: every VERIFIED payment is a row in `x402_payments` (payer+nonce
unique — a replayed payload is refused before any work is done; unverified
attempts never touch the permanent table). Rows are PERMANENT revenue
records (docs/CORE_PRINCIPLES.md §5): status moves
pending → settled | content_failed | settle_failed | settle_pending, never
deleted. `settle_pending` = the authorization may have landed after the
reply was lost; `finalize_pending()` (autopilot loop) checks the chain and
flips it to settled — the buyer's retry with the SAME signature then gets
the content without paying twice (`delivered` flag in meta_json).

Revenue share: an "insight" sale credits the agent's OWNER with a share of the
sale as Gas (default 70%), idempotent per settlement via bonus_grants.

Everything is default-off: no facilitator API key (or no funded registrar in
`self` mode) or no payTo → enabled() is False and every paid endpoint answers 503.
"""
from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

import httpx

from .. import db, service_config
from ..models import points_model, notification_model
from ..services import oplog, pricing
from . import wallet

# --------------------------------------------------------------- constants --

FACILITATOR_DEFAULT = "https://api.x402.celo.org"
CHAIN_ID = 42220
NETWORK = f"eip155:{CHAIN_ID}"                    # CAIP-2
USDC = "0xcebA9300f2b948710d2653dD7B07f33A8B32118C"   # EIP-55 checksum (the docs page mis-cases it)
USAT = "0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771"   # Tether America USD (USA₮), verified on-chain 2026-09-11
USDC_DECIMALS = 6
USDC_EIP712 = {"name": "USDC", "version": "2"}    # contract name()/version(), verified live
# Order matters: the first entry is the default the Arena signs with.
ASSETS: Dict[str, Dict[str, Any]] = {
    "USDC": {"address": USDC, "symbol": "USDC", "decimals": 6, "eip712": {"name": "USDC", "version": "2"}},
    "USAT": {"address": USAT, "symbol": "USA₮", "decimals": 6, "eip712": {"name": "Tether America USD", "version": "1"}},
}
MAX_TIMEOUT_S = 120
CHAIN = {
    "chain_id": CHAIN_ID, "chain_id_hex": "0xa4ec", "name": "Celo",
    "rpc": "https://forno.celo.org", "explorer": "https://celoscan.io",
    "explorer_tx": "https://celoscan.io/tx/", "explorer_address": "https://celoscan.io/address/",
    "symbol": "CELO", "decimals": 18,
}

# What we sell. Prices in USD (= stablecoin units). The operator can override
# with X402_PRICES_JSON or the admin key `x402_prices`, bounded to [0.001, 10].
PRODUCTS: Dict[str, Dict[str, Any]] = {
    "chat":    {"usd": 0.02, "description": "Ask ManekiAI — one market question answered by the platform analyst (same data + model the trading agents use)"},
    "brief":   {"usd": 0.01, "description": "ManekiAI symbol brief — trend, levels, risk and stance for one US-stock perp (shared, refreshed every 10 min)"},
    "insight": {"usd": 0.05, "description": "Latest decision insight of one live ManekiAI trading agent — action, confidence, reasoning and market read"},
}
_PRICE_BOUNDS = (0.001, 10.0)
OWNER_SHARE_DEFAULT = 0.70

# Fixed, neutral strings for buyers; the real exception goes to oplog.
ERR_FACILITATOR_DOWN = "facilitator unreachable"


def _admin(key: str) -> str:
    return service_config._admin_setting(key)


# ----------------------------------------------------------------- config --

def facilitator_url() -> str:
    return (os.environ.get("X402_FACILITATOR_URL", "").strip().rstrip("/")
            or FACILITATOR_DEFAULT)


def api_key() -> str:
    """Facilitator API key (x402.celo.org → connect wallet → Create API key).
    Server-side secret: admin-published > env."""
    return _admin("x402_api_key") or os.environ.get("X402_API_KEY", "").strip()


def pay_to() -> str:
    """Seller payout address on Celo (receive-only). admin > env > the Celo
    Gas treasury — one address collects both top-ups and x402 sales, which
    also keeps the hackathon's per-wallet on-chain accounting in one place
    (deposits.py skips settlement tx hashes so a sale is never re-credited
    as a top-up)."""
    v = (_admin("x402_pay_to") or os.environ.get("X402_PAY_TO", "").strip()
         or service_config.celo_treasury()).lower()
    return v if (v.startswith("0x") and len(v) == 42) else ""


def settler_mode() -> str:
    """'facilitator' (default, needs the API key) | 'self' (explicit opt-in:
    our registrar broadcasts the authorization) | 'off'."""
    want = (os.environ.get("X402_SETTLER", "") or _admin("x402_settler") or "facilitator").strip().lower()
    if want == "self":
        return "self" if wallet.address() else "off"
    return "facilitator" if api_key() else "off"


def enabled() -> bool:
    if (os.environ.get("X402_ENABLED", "1").strip().lower() in ("0", "false", "off", "no")):
        return False
    return bool(pay_to()) and settler_mode() != "off"


def owner_share() -> float:
    try:
        v = float(os.environ.get("X402_OWNER_SHARE", "") or OWNER_SHARE_DEFAULT)
    except ValueError:
        v = OWNER_SHARE_DEFAULT
    return min(1.0, max(0.0, v))


def operator_wallets() -> set:
    """Builder-owned wallets (env X402_OPERATOR_WALLETS, comma list): their
    purchases are recorded but excluded from public activity/summary —
    builder wallets are excluded by the hackathon rules and at volume read
    as farming."""
    raw = os.environ.get("X402_OPERATOR_WALLETS", "") or _admin("x402_operator_wallets") or ""
    return {w.strip().lower() for w in raw.split(",") if w.strip().startswith("0x")}


def public_base() -> str:
    return (os.environ.get("MANEKI_PUBLIC_BASE", "").strip().rstrip("/")
            or "https://manekiai.io")


def links() -> Dict[str, str]:
    out = {"repo": os.environ.get("CELO_PUBLIC_REPO", "").strip() or "https://github.com/OneHaydenZhang/ManekiAI-Celo",
           "dune": os.environ.get("CELO_DUNE_URL", "").strip(),
           "analyst_card": f"{public_base()}/api/agent-card/maneki-analyst"}
    # The hackathon guide page (/hackathon: requirements, journeys, proofs,
    # live counters). Advertised here so the host app can show its top strip;
    # CELO_GUIDE_ENABLED=0 hides both the link and the strip.
    if os.environ.get("CELO_GUIDE_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no"):
        out["guide"] = f"{public_base()}/hackathon"
    return out


def prices() -> Dict[str, float]:
    out = {k: float(v["usd"]) for k, v in PRODUCTS.items()}
    for raw in (os.environ.get("X402_PRICES_JSON", ""), _admin("x402_prices")):
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
            for k, v in (d or {}).items():
                if k in out:
                    v = float(v)
                    if _PRICE_BOUNDS[0] <= v <= _PRICE_BOUNDS[1]:
                        out[k] = v
        except (ValueError, TypeError):
            continue
    return out


def price_usd(product: str) -> float:
    return prices().get(product, 0.0)


def atomic(usd: float, decimals: int = USDC_DECIMALS) -> str:
    return str(int(round(float(usd) * (10 ** decimals))))


# --------------------------------------------------------------- protocol --

def b64e(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def b64d(s: str) -> Any:
    s = (s or "").strip()
    pad = "=" * (-len(s) % 4)
    return json.loads(base64.b64decode(s + pad).decode())


def requirements(product: str, resource_url: str, asset: str = "USDC") -> Dict[str, Any]:
    """One `accepts` entry for `product` in `asset` (USDC by default)."""
    a = ASSETS[asset]
    return {
        "scheme": "exact",
        "network": NETWORK,
        "amount": atomic(price_usd(product), a["decimals"]),
        "asset": a["address"],
        "payTo": pay_to(),
        "maxTimeoutSeconds": MAX_TIMEOUT_S,
        "extra": dict(a["eip712"]),
    }


def accepts(product: str, resource_url: str) -> List[Dict[str, Any]]:
    """Every payment option we offer, USDC first."""
    return [requirements(product, resource_url, sym) for sym in ASSETS]


def resource_info(product: str, resource_url: str) -> Dict[str, Any]:
    return {"url": resource_url, "description": PRODUCTS[product]["description"],
            "mimeType": "application/json"}


def payment_required(product: str, resource_url: str, error: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "x402Version": 2,
        "resource": resource_info(product, resource_url),
        "accepts": accepts(product, resource_url),
        "extensions": {},
    }
    if error:
        out["error"] = error
    return out


def matches(accepted: Dict[str, Any], req: Dict[str, Any]) -> bool:
    """Client's chosen requirements must be OUR offer (same scheme / network /
    asset / payTo, amount ≥ ours). The verifier re-checks the signed
    authorization against `req` anyway; this is the cheap early reject."""
    try:
        return (str(accepted.get("scheme")) == req["scheme"]
                and str(accepted.get("network")) == req["network"]
                and str(accepted.get("asset") or "").lower() == req["asset"].lower()
                and str(accepted.get("payTo") or "").lower() == req["payTo"].lower()
                and int(str(accepted.get("amount") or "0")) >= int(req["amount"]))
    except (TypeError, ValueError):
        return False


def match_offer(accepted: Dict[str, Any], offers: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The offer the client accepted, or None."""
    for req in offers:
        if matches(accepted, req):
            return req
    return None


def asset_symbol(address: str) -> str:
    for sym, a in ASSETS.items():
        if a["address"].lower() == (address or "").lower():
            return sym
    return ""


def payload_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    """payer / nonce / value / to / validity out of an exact-scheme EVM payload."""
    auth = ((payload.get("payload") or {}).get("authorization") or {})
    return {"payer": str(auth.get("from") or "").lower(),
            "nonce": str(auth.get("nonce") or "").lower(),
            "value": str(auth.get("value") or ""),
            "to": str(auth.get("to") or "").lower(),
            "valid_after": str(auth.get("validAfter") or "0"),
            "valid_before": str(auth.get("validBefore") or "")}


_EIP3009_TYPES = {"TransferWithAuthorization": [
    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}


def signature_matches_payer(payload: Dict[str, Any], req: Dict[str, Any]) -> bool:
    """Local EIP-712 check that `payload.payload.signature` really recovers to the
    claimed `from` address for THIS exact authorization (asset domain + amount +
    to + validity window + nonce). No network round trip.

    This exists ONLY for the "already settled, redeliver" recovery path in
    routes.py: once a payment settles on-chain, (payer, nonce) alone are public
    (standard ERC20/AuthorizationUsed event topics) and are NOT proof that the
    caller is the original buyer — a resubmitted request must still show a
    signature that actually recovers to the payer for the stored fields, not
    just matching hex strings. The fresh-payment path already gets this for
    free from the facilitator's/self-settle's own verify(); this covers the
    retry path, which used to skip verification entirely."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        auth = ((payload.get("payload") or {}).get("authorization") or {})
        sig = str((payload.get("payload") or {}).get("signature") or "")
        claimed_from = str(auth.get("from") or "")
        extra = req.get("extra") or {}
        domain = {"name": str(extra.get("name") or ""), "version": str(extra.get("version") or ""),
                  "chainId": CHAIN_ID, "verifyingContract": req["asset"]}
        message = {"from": claimed_from, "to": str(auth.get("to") or ""),
                   "value": int(str(auth.get("value") or "0")),
                   "validAfter": int(str(auth.get("validAfter") or "0")),
                   "validBefore": int(str(auth.get("validBefore") or "0")),
                   "nonce": str(auth.get("nonce") or "")}
        signable = encode_typed_data(domain_data=domain, message_types=_EIP3009_TYPES, message_data=message)
        recovered = Account.recover_message(signable, signature=sig)
        return bool(claimed_from) and recovered.lower() == claimed_from.lower()
    except Exception:
        return False


# ------------------------------------------------------------ facilitator --

def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    k = api_key()
    if k:
        h["X-API-Key"] = k
    return h


def _fac_post(path: str, payload: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    body = {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": req}
    r = httpx.post(f"{facilitator_url()}{path}", json=body, headers=_headers(), timeout=45.0)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {"raw": data}
    data.setdefault("_http", r.status_code)
    return data


def facilitator_verify(payload: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    try:
        d = _fac_post("/verify", payload, req)
    except Exception as e:
        oplog.error("x402.facilitator", f"verify: {e!r}"[:300])
        return {"isValid": False, "invalidReason": ERR_FACILITATOR_DOWN, "transport": True}
    if "isValid" not in d:
        oplog.error("x402.facilitator", f"verify http {d.get('_http')}: {str(d)[:200]}")
        d["isValid"] = False
        d.setdefault("invalidReason", ERR_FACILITATOR_DOWN if int(d.get("_http") or 0) >= 500 else "rejected by facilitator")
        d["transport"] = int(d.get("_http") or 0) >= 500
    return d


def facilitator_settle(payload: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    try:
        d = _fac_post("/settle", payload, req)
    except Exception as e:
        oplog.error("x402.facilitator", f"settle: {e!r}"[:300])
        return {"success": False, "errorReason": ERR_FACILITATOR_DOWN, "transaction": "",
                "network": NETWORK, "transport": True}
    if "success" not in d:
        oplog.error("x402.facilitator", f"settle http {d.get('_http')}: {str(d)[:200]}")
        d["success"] = False
        d.setdefault("errorReason", ERR_FACILITATOR_DOWN if int(d.get("_http") or 0) >= 500 else "rejected by facilitator")
        d["transport"] = int(d.get("_http") or 0) >= 500
    d.setdefault("transaction", "")
    d.setdefault("network", NETWORK)
    return d


def facilitator_supported() -> Dict[str, Any]:
    r = httpx.get(f"{facilitator_url()}/supported", timeout=15.0)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------- dispatch --

def verify(payload: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    mode = settler_mode()
    if mode == "self":
        from . import settle as _s
        return _s.verify(payload, req)
    return facilitator_verify(payload, req)


# Failures the BUYER caused (count toward their cooldown); everything else is
# ours (model busy, facilitator down, our wallet unfunded, expiry after a queue).
PAYER_FAULT_REASONS = ("insufficient_funds", "invalid_signature", "nonce_already_used")


def settle(payload: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    """Settle, then reconcile a lost reply: if the facilitator/RPC failed at the
    transport level (or our own broadcast timed out), the authorization may
    still have landed — the on-chain state is the truth, never the HTTP
    reply. An undecidable outcome is returned with pending=True so the
    caller parks the row as settle_pending instead of failing it."""
    mode = settler_mode()
    if mode == "self":
        from . import settle as _s
        s = _s.settle(payload, req)
        if not s.get("success") and (s.get("errorReason") == "receipt_timeout" or s.get("transport")):
            s = reconcile(payload_fields(payload), req, s)
        return s
    s = facilitator_settle(payload, req)
    if not s.get("success") and s.get("transport"):
        s = reconcile(payload_fields(payload), req, s)
    return s


def reconcile(f: Dict[str, str], req: Dict[str, Any], s: Dict[str, Any]) -> Dict[str, Any]:
    """Decide a lost-reply settlement from the chain. Success ONLY when the
    exact authorization (payer + nonce) is found in a Transfer worth the
    price whose tx is not already on the ledger; 'nonce used' alone proves
    nothing (a previous sale or a cancelAuthorization also use nonces)."""
    asset, payer, nonce = str(req["asset"]), f["payer"], f["nonce"]
    used = wallet.authorization_state(asset, payer, nonce)
    if used is None:
        return {**s, "pending": True}
    if not used:
        return {**s, "pending": True}             # may still be in the mempool
    tx = str(s.get("transaction") or "")
    if not tx:
        tx = wallet.find_settlement_tx(asset, payer, str(req["payTo"]), nonce, int(req["amount"]))
    if not tx or is_settlement_tx(tx):
        oplog.error("x402.reconcile", "nonce used but no matching settlement tx found",
                    params={"payer": payer, "nonce": nonce[:18]})
        return {**s, "pending": True}
    oplog.op("x402.reconciled", params={"payer": payer, "nonce": nonce[:18], "tx": tx[:18]})
    return {"success": True, "payer": payer, "transaction": tx, "network": NETWORK, "reconciled": True}


# ----------------------------------------------------------------- ledger --

class Duplicate(Exception):
    """Same payer + nonce seen before (replayed payload)."""


_schema_ready = False


def ensure_schema() -> None:
    """Additive, self-healing (CREATE IF NOT EXISTS) — safe to call any time."""
    global _schema_ready
    if _schema_ready:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS x402_payments (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            REAL NOT NULL,
        payer         TEXT NOT NULL,
        nonce         TEXT NOT NULL,
        product       TEXT NOT NULL,
        agent_id      TEXT NOT NULL DEFAULT '',
        resource      TEXT NOT NULL DEFAULT '',
        amount_atomic TEXT NOT NULL DEFAULT '0',
        amount_usd    REAL NOT NULL DEFAULT 0,
        asset         TEXT NOT NULL DEFAULT '',
        network       TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'pending',
        tx            TEXT NOT NULL DEFAULT '',
        error         TEXT NOT NULL DEFAULT '',
        owner_credits REAL NOT NULL DEFAULT 0,
        meta_json     TEXT NOT NULL DEFAULT ''
    )""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_x402_payer_nonce ON x402_payments(payer, nonce)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_x402_ts ON x402_payments(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_x402_tx ON x402_payments(tx)")
    _schema_ready = True


def begin(payer: str, nonce: str, product: str, req: Dict[str, Any], resource: str,
          agent_id: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
    """Record a payment attempt. Raises Duplicate for a replayed payer+nonce."""
    ensure_schema()
    if not payer or not nonce:
        raise ValueError("payer and nonce required")
    meta = dict(meta or {})
    if payer.lower() in operator_wallets():
        meta["operator"] = True
    decimals = ASSETS.get(asset_symbol(str(req.get("asset") or "")) or "USDC", ASSETS["USDC"])["decimals"]
    try:
        cur = db.execute(
            "INSERT INTO x402_payments(ts, payer, nonce, product, agent_id, resource, amount_atomic, "
            "amount_usd, asset, network, status, meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), payer.lower(), nonce.lower(), product, agent_id or "", resource or "",
             str(req.get("amount") or "0"), int(str(req.get("amount") or "0")) / (10 ** decimals),
             str(req.get("asset") or ""), str(req.get("network") or ""), "pending",
             json.dumps(meta, default=str)),
        )
    except sqlite3.IntegrityError as e:
        raise Duplicate(str(e))
    return int(cur.lastrowid)


def finish(payment_id: int, status: str, tx: str = "", error: str = "",
           owner_credits: Optional[float] = None) -> None:
    """Advance a row. A known tx hash is never overwritten with '' (a
    settle_pending row keeps the hash it broadcast); owner_credits only
    changes when given."""
    row = db.query_one("SELECT tx, owner_credits FROM x402_payments WHERE id=?", (int(payment_id),)) or {}
    tx = (tx or "").lower() or (row.get("tx") or "")
    oc = float(row.get("owner_credits") or 0) if owner_credits is None else float(owner_credits)
    db.execute("UPDATE x402_payments SET status=?, tx=?, error=?, owner_credits=? WHERE id=?",
               (status, tx, (error or "")[:300], oc, int(payment_id)))


def _meta(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("meta_json") or "{}") or {}
    except ValueError:
        return {}


def update_meta(payment_id: int, **fields: Any) -> None:
    row = db.query_one("SELECT meta_json FROM x402_payments WHERE id=?", (int(payment_id),)) or {}
    m = _meta(row)
    m.update(fields)
    db.execute("UPDATE x402_payments SET meta_json=? WHERE id=?", (json.dumps(m, default=str), int(payment_id)))


def get_payment(payer: str, nonce: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    row = db.query_one("SELECT * FROM x402_payments WHERE payer=? AND nonce=?",
                       ((payer or "").lower(), (nonce or "").lower()))
    if row:
        row = dict(row)
        row["meta"] = _meta(row)
    return row


def mark_delivered(payment_id: int) -> None:
    update_meta(payment_id, delivered=True, delivered_at=time.time())


def finalize_one(row: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check one settle_pending row against the chain; flip to settled (and
    pay the owner share) when the exact settlement is found. Content is NOT
    delivered here — the buyer's retry with the same signature gets it."""
    if row.get("status") != "settle_pending":
        return row
    sym = asset_symbol(row.get("asset") or "") or "USDC"
    req = {"asset": row.get("asset") or ASSETS["USDC"]["address"], "payTo": pay_to(),
           "amount": str(row.get("amount_atomic") or "0")}
    f = {"payer": row["payer"], "nonce": row["nonce"]}
    s = reconcile(f, req, {"success": False, "transaction": row.get("tx") or ""})
    if s.get("success"):
        finish(int(row["id"]), "settled", tx=s.get("transaction") or "", error="")
        credits = 0.0
        if row.get("agent_id"):
            from ..models import agent_model
            agent = agent_model.get(row["agent_id"])
            if agent:
                try:
                    credits = credit_owner(agent, float(row.get("amount_usd") or 0),
                                           s.get("transaction") or "", row.get("product") or "insight",
                                           row["payer"], row["nonce"])
                except Exception as e:
                    oplog.error("x402.owner_share", repr(e)[:300], params={"id": row["id"]})
                    update_meta(int(row["id"]), owner_share_failed=True)
        finish(int(row["id"]), "settled", owner_credits=credits if credits else None)
        return get_payment(row["payer"], row["nonce"]) or row
    if time.time() - float(row.get("ts") or 0) > 24 * 3600:
        finish(int(row["id"]), "settle_failed", error="unresolved after 24h")
    return get_payment(row["payer"], row["nonce"]) or row


def finalize_pending(limit: int = 50) -> Dict[str, int]:
    """Autopilot pass: resolve settle_pending rows and retry owner shares that
    failed to credit. Cheap when there is nothing to do."""
    ensure_schema()
    out = {"checked": 0, "settled": 0, "shares": 0}
    for row in db.query_all("SELECT * FROM x402_payments WHERE status='settle_pending' ORDER BY id ASC LIMIT ?",
                            (int(limit),)):
        out["checked"] += 1
        r = finalize_one(dict(row))
        if r.get("status") == "settled":
            out["settled"] += 1
    for row in db.query_all("SELECT * FROM x402_payments WHERE status='settled' AND agent_id<>'' "
                            "AND owner_credits=0 AND meta_json LIKE '%owner_share_failed%' LIMIT ?", (int(limit),)):
        from ..models import agent_model
        agent = agent_model.get(row["agent_id"])
        if not agent:
            continue
        try:
            c = credit_owner(agent, float(row["amount_usd"] or 0), row.get("tx") or "",
                             row.get("product") or "insight", row["payer"], row["nonce"])
        except Exception:
            continue
        if c:
            finish(int(row["id"]), "settled", owner_credits=c)
            update_meta(int(row["id"]), owner_share_failed=False)
            out["shares"] += 1
    return out


def is_settlement_tx(txhash: str) -> bool:
    """True when this on-chain tx hash is one of OUR x402 settlements — the
    deposit scanner must never credit it as a Gas top-up (the payer→payTo
    Transfer log of a sale would otherwise look like a deposit)."""
    if not txhash:
        return False
    ensure_schema()
    return db.query_one("SELECT 1 FROM x402_payments WHERE tx=? AND tx<>''", (txhash.lower(),)) is not None


SETTLEMENT_MATCH_WINDOW_S = 1800


def is_settlement_like(txhash: str, sender: str, token: str, amount: float) -> bool:
    """The scanner's guard when the hash is not (yet) on the ledger: the
    settlement row may still be pending (we record the hash only after the
    reply), or a lost reply left it without a hash. Match on payer + asset +
    exact amount within the last 30 minutes, any non-failed status."""
    if is_settlement_tx(txhash):
        return True
    a = ASSETS.get((token or "").upper())
    if not a or not sender:
        return False
    ensure_schema()
    atomic_amt = str(int(round(float(amount) * (10 ** a["decimals"]))))
    row = db.query_one(
        "SELECT 1 FROM x402_payments WHERE payer=? AND asset=? AND amount_atomic=? AND ts>? "
        "AND status IN ('pending','settled','settle_pending','settle_failed') LIMIT 1",
        ((sender or "").lower(), a["address"], atomic_amt, time.time() - SETTLEMENT_MATCH_WINDOW_S))
    return row is not None


def recent(limit: int = 100, status: str = "") -> List[Dict[str, Any]]:
    ensure_schema()
    if status:
        return db.query_all("SELECT * FROM x402_payments WHERE status=? ORDER BY id DESC LIMIT ?",
                            (status, int(limit)))
    return db.query_all("SELECT * FROM x402_payments ORDER BY id DESC LIMIT ?", (int(limit),))


def _op_clause(exclude_operator: bool):
    ops = sorted(operator_wallets()) if exclude_operator else []
    if not ops:
        return "", ()
    return " AND payer NOT IN (%s)" % ",".join("?" for _ in ops), tuple(ops)


def summary(exclude_operator: bool = False) -> Dict[str, Any]:
    ensure_schema()
    cl, params = _op_clause(exclude_operator)
    row = db.query_one(
        "SELECT COUNT(*) n, COUNT(DISTINCT payer) payers, COALESCE(SUM(amount_usd),0) usd "
        f"FROM x402_payments WHERE status='settled'{cl}", params) or {}
    by_day = db.query_all(
        "SELECT date(ts,'unixepoch') d, COUNT(*) n, COUNT(DISTINCT payer) payers, "
        f"COALESCE(SUM(amount_usd),0) usd FROM x402_payments WHERE status='settled'{cl} "
        "GROUP BY d ORDER BY d DESC LIMIT 30", params)
    by_product = db.query_all(
        "SELECT product, COUNT(*) n, COALESCE(SUM(amount_usd),0) usd FROM x402_payments "
        f"WHERE status='settled'{cl} GROUP BY product", params)
    returning = db.query_one(
        "SELECT COUNT(*) c FROM (SELECT payer, COUNT(DISTINCT date(ts,'unixepoch')) d "
        f"FROM x402_payments WHERE status='settled'{cl} GROUP BY payer HAVING d>=2)", params) or {}
    return {"settled": int(row.get("n") or 0), "payers": int(row.get("payers") or 0),
            "usd": round(float(row.get("usd") or 0), 4),
            "returning_payers": int(returning.get("c") or 0),
            "by_day": by_day, "by_product": by_product}


def _short_addr(a: str) -> str:
    a = a or ""
    return (a[:6] + "…" + a[-4:]) if len(a) > 12 else a


def activity() -> Dict[str, Any]:
    """PUBLIC proof of on-chain activity — totals, recent settlements, the
    registrations and the deposit lane's distinct senders. Never an IP, a
    full payer address, meta_json or an owner address. Operator wallets are
    excluded (builder activity does not count and must not look like usage)."""
    from ..models import agent_model
    from . import agentid
    ensure_schema()
    cl, params = _op_clause(True)
    rows = db.query_all(
        "SELECT ts, product, amount_usd, asset, tx, agent_id FROM x402_payments "
        f"WHERE status='settled' AND tx<>''{cl} ORDER BY id DESC LIMIT 20", params)
    recent_rows = []
    for r in rows:
        payer = db.query_one("SELECT payer FROM x402_payments WHERE tx=? LIMIT 1", (r["tx"],)) or {}
        recent_rows.append({
            "ts": r["ts"], "product": r["product"], "amount_usd": round(float(r["amount_usd"] or 0), 4),
            "asset": asset_symbol(r.get("asset") or "") or "USDC",
            "tx": r["tx"], "explorer": CHAIN["explorer_tx"] + r["tx"],
            "payer_short": _short_addr(payer.get("payer") or ""),
            "agent_code": agent_model.agent_code(r["agent_id"]) if r.get("agent_id") else "",
        })
    ops = sorted(operator_wallets())
    dep_cl = (" AND address NOT IN (%s)" % ",".join("?" for _ in ops)) if ops else ""
    dep = db.query_one(
        "SELECT COUNT(DISTINCT address) senders, COUNT(*) n, COALESCE(SUM(usd_value),0) usd "
        f"FROM points_tx WHERE kind='deposit' AND chain='CELO'{dep_cl}", tuple(ops)) or {}
    dep_ret = db.query_one(
        "SELECT COUNT(*) c FROM (SELECT address, COUNT(DISTINCT date(ts,'unixepoch')) d FROM points_tx "
        f"WHERE kind='deposit' AND chain='CELO'{dep_cl} GROUP BY address HAVING d>=2)", tuple(ops)) or {}
    regs = db.query_all(
        "SELECT agent_id, label, symbol, celo_agent_id, celo_agent_tx FROM agents "
        "WHERE deleted_at=0 AND celo_agent_id>0 ORDER BY celo_registered_at ASC LIMIT 50")
    plat = agentid.platform_agent()
    s = summary(exclude_operator=True)
    return {
        "generated_at": time.time(),
        "summary": {k: s[k] for k in ("settled", "payers", "returning_payers", "usd", "by_product")},
        "recent": recent_rows,
        "registrations": {
            "platform": ({"agentId": int(plat["agentId"]), "txhash": plat.get("txhash", ""),
                          "explorer": CHAIN["explorer_tx"] + plat.get("txhash", "")} if plat else None),
            "agents": [{"code": agent_model.agent_code(r["agent_id"]),
                        "label": r.get("label") or (r.get("symbol") or "").split(":")[-1],
                        "symbol": (r.get("symbol") or "").split(":")[-1].upper(),
                        "celo_agent_id": int(r["celo_agent_id"]), "tx": r.get("celo_agent_tx") or "",
                        "explorer": CHAIN["explorer_tx"] + (r.get("celo_agent_tx") or "")} for r in regs],
            "count": agentid.registered_count(),
        },
        "deposits": {"senders": int(dep.get("senders") or 0), "count": int(dep.get("n") or 0),
                     "usd": round(float(dep.get("usd") or 0), 4), "returning": int(dep_ret.get("c") or 0),
                     "assets": ["USDC", "USD₮", "USDm", "USA₮"]},
        "wallets": {"pay_to": pay_to(), "registrar": wallet.address(),
                    "identity_registry": agentid.REGISTRY, "reputation_registry": agentid.REPUTATION_REGISTRY,
                    "facilitator_signer": "0x0d74D5Cefd2e7F24E623330ebE3d8D4cB45fFB48"},
        "links": links(),
        "settler": settler_mode(),
    }


# ---------------------------------------------------------- revenue share --

def credit_owner(agent: Dict[str, Any], amount_usd: float, tx: str, product: str,
                 payer: str = "", nonce: str = "") -> float:
    """Credit the agent owner's Gas with their share of an x402 sale. Idempotent
    per settlement (bonus_grants PK on the tx hash; the payer nonce is the key
    when a facilitator reply carries no hash, so two sales can never collapse
    into one grant). Returns the credits granted."""
    owner = (agent.get("address") or "").lower()
    share = owner_share()
    credits = float(math.floor(round(float(amount_usd) * share * pricing.CREDITS_PER_USDC, 6)))
    if not owner or credits < 1:
        return 0.0
    key = (tx or "").lower() or ("nonce:" + (nonce or "").lower())
    if key in ("", "nonce:"):
        return 0.0
    tag = f"x402:{key[:72]}"
    with db._LOCK:
        if db.query_one("SELECT 1 FROM bonus_grants WHERE address=? AND tag=?", (owner, tag)):
            return 0.0
        db.execute("INSERT INTO bonus_grants(address, tag, granted_at) VALUES(?,?,?)",
                   (owner, tag, time.time()))
    code = agent.get("agent_id") or ""
    label = agent.get("label") or agent.get("symbol") or "agent"
    try:
        points_model.credit(
            owner, credits, kind="grant", ref=tag, agent_id=code,
            note=f"x402 sale · {product} · {amount_usd:.2f} USDC on Celo · {share:.0%} to owner",
            meta={"x402": True, "product": product, "tx": tx, "payer": payer,
                  "amount_usd": amount_usd, "share": share},
        )
    except Exception:
        # The grant marker must not outlive a failed credit — otherwise the
        # owner's share of this sale is lost forever (idempotency would refuse
        # every retry). Release it and re-raise for the caller's oplog.
        db.execute("DELETE FROM bonus_grants WHERE address=? AND tag=?", (owner, tag))
        raise
    try:
        notification_model.create(
            owner, kind="credits_grant", severity="info", agent_id=code,
            title=f"{label} sold an insight on Celo: +{credits:,.0f} Gas",
            title_zh=f"{label} 在 Celo 上卖出一次洞察：+{credits:,.0f} 燃料",
            body=f"A buyer paid {amount_usd:.2f} USDC over x402 for your agent's latest insight; "
                 f"{share:.0%} landed in your Gas balance.",
            body_zh=f"有人通过 x402 支付 {amount_usd:.2f} USDC 购买了你 Agent 的最新洞察，"
                    f"{share:.0%} 已计入你的燃料余额。",
            dedup_key=tag,
        )
    except Exception:
        pass
    return credits


def agent_sales(agent_id: str) -> Dict[str, float]:
    """Per-agent settled sales + Gas earned (owner-facing card)."""
    try:
        ensure_schema()
        row = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(owner_credits),0) c FROM x402_payments "
                           "WHERE agent_id=? AND status='settled'", (agent_id,)) or {}
        return {"x402_sales": int(row.get("n") or 0), "x402_earned_credits": float(row.get("c") or 0)}
    except Exception:
        return {"x402_sales": 0, "x402_earned_credits": 0.0}


# ---------------------------------------------------------------- public --

def public_config() -> Dict[str, Any]:
    p = prices()
    return {
        "enabled": enabled(),
        "settler": settler_mode(),
        "network": NETWORK,
        "chain": dict(CHAIN),
        "asset": {"address": USDC, "symbol": "USDC", "decimals": USDC_DECIMALS,
                  "eip712": dict(USDC_EIP712)},
        "assets": {k: dict(v) for k, v in ASSETS.items()},
        "pay_to": pay_to(),
        "facilitator": facilitator_url(),
        "prices": {k: {"usd": v, "atomic": atomic(v), "description": PRODUCTS[k]["description"]}
                   for k, v in p.items()},
        "owner_share": owner_share(),
        "max_timeout_s": MAX_TIMEOUT_S,
        "links": links(),
    }
