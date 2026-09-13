"""ERC-8004 identity on Celo — every ManekiAI agent gets an on-chain Agent ID.

Celo's ERC-8004 Identity Registry lives at the SAME deterministic address as
0G's (`0x8004A169…a432`, docs.celo.org → build-with-ai/8004), so this module
reuses the pure calldata / receipt helpers of services/zerog_agentid.py and
adds what differs: the Celo RPC, its own agent columns, the platform-level
"ManekiAI Analyst" agent that fronts the x402 endpoints, and registration-v1
agent cards that carry BOTH chains' registrations plus the x402 services.

Scope (product decision 2026-09-10 — "the fleet, not just one model"): unlike
0G, where only 0G-Compute agents register, on Celo EVERY live (non-deleted)
agent is registered, regardless of model. One registration ≈ 180k gas
≈ 0.037 CELO at today's 200 gwei.

Signing: the registrar wallet (celo/wallet.py — key `CELO_REGISTRAR_KEY`,
falls back to `ZEROG_REGISTRAR_KEY`; one nonce chain shared with self-settled
x402 payments). No key → the feature is silently OFF. The wallet only pays
gas; it never receives user funds (the treasury is a separate, receive-only
address). Fund it with a little CELO.

Zero-touch (2026-09-11): app.py runs `autopilot_tick()` every two minutes —
as soon as the registrar holds ≥ 0.05 CELO it mints the platform Analyst and
then every live agent that has no id, no admin click needed. A broadcast
whose receipt did not arrive in time is PERSISTED (celo_agent_tx with id 0)
and finalised on a later pass instead of minting a second id.

Failure model: best-effort and asynchronous — registration never blocks or
fails agent creation. Errors land in oplog; the admin console can still
force a batch. Idempotent: an agent with an id is never re-registered.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .. import db, admin_store
from ..models import agent_model
from ..services import oplog
from ..services import zerog_agentid as zg
from . import wallet

RPC_URLS = wallet.RPC_URLS
CHAIN_ID = 42220
REGISTRY = zg.REGISTRY                      # 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432
REPUTATION_REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
REGISTRY_CAIP = f"eip155:{CHAIN_ID}:{REGISTRY}"
EXPLORER_TX = "https://celoscan.io/tx/"
EXPLORER_ADDR = "https://celoscan.io/address/"
# keccak("setAgentURI(uint256,string)")[:4] / keccak("tokenURI(uint256)")[:4]
SEL_SET_AGENT_URI = "0af28bd3"
SEL_TOKEN_URI = "c87b56dd"

# The platform's own agent — the "ManekiAI Analyst" that answers Ask-ManekiAI
# and symbol briefs over x402. Registered once; its id/tx live in the admin
# store (there is no agents row for it).
PLATFORM_CODE = "maneki-analyst"
_PLATFORM_KEY = "celo_platform_agent"
_PLATFORM_PENDING_KEY = "celo_platform_pending_tx"

GAS_LIMIT_FALLBACK = 400_000
MIN_REGISTRAR_CELO = wallet.MIN_CELO
RETRY_BACKOFF_S = 600
PENDING_MAX_AGE_S = 3600        # a persisted-but-unmined tx older than this may be re-sent
# A durably-reverting registration (bad URI, permanent contract-side reject)
# would otherwise be retried forever by the 2-minute autopilot, broadcasting
# (and burning real gas on) a doomed tx every RETRY_BACKOFF_S. Stop for real
# after this many consecutive failures; an admin can clear the counter
# (POST /admin/celo/register-all {"reset_failed": true}) to force more tries.
MAX_CONSECUTIVE_FAILS = 8

_inflight_lock = threading.Lock()
_inflight: set = set()
_bal_cache = wallet._bal_cache  # shared with the wallet (tests reset it here)
_last_fail: Dict[str, float] = {}
_fail_count: Dict[str, int] = {}


def reset_fail_counts() -> int:
    """Admin escape hatch: clear every agent's consecutive-failure count so
    the next autopilot pass / admin batch retries them again."""
    n = len(_fail_count)
    _fail_count.clear()
    _last_fail.clear()
    return n


# ------------------------------------------------------------- config -----

def registrar_key() -> str:
    return wallet.key()


def enabled() -> bool:
    """Feature-gated on the registrar key; also honors a kill switch."""
    if (os.environ.get("CELO_AGENTID_ENABLED", "1").strip().lower()
            in ("0", "false", "off", "no")):
        return False
    return len(registrar_key()) >= 32


def registrar_address() -> str:
    return wallet.address()


def public_base_url() -> str:
    return zg.public_base_url()


def agent_uri(code: str) -> str:
    return f"{public_base_url()}/api/agent-card/{code}"


def platform_uri() -> str:
    return agent_uri(PLATFORM_CODE)


# ---------------------------------------------------------- rpc plumbing --

def _rpc(method: str, params: list) -> Any:
    """Module-level so tests can stub the node; the wallet primitives receive
    it as rpc_fn and therefore see the stub too."""
    return wallet.rpc(method, params)


def registrar_balance_celo() -> Optional[float]:
    return wallet.balance_celo(rpc_fn=_rpc)


def _funded() -> bool:
    return wallet.funded(rpc_fn=_rpc)


def _send_register(uri: str) -> Dict[str, Any]:
    """Sign + broadcast register(uri) from the registrar (serialized on the
    wallet's send lock), wait for the receipt, return {agentId, txhash}.
    Raises wallet.ReceiptTimeout (hash attached) when the tx is out but
    unconfirmed, RuntimeError on revert."""
    data = zg._encode_register(uri)
    r = wallet.send_and_wait(REGISTRY, data, rpc_fn=_rpc, gas_fallback=GAS_LIMIT_FALLBACK)
    aid = zg._parse_agent_id(r["receipt"])
    if aid is None:
        raise RuntimeError(f"agentId not found in receipt logs: {r['txhash']}")
    return {"agentId": aid, "txhash": r["txhash"]}


class RevertedPending(RuntimeError):
    """The persisted tx mined but reverted / carried no agentId — dead; the
    hash must be cleared so a later pass can re-send."""


def _finalize_pending(txhash: str) -> Optional[Dict[str, Any]]:
    """A broadcast we persisted without a receipt: mined → {agentId, txhash};
    still pending → None; unknown to the node → LookupError; mined but
    reverted / no agentId → RevertedPending."""
    rec = wallet.receipt_of(txhash, rpc_fn=_rpc)
    if rec is None:
        return None
    if (rec.get("status") or "").lower() != "0x1":
        raise RevertedPending(f"register tx reverted: {txhash}")
    aid = zg._parse_agent_id(rec)
    if aid is None:
        raise RevertedPending(f"agentId not found in receipt logs: {txhash}")
    return {"agentId": aid, "txhash": txhash}


def _resolve_pending(txhash: str, since: float) -> Dict[str, Any]:
    """Shared pending-tx policy for agents and the platform agent:
      mined ok      → {"done": {agentId, txhash}}
      reverted      → {"clear": True, "reason": …}           (re-send after backoff)
      unknown+fresh → {"wait": True}                          (gateway lag: keep waiting)
      unknown+old   → {"clear": True, "reason": "dropped"}    (re-send)
      stuck+old     → {"wait": True, "bumped": newhash}       (replace-by-fee, SAME nonce)
      pending       → {"wait": True}
    A fresh nonce is never used while the node still knows the old tx — it
    would only queue behind it and mint a second id when both land."""
    age = time.time() - float(since or 0)
    try:
        r = _finalize_pending(txhash)
    except LookupError:
        return {"wait": True} if age < PENDING_MAX_AGE_S else {"clear": True, "reason": "dropped by the node"}
    except RevertedPending as e:
        return {"clear": True, "reason": str(e)[:160]}
    if r is not None:
        return {"done": r}
    if age >= PENDING_MAX_AGE_S:
        try:
            new = wallet.bump_pending(txhash, rpc_fn=_rpc)
            return {"wait": True, "bumped": new}
        except LookupError:
            return {"clear": True, "reason": "dropped by the node"}
        except Exception as e:
            oplog.error("celo.bump_pending", repr(e)[:300], params={"tx": txhash[:18]})
    return {"wait": True}


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
    if _fail_count.get(agent_id, 0) >= MAX_CONSECUTIVE_FAILS:
        return {"ok": False, "skipped": "permanently failed — exceeded max retry attempts, "
                                        "admin must reset (POST /admin/celo/register-all "
                                        "{\"reset_failed\": true})"}
    with _inflight_lock:
        if agent_id in _inflight:
            return {"ok": False, "skipped": "in flight"}
        _inflight.add(agent_id)
    try:
        code = agent_model.agent_code(agent_id)
        uri = agent_uri(code)
        pending = (agent.get("celo_agent_tx") or "").strip()
        r: Optional[Dict[str, Any]] = None
        if pending:
            # A previous broadcast without a receipt — never mint twice.
            st = _resolve_pending(pending, float(agent.get("celo_registered_at") or 0))
            if st.get("done"):
                r = st["done"]
            elif st.get("wait"):
                if st.get("bumped"):
                    db.execute("UPDATE agents SET celo_agent_tx=?, celo_registered_at=? WHERE agent_id=?",
                               (st["bumped"], time.time(), agent_id))
                    oplog.op("agent.celo_bump", agent.get("address") or "",
                             params={"agent_id": agent_id, "old": pending[:18], "new": st["bumped"][:18]})
                return {"ok": False, "skipped": "pending receipt", "txhash": st.get("bumped") or pending}
            else:
                db.execute("UPDATE agents SET celo_agent_tx='' WHERE agent_id=?", (agent_id,))
                oplog.error("agent.celo_register", f"pending tx cleared: {st.get('reason')} ({pending})",
                            address=agent.get("address") or "", params={"agent_id": agent_id})
                if not str(st.get("reason") or "").startswith("dropped"):
                    _last_fail[agent_id] = time.time()          # reverted: retry after the backoff
                    _fail_count[agent_id] = _fail_count.get(agent_id, 0) + 1
                    return {"ok": False, "error": f"pending tx cleared: {st.get('reason')}"}
                # dropped by the node: nothing is in flight — send again now
        if r is None:
            if not _funded():
                return {"ok": False, "skipped": "registrar unfunded (needs CELO for gas)"}
            try:
                r = _send_register(uri)
            except wallet.ReceiptTimeout as e:
                # Persist the hash (id stays 0); the next pass finalises it.
                db.execute("UPDATE agents SET celo_agent_tx=?, celo_registered_at=? WHERE agent_id=?",
                           (e.txhash, time.time(), agent_id))
                oplog.error("agent.celo_register", f"receipt timeout, persisted {e.txhash}",
                            address=agent.get("address") or "", params={"agent_id": agent_id})
                return {"ok": False, "skipped": "pending receipt", "txhash": e.txhash}
        db.execute(
            "UPDATE agents SET celo_agent_id=?, celo_agent_tx=?, celo_registered_at=? WHERE agent_id=?",
            (r["agentId"], r["txhash"], time.time(), agent_id),
        )
        oplog.op("agent.celo_register", agent.get("address") or "",
                 params={"agent_id": agent_id, "celo_agent_id": r["agentId"],
                         "txhash": r["txhash"][:18], "uri": uri})
        print(f"[vectora-live] celo: minted {code} Agent ID #{r['agentId']} tx {r['txhash']}")
        _last_fail.pop(agent_id, None)
        _fail_count.pop(agent_id, None)
        return {"ok": True, "agentId": r["agentId"], "txhash": r["txhash"], "uri": uri}
    except Exception as e:
        _last_fail[agent_id] = time.time()
        _fail_count[agent_id] = _fail_count.get(agent_id, 0) + 1
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
            return                       # recent failure — the autopilot / admin batch retries
        threading.Thread(target=register_agent, args=(agent_id,), daemon=True).start()
    except Exception:
        pass


def register_all_missing(limit: int = 50, respect_backoff: bool = False) -> Dict[str, Any]:
    """Register every live agent that has no Celo id yet. Sequential on
    purpose (one registrar nonce chain). The autopilot passes
    respect_backoff=True so a recently failed agent is not retried every
    tick; the admin batch forces everything."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    rows = db.query_all(
        "SELECT agent_id FROM agents WHERE deleted_at=0 AND celo_agent_id=0 "
        "ORDER BY created_at ASC LIMIT ?", (int(limit),))
    done, failed, skipped = [], [], []
    for r in rows:
        if respect_backoff and time.time() - _last_fail.get(r["agent_id"], 0.0) < RETRY_BACKOFF_S:
            skipped.append(r["agent_id"])
            continue
        res = register_agent(r["agent_id"])
        (done if res.get("ok") else failed).append({"agent_id": r["agent_id"], **res})
        if not res.get("ok") and "unfunded" in str(res.get("skipped") or ""):
            break                                   # no point trying the rest this pass
    return {"ok": True, "registered": len(done), "failed": len(failed), "backoff": len(skipped),
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
    """Mint the platform Analyst's Celo Agent ID once (idempotent, timeout-safe)."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    cur = platform_agent()
    if cur:
        return {"ok": True, "already": True, **cur}
    with _inflight_lock:
        if PLATFORM_CODE in _inflight:
            return {"ok": False, "skipped": "in flight"}
        _inflight.add(PLATFORM_CODE)
    try:
        r: Optional[Dict[str, Any]] = None
        pend = admin_store.get(_PLATFORM_PENDING_KEY, None)
        if isinstance(pend, dict) and pend.get("txhash"):
            st = _resolve_pending(pend["txhash"], float(pend.get("at") or 0))
            if st.get("done"):
                r = st["done"]
            elif st.get("wait"):
                if st.get("bumped"):
                    admin_store.set(_PLATFORM_PENDING_KEY, {"txhash": st["bumped"], "at": time.time()})
                return {"ok": False, "skipped": "pending receipt", "txhash": st.get("bumped") or pend["txhash"]}
            else:
                admin_store.set(_PLATFORM_PENDING_KEY, {})
                oplog.error("celo.platform_register", f"pending tx cleared: {st.get('reason')} ({pend['txhash']})")
                if not str(st.get("reason") or "").startswith("dropped"):
                    return {"ok": False, "error": f"pending tx cleared: {st.get('reason')}"}
        if r is None:
            if not _funded():
                return {"ok": False, "skipped": "registrar unfunded (needs CELO for gas)"}
            try:
                r = _send_register(platform_uri())
            except wallet.ReceiptTimeout as e:
                admin_store.set(_PLATFORM_PENDING_KEY, {"txhash": e.txhash, "at": time.time()})
                oplog.error("celo.platform_register", f"receipt timeout, persisted {e.txhash}")
                return {"ok": False, "skipped": "pending receipt", "txhash": e.txhash}
        rec = {"agentId": r["agentId"], "txhash": r["txhash"], "uri": platform_uri(),
               "registered_at": time.time(), "chain_id": CHAIN_ID, "registry": REGISTRY}
        admin_store.set(_PLATFORM_KEY, rec)
        admin_store.set(_PLATFORM_PENDING_KEY, {})
        oplog.op("celo.platform_register", params={"celo_agent_id": r["agentId"],
                                                    "txhash": r["txhash"][:18]})
        print(f"[vectora-live] celo: minted platform Analyst Agent ID #{r['agentId']} tx {r['txhash']}")
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


def pending_count() -> int:
    row = db.query_one("SELECT COUNT(*) c FROM agents WHERE deleted_at=0 AND celo_agent_id=0")
    return int(row["c"]) if row else 0


# ------------------------------------------------------------- autopilot --

_autopilot_lock = threading.Lock()


def autopilot_tick(limit: int = 50) -> Dict[str, Any]:
    """One pass of the zero-touch registrar: re-read the balance, mint the
    platform Analyst if missing, then the live agents without an id. Safe to
    call every couple of minutes (idempotent, skips while unfunded, one pass
    at a time). Returns what it did for the caller's log line."""
    if not enabled():
        return {"skipped": "disabled"}
    if not _autopilot_lock.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        wallet.invalidate_balance()
        bal = registrar_balance_celo()
        if bal is not None and bal < MIN_REGISTRAR_CELO:
            return {"skipped": "unfunded", "registrar_celo": bal, "pending": pending_count()}
        out: Dict[str, Any] = {"registrar_celo": bal}
        if platform_agent() is None:
            out["platform"] = register_platform()
        if pending_count() > 0:
            r = register_all_missing(limit, respect_backoff=True)
            out["registered"] = r.get("registered", 0)
            out["failed"] = r.get("failed", 0)
        out["pending"] = pending_count()
        return out
    finally:
        _autopilot_lock.release()


# -------------------------------------------------------- URI maintenance --

def _encode_set_agent_uri(agent_id_onchain: int, uri: str) -> str:
    b = uri.encode()
    pad = ((len(b) + 31) // 32) * 32
    enc = (int(agent_id_onchain).to_bytes(32, "big") + (64).to_bytes(32, "big")
           + len(b).to_bytes(32, "big") + b + b"\x00" * (pad - len(b)))
    return "0x" + SEL_SET_AGENT_URI + enc.hex()


def token_uri(agent_id_onchain: int) -> str:
    """Current agentURI on-chain ('' when unreadable)."""
    try:
        data = "0x" + SEL_TOKEN_URI + int(agent_id_onchain).to_bytes(32, "big").hex()
        res = _rpc("eth_call", [{"to": REGISTRY, "data": data}, "latest"]) or "0x"
        b = bytes.fromhex(res[2:])
        ln = int.from_bytes(b[32:64], "big")
        return b[64:64 + ln].decode(errors="replace")
    except Exception:
        return ""


def set_agent_uri(agent_id_onchain: int, uri: str) -> Dict[str, Any]:
    """setAgentURI(agentId, uri) from the registrar (the NFT owner). Used by
    repoint_uris() when the public host changes after minting."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    if not _funded():
        return {"ok": False, "skipped": "registrar unfunded (needs CELO for gas)"}
    try:
        r = wallet.send_and_wait(REGISTRY, _encode_set_agent_uri(agent_id_onchain, uri),
                                 rpc_fn=_rpc, gas_fallback=0)
        return {"ok": True, "txhash": r["txhash"]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def repoint_uris_async() -> Dict[str, Any]:
    """Admin entry point: the writes run in a background thread (each waits
    for its receipt); one run at a time; the panel polls /celo/status."""
    if not enabled():
        return {"ok": False, "skipped": "disabled"}
    if not _funded():
        return {"ok": False, "skipped": "registrar unfunded (needs CELO for gas)"}
    with _inflight_lock:
        if "__repoint__" in _inflight:
            return {"ok": True, "started": False, "already_running": True}
        _inflight.add("__repoint__")

    def _run():
        try:
            repoint_uris(dry_run=False)
        finally:
            with _inflight_lock:
                _inflight.discard("__repoint__")
    threading.Thread(target=_run, daemon=True).start()
    plan = repoint_uris(dry_run=True)
    return {"ok": True, "started": True, "to_change": len(plan.get("changed") or []),
            "unchanged": plan.get("unchanged", 0)}


def repoint_uris(dry_run: bool = False) -> Dict[str, Any]:
    """Re-derive every registered URI from the CURRENT public base and send
    setAgentURI only where the on-chain value differs. Lets IDs be minted
    today and follow the host later."""
    targets: List[Dict[str, Any]] = []
    plat = platform_agent()
    if plat:
        targets.append({"kind": "platform", "onchain": int(plat["agentId"]), "uri": platform_uri()})
    for r in db.query_all("SELECT agent_id, celo_agent_id FROM agents WHERE celo_agent_id>0"):
        targets.append({"kind": "agent", "agent_id": r["agent_id"], "onchain": int(r["celo_agent_id"]),
                        "uri": agent_uri(agent_model.agent_code(r["agent_id"]))})
    changed, same, failed = [], [], []
    for t in targets:
        cur = token_uri(t["onchain"])
        if cur == t["uri"]:
            same.append(t["onchain"])
            continue
        if dry_run:
            changed.append({**t, "current": cur})
            continue
        res = set_agent_uri(t["onchain"], t["uri"])
        (changed if res.get("ok") else failed).append({**t, "current": cur, **res})
    if not dry_run and changed and plat:
        admin_store.set(_PLATFORM_KEY, {**plat, "uri": platform_uri()})
    oplog.op("celo.repoint_uris", params={"changed": len(changed), "same": len(same),
                                          "failed": len(failed), "dry_run": dry_run})
    return {"ok": True, "changed": changed, "unchanged": len(same), "failed": failed, "dry_run": dry_run}


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
        "pending_agents": pending_count(),
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
            {"name": "x402-activity", "endpoint": f"{base}/api/x402/activity"},
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
