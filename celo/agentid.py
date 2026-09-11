"""ERC-8004 identity on Celo — every ManekiAI agent gets an on-chain Agent ID.

Celo's ERC-8004 Identity Registry lives at the SAME deterministic address as
0G's (`0x8004A169…a432`, docs.celo.org → build-with-ai/8004), so this module
reuses the pure calldata / receipt helpers of services/zerog_agentid.py and
adds what differs: the Celo RPC, its own agent columns, the platform-level
"ManekiAI Analyst" agent that fronts the x402 endpoints, and registration-v1
agent cards that carry BOTH chains' registrations plus the x402 services.

Scope (product decision 2026-09-10 — "the fleet, not just one model"): unlike
0G, where only 0G-Compute agents register, on Celo EVERY live (non-deleted)
agent is registered, regardless of model. One registration ≈ 183k gas
≈ 0.04 CELO at today's gas price.

Signing: a REGISTRAR wallet whose key lives in the server env
(`CELO_REGISTRAR_KEY`; falls back to `ZEROG_REGISTRAR_KEY` — one EVM key works
on every chain). No key → the feature is silently OFF. The wallet only pays
gas; it never receives user funds (the treasury is a separate, receive-only
address). Fund it with a little CELO.

Failure model: best-effort and asynchronous — registration never blocks or
fails agent creation. Errors land in oplog; the admin console can batch
backfill. Idempotent: an agent with an id is never re-registered.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from .. import db, admin_store
from ..models import agent_model
from ..services import oplog
from ..services import zerog_agentid as zg

RPC_URLS = ["https://forno.celo.org", "https://celo.drpc.org"]
CHAIN_ID = 42220
REGISTRY = zg.REGISTRY                      # 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432
REPUTATION_REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
REGISTRY_CAIP = f"eip155:{CHAIN_ID}:{REGISTRY}"
EXPLORER_TX = "https://celoscan.io/tx/"
EXPLORER_ADDR = "https://celoscan.io/address/"

# The platform's own agent — the "ManekiAI Analyst" that answers Ask-ManekiAI
# and symbol briefs over x402. Registered once; its id/tx live in the admin
# store (there is no agents row for it).
PLATFORM_CODE = "maneki-analyst"
_PLATFORM_KEY = "celo_platform_agent"

GAS_LIMIT_FALLBACK = 400_000
RECEIPT_TIMEOUT_S = 90
RECEIPT_POLL_S = 2.0

_inflight_lock = threading.Lock()
_inflight: set = set()
# Review fix (2026-09-11): an unfunded registrar must not spawn a failing tx
# attempt on EVERY agent edit. Balance is checked (60s cache) before signing,
# and a failed agent waits RETRY_BACKOFF_S before the create/edit hook retries.
MIN_REGISTRAR_CELO = 0.05
RETRY_BACKOFF_S = 600
_bal_cache: Dict[str, Any] = {"at": 0.0, "wei": None}
_last_fail: Dict[str, float] = {}


# ------------------------------------------------------------- config -----

def registrar_key() -> str:
    return (os.environ.get("CELO_REGISTRAR_KEY", "").strip()
            or os.environ.get("ZEROG_REGISTRAR_KEY", "").strip())


def enabled() -> bool:
    """Feature-gated on the registrar key; also honors a kill switch."""
    if (os.environ.get("CELO_AGENTID_ENABLED", "1").strip().lower()
            in ("0", "false", "off", "no")):
        return False
    return len(registrar_key()) >= 32


def registrar_address() -> str:
    if not registrar_key():
        return ""
    try:
        from eth_account import Account
        return Account.from_key(registrar_key()).address
    except Exception:
        return ""


def public_base_url() -> str:
    return zg.public_base_url()


def agent_uri(code: str) -> str:
    return f"{public_base_url()}/api/agent-card/{code}"


def platform_uri() -> str:
    return agent_uri(PLATFORM_CODE)


# ---------------------------------------------------------- rpc plumbing --

def _rpc(method: str, params: list) -> Any:
    """JSON-RPC against the first gateway that answers (forno, then dRPC)."""
    last: Exception | None = None
    for url in RPC_URLS:
        try:
            r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params}, timeout=20.0)
            r.raise_for_status()
            body = r.json() or {}
            if body.get("error"):
                raise RuntimeError(f"{method}: {body['error']}")
            return body.get("result")
        except Exception as e:          # try the next gateway
            last = e
    raise RuntimeError(f"celo rpc failed on all gateways: {last!r}")


def registrar_balance_celo() -> Optional[float]:
    """Registrar's CELO balance (60s cache). None when the RPC read fails —
    callers treat 'unknown' as 'try anyway' so a flaky read never blocks."""
    addr = registrar_address()
    if not addr:
        return None
    now = time.time()
    if _bal_cache["wei"] is not None and now - _bal_cache["at"] < 60:
        return _bal_cache["wei"] / 1e18
    try:
        wei = int(_rpc("eth_getBalance", [addr, "latest"]), 16)
    except Exception:
        return None
    _bal_cache["wei"], _bal_cache["at"] = wei, now
    return wei / 1e18


def _funded() -> bool:
    bal = registrar_balance_celo()
    return True if bal is None else bal >= MIN_REGISTRAR_CELO


def _send_register(uri: str) -> Dict[str, Any]:
    """Sign + broadcast register(uri) from the registrar, wait for the receipt,
    return {agentId, txhash}. Raises on revert / receipt timeout."""
    from eth_account import Account
    acct = Account.from_key(registrar_key())
    data = zg._encode_register(uri)
    nonce = int(_rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
    gas_price = int(_rpc("eth_gasPrice", []), 16)
    try:
        gas = int(_rpc("eth_estimateGas", [{"from": acct.address, "to": REGISTRY, "data": data}]), 16)
        gas = int(gas * 1.3)
    except Exception:
        gas = GAS_LIMIT_FALLBACK
    signed = acct.sign_transaction({
        "chainId": CHAIN_ID, "nonce": nonce, "to": REGISTRY, "value": 0,
        "gas": gas, "gasPrice": int(gas_price * 1.2), "data": data,
    })
    txh = _rpc("eth_sendRawTransaction", [zg._raw_hex(signed)])
    receipt = None
    deadline = time.time() + RECEIPT_TIMEOUT_S
    while time.time() < deadline:
        receipt = _rpc("eth_getTransactionReceipt", [txh])
        if receipt:
            break
        time.sleep(RECEIPT_POLL_S)
    if not receipt:
        raise RuntimeError(f"receipt timeout for {txh}")
    if (receipt.get("status") or "").lower() != "0x1":
        raise RuntimeError(f"register tx reverted: {txh}")
    aid = zg._parse_agent_id(receipt)
    if aid is None:
        raise RuntimeError(f"agentId not found in receipt logs: {txh}")
    return {"agentId": aid, "txhash": txh}


# ------------------------------------------------------------ registration --

def register_agent(agent_id: str) -> Dict[str, Any]:
    """Register one agent in the Celo Identity Registry (sync — call via
    to_thread). Idempotent; never raises into the caller's flow."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    agent = agent_model.get(agent_id)
    if not agent:
        return {"ok": False, "skipped": "agent not found"}
    if float(agent.get("deleted_at") or 0) > 0:
        return {"ok": False, "skipped": "agent deleted"}
    if int(agent.get("celo_agent_id") or 0) > 0:
        return {"ok": True, "already": True, "agentId": int(agent["celo_agent_id"])}
    if not _funded():
        return {"ok": False, "skipped": "registrar unfunded (needs CELO for gas)"}
    with _inflight_lock:
        if agent_id in _inflight:
            return {"ok": False, "skipped": "in flight"}
        _inflight.add(agent_id)
    try:
        code = agent_model.agent_code(agent_id)
        uri = agent_uri(code)
        r = _send_register(uri)
        db.execute(
            "UPDATE agents SET celo_agent_id=?, celo_agent_tx=?, celo_registered_at=? WHERE agent_id=?",
            (r["agentId"], r["txhash"], time.time(), agent_id),
        )
        oplog.op("agent.celo_register", agent.get("address") or "",
                 params={"agent_id": agent_id, "celo_agent_id": r["agentId"],
                         "txhash": r["txhash"][:18], "uri": uri})
        _last_fail.pop(agent_id, None)
        _bal_cache["at"] = 0.0          # balance moved — re-read next time
        return {"ok": True, "agentId": r["agentId"], "txhash": r["txhash"], "uri": uri}
    except Exception as e:
        _last_fail[agent_id] = time.time()
        oplog.error("agent.celo_register", repr(e)[:400], address=agent.get("address") or "",
                    params={"agent_id": agent_id})
        return {"ok": False, "error": str(e)[:200]}
    finally:
        with _inflight_lock:
            _inflight.discard(agent_id)


def maybe_register_async(agent_id: str) -> None:
    """Fire-and-forget hook for create/edit paths. Never blocks, never raises."""
    try:
        if not enabled():
            return
        agent = agent_model.get(agent_id)
        if not agent or float(agent.get("deleted_at") or 0) > 0:
            return
        if int(agent.get("celo_agent_id") or 0) > 0:
            return
        if time.time() - _last_fail.get(agent_id, 0.0) < RETRY_BACKOFF_S:
            return                       # recent failure — the admin backfill can force it
        threading.Thread(target=register_agent, args=(agent_id,), daemon=True).start()
    except Exception:
        pass


def register_all_missing(limit: int = 50) -> Dict[str, Any]:
    """Admin backfill: register every live agent that has no Celo id yet.
    Sequential on purpose (one registrar nonce chain)."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    rows = db.query_all(
        "SELECT agent_id FROM agents WHERE deleted_at=0 AND celo_agent_id=0 "
        "ORDER BY created_at ASC LIMIT ?", (int(limit),))
    done, failed = [], []
    for r in rows:
        res = register_agent(r["agent_id"])
        (done if res.get("ok") else failed).append({"agent_id": r["agent_id"], **res})
    return {"ok": True, "registered": len(done), "failed": len(failed),
            "done": done, "failures": failed}


def register_all_missing_async(limit: int = 50) -> Dict[str, Any]:
    """Admin entry point: the batch runs in a background thread (each
    registration waits for its receipt, so 20+ agents take minutes — far past
    any proxy timeout). Returns immediately with the queue size; the admin
    panel polls /celo/status for progress."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    if not _funded():
        return {"ok": False, "skipped": "registrar unfunded (needs CELO for gas)"}
    row = db.query_one("SELECT COUNT(*) c FROM agents WHERE deleted_at=0 AND celo_agent_id=0") or {}
    pending = int(row.get("c") or 0)
    with _inflight_lock:
        if "__batch__" in _inflight:
            return {"ok": True, "started": 0, "pending": pending, "already_running": True}
        _inflight.add("__batch__")

    def _run():
        try:
            r = register_all_missing(limit)
            oplog.op("celo.register_all", params={"registered": r.get("registered"),
                                                   "failed": r.get("failed")})
        finally:
            with _inflight_lock:
                _inflight.discard("__batch__")
    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": min(pending, int(limit)), "pending": pending}


def register_platform() -> Dict[str, Any]:
    """Mint the platform Analyst's Celo Agent ID once (idempotent)."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    cur = platform_agent()
    if cur:
        return {"ok": True, "already": True, **cur}
    if not _funded():
        return {"ok": False, "skipped": "registrar unfunded (needs CELO for gas)"}
    with _inflight_lock:
        if PLATFORM_CODE in _inflight:
            return {"ok": False, "skipped": "in flight"}
        _inflight.add(PLATFORM_CODE)
    try:
        r = _send_register(platform_uri())
        rec = {"agentId": r["agentId"], "txhash": r["txhash"], "uri": platform_uri(),
               "registered_at": time.time(), "chain_id": CHAIN_ID, "registry": REGISTRY}
        admin_store.set(_PLATFORM_KEY, rec)
        oplog.op("celo.platform_register", params={"celo_agent_id": r["agentId"],
                                                    "txhash": r["txhash"][:18]})
        return {"ok": True, **rec}
    except Exception as e:
        oplog.error("celo.platform_register", repr(e)[:400])
        return {"ok": False, "error": str(e)[:200]}
    finally:
        with _inflight_lock:
            _inflight.discard(PLATFORM_CODE)


def platform_agent() -> Optional[Dict[str, Any]]:
    try:
        rec = admin_store.get(_PLATFORM_KEY, None)
        if isinstance(rec, dict) and int(rec.get("agentId") or 0) > 0:
            return rec
    except Exception:
        pass
    return None


def registered_count() -> int:
    row = db.query_one("SELECT COUNT(*) c FROM agents WHERE celo_agent_id>0")
    return int(row["c"]) if row else 0


def public_status() -> Dict[str, Any]:
    """Facts safe to expose (no keys)."""
    return {
        "enabled": enabled(),
        "chain_id": CHAIN_ID,
        "identity_registry": REGISTRY,
        "reputation_registry": REPUTATION_REGISTRY,
        "registrar": registrar_address(),
        "registrar_celo": registrar_balance_celo(),
        "explorer_tx": EXPLORER_TX,
        "platform_agent": platform_agent(),
        "registered_agents": registered_count(),
    }


# ---------------------------------------------------------------- cards ----

def _x402_cfg():
    from . import x402 as _x
    return _x


def agent_card(agent: Dict[str, Any]) -> Dict[str, Any]:
    """ERC-8004 registration-v1 file for one agent — PUBLIC facts only. Builds
    on the 0G card (same public facts) and adds the Celo registration, the
    x402 service endpoint (when the owner sells insights) and the trust
    models this agent supports."""
    card = zg.agent_card(agent)
    code = agent_model.agent_code(agent["agent_id"])
    base = public_base_url()
    x = _x402_cfg()
    sells = bool(int(agent.get("x402_sell") or 0)) and x.enabled()
    services: List[Dict[str, Any]] = []
    for s in card.get("services") or []:
        s = dict(s)
        s.setdefault("endpoint", s.get("url"))
        services.append(s)
    services.append({"name": "agent-card", "endpoint": agent_uri(code), "version": "erc-8004/registration-v1"})
    if sells:
        services.append({"name": "x402-insight", "version": "x402/2",
                         "endpoint": f"{base}/api/x402/agents/{code}/insight",
                         "price": {"asset": "USDC", "network": x.NETWORK,
                                   "usd": x.price_usd("insight")}})
    card["services"] = services
    regs = list(card.get("registrations") or [])
    cid = int(agent.get("celo_agent_id") or 0)
    if cid > 0:
        regs.append({"agentId": cid, "agentRegistry": REGISTRY_CAIP})
    if regs:
        card["registrations"] = regs
    card["supportedTrust"] = ["reputation"]
    card["x402Support"] = sells
    card["active"] = (agent.get("status") in ("starting", "running")
                      and float(agent.get("deleted_at") or 0) == 0)
    xm = dict(card.get("x-maneki") or {})
    xm["celo"] = {"agentId": cid, "tx": agent.get("celo_agent_tx") or "",
                  "registry": REGISTRY_CAIP,
                  "reputationRegistry": f"eip155:{CHAIN_ID}:{REPUTATION_REGISTRY}"}
    if x.pay_to():
        xm["wallet"] = {"chainId": CHAIN_ID, "address": x.pay_to(),
                        "role": "x402 payTo / stablecoin Gas treasury"}
    card["x-maneki"] = xm
    return card


def platform_card() -> Dict[str, Any]:
    """Registration file for the platform Analyst agent (Ask ManekiAI + briefs)."""
    base = public_base_url()
    x = _x402_cfg()
    rec = platform_agent() or {}
    card: Dict[str, Any] = {
        "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
        "name": "ManekiAI Analyst · Maneki AI",
        "description": ("The platform analyst of Maneki AI — answers market questions and "
                        "writes per-symbol briefs for US-stock perpetuals on Hyperliquid, "
                        "paid per request in USDC over x402 on Celo. Same market data and "
                        "models the autonomous trading agents use."),
        "image": f"{base}/maneki-icon.png",
        "url": base,
        "services": [
            {"name": "web", "endpoint": base, "url": base},
            {"name": "agent-card", "endpoint": platform_uri(), "version": "erc-8004/registration-v1"},
            {"name": "x402-chat", "version": "x402/2", "endpoint": f"{base}/api/x402/chat",
             "method": "POST", "price": {"asset": "USDC", "network": x.NETWORK, "usd": x.price_usd("chat")}},
            {"name": "x402-brief", "version": "x402/2", "endpoint": f"{base}/api/x402/brief",
             "method": "GET", "price": {"asset": "USDC", "network": x.NETWORK, "usd": x.price_usd("brief")}},
            {"name": "x402-catalog", "endpoint": f"{base}/api/x402/catalog"},
        ],
        "supportedTrust": ["reputation"],
        "x402Support": x.enabled(),
        "active": True,
        "x-maneki": {"code": PLATFORM_CODE, "role": "platform-analyst"},
    }
    if rec:
        card["registrations"] = [{"agentId": int(rec["agentId"]), "agentRegistry": REGISTRY_CAIP}]
        card["x-maneki"]["celo"] = {"agentId": int(rec["agentId"]), "tx": rec.get("txhash", ""),
                                    "registry": REGISTRY_CAIP}
    if x.pay_to():
        card["x-maneki"]["wallet"] = {"chainId": CHAIN_ID, "address": x.pay_to(),
                                      "role": "x402 payTo / stablecoin Gas treasury"}
    return card
