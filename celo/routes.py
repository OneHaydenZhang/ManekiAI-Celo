"""Public pay-per-request endpoints (x402 on Celo) + the Arena catalog.

No login, no exchange keys: anyone with USDC on Celo can buy
  POST /api/x402/chat                      Ask ManekiAI (platform analyst)
  GET  /api/x402/brief?symbol=NVDA         shared 10-minute symbol brief
  GET  /api/x402/agents/{code}/insight     one live agent's latest decision
and read for free
  GET  /api/x402/config                    chain / asset / prices / registries
  GET  /api/x402/catalog                   agents whose owners sell insights

Flow per paid call: 402 challenge → client signs EIP-3009 → verify →
produce content → settle → 200 (see celo/x402.py). Content failures are never
charged; settlement failures never leak content.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db
from ..admin_auth import client_ip
from ..models import agent_model, config_model, trade_model
from ..services import agent_service, chat_service, oplog, pricing
from . import x402, agentid, tasks, native_pay

router = APIRouter(prefix="/api/x402")

_EXPOSE = "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-Trace-Id"
_SYMBOL_RE = re.compile(r"[A-Za-z0-9:._\-]{1,24}")
_ADDR_RE = re.compile(r"0x[0-9a-f]{40}")
_NONCE_RE = re.compile(r"0x[0-9a-f]{64}")
_MSG_MAX = 600
_BRIEF_TTL_S = 600
_CATALOG_TTL_S = 30
_ACTIVITY_TTL_S = 60
_MIN_VALID_S = 60                 # authorization must outlive verify + content + settle
# Abuse guards (review 2026-09-11): a payer who makes settlement fail after
# the LLM ran (deliberately short validBefore, drained balance) burns model
# cost for free — two failures in 10 min → 429 for that payer; and at most
# three paid generations run concurrently.
_FAIL_WINDOW_S, _FAIL_MAX = 600, 2
_payer_fails: Dict[str, List[float]] = {}
# Unverified attempts per payer address (bogus signatures cost a verifier
# round trip): 10 per 10 min, then 429. Verified payments never count — the
# attempt is recorded before the verifier call and forgiven right after a
# valid verdict (see _attempt_forgive), so a real buyer can purchase as often
# as they like while a signature-spammer is still capped.
_ATTEMPT_WINDOW_S, _ATTEMPT_MAX = 600, 10
_payer_attempts: Dict[str, List[float]] = {}
_llm_sem = asyncio.Semaphore(3)
_LLM_QUEUE_TIMEOUT_S = 30
_LLM_MIN_VALID_LEFT_S = 45         # the signature must still cover generation + settle
_activity_cache: Dict[str, Any] = {"at": 0.0, "val": None}
# (payer, nonce) currently being verified — replay protection BEFORE a ledger
# row exists (rows are only written for verified payments).
_verifying_lock = threading.Lock()
_verifying: set = set()
# Content generated for a payment whose settlement is still pending: the
# buyer's retry with the SAME signature gets it without a second model call.
_content_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_CONTENT_CACHE_TTL_S = 900

# Per-IP request budget on the paid surface (the 402 challenge itself is free
# to request; the payment is the real rate limiter for content).
_RL_WINDOW_S, _RL_MAX = 60, 120
_RL: Dict[str, List[float]] = {}
_brief_cache: Dict[str, Dict[str, Any]] = {}
_catalog_cache: Dict[str, Any] = {"at": 0.0, "val": None}


def _ip(request: Request) -> str:
    # Never the first X-Forwarded-For hop (client-controlled): see admin_auth.client_ip.
    return client_ip(request)


def _payer_cooling(payer: str) -> bool:
    now = time.time()
    q = [t for t in _payer_fails.get(payer, []) if now - t < _FAIL_WINDOW_S]
    _payer_fails[payer] = q
    return len(q) >= _FAIL_MAX


def _note_payer_failure(payer: str) -> None:
    _payer_fails.setdefault(payer, []).append(time.time())
    if len(_payer_fails) > 5000:
        now = time.time()
        for k in [k for k, v in _payer_fails.items() if not v or now - v[-1] > _FAIL_WINDOW_S]:
            _payer_fails.pop(k, None)


def _attempt_ok(payer: str) -> bool:
    now = time.time()
    q = [t for t in _payer_attempts.get(payer, []) if now - t < _ATTEMPT_WINDOW_S]
    _payer_attempts[payer] = q
    if len(q) >= _ATTEMPT_MAX:
        return False
    q.append(now)
    if len(_payer_attempts) > 5000:
        for k in [k for k, v in _payer_attempts.items() if not v or now - v[-1] > _ATTEMPT_WINDOW_S]:
            _payer_attempts.pop(k, None)
    return True


def _attempt_forgive(payer: str) -> None:
    """The verifier accepted this payment: it was a real purchase, not a probe.
    Drop the attempt that _attempt_ok just recorded so honest buyers never hit
    the cap (a demo wallet buying 11 insights in 10 minutes must keep working)."""
    q = _payer_attempts.get(payer)
    if q:
        q.pop()


def _pending_response(info_or_product: Any, resource: str = "") -> JSONResponse:
    """202: the settlement is undecided (lost reply / unmined). The client
    keeps its signed payload and retries the same request; a settled row is
    then served without paying again."""
    resp = JSONResponse(status_code=202, content={
        "status": "settle_pending", "retry_after_s": 8, "retry_with_same_signature": True,
        "message": "Settlement is being confirmed on Celo — retry this request with the same "
                   "PAYMENT-SIGNATURE in a few seconds; you will not be charged twice."})
    resp.headers["Access-Control-Expose-Headers"] = _EXPOSE
    return resp


def _cache_content(payer: str, nonce: str, content: Dict[str, Any]) -> None:
    now = time.time()
    _content_cache[(payer, nonce)] = (now, content)
    if len(_content_cache) > 500:
        for k in [k for k, (t, _) in _content_cache.items() if now - t > _CONTENT_CACHE_TTL_S]:
            _content_cache.pop(k, None)


def _cached_content(payer: str, nonce: str) -> Optional[Dict[str, Any]]:
    ent = _content_cache.get((payer, nonce))
    if ent and time.time() - ent[0] < _CONTENT_CACHE_TTL_S:
        return ent[1]
    return None


def _rate_ok(ip: str) -> bool:
    now = time.time()
    q = [t for t in _RL.get(ip, []) if now - t < _RL_WINDOW_S]
    if len(q) >= _RL_MAX:
        _RL[ip] = q
        return False
    q.append(now)
    _RL[ip] = q
    if len(_RL) > 5000:                       # bound the dict itself
        for k in [k for k, v in _RL.items() if not v or now - v[-1] > _RL_WINDOW_S]:
            _RL.pop(k, None)
    return True


def _resource_url(request: Request) -> str:
    return x402.public_base() + request.url.path


def _bare(symbol: str) -> str:
    return (symbol or "").split(":")[-1].upper()


def _402(product: str, resource: str, error: str = "",
         amount_usd: Optional[float] = None) -> JSONResponse:
    pr = x402.payment_required(product, resource, error, amount_usd)
    resp = JSONResponse(status_code=402, content=pr)
    resp.headers["PAYMENT-REQUIRED"] = x402.b64e(pr)
    resp.headers["Access-Control-Expose-Headers"] = _EXPOSE
    return resp


async def _gate(request: Request, product: str, agent_id: str = "",
                amount_usd: Optional[float] = None) -> Tuple[Optional[JSONResponse], Dict[str, Any]]:
    """Returns (response, {}) when the caller must (re)pay, or (None, info)
    when a valid payment authorization is in hand (verified, not settled).

    `amount_usd` prices ONE request (a research task = unit × checks). The
    caller recomputes it from the request body on every hop, so a client that
    re-sends a signature against a bigger configuration simply fails the
    `value >= amount` check below."""
    if not x402.enabled():
        raise HTTPException(503, "x402 payments are not enabled on this server")
    ip = _ip(request)
    if not _rate_ok(ip):
        raise HTTPException(429, "too many requests — slow down")
    resource = _resource_url(request)
    offers = x402.accepts(product, resource, amount_usd)
    sig = request.headers.get("payment-signature") or ""
    if not sig:
        return _402(product, resource, "PAYMENT-SIGNATURE header is required", amount_usd), {}
    try:
        payload = x402.b64d(sig)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except Exception:
        return _402(product, resource, "malformed PAYMENT-SIGNATURE header", amount_usd), {}
    if int(payload.get("x402Version") or 0) != 2:
        return _402(product, resource, "x402Version must be 2", amount_usd), {}
    req = x402.match_offer(payload.get("accepted") or {}, offers)
    if req is None:
        return _402(product, resource, "payment requirements mismatch — re-read PAYMENT-REQUIRED", amount_usd), {}
    f = x402.payload_fields(payload)
    # Shape + validity window BEFORE anything is recorded or verified: the
    # ledger is permanent and the verifier costs a network round trip.
    if not _ADDR_RE.fullmatch(f["payer"]) or not _NONCE_RE.fullmatch(f["nonce"]):
        return _402(product, resource, "authorization.from and nonce must be well-formed hex", amount_usd), {}
    if f["to"] != req["payTo"].lower():
        return _402(product, resource, "authorization.to must equal payTo", amount_usd), {}
    try:
        now = int(time.time())
        if int(f["valid_after"]) > now:
            return _402(product, resource, "authorization_not_yet_valid", amount_usd), {}
        if int(f["valid_before"]) < now + _MIN_VALID_S:
            return _402(product, resource, "authorization_expired — sign with validBefore ≥ now + 60s", amount_usd), {}
        if int(f["value"]) < int(req["amount"]):
            return _402(product, resource, "authorization.value below the required amount", amount_usd), {}
    except (TypeError, ValueError):
        return _402(product, resource, "authorization fields must be integers", amount_usd), {}
    decimals = x402.ASSETS.get(x402.asset_symbol(req["asset"]) or "USDC", x402.ASSETS["USDC"])["decimals"]
    info: Dict[str, Any] = {"payload": payload, "req": req, "product": product, "resource": resource,
                            "payer": f["payer"], "nonce": f["nonce"],
                            "asset": x402.asset_symbol(req["asset"]) or "USDC",
                            "amount_usd": int(req["amount"]) / (10 ** decimals),
                            "valid_before": int(f["valid_before"])}
    # Same signature again? Serve what it already paid for. (payer, nonce)
    # alone are NOT proof of anything once a payment settles — they become
    # public on-chain the instant the tx mines (standard ERC20/
    # AuthorizationUsed event topics) — so this recovery path still requires
    # a signature that actually recovers to `payer` for these exact fields,
    # same as a fresh payment would need. No network round trip.
    existing = await asyncio.to_thread(x402.get_payment, f["payer"], f["nonce"])
    if existing:
        if existing["status"] == "settle_pending":
            existing = await asyncio.to_thread(x402.finalize_one, existing)
        if existing["status"] == "settled" and not existing.get("meta", {}).get("delivered"):
            if not await asyncio.to_thread(x402.signature_matches_payer, payload, req):
                return _402(product, resource, "payment invalid: signature does not match payer", amount_usd), {}
            info.update({"pid": int(existing["id"]), "already_settled": True, "tx": existing.get("tx") or ""})
            return None, info
        if existing["status"] == "settle_pending":
            return _pending_response(product, resource), {}
        if existing["status"] == "pending":
            return _402(product, resource, "this payment is still being processed", amount_usd), {}
        return _402(product, resource, "duplicate payment nonce — sign a fresh authorization", amount_usd), {}
    if _payer_cooling(f["payer"]):
        raise HTTPException(429, "too many failed payments from this wallet — try again in 10 minutes")
    if not _attempt_ok(f["payer"]):
        raise HTTPException(429, "too many payment attempts from this wallet — try again in 10 minutes")
    key = (f["payer"], f["nonce"])
    with _verifying_lock:
        if key in _verifying:
            return _402(product, resource, "this payment is still being verified", amount_usd), {}
        _verifying.add(key)
    try:
        v = await asyncio.to_thread(x402.verify, payload, req)
        if not v.get("isValid"):
            reason = str(v.get("invalidReason") or "rejected")[:160]
            oplog.error("x402.verify", reason, params={"product": product, "payer": f["payer"][:12]}, status=402)
            # No strike here: nothing was generated, so nothing was wasted —
            # the per-payer attempt cap bounds verifier round trips instead.
            return _402(product, resource, f"payment invalid: {reason}", amount_usd), {}
        _attempt_forgive(f["payer"])
        try:
            pid = x402.begin(f["payer"], f["nonce"], product, req, resource, agent_id=agent_id,
                             meta={"ip": ip})
        except x402.Duplicate:
            return _402(product, resource, "duplicate payment nonce — sign a fresh authorization", amount_usd), {}
    finally:
        with _verifying_lock:
            _verifying.discard(key)
    info.update({"pid": pid, "payer": (v.get("payer") or f["payer"]).lower()})
    return None, info


async def _llm_slot(info: Dict[str, Any]) -> Optional[JSONResponse]:
    """Take a generation slot; refuse (nothing charged, no strike) when the
    queue would eat the signature's validity window."""
    try:
        await asyncio.wait_for(_llm_sem.acquire(), timeout=_LLM_QUEUE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _content_failed(info, "analyst queue full")
    if int(info.get("valid_before") or 0) < int(time.time()) + _LLM_MIN_VALID_LEFT_S:
        _llm_sem.release()
        return _content_failed(info, "authorization window too short after queueing")
    return None


def _content_failed(info: Dict[str, Any], reason: str) -> JSONResponse:
    # Our failure, not the buyer's: no strike. An already-settled row keeps
    # its status (the retry will get the content once we recover).
    already_settled = bool(info.get("already_settled"))
    if not already_settled:
        x402.finish(info["pid"], "content_failed", error=reason)
    oplog.error("x402.content", reason, params={"product": info["product"]}, status=503)
    msg = ("Payment already settled but content could not be regenerated — please "
           "retry the same request shortly, you will not be charged again."
           if already_settled else
           "The analyst is busy right now — nothing was charged, please retry shortly.")
    return JSONResponse(status_code=503, content={"error": msg, "charged": already_settled})


async def _deliver(info: Dict[str, Any], content: Dict[str, Any],
                   agent: Optional[Dict[str, Any]] = None) -> JSONResponse:
    pid = info["pid"]
    if info.get("already_settled"):
        # Paid earlier, content never delivered (lost reply): serve it now.
        s = {"success": True, "transaction": info.get("tx") or "", "settler": x402.settler_mode()}
        tx = str(info.get("tx") or "")
        credits = 0.0
    else:
        _cache_content(info["payer"], info["nonce"], content)
        s = await asyncio.to_thread(x402.settle, info["payload"], info["req"])
        if not s.get("success"):
            reason = str(s.get("errorReason") or "settlement rejected")[:160]
            if s.get("pending"):
                # Undecided (lost reply / unmined broadcast): keep the hash if we
                # have one, park the row, let the buyer retry the same signature.
                x402.finish(pid, "settle_pending", tx=str(s.get("transaction") or ""), error=reason)
                oplog.error("x402.settle", f"pending: {reason}", params={"product": info["product"],
                                                                         "payer": info["payer"][:12]}, status=202)
                return _pending_response(info["product"], info["resource"])
            x402.finish(pid, "settle_failed", tx=str(s.get("transaction") or ""), error=reason)
            if reason in x402.PAYER_FAULT_REASONS:
                _note_payer_failure(info["payer"])
            oplog.error("x402.settle", reason, params={"product": info["product"],
                                                        "payer": info["payer"][:12]}, status=402)
            return _402(info["product"], info["resource"], f"settlement failed: {reason}")
        tx = str(s.get("transaction") or "")
        # The hash goes to the ledger FIRST — from this instant the deposit
        # scanner knows this Transfer is a sale, not a top-up.
        x402.finish(pid, "settled", tx=tx, error="")
        _activity_cache["at"] = 0.0        # the public activity feed shows the sale at once
        credits = 0.0
        if agent is not None:
            try:
                credits = await asyncio.to_thread(x402.credit_owner, agent, info["amount_usd"], tx,
                                                  info["product"], info["payer"], info["nonce"])
                x402.finish(pid, "settled", owner_credits=credits)
            except Exception as e:
                oplog.error("x402.owner_share", repr(e)[:300], params={"tx": tx[:18]})
                x402.update_meta(pid, owner_share_failed=True)   # autopilot retries it
        oplog.op("x402.sale", params={"product": info["product"], "payer": info["payer"],
                                      "usd": info["amount_usd"], "tx": tx[:18],
                                      "agent_id": (agent or {}).get("agent_id", ""),
                                      "owner_credits": credits})
    x402.mark_delivered(pid)
    # The buyer paid for this; keep it so a new browser or device can open it
    # again (celo/tasks.recover hands it back after a wallet signature).
    await asyncio.to_thread(
        x402.store_receipt, info["payer"], info["nonce"], int(pid), info["product"],
        str(content.get("symbol") or ""), float(info.get("amount_usd") or 0), tx, content)
    _content_cache.pop((info["payer"], info["nonce"]), None)
    body = dict(content)
    body["payment"] = {"tx": tx, "explorer": (x402.CHAIN["explorer_tx"] + tx) if tx else "",
                       "payer": info["payer"], "amount_usd": info["amount_usd"],
                       "asset": info.get("asset", "USDC"), "network": x402.NETWORK,
                       "settler": s.get("settler") or x402.settler_mode()}
    resp = JSONResponse(body)
    resp.headers["PAYMENT-RESPONSE"] = x402.b64e({
        "success": True, "payer": s.get("payer") or info["payer"], "transaction": tx,
        "network": s.get("network") or x402.NETWORK})
    resp.headers["Access-Control-Expose-Headers"] = _EXPOSE
    return resp


# ------------------------------------------------------------ free reads --

def _analyst_ready() -> bool:
    try:
        return chat_service._client(config_model.load("")) is not None
    except Exception:
        return False


@router.get("/config")
async def x402_config() -> Dict[str, Any]:
    cfg = x402.public_config()
    cfg["agentid"] = await asyncio.to_thread(agentid.public_status)
    cfg["analyst_card"] = agentid.platform_uri()
    cfg["analyst_ready"] = await asyncio.to_thread(_analyst_ready)
    cfg["tasks"] = {
        "enabled": tasks.enabled(),
        "types": list(tasks.TASK_TYPES),
        "hours": list(tasks.HOURS_CHOICES),
        "frequencies": list(tasks.FREQ_CHOICES),
        "unit_usd": {t: tasks.unit_usd(t) for t in tasks.TASK_TYPES},
        "max_checks": tasks.MAX_CHECKS,
        "max_total_usd": tasks.MAX_TOTAL_USD,
        "max_running_per_wallet": tasks.MAX_RUNNING_PER_PAYER,
        "goal_max": tasks.GOAL_MAX,
        "refundable": False,
    }
    cfg["celo_pay"] = await asyncio.to_thread(native_pay.public_config)
    return cfg


@router.get("/activity")
async def x402_activity() -> Dict[str, Any]:
    """PUBLIC on-chain proof: totals, recent settlements, registrations,
    deposit-lane senders. 60s cache; never IPs, full payer or owner addresses."""
    now = time.time()
    if _activity_cache["val"] is not None and now - _activity_cache["at"] < _ACTIVITY_TTL_S:
        return _activity_cache["val"]
    val = await asyncio.to_thread(x402.activity)
    try:
        val["tasks"] = await asyncio.to_thread(tasks.summary)
        val["tasks"]["samples"] = await asyncio.to_thread(tasks.shared_recent, 10)
    except Exception as e:
        oplog.error("x402.activity_tasks", repr(e)[:200])
    try:
        # Paying in CELO is a second lane with its own totals, and the rate it
        # charges at is worth publishing next to them.
        val["celo_pay"] = await asyncio.to_thread(native_pay.summary)
    except Exception as e:
        oplog.error("x402.activity_celo_pay", repr(e)[:200])
    try:
        # Two payment lanes, one public log: x402 settlements and paid-in-CELO
        # orders are the same purchases and belong in the same list, each row
        # carrying which asset the money actually moved in.
        mine = await asyncio.to_thread(x402.operator_wallets)
        cel = await asyncio.to_thread(native_pay.recent, 30)
        rows = list(val.get("recent") or []) + [
            {"ts": r["ts"], "product": r["product"], "amount_usd": r["amount_usd"],
             "asset": "CELO", "tx": r["tx"], "explorer": x402.CHAIN["explorer_tx"] + r["tx"],
             "payer_short": x402._short_addr(r["payer"]), "team": r["payer"] in mine,
             "agent_code": ""} for r in cel]
        rows.sort(key=lambda r: -float(r.get("ts") or 0))
        val["recent"] = rows[:30]
    except Exception as e:
        oplog.error("x402.activity_celo_recent", repr(e)[:200])
    try:
        # The same purchases, grouped the way this lane is judged: wallets that
        # are not ours counted apart from our own testing, plus which asset the
        # money moved in. Paying in CELO is its own lane, so it is appended here.
        sb = await asyncio.to_thread(x402.scoreboard)
        cp = val.get("celo_pay") or {}
        if int(cp.get("orders") or 0):
            sb["by_asset"] = list(sb.get("by_asset") or []) + [
                {"asset": "CELO", "n": int(cp["orders"]), "usd": round(float(cp.get("usd") or 0), 4)}]
        val["scoreboard"] = sb
    except Exception as e:
        oplog.error("x402.activity_scoreboard", repr(e)[:200])
    _activity_cache["val"], _activity_cache["at"] = val, now
    return val


def _agent_public(agent: Dict[str, Any]) -> Dict[str, Any]:
    """Catalog entry — public facts + performance, never the owner address."""
    st = agent_service.estimated_stats(agent)
    last_ts = trade_model.last_decision_ts(agent["agent_id"]) or 0.0
    last = trade_model.list_decisions(agent_id=agent["agent_id"], limit=1)
    last_action = str((last[0].get("action") if last else "") or "")
    last_round = int((last[0].get("tick_no") if last else 0) or 0)
    model = agent.get("model") or ""
    return {
        "code": agent_model.agent_code(agent["agent_id"]),
        "label": agent.get("label") or _bare(agent.get("symbol") or ""),
        "symbol": _bare(agent.get("symbol") or ""),
        "persona": agent.get("persona") or "navigator",
        "model_tier": pricing.tier_of(model),
        "model_family": (model.split("/")[0] if "/" in model else (model or "default")),
        "status": agent.get("status") or "",
        "running": agent.get("status") in ("starting", "running"),
        "total_ticks": int(agent.get("total_ticks") or 0),
        "created_at": agent.get("created_at") or 0,
        "last_decision_ts": last_ts,
        # Free teaser: the action word only (never reasoning / confidence /
        # market read — those are the paid insight).
        "last_action": last_action,
        "last_round": last_round,
        "purchasable": last_ts > 0,
        "estimated_profit": st.get("estimated_profit"),
        "trade_volume": st.get("trade_volume"),
        "closed_trades": st.get("closed_trades"),
        "win_rate": st.get("win_rate"),
        "order_success_rate": st.get("order_success_rate"),
        "celo_agent_id": int(agent.get("celo_agent_id") or 0),
        "celo_agent_tx": agent.get("celo_agent_tx") or "",
        "zerog_agent_id": int(agent.get("zerog_agent_id") or 0),
        "price_usd": x402.price_usd("insight"),
        "card": agentid.agent_uri(agent_model.agent_code(agent["agent_id"])),
    }


def _catalog_sync() -> Dict[str, Any]:
    now = time.time()
    if _catalog_cache["val"] is not None and now - _catalog_cache["at"] < _CATALOG_TTL_S:
        return _catalog_cache["val"]
    rows = db.query_all("SELECT * FROM agents WHERE deleted_at=0 AND x402_sell=1 "
                        "ORDER BY status='running' DESC, updated_at DESC LIMIT 60")
    agents = []
    for r in rows:
        try:
            agents.append(_agent_public(agent_model._row_to_dict(r)))
        except Exception as e:
            oplog.error("x402.catalog", repr(e)[:200], params={"agent_id": r.get("agent_id")})
    val = {
        "enabled": x402.enabled(),
        "prices": {k: v["usd"] for k, v in x402.public_config()["prices"].items()},
        "analyst": {"code": agentid.PLATFORM_CODE, "name": "ManekiAI Analyst",
                    "celo": agentid.platform_agent(), "card": agentid.platform_uri(),
                    "symbols": _tracked_symbols()},
        "agents": agents,
        "registered_agents": agentid.registered_count(),
        "ts": now,
    }
    _catalog_cache["val"], _catalog_cache["at"] = val, now
    return val


_DEFAULT_SYMBOLS = ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD", "COIN", "MSTR"]


def _tracked_symbols() -> List[str]:
    rows = db.query_all("SELECT DISTINCT symbol FROM agents WHERE deleted_at=0 "
                        "ORDER BY symbol LIMIT 40")
    syms = {_bare(r["symbol"]) for r in rows if r.get("symbol")}
    return sorted(syms | set(_DEFAULT_SYMBOLS))


def invalidate_catalog() -> None:
    _catalog_cache["val"], _catalog_cache["at"] = None, 0.0


@router.get("/catalog")
async def catalog() -> Dict[str, Any]:
    return await asyncio.to_thread(_catalog_sync)


# ------------------------------------------------------------- paid: chat --

def _analyst_config() -> Dict[str, Any]:
    return config_model.load("")          # address='' → operator's shared LLM key


def _full_symbol(sym: str) -> str:
    """Agents trade `xyz:NVDA`-style tickers; accept bare names from buyers."""
    sym = (sym or "").strip()
    if not sym:
        return ""
    if ":" in sym:
        return sym
    row = db.query_one("SELECT symbol FROM agents WHERE deleted_at=0 AND UPPER(symbol) LIKE ? LIMIT 1",
                       ("%:" + sym.upper(),))
    return row["symbol"] if row else f"xyz:{sym.upper()}"


@router.post("/chat")
async def paid_chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    message = str(body.get("message") or "").strip()
    symbol = str(body.get("symbol") or "").strip()
    if symbol and not _SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(400, "invalid symbol")
    if len(message) > _MSG_MAX:
        raise HTTPException(400, f"message must be ≤ {_MSG_MAX} characters")
    if not message:
        raise HTTPException(400, "message required")
    resp, info = await _gate(request, "chat")
    if resp is not None:
        return resp
    content = _cached_content(info["payer"], info["nonce"])
    if content is None:
        busy = await _llm_slot(info)
        if busy is not None:
            return busy
        try:
            c = _analyst_config()
            data = await asyncio.to_thread(chat_service._run_chat, c, [], message, _full_symbol(symbol), False)
        finally:
            _llm_sem.release()
        if not data.pop("_billable", False):
            return _content_failed(info, "model busy / no answer")
        structured = {"on_topic": data.get("on_topic", True), "headline": data.get("headline", ""),
                      "points": data.get("points") or [], "analysis": data.get("analysis"),
                      "note": data.get("note")}
        content = {"product": "chat", "symbol": _bare(symbol), "reply": chat_service._flatten(structured),
                   "structured": structured,
                   "idea": {k: data.get(k) for k in ("has_trade_idea", "side", "confidence", "rationale", "mark")
                            if data.get(k) is not None},
                   "ts": time.time()}
    return await _deliver(info, content)


# ------------------------------------------------------------ paid: brief --

_BRIEF_PROMPT = ("Write a concise trading brief for {sym} for the next session, as a sell-side desk "
                 "note: (1) trend and momentum from the recent 1-minute candles, (2) key price "
                 "levels, (3) funding / open-interest read, (4) the main risk, (5) stance — long, "
                 "short or watch — with a confidence 0–1. Be specific with numbers.")


def _brief_sync(symbol: str) -> Optional[Dict[str, Any]]:
    bare = _bare(symbol)
    ent = _brief_cache.get(bare)
    now = time.time()
    if ent and now - ent["ts"] < _BRIEF_TTL_S:
        return {**ent["content"], "cached": True}
    c = _analyst_config()
    data = chat_service._run_chat(c, [], _BRIEF_PROMPT.format(sym=bare), _full_symbol(symbol), False)
    if not data.pop("_billable", False):
        return None
    structured = {"on_topic": True, "headline": data.get("headline", ""),
                  "points": data.get("points") or [], "analysis": data.get("analysis"),
                  "note": data.get("note")}
    content = {"product": "brief", "symbol": bare, "brief": chat_service._flatten(structured),
               "structured": structured,
               "stance": {k: data.get(k) for k in ("side", "confidence", "mark") if data.get(k) is not None},
               "generated_at": now, "ttl_s": _BRIEF_TTL_S}
    _brief_cache[bare] = {"ts": now, "content": content}
    if len(_brief_cache) > 200:
        for k in sorted(_brief_cache, key=lambda k: _brief_cache[k]["ts"])[:50]:
            _brief_cache.pop(k, None)
    return {**content, "cached": False}


@router.get("/brief")
async def paid_brief(request: Request, symbol: str = ""):
    symbol = (symbol or "").strip()
    if not symbol or not _SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(400, "symbol required, e.g. ?symbol=NVDA")
    if _bare(symbol) not in set(_tracked_symbols()):
        # One brief per tracked symbol (each is one shared LLM call) — an
        # arbitrary string must not mint a fresh model call per request.
        raise HTTPException(400, "unknown symbol — see /api/x402/catalog analyst.symbols")
    resp, info = await _gate(request, "brief")
    if resp is not None:
        return resp
    content = _cached_content(info["payer"], info["nonce"])
    if content is None:
        busy = await _llm_slot(info)
        if busy is not None:
            return busy
        try:
            content = await asyncio.to_thread(_brief_sync, symbol)
        finally:
            _llm_sem.release()
        if content is None:
            return _content_failed(info, "model busy / no brief")
    return await _deliver(info, content)


# ---------------------------------------------------------- paid: insight --

_OBS_PUBLIC = ("symbol", "mid", "mark", "prev_day", "funding", "open_interest", "source")


def _insight_sync(agent: Dict[str, Any]) -> Dict[str, Any]:
    rows = trade_model.list_decisions(agent_id=agent["agent_id"], limit=1)
    d = rows[0] if rows else None
    decision: Optional[Dict[str, Any]] = None
    if d:
        obs = d.get("observation") or {}
        market = {k: obs.get(k) for k in _OBS_PUBLIC if obs.get(k) is not None}
        ps = obs.get("position_size")
        try:
            psf = float(ps) if ps is not None else 0.0
        except (TypeError, ValueError):
            psf = 0.0
        market["position"] = "long" if psf > 0 else "short" if psf < 0 else "flat"
        decision = {"ts": d.get("ts"), "round": d.get("tick_no"), "action": d.get("action"),
                    "confidence": d.get("confidence"), "executed": bool(d.get("executed")),
                    "reasoning": d.get("reasoning") or "", "reasoning_zh": d.get("reasoning_zh") or "",
                    "market": market}
    pub = _agent_public(agent)
    st = agent_service.estimated_stats(agent)
    return {"product": "insight", "agent": pub, "decision": decision,
            "stats": {k: st.get(k) for k in ("estimated_profit", "trade_volume", "closed_trades",
                                              "win_rate", "order_attempted", "order_executed",
                                              "order_success_rate")},
            "lessons": (agent.get("lessons") or "")[:800],
            "ts": time.time()}


@router.get("/agents/{code}/insight")
async def paid_insight(code: str, request: Request):
    agent = await asyncio.to_thread(agent_model.by_code, code)
    if not agent or not int(agent.get("x402_sell") or 0):
        raise HTTPException(404, "this agent is not for sale")
    if not (trade_model.last_decision_ts(agent["agent_id"]) or 0):
        raise HTTPException(409, "this agent has no decision yet — nothing to unlock")
    resp, info = await _gate(request, "insight", agent_id=agent["agent_id"])
    if resp is not None:
        return resp
    content = _cached_content(info["payer"], info["nonce"])
    if content is None:
        content = await asyncio.to_thread(_insight_sync, agent)
        if not content.get("decision"):
            return _content_failed(info, "agent has no decision yet")
    return await _deliver(info, content, agent=agent)


# ------------------------------------------------------------ paid: tasks --
# "Create an agent": the buyer configures a standing assignment, pays once for
# the whole run, and a background runner (celo/tasks.py) delivers one report
# per check. The price is computed from the request body on EVERY hop — the
# 402 challenge, the signature check and the ledger all see the same number.

async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return body if isinstance(body, dict) else {}


def _refused(info: Dict[str, Any], status: int, message: str, reason: str) -> JSONResponse:
    """Refuse AFTER the authorization was verified but BEFORE settlement —
    nothing is charged and the payer keeps no strike (it is not their fault
    the run could not be accepted)."""
    if not info.get("already_settled"):
        x402.finish(info["pid"], "content_failed", error=reason)
    oplog.error("x402.task", reason, params={"payer": info["payer"][:12]}, status=status)
    return JSONResponse(status_code=status, content={"error": message, "charged": False})


def _quote_public(q: Dict[str, Any]) -> Dict[str, Any]:
    return {"task": q["task"], "symbol": q["symbol"], "hours": q["hours"],
            "frequency": q["frequency"], "checks": q["checks"],
            "unit_usd": q["unit_usd"], "total_usd": q["total_usd"],
            "atomic": x402.atomic(q["total_usd"]), "asset": "USDC",
            "product": q["product"], "interval_s": q["interval_s"], "window_s": q["window_s"]}


@router.post("/tasks/quote")
async def task_quote(request: Request):
    """FREE: what this configuration would cost. Same pure function the 402
    challenge uses, so the number the buyer sees is the number they sign."""
    body = await _json(request)
    try:
        q = await asyncio.to_thread(tasks.quote_from_body, body)
    except tasks.Invalid as e:
        raise HTTPException(400, str(e))
    return {"enabled": tasks.enabled(), **_quote_public(q),
            "note": "One payment covers the whole run. Stopping early does not refund."}


@router.post("/tasks")
async def task_create(request: Request):
    """PAID (variable): pay once, the run starts immediately."""
    if not tasks.enabled():
        raise HTTPException(503, "paid research tasks are not enabled on this server")
    body = await _json(request)
    try:
        q = await asyncio.to_thread(tasks.quote_from_body, body)
    except tasks.Invalid as e:
        raise HTTPException(400, str(e))
    resp, info = await _gate(request, q["product"], amount_usd=q["total_usd"])
    if resp is not None:
        return resp
    # Verified, not yet settled: refuse here and nothing is charged.
    running = await asyncio.to_thread(tasks.running_for, info["payer"])
    if running >= tasks.MAX_RUNNING_PER_PAYER and not info.get("already_settled"):
        return _refused(info, 409,
                        f"This wallet already has {running} running tasks — let one finish first. "
                        f"Nothing was charged.", "per-wallet task limit")
    content = _cached_content(info["payer"], info["nonce"])
    if content is None:
        row, token = await asyncio.to_thread(
            tasks.create, q, info["payer"], info["nonce"], int(info["pid"]), float(info["amount_usd"]))
        if not row:
            return _content_failed(info, "task could not be created")
        content = {"product": q["product"], "quote": _quote_public(q), "token": token,
                   "task": tasks.view(row, with_runs=False), "ts": time.time()}
    task_id = str((content.get("task") or {}).get("task_id") or "")
    out = await _deliver(info, content)
    if out.status_code != 200 or not task_id:
        return out
    # Settled: the run starts now. The settlement hash lives in the delivered
    # body (only _deliver sees it), so read it back from there.
    try:
        payload = json.loads(bytes(out.body).decode("utf-8"))
    except Exception:
        payload = None
    tx = str(((payload or {}).get("payment") or {}).get("tx") or info.get("tx") or "")
    row = await asyncio.to_thread(tasks.activate, task_id, tx)
    if payload is None:
        return out
    if row:
        payload["task"] = tasks.view(row, with_runs=False)
    fresh = JSONResponse(status_code=200, content=payload)
    for k, v in out.headers.items():
        if k.lower() in ("payment-response", "access-control-expose-headers"):
            fresh.headers[k] = v
    return fresh


def _task_token(request: Request, token: str = "") -> str:
    return (token or request.headers.get("x-task-token") or "").strip()


def _task_or_403(task_id: str, token: str) -> Dict[str, Any]:
    row = tasks.by_id(task_id)
    if not row or row["status"] == "awaiting_payment":
        raise HTTPException(404, "task not found")
    if not tasks.token_ok(row, token):
        # Same answer for "wrong token" and "not yours": a task id must not be
        # a way to learn that someone else's task exists.
        raise HTTPException(403, "this task needs the access token it was created with — "
                                 "restore it by signing with the wallet that paid")
    return row


@router.get("/tasks/challenge")
async def task_challenge(address: str = ""):
    """FREE: the exact text a buyer signs to restore their tasks on a new
    device. No transaction, no gas."""
    address = (address or "").strip().lower()
    if not _ADDR_RE.fullmatch(address):
        raise HTTPException(400, "address required")
    issued = int(time.time())
    return {"address": address, "issued_at": issued,
            "message": tasks.recovery_message(address, issued), "ttl_s": tasks.RECOVER_TTL_S}


@router.get("/tasks/shared")
async def task_shared_list():
    """FREE, public: delivery samples their buyers chose to publish. This is how
    anyone (a judge, a prospective buyer) can check that a paid run actually
    produced something, without owning a task."""
    rows = await asyncio.to_thread(tasks.shared_recent, 20)
    return {"count": len(rows), "samples": rows}


@router.get("/tasks/shared/{share_id}")
async def task_shared_get(share_id: str):
    """FREE, public: one published run — the assignment, every delivered report
    and the settlement tx. Never the access token or the payer address."""
    row = await asyncio.to_thread(tasks.by_share_id, share_id)
    if not row:
        raise HTTPException(404, "this report is not published (or was unpublished)")
    return await asyncio.to_thread(tasks.public_view, row)


@router.post("/tasks/{task_id}/share")
async def task_share(task_id: str, request: Request, token: str = ""):
    """Token-gated: the BUYER decides whether their run is public. Off by
    default — the assignment is their own words."""
    body = await _json(request)
    row = await asyncio.to_thread(_task_or_403, task_id, _task_token(request, token))
    want = bool(body.get("shared", True))
    if want and int(row.get("checks_done") or 0) < 1:
        raise HTTPException(409, "nothing to publish yet — wait for the first report")
    out = await asyncio.to_thread(tasks.set_shared, row["task_id"], want)
    return {"shared": bool(int((out or {}).get("shared") or 0)),
            "share_url": tasks.share_url(out or row),
            "task": tasks.view(out or row, with_runs=False)}


@router.get("/tasks/{task_id}")
async def task_get(task_id: str, request: Request, token: str = ""):
    """FREE, token-gated: the buyer's own configuration, progress and reports."""
    row = await asyncio.to_thread(_task_or_403, task_id, _task_token(request, token))
    return await asyncio.to_thread(tasks.view, row, True)


@router.post("/tasks/{task_id}/stop")
async def task_stop(task_id: str, request: Request, token: str = ""):
    """FREE, token-gated. Soft stop, no refund — the run was paid in full up
    front and the buyer is told so before signing."""
    tok = _task_token(request, token)
    row = await asyncio.to_thread(_task_or_403, task_id, tok)
    out = await asyncio.to_thread(tasks.stop, row["task_id"])
    return {"stopped": True, "refunded": False, "task": tasks.view(out or row, with_runs=False)}


@router.post("/tasks/recover")
async def task_recover(request: Request):
    """FREE: prove the wallet with a signature and get everything it owns back —
    its agent runs (with fresh access tokens) and the consultations it paid for,
    content included. Records live on the server; a browser only ever held a
    copy, which is why changing device or origin looked like data loss."""
    body = await _json(request)
    try:
        rows = await asyncio.to_thread(tasks.recover, str(body.get("address") or ""),
                                       body.get("issued_at"), str(body.get("signature") or ""))
    except tasks.Invalid as e:
        raise HTTPException(400, str(e))
    address = str(body.get("address") or "").strip().lower()
    receipts = await asyncio.to_thread(x402.receipts_for, address)
    return {"tasks": rows, "count": len(rows), "purchases": receipts,
            "purchase_count": len(receipts)}

# ------------------------------------------------------- paid in CELO --
# Same two products, paid by transferring CELO to our own address instead of
# signing a USDC authorization. No facilitator is involved — we are the
# recipient — and the price comes from the same Uniswap pools the swap box
# uses. The transfer must go through the token contract: a native send emits
# no log and could never be detected (see celo/native_pay.py).

def _celo_price_usd(product: str, payload: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Dollar price of one order — the SAME numbers the USDC lane charges."""
    if product in ("task_monitor", "task_research"):
        q = tasks.quote_from_body(payload)
        if q["product"] != product:
            raise tasks.Invalid("task type does not match the product")
        return float(q["total_usd"]), q
    if product == "chat":
        msg = str(payload.get("message") or "").strip()
        if not msg:
            raise tasks.Invalid("message required")
        if len(msg) > _MSG_MAX:
            raise tasks.Invalid(f"message must be ≤ {_MSG_MAX} characters")
        return float(x402.price_usd("chat")), payload
    if product == "brief":
        sym = _bare(str(payload.get("symbol") or ""))
        if sym not in set(_tracked_symbols()):
            raise tasks.Invalid("unknown symbol")
        return float(x402.price_usd("brief")), payload
    raise tasks.Invalid("unknown product")


def _celo_guard() -> None:
    if not native_pay.enabled():
        raise HTTPException(503, "paying in CELO is not enabled on this server")


@router.post("/celo/quote")
async def celo_quote(request: Request):
    """FREE: what this order costs in CELO right now, at the on-chain rate."""
    _celo_guard()
    body = await _json(request)
    product = str(body.get("product") or "")
    try:
        usd, _ = await asyncio.to_thread(_celo_price_usd, product, body)
        wei, rate, fee = await asyncio.to_thread(native_pay.celo_for_usd, usd)
    except (tasks.Invalid, native_pay.Invalid) as e:
        raise HTTPException(400, str(e))
    return {"product": product, "amount_usd": usd, "celo_wei": str(wei),
            "celo": round(wei / 10 ** 18, 6), "rate_usd": rate, "fee_tier": fee,
            "pay_to": native_pay.pay_to(), "token_contract": native_pay.CELO_TOKEN,
            "quote_ttl_s": native_pay.QUOTE_TTL_S}


@router.post("/celo/orders")
async def celo_order_create(request: Request):
    """FREE: lock a CELO amount for this order. Nothing is charged until the
    buyer's transfer lands."""
    _celo_guard()
    body = await _json(request)
    product = str(body.get("product") or "")
    payer = str(body.get("payer") or "")
    try:
        usd, _ = await asyncio.to_thread(_celo_price_usd, product, body)
        row, token = await asyncio.to_thread(native_pay.create, product, payer, usd, body)
    except (tasks.Invalid, native_pay.Invalid) as e:
        raise HTTPException(400, str(e))
    return {**native_pay.view(row), "token": token}


def _celo_order_or_403(order_id: str, token: str) -> Dict[str, Any]:
    row = native_pay.by_id(order_id)
    if not row:
        raise HTTPException(404, "order not found")
    if not native_pay.token_ok(row, token):
        raise HTTPException(403, "this order needs the token it was created with")
    return row


async def _celo_deliver(row: Dict[str, Any]) -> Dict[str, Any]:
    """Run the thing that was paid for. Idempotent: a delivered order returns
    its stored result, and a failure keeps the order 'paid' so a retry can
    still deliver what the buyer already paid for."""
    if row["status"] == "delivered":
        return native_pay.result_of(row)
    if row["status"] != "paid":
        return {}
    product, payload = row["product"], native_pay.payload_of(row)
    try:
        if product in ("task_monitor", "task_research"):
            q = await asyncio.to_thread(tasks.quote_from_body, payload)
            trow, ttoken = await asyncio.to_thread(
                tasks.create, q, row["payer"], "celo:" + row["order_id"], 0,
                float(row["amount_usd"]))
            trow = await asyncio.to_thread(tasks.activate, trow["task_id"], row.get("tx") or "")
            result = {"product": product, "task": tasks.view(trow, with_runs=False),
                      "token": ttoken}
        elif product == "chat":
            c = _analyst_config()
            data = await asyncio.to_thread(chat_service._run_chat, c, [],
                                           str(payload.get("message") or ""),
                                           _full_symbol(str(payload.get("symbol") or "")), False)
            if not data.pop("_billable", False):
                raise RuntimeError("model busy / no answer")
            structured = {"on_topic": data.get("on_topic", True), "headline": data.get("headline", ""),
                          "points": data.get("points") or [], "analysis": data.get("analysis"),
                          "note": data.get("note")}
            result = {"product": "chat", "symbol": _bare(str(payload.get("symbol") or "")),
                      "reply": chat_service._flatten(structured), "structured": structured,
                      "idea": {k: data.get(k) for k in ("has_trade_idea", "side", "confidence",
                                                        "rationale", "mark") if data.get(k) is not None},
                      "ts": time.time()}
        else:
            content = await asyncio.to_thread(_brief_sync, str(payload.get("symbol") or ""))
            if content is None:
                raise RuntimeError("model busy / no brief")
            result = content
    except Exception as e:                      # paid but undelivered — keep it claimable
        await asyncio.to_thread(native_pay.store_error, row["order_id"], repr(e)[:200])
        oplog.error("celo_pay.deliver", repr(e)[:200], params={"order": row["order_id"]})
        return {}
    result["payment"] = {"tx": row.get("tx") or "", "asset": "CELO",
                         "explorer": (x402.CHAIN["explorer_tx"] + row["tx"]) if row.get("tx") else "",
                         "amount_usd": float(row["amount_usd"]),
                         "celo": round(int(row.get("paid_wei") or 0) / 10 ** 18, 6),
                         "network": x402.NETWORK, "settler": "direct"}
    await asyncio.to_thread(native_pay.store_result, row["order_id"], result)
    return result


@router.get("/celo/health")
async def celo_health():
    """PUBLIC: is the Celo link healthy right now — which gateway answers, how
    fast, and what the price feed says. Nothing here is not already on-chain."""
    return await asyncio.to_thread(native_pay.health)


@router.get("/celo/orders/{order_id}/trace")
async def celo_order_trace(order_id: str, request: Request, token: str = ""):
    """Token-gated: every step this order went through, in order — including
    the exact reason a payment was refused. Written for a human to read while
    testing."""
    row = await asyncio.to_thread(_celo_order_or_403, order_id,
                                  (token or request.headers.get("x-order-token") or ""))
    return await asyncio.to_thread(native_pay.order_trail, row)


@router.post("/celo/orders/{order_id}/tx")
async def celo_order_tx(order_id: str, request: Request, token: str = ""):
    """The buyer hands us their transfer hash; we verify it and deliver."""
    _celo_guard()
    body = await _json(request)
    row = await asyncio.to_thread(_celo_order_or_403, order_id,
                                  (token or request.headers.get("x-order-token") or ""))
    try:
        row = await asyncio.to_thread(native_pay.attach_tx, row, str(body.get("tx") or ""))
    except native_pay.Invalid as e:
        raise HTTPException(400, str(e))
    result = await _celo_deliver(row)
    fresh = native_pay.by_id(order_id) or row
    return {**native_pay.view(fresh), "result": result}


@router.get("/celo/orders/{order_id}")
async def celo_order_get(order_id: str, request: Request, token: str = ""):
    """Poll: if the buyer never sent us the hash, look for their transfer."""
    _celo_guard()
    row = await asyncio.to_thread(_celo_order_or_403, order_id,
                                  (token or request.headers.get("x-order-token") or ""))
    if row["status"] == "awaiting":
        found = await asyncio.to_thread(native_pay.find_payment, row)
        if found:
            row = found
    result = await _celo_deliver(row) if row["status"] in ("paid", "delivered") else {}
    fresh = native_pay.by_id(order_id) or row
    return {**native_pay.view(fresh), "result": result}
