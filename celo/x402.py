"""x402 v2 seller on Celo — pay-per-request in USDC, settled by the Celo
facilitator (api.x402.celo.org).

Protocol (specs/x402-specification-v2 + transports-v2/http, verified against
the live facilitator 2026-09-10):

  1. Client calls a paid endpoint → 402 + `PAYMENT-REQUIRED` header
     (base64 JSON PaymentRequired: {x402Version:2, resource, accepts:[…]}).
  2. Client signs an EIP-3009 `TransferWithAuthorization` for USDC
     (EIP-712 domain name "USDC" / version "2" — read from the contract) and
     retries with `PAYMENT-SIGNATURE` (base64 JSON PaymentPayload).
  3. Server → facilitator POST /verify (open) → produces the content →
     POST /settle (X-API-Key) → 200 + `PAYMENT-RESPONSE` header with the
     on-chain tx. The facilitator submits the authorization on-chain and
     pays gas itself; the payer signs off-chain only.

Ledger: every payment attempt is a row in `x402_payments` (payer+nonce
unique — a replayed payload is refused before any work is done). Rows are
PERMANENT revenue records (docs/CORE_PRINCIPLES.md §5): status moves
pending → settled | invalid | content_failed | settle_failed, never deleted.

Revenue share: an "insight" sale credits the agent's OWNER with a share of the
sale as Gas (default 70%), idempotent per settlement tx via bonus_grants.

Everything is default-off: no facilitator API key or no payTo → enabled() is
False and every paid endpoint answers 503.
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

# --------------------------------------------------------------- constants --

FACILITATOR_DEFAULT = "https://api.x402.celo.org"
CHAIN_ID = 42220
NETWORK = f"eip155:{CHAIN_ID}"                    # CAIP-2
USDC = "0xcEBA9300f2b948710d2653dD7B07f33A8B32118C"
USDC_DECIMALS = 6
USDC_EIP712 = {"name": "USDC", "version": "2"}    # contract name()/version(), verified live
MAX_TIMEOUT_S = 120
CHAIN = {
    "chain_id": CHAIN_ID, "chain_id_hex": "0xa4ec", "name": "Celo",
    "rpc": "https://forno.celo.org", "explorer": "https://celoscan.io",
    "explorer_tx": "https://celoscan.io/tx/", "symbol": "CELO", "decimals": 18,
}

# What we sell. Prices in USD (= USDC). The operator can override with
# X402_PRICES_JSON or the admin key `x402_prices`, bounded to [0.001, 10].
PRODUCTS: Dict[str, Dict[str, Any]] = {
    "chat":    {"usd": 0.02, "description": "Ask ManekiAI — one market question answered by the platform analyst (same data + model the trading agents use)"},
    "brief":   {"usd": 0.01, "description": "ManekiAI symbol brief — trend, levels, risk and stance for one US-stock perp (shared, refreshed every 10 min)"},
    "insight": {"usd": 0.05, "description": "Latest decision insight of one live ManekiAI trading agent — action, confidence, reasoning and market read"},
}
_PRICE_BOUNDS = (0.001, 10.0)
OWNER_SHARE_DEFAULT = 0.70


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
    also keeps the hackathon's per-wallet on-chain accounting in one place."""
    v = (_admin("x402_pay_to") or os.environ.get("X402_PAY_TO", "").strip()
         or service_config.celo_treasury()).lower()
    return v if (v.startswith("0x") and len(v) == 42) else ""


def enabled() -> bool:
    if (os.environ.get("X402_ENABLED", "1").strip().lower() in ("0", "false", "off", "no")):
        return False
    return bool(api_key()) and bool(pay_to())


def owner_share() -> float:
    try:
        v = float(os.environ.get("X402_OWNER_SHARE", "") or OWNER_SHARE_DEFAULT)
    except ValueError:
        v = OWNER_SHARE_DEFAULT
    return min(1.0, max(0.0, v))


def public_base() -> str:
    return (os.environ.get("MANEKI_PUBLIC_BASE", "").strip().rstrip("/")
            or "https://manekiai.io")


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


def atomic(usd: float) -> str:
    return str(int(round(float(usd) * (10 ** USDC_DECIMALS))))


# --------------------------------------------------------------- protocol --

def b64e(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def b64d(s: str) -> Any:
    s = (s or "").strip()
    pad = "=" * (-len(s) % 4)
    return json.loads(base64.b64decode(s + pad).decode())


def requirements(product: str, resource_url: str) -> Dict[str, Any]:
    """The single `accepts` entry we offer for `product`."""
    return {
        "scheme": "exact",
        "network": NETWORK,
        "amount": atomic(price_usd(product)),
        "asset": USDC,
        "payTo": pay_to(),
        "maxTimeoutSeconds": MAX_TIMEOUT_S,
        "extra": dict(USDC_EIP712),
    }


def resource_info(product: str, resource_url: str) -> Dict[str, Any]:
    return {"url": resource_url, "description": PRODUCTS[product]["description"],
            "mimeType": "application/json"}


def payment_required(product: str, resource_url: str, error: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "x402Version": 2,
        "resource": resource_info(product, resource_url),
        "accepts": [requirements(product, resource_url)],
        "extensions": {},
    }
    if error:
        out["error"] = error
    return out


def matches(accepted: Dict[str, Any], req: Dict[str, Any]) -> bool:
    """Client's chosen requirements must be OUR offer (same scheme / network /
    asset / payTo, amount ≥ ours). The facilitator re-verifies the signed
    authorization against `req` anyway; this is the cheap early reject."""
    try:
        return (str(accepted.get("scheme")) == req["scheme"]
                and str(accepted.get("network")) == req["network"]
                and str(accepted.get("asset") or "").lower() == req["asset"].lower()
                and str(accepted.get("payTo") or "").lower() == req["payTo"].lower()
                and int(str(accepted.get("amount") or "0")) >= int(req["amount"]))
    except (TypeError, ValueError):
        return False


def payload_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    """payer / nonce / value / to out of an exact-scheme EVM payload."""
    auth = ((payload.get("payload") or {}).get("authorization") or {})
    return {"payer": str(auth.get("from") or "").lower(),
            "nonce": str(auth.get("nonce") or "").lower(),
            "value": str(auth.get("value") or ""),
            "to": str(auth.get("to") or "").lower(),
            "valid_before": str(auth.get("validBefore") or "")}


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
    if r.status_code >= 400 and not isinstance(data, dict):
        data = {}
    if not isinstance(data, dict):
        data = {"raw": data}
    data.setdefault("_http", r.status_code)
    return data


def facilitator_verify(payload: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    try:
        d = _fac_post("/verify", payload, req)
    except Exception as e:
        return {"isValid": False, "invalidReason": f"facilitator unreachable: {e!r}"[:200]}
    if "isValid" not in d:
        d["isValid"] = False
        d.setdefault("invalidReason", f"facilitator http {d.get('_http')}: {str(d.get('error') or d.get('message') or '')[:120]}")
    return d


def facilitator_settle(payload: Dict[str, Any], req: Dict[str, Any]) -> Dict[str, Any]:
    try:
        d = _fac_post("/settle", payload, req)
    except Exception as e:
        return {"success": False, "errorReason": f"facilitator unreachable: {e!r}"[:200], "transaction": "", "network": NETWORK}
    if "success" not in d:
        d["success"] = False
        d.setdefault("errorReason", f"facilitator http {d.get('_http')}: {str(d.get('error') or d.get('message') or '')[:120]}")
    d.setdefault("transaction", "")
    d.setdefault("network", NETWORK)
    return d


def facilitator_supported() -> Dict[str, Any]:
    r = httpx.get(f"{facilitator_url()}/supported", timeout=15.0)
    r.raise_for_status()
    return r.json()


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
    _schema_ready = True


def begin(payer: str, nonce: str, product: str, req: Dict[str, Any], resource: str,
          agent_id: str = "", meta: Optional[Dict[str, Any]] = None) -> int:
    """Record a payment attempt. Raises Duplicate for a replayed payer+nonce."""
    ensure_schema()
    if not payer or not nonce:
        raise ValueError("payer and nonce required")
    try:
        cur = db.execute(
            "INSERT INTO x402_payments(ts, payer, nonce, product, agent_id, resource, amount_atomic, "
            "amount_usd, asset, network, status, meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), payer.lower(), nonce.lower(), product, agent_id or "", resource or "",
             str(req.get("amount") or "0"), int(str(req.get("amount") or "0")) / (10 ** USDC_DECIMALS),
             str(req.get("asset") or ""), str(req.get("network") or ""), "pending",
             json.dumps(meta or {}, default=str)),
        )
    except sqlite3.IntegrityError as e:
        raise Duplicate(str(e))
    return int(cur.lastrowid)


def finish(payment_id: int, status: str, tx: str = "", error: str = "",
           owner_credits: float = 0.0) -> None:
    db.execute("UPDATE x402_payments SET status=?, tx=?, error=?, owner_credits=? WHERE id=?",
               (status, tx or "", (error or "")[:300], float(owner_credits or 0), int(payment_id)))


def recent(limit: int = 100, status: str = "") -> List[Dict[str, Any]]:
    ensure_schema()
    if status:
        return db.query_all("SELECT * FROM x402_payments WHERE status=? ORDER BY id DESC LIMIT ?",
                            (status, int(limit)))
    return db.query_all("SELECT * FROM x402_payments ORDER BY id DESC LIMIT ?", (int(limit),))


def summary() -> Dict[str, Any]:
    ensure_schema()
    row = db.query_one(
        "SELECT COUNT(*) n, COUNT(DISTINCT payer) payers, COALESCE(SUM(amount_usd),0) usd "
        "FROM x402_payments WHERE status='settled'") or {}
    by_day = db.query_all(
        "SELECT date(ts,'unixepoch') d, COUNT(*) n, COUNT(DISTINCT payer) payers, "
        "COALESCE(SUM(amount_usd),0) usd FROM x402_payments WHERE status='settled' "
        "GROUP BY d ORDER BY d DESC LIMIT 30")
    by_product = db.query_all(
        "SELECT product, COUNT(*) n, COALESCE(SUM(amount_usd),0) usd FROM x402_payments "
        "WHERE status='settled' GROUP BY product")
    returning = db.query_one(
        "SELECT COUNT(*) c FROM (SELECT payer, COUNT(DISTINCT date(ts,'unixepoch')) d "
        "FROM x402_payments WHERE status='settled' GROUP BY payer HAVING d>=2)") or {}
    return {"settled": int(row.get("n") or 0), "payers": int(row.get("payers") or 0),
            "usd": round(float(row.get("usd") or 0), 4),
            "returning_payers": int(returning.get("c") or 0),
            "by_day": by_day, "by_product": by_product}


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
    points_model.credit(
        owner, credits, kind="grant", ref=tag, agent_id=code,
        note=f"x402 sale · {product} · {amount_usd:.2f} USDC on Celo · {share:.0%} to owner",
        meta={"x402": True, "product": product, "tx": tx, "payer": payer,
              "amount_usd": amount_usd, "share": share},
    )
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


# ---------------------------------------------------------------- public --

def public_config() -> Dict[str, Any]:
    p = prices()
    return {
        "enabled": enabled(),
        "network": NETWORK,
        "chain": dict(CHAIN),
        "asset": {"address": USDC, "symbol": "USDC", "decimals": USDC_DECIMALS,
                  "eip712": dict(USDC_EIP712)},
        "pay_to": pay_to(),
        "facilitator": facilitator_url(),
        "prices": {k: {"usd": v, "atomic": atomic(v), "description": PRODUCTS[k]["description"]}
                   for k, v in p.items()},
        "owner_share": owner_share(),
        "max_timeout_s": MAX_TIMEOUT_S,
    }
