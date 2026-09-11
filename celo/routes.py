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
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db
from ..models import agent_model, config_model, trade_model
from ..services import agent_service, chat_service, oplog, pricing
from . import x402, agentid

router = APIRouter(prefix="/api/x402")

_EXPOSE = "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-Trace-Id"
_SYMBOL_RE = re.compile(r"[A-Za-z0-9:._\-]{1,24}")
_MSG_MAX = 600
_BRIEF_TTL_S = 600
_CATALOG_TTL_S = 30

# Per-IP request budget on the paid surface (the 402 challenge itself is free
# to request; the payment is the real rate limiter for content).
_RL_WINDOW_S, _RL_MAX = 60, 120
_RL: Dict[str, List[float]] = {}
_brief_cache: Dict[str, Dict[str, Any]] = {}
_catalog_cache: Dict[str, Any] = {"at": 0.0, "val": None}


def _ip(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


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


def _402(product: str, resource: str, error: str = "") -> JSONResponse:
    pr = x402.payment_required(product, resource, error)
    resp = JSONResponse(status_code=402, content=pr)
    resp.headers["PAYMENT-REQUIRED"] = x402.b64e(pr)
    resp.headers["Access-Control-Expose-Headers"] = _EXPOSE
    return resp


async def _gate(request: Request, product: str, agent_id: str = "") -> Tuple[Optional[JSONResponse], Dict[str, Any]]:
    """Returns (response, {}) when the caller must (re)pay, or (None, info)
    when a valid payment authorization is in hand (verified, not settled)."""
    if not x402.enabled():
        raise HTTPException(503, "x402 payments are not enabled on this server")
    ip = _ip(request)
    if not _rate_ok(ip):
        raise HTTPException(429, "too many requests — slow down")
    resource = _resource_url(request)
    req = x402.requirements(product, resource)
    sig = request.headers.get("payment-signature") or ""
    if not sig:
        return _402(product, resource, "PAYMENT-SIGNATURE header is required"), {}
    try:
        payload = x402.b64d(sig)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
    except Exception:
        return _402(product, resource, "malformed PAYMENT-SIGNATURE header"), {}
    if int(payload.get("x402Version") or 0) != 2:
        return _402(product, resource, "x402Version must be 2"), {}
    if not x402.matches(payload.get("accepted") or {}, req):
        return _402(product, resource, "payment requirements mismatch — re-read PAYMENT-REQUIRED"), {}
    f = x402.payload_fields(payload)
    if f["to"] != req["payTo"].lower():
        return _402(product, resource, "authorization.to must equal payTo"), {}
    try:
        pid = x402.begin(f["payer"], f["nonce"], product, req, resource, agent_id=agent_id,
                         meta={"ip": ip})
    except x402.Duplicate:
        return _402(product, resource, "duplicate payment nonce — sign a fresh authorization"), {}
    except ValueError:
        return _402(product, resource, "authorization.from and nonce are required"), {}
    v = await asyncio.to_thread(x402.facilitator_verify, payload, req)
    if not v.get("isValid"):
        reason = str(v.get("invalidReason") or "rejected")[:160]
        x402.finish(pid, "invalid", error=reason)
        oplog.error("x402.verify", reason, params={"product": product, "payer": f["payer"][:12]}, status=402)
        return _402(product, resource, f"payment invalid: {reason}"), {}
    return None, {"pid": pid, "payload": payload, "req": req, "product": product,
                  "resource": resource, "payer": (v.get("payer") or f["payer"]).lower(),
                  "amount_usd": int(req["amount"]) / (10 ** x402.USDC_DECIMALS)}


def _content_failed(info: Dict[str, Any], reason: str) -> JSONResponse:
    x402.finish(info["pid"], "content_failed", error=reason)
    oplog.error("x402.content", reason, params={"product": info["product"]}, status=503)
    return JSONResponse(status_code=503, content={
        "error": "The analyst is busy right now — nothing was charged, please retry shortly.",
        "charged": False})


async def _deliver(info: Dict[str, Any], content: Dict[str, Any],
                   agent: Optional[Dict[str, Any]] = None) -> JSONResponse:
    s = await asyncio.to_thread(x402.facilitator_settle, info["payload"], info["req"])
    if not s.get("success"):
        reason = str(s.get("errorReason") or "settlement rejected")[:160]
        x402.finish(info["pid"], "settle_failed", error=reason)
        oplog.error("x402.settle", reason, params={"product": info["product"],
                                                    "payer": info["payer"][:12]}, status=402)
        return _402(info["product"], info["resource"], f"settlement failed: {reason}")
    tx = str(s.get("transaction") or "")
    credits = 0.0
    if agent is not None:
        try:
            credits = await asyncio.to_thread(x402.credit_owner, agent, info["amount_usd"], tx,
                                              info["product"], info["payer"],
                                              x402.payload_fields(info["payload"])["nonce"])
        except Exception as e:
            oplog.error("x402.owner_share", repr(e)[:300], params={"tx": tx[:18]})
    x402.finish(info["pid"], "settled", tx=tx, owner_credits=credits)
    oplog.op("x402.sale", params={"product": info["product"], "payer": info["payer"],
                                  "usd": info["amount_usd"], "tx": tx[:18],
                                  "agent_id": (agent or {}).get("agent_id", ""),
                                  "owner_credits": credits})
    body = dict(content)
    body["payment"] = {"tx": tx, "explorer": (x402.CHAIN["explorer_tx"] + tx) if tx else "",
                       "payer": info["payer"], "amount_usd": info["amount_usd"],
                       "asset": "USDC", "network": x402.NETWORK}
    resp = JSONResponse(body)
    resp.headers["PAYMENT-RESPONSE"] = x402.b64e({
        "success": True, "payer": s.get("payer") or info["payer"], "transaction": tx,
        "network": s.get("network") or x402.NETWORK})
    resp.headers["Access-Control-Expose-Headers"] = _EXPOSE
    return resp


# ------------------------------------------------------------ free reads --

@router.get("/config")
async def x402_config() -> Dict[str, Any]:
    cfg = x402.public_config()
    cfg["agentid"] = await asyncio.to_thread(agentid.public_status)
    cfg["analyst_card"] = agentid.platform_uri()
    return cfg


def _agent_public(agent: Dict[str, Any]) -> Dict[str, Any]:
    """Catalog entry — public facts + performance, never the owner address."""
    st = agent_service.estimated_stats(agent)
    last_ts = trade_model.last_decision_ts(agent["agent_id"]) or 0.0
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


def _tracked_symbols() -> List[str]:
    rows = db.query_all("SELECT DISTINCT symbol FROM agents WHERE deleted_at=0 "
                        "ORDER BY symbol LIMIT 40")
    syms = sorted({_bare(r["symbol"]) for r in rows if r.get("symbol")})
    return syms or ["NVDA", "TSLA", "AAPL", "MSFT"]


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
    resp, info = await _gate(request, "chat")
    if resp is not None:
        return resp
    if not message:
        return _content_failed(info, "empty message")   # nothing charged
    c = _analyst_config()
    data = await asyncio.to_thread(chat_service._run_chat, c, [], message, _full_symbol(symbol), False)
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
    resp, info = await _gate(request, "brief")
    if resp is not None:
        return resp
    content = await asyncio.to_thread(_brief_sync, symbol)
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
    resp, info = await _gate(request, "insight", agent_id=agent["agent_id"])
    if resp is not None:
        return resp
    content = await asyncio.to_thread(_insight_sync, agent)
    if not content.get("decision"):
        return _content_failed(info, "agent has no decision yet")
    return await _deliver(info, content, agent=agent)
