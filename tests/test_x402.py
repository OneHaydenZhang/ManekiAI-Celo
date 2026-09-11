"""x402 v2 seller on Celo (auto_service/celo/x402.py + routes.py) — wire format,
requirement matching, the permanent payment ledger, owner revenue share, and
the full 402 → verify → content → settle → 200 flow over a FastAPI test client
with the facilitator and the LLM mocked.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_points_v1 import _TMP  # noqa: F401 — temp-DB bootstrap
from auto_service import db, service_config
from auto_service.models import agent_model, points_model, trade_model
from auto_service.services import chat_service
from auto_service.celo import x402, routes as celo_routes

TREASURY = "0x26523f5cea5da5d9411749afefe741ba340f6566"
PAYER = "0x857b06519e91e3a54538791bdbb0e22373e36b66"
OWNER = "0x1111111111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for t in ("agents", "decisions", "points_ledger", "points_tx", "bonus_grants",
              "notifications", "admin_settings"):
        db.execute(f"DELETE FROM {t}")
    x402.ensure_schema()
    db.execute("DELETE FROM x402_payments")
    monkeypatch.setattr(service_config, "_admin_setting", lambda k: "")
    monkeypatch.setenv("CELO_TREASURY_ADDRESS", TREASURY)
    monkeypatch.setenv("X402_API_KEY", "test-key")
    monkeypatch.setenv("MANEKI_PUBLIC_BASE", "https://manekiai.io")
    for env in ("X402_PAY_TO", "X402_PRICES_JSON", "X402_ENABLED", "X402_OWNER_SHARE"):
        monkeypatch.delenv(env, raising=False)
    celo_routes.invalidate_catalog()
    celo_routes._RL.clear()
    celo_routes._brief_cache.clear()
    celo_routes._payer_fails.clear()
    celo_routes._activity_cache["val"] = None
    monkeypatch.delenv("X402_SETTLER", raising=False)
    monkeypatch.delenv("X402_OPERATOR_WALLETS", raising=False)
    monkeypatch.delenv("CELO_REGISTRAR_KEY", raising=False)
    monkeypatch.delenv("ZEROG_REGISTRAR_KEY", raising=False)
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(celo_routes.router)
    return TestClient(app)


def _payload(req, payer=PAYER, nonce="0x" + "ab" * 32, value=None, to=None, version=2):
    return {"x402Version": version,
            "resource": {"url": "https://manekiai.io/api/x402/chat"},
            "accepted": dict(req),
            "payload": {"signature": "0x" + "11" * 65,
                        "authorization": {"from": payer, "to": to or req["payTo"],
                                          "value": value or req["amount"],
                                          "validAfter": "0", "validBefore": str(int(time.time()) + 600),
                                          "nonce": nonce}}}


def _mock_facilitator(monkeypatch, verify_ok=True, settle_ok=True, tx="0xsettled"):
    calls = {"verify": 0, "settle": 0}

    def verify(payload, req):
        calls["verify"] += 1
        return ({"isValid": True, "payer": PAYER} if verify_ok
                else {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER})

    def settle(payload, req):
        calls["settle"] += 1
        return ({"success": True, "payer": PAYER, "transaction": tx, "network": x402.NETWORK} if settle_ok
                else {"success": False, "errorReason": "nonce_used", "transaction": "", "network": x402.NETWORK})
    monkeypatch.setattr(x402, "facilitator_verify", verify)
    monkeypatch.setattr(x402, "facilitator_settle", settle)
    return calls


def _mock_llm(monkeypatch, billable=True):
    def run(c, history, message, symbol, advice=False):
        if not billable:
            return {"on_topic": True, "headline": "busy", "points": []}
        return {"on_topic": True, "headline": f"Answer to: {message[:20]}",
                "points": [{"label": "Trend", "text": "up"}], "_billable": True,
                "has_trade_idea": True, "side": "long", "confidence": 0.7, "mark": 123.4}
    monkeypatch.setattr(chat_service, "_run_chat", run)


# --------------------------------------------------------------- protocol --

def test_config_and_prices(monkeypatch):
    assert x402.enabled() and x402.pay_to() == TREASURY
    assert x402.atomic(0.02) == "20000" and x402.atomic(0.05) == "50000"
    cfg = x402.public_config()
    assert cfg["network"] == "eip155:42220" and cfg["asset"]["address"] == x402.USDC
    assert cfg["asset"]["eip712"] == {"name": "USDC", "version": "2"}
    assert cfg["prices"]["chat"]["atomic"] == "20000" and cfg["prices"]["insight"]["usd"] == 0.05
    monkeypatch.setenv("X402_PRICES_JSON", json.dumps({"chat": 0.5, "brief": 99, "bogus": 1}))
    p = x402.prices()
    assert p["chat"] == 0.5 and p["brief"] == 0.01           # out-of-bounds ignored
    monkeypatch.setenv("X402_ENABLED", "0")
    assert not x402.enabled()


def test_disabled_without_key_or_payto(monkeypatch):
    monkeypatch.delenv("X402_API_KEY")
    assert not x402.enabled()
    monkeypatch.setenv("X402_API_KEY", "k")
    monkeypatch.delenv("CELO_TREASURY_ADDRESS")
    assert x402.pay_to() == "" and not x402.enabled()
    monkeypatch.setenv("X402_PAY_TO", TREASURY)
    assert x402.enabled()


def test_payment_required_shape_and_b64():
    pr = x402.payment_required("chat", "https://manekiai.io/api/x402/chat", "need payment")
    assert pr["x402Version"] == 2 and pr["error"] == "need payment"
    assert pr["resource"]["url"].endswith("/api/x402/chat") and pr["resource"]["mimeType"] == "application/json"
    acc = pr["accepts"][0]
    assert acc == {"scheme": "exact", "network": "eip155:42220", "amount": "20000",
                   "asset": x402.USDC, "payTo": TREASURY, "maxTimeoutSeconds": 120,
                   "extra": {"name": "USDC", "version": "2"}}
    assert x402.b64d(x402.b64e(pr)) == pr
    assert x402.b64d(x402.b64e(pr).rstrip("=")) == pr           # tolerant of stripped padding


def test_matches_requirements():
    req = x402.requirements("chat", "u")
    assert x402.matches(dict(req), req)
    assert x402.matches({**req, "amount": "30000"}, req)          # overpaying is fine
    assert not x402.matches({**req, "amount": "19999"}, req)
    assert not x402.matches({**req, "payTo": PAYER}, req)
    assert not x402.matches({**req, "network": "eip155:8453"}, req)
    assert not x402.matches({}, req)


# ----------------------------------------------------------------- ledger --

def test_ledger_begin_finish_duplicate_summary():
    req = x402.requirements("insight", "u")
    pid = x402.begin(PAYER, "0xn1", "insight", req, "u", agent_id="ag_1")
    with pytest.raises(x402.Duplicate):
        x402.begin(PAYER, "0xN1", "insight", req, "u")             # case-insensitive replay
    x402.finish(pid, "settled", tx="0xtx1", owner_credits=35)
    pid2 = x402.begin(PAYER, "0xn2", "chat", req, "u")
    x402.finish(pid2, "invalid", error="insufficient_funds")
    rows = x402.recent()
    assert [r["status"] for r in rows] == ["invalid", "settled"]
    assert rows[1]["amount_usd"] == pytest.approx(0.05) and rows[1]["tx"] == "0xtx1"
    s = x402.summary()
    assert s["settled"] == 1 and s["payers"] == 1 and s["usd"] == pytest.approx(0.05)
    assert s["by_product"][0]["product"] == "insight"


def test_owner_share_is_idempotent_and_notifies():
    agent = {"agent_id": "ag_1", "address": OWNER, "label": "My NVDA", "symbol": "xyz:NVDA"}
    got = x402.credit_owner(agent, 0.05, "0xTX", "insight", PAYER)
    assert got == 35.0                                              # floor(0.05 × 0.70 × 1000)
    assert points_model.balance(OWNER) == 35.0
    assert x402.credit_owner(agent, 0.05, "0xTX", "insight", PAYER) == 0.0   # same tx → no double credit
    assert points_model.balance(OWNER) == 35.0
    n = db.query_one("SELECT * FROM notifications WHERE address=?", (OWNER,))
    assert n and "sold an insight" in n["title"] and n["dedup_key"] == "x402:0xtx"
    tx = db.query_one("SELECT * FROM points_tx WHERE address=? AND kind='grant'", (OWNER,))
    assert tx["ref"] == "x402:0xtx" and "x402 sale" in tx["note"]


# ---------------------------------------------------------------- routes ---

def test_routes_503_when_disabled(client, monkeypatch):
    monkeypatch.delenv("X402_API_KEY")
    r = client.get("/api/x402/brief?symbol=NVDA")
    assert r.status_code == 503
    cfg = client.get("/api/x402/config").json()
    assert cfg["enabled"] is False and cfg["agentid"]["chain_id"] == 42220


def test_challenge_402_carries_payment_required_header(client):
    r = client.post("/api/x402/chat", json={"message": "hi", "symbol": "NVDA"})
    assert r.status_code == 402
    pr = x402.b64d(r.headers["PAYMENT-REQUIRED"])
    assert pr["accepts"][0]["amount"] == "20000" and pr["resource"]["url"] == "https://manekiai.io/api/x402/chat"
    assert "PAYMENT-SIGNATURE" in pr["error"]
    assert r.json()["x402Version"] == 2                             # body mirrors the header
    assert "PAYMENT-REQUIRED" in r.headers["Access-Control-Expose-Headers"]
    # malformed header → 402 again, nothing recorded
    r2 = client.post("/api/x402/chat", json={"message": "hi"}, headers={"PAYMENT-SIGNATURE": "!!"})
    assert r2.status_code == 402 and "malformed" in r2.json()["error"]
    assert x402.recent() == []


def test_chat_happy_path_settles_and_records(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch)
    _mock_llm(monkeypatch)
    req = x402.requirements("chat", "https://manekiai.io/api/x402/chat")
    hdr = {"PAYMENT-SIGNATURE": x402.b64e(_payload(req))}
    r = client.post("/api/x402/chat", json={"message": "Is NVDA a buy?", "symbol": "NVDA"}, headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["product"] == "chat" and body["reply"].startswith("Answer to")
    assert body["structured"]["points"][0]["label"] == "Trend"
    assert body["idea"]["side"] == "long"
    assert body["payment"] == {"tx": "0xsettled", "explorer": "https://celoscan.io/tx/0xsettled",
                               "payer": PAYER, "amount_usd": 0.02, "asset": "USDC", "network": "eip155:42220",
                               "settler": "facilitator"}
    assert x402.b64d(r.headers["PAYMENT-RESPONSE"])["transaction"] == "0xsettled"
    assert calls == {"verify": 1, "settle": 1}
    row = x402.recent()[0]
    assert row["status"] == "settled" and row["product"] == "chat" and row["payer"] == PAYER
    assert row["amount_usd"] == pytest.approx(0.02)
    # replaying the exact same payload is refused before any work
    r2 = client.post("/api/x402/chat", json={"message": "again"}, headers=hdr)
    assert r2.status_code == 402 and "duplicate" in r2.json()["error"]
    assert calls["verify"] == 1


def test_invalid_payment_and_mismatch_are_402(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch, verify_ok=False)
    _mock_llm(monkeypatch)
    req = x402.requirements("chat", "u")
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 402 and "insufficient_funds" in r.json()["error"]
    assert x402.recent()[0]["status"] == "invalid" and calls["settle"] == 0
    # cheaper `accepted` than our offer → rejected before the facilitator
    cheap = _payload(req, nonce="0x" + "cd" * 32, value="1")
    cheap["accepted"]["amount"] = "1"
    r2 = client.post("/api/x402/chat", json={"message": "hi"},
                     headers={"PAYMENT-SIGNATURE": x402.b64e(cheap)})
    assert r2.status_code == 402 and "mismatch" in r2.json()["error"]
    r3 = client.post("/api/x402/chat", json={"message": "hi"},
                     headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + "ef" * 32, version=1))})
    assert r3.status_code == 402 and "x402Version" in r3.json()["error"]
    assert calls["verify"] == 1


def test_content_failure_is_never_charged(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch)
    _mock_llm(monkeypatch, billable=False)
    req = x402.requirements("chat", "u")
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 503 and r.json()["charged"] is False
    assert calls == {"verify": 1, "settle": 0}
    assert x402.recent()[0]["status"] == "content_failed"


def test_settlement_failure_withholds_content(client, monkeypatch):
    _mock_facilitator(monkeypatch, settle_ok=False)
    _mock_llm(monkeypatch)
    req = x402.requirements("chat", "u")
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 402 and "settlement failed" in r.json()["error"]
    assert "reply" not in r.json()
    assert x402.recent()[0]["status"] == "settle_failed"


def test_brief_is_cached_per_symbol(client, monkeypatch):
    _mock_facilitator(monkeypatch)
    n = {"llm": 0}

    def run(c, history, message, symbol, advice=False):
        n["llm"] += 1
        return {"on_topic": True, "headline": "NVDA brief", "points": [], "_billable": True,
                "side": "watch", "confidence": 0.4}
    monkeypatch.setattr(chat_service, "_run_chat", run)
    req = x402.requirements("brief", "u")
    for i in range(2):
        r = client.get("/api/x402/brief?symbol=NVDA",
                       headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + f"{i:02d}" * 32))})
        assert r.status_code == 200, r.text
        assert r.json()["brief"].startswith("NVDA brief") and r.json()["cached"] is (i == 1)
    assert n["llm"] == 1                                            # shared brief, paid twice
    assert client.get("/api/x402/brief").status_code == 400


def _sell_agent(agent_id="ag_sale", sell=1, with_decision=True):
    db.execute(
        "INSERT INTO agents(agent_id, address, symbol, model, persona, label, created_at, updated_at,"
        " x402_sell, celo_agent_id, status, total_ticks, lessons)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, OWNER, "xyz:NVDA", "openai/gpt-4o-mini", "navigator", "NVDA momentum",
         time.time(), time.time(), sell, 5150, "running", 12, "Cut losers fast."))
    if with_decision:
        trade_model.add_decision(agent_id, OWNER, tick_no=12, action="open_long", confidence=0.72,
                                 observation={"symbol": "xyz:NVDA", "mid": 120.5, "mark": 120.6,
                                              "funding": 0.0001, "position_size": "0.0",
                                              "spot_usdc_free": 999.0, "perp_dex_value": 500.0},
                                 reasoning="Breakout above 120 with rising OI.",
                                 reasoning_zh="突破 120，持仓量上升。", outcome={}, executed=True)
    return agent_model.get(agent_id)


def test_catalog_lists_only_opted_in_agents(client):
    _sell_agent("ag_a", sell=1)
    _sell_agent("ag_b", sell=0)
    cat = client.get("/api/x402/catalog").json()
    codes = [a["code"] for a in cat["agents"]]
    assert codes == [agent_model.agent_code("ag_a")]
    a = cat["agents"][0]
    assert a["symbol"] == "NVDA" and a["model_tier"] == "gpt" and a["celo_agent_id"] == 5150
    assert a["price_usd"] == 0.05 and a["running"] is True and a["total_ticks"] == 12
    assert OWNER not in json.dumps(cat)                             # never the owner address
    assert cat["analyst"]["code"] == "maneki-analyst" and cat["prices"]["chat"] == 0.02


def test_insight_happy_path_pays_owner(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch, tx="0xinsightTx")
    agent = _sell_agent()
    code = agent_model.agent_code(agent["agent_id"])
    url = f"/api/x402/agents/{code}/insight"
    r0 = client.get(url)
    assert r0.status_code == 402
    pr = x402.b64d(r0.headers["PAYMENT-REQUIRED"])
    assert pr["accepts"][0]["amount"] == "50000" and pr["resource"]["url"] == "https://manekiai.io" + url
    req = pr["accepts"][0]
    r = client.get(url, headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"]["action"] == "open_long" and body["decision"]["round"] == 12
    assert body["decision"]["reasoning"].startswith("Breakout")
    assert body["decision"]["market"] == {"symbol": "xyz:NVDA", "mid": 120.5, "mark": 120.6,
                                          "funding": 0.0001, "position": "flat"}
    assert "spot_usdc_free" not in json.dumps(body) and OWNER not in json.dumps(body)
    assert body["lessons"] == "Cut losers fast." and body["agent"]["code"] == code
    assert body["payment"]["tx"] == "0xinsightTx"
    assert calls == {"verify": 1, "settle": 1}
    # owner got 70% of $0.05 as Gas, recorded on the ledger row
    assert points_model.balance(OWNER) == 35.0
    row = x402.recent()[0]
    assert row["status"] == "settled" and row["agent_id"] == agent["agent_id"] and row["owner_credits"] == 35.0
    # not-for-sale and unknown agents are 404 before any payment dance
    other = _sell_agent("ag_off", sell=0)
    assert client.get(f"/api/x402/agents/{agent_model.agent_code('ag_off')}/insight").status_code == 404
    assert client.get("/api/x402/agents/A-NOPE99/insight").status_code == 404


def test_insight_without_decision_is_refused_before_any_signature(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch)
    agent = _sell_agent("ag_new", with_decision=False)
    code = agent_model.agent_code("ag_new")
    # 409 on the FREE challenge already — the buyer never signs for nothing
    assert client.get(f"/api/x402/agents/{code}/insight").status_code == 409
    req = x402.requirements("insight", "u")
    r = client.get(f"/api/x402/agents/{code}/insight",
                   headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 409 and calls == {"verify": 0, "settle": 0}
    assert points_model.balance(OWNER) == 0.0 and x402.recent() == []
    cat = client.get("/api/x402/catalog").json()
    entry = [a for a in cat["agents"] if a["code"] == code][0]
    assert entry["purchasable"] is False and entry["last_action"] == "" and entry["last_round"] == 0


def test_owner_share_keys_on_nonce_when_tx_missing():
    agent = {"agent_id": "ag_1", "address": OWNER, "label": "A", "symbol": "xyz:NVDA"}
    assert x402.credit_owner(agent, 0.05, "", "insight", PAYER, nonce="0xn1") == 35.0
    assert x402.credit_owner(agent, 0.05, "", "insight", PAYER, nonce="0xn2") == 35.0   # second sale still pays
    assert x402.credit_owner(agent, 0.05, "", "insight", PAYER, nonce="0xn2") == 0.0    # same sale → no double
    assert x402.credit_owner(agent, 0.05, "", "insight", PAYER) == 0.0                  # no key at all → refuse
    assert points_model.balance(OWNER) == 70.0


# ----------------------------------------------- round-2 hardening (09-11) ----

def test_two_accepts_usdc_first_and_usat_payment_path(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch, tx="0xusat")
    _mock_llm(monkeypatch)
    r0 = client.post("/api/x402/chat", json={"message": "hi"})
    acc = x402.b64d(r0.headers["PAYMENT-REQUIRED"])["accepts"]
    assert [a["asset"] for a in acc] == [x402.USDC, x402.USAT]
    assert acc[1]["extra"] == {"name": "Tether America USD", "version": "1"} and acc[1]["amount"] == "20000"
    # paying with the USA₮ offer works end to end and is recorded as USAT
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(acc[1]))})
    assert r.status_code == 200, r.text
    assert r.json()["payment"]["asset"] == "USAT" and calls["settle"] == 1
    assert x402.recent()[0]["asset"] == x402.USAT and x402.recent()[0]["amount_usd"] == pytest.approx(0.02)


def test_structural_rejections_happen_before_verify_and_ledger(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch)
    req = x402.requirements("chat", "u")
    short = _payload(req, nonce="0x" + "01" * 32)
    short["payload"]["authorization"]["validBefore"] = str(int(time.time()) + 7)
    r = client.post("/api/x402/chat", json={"message": "hi"}, headers={"PAYMENT-SIGNATURE": x402.b64e(short)})
    assert r.status_code == 402 and "authorization_expired" in r.json()["error"]
    bad = _payload(req, payer="0xnotanaddress", nonce="0x" + "02" * 32)
    r = client.post("/api/x402/chat", json={"message": "hi"}, headers={"PAYMENT-SIGNATURE": x402.b64e(bad)})
    assert r.status_code == 402 and "well-formed" in r.json()["error"]
    cheap = _payload(req, nonce="0x" + "03" * 32, value="1")
    r = client.post("/api/x402/chat", json={"message": "hi"}, headers={"PAYMENT-SIGNATURE": x402.b64e(cheap)})
    assert r.status_code == 402 and "below the required amount" in r.json()["error"]
    assert calls["verify"] == 0 and x402.recent() == []
    # empty message is a plain 400 before any payment work
    assert client.post("/api/x402/chat", json={"message": ""}).status_code == 400


def test_payer_cooldown_after_repeated_failures(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch, settle_ok=False)
    _mock_llm(monkeypatch)
    req = x402.requirements("chat", "u")
    for i in range(2):
        r = client.post("/api/x402/chat", json={"message": "hi"},
                        headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + f"{i + 10:02d}" * 32))})
        assert r.status_code == 402
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + "77" * 32))})
    assert r.status_code == 429 and calls["verify"] == 2          # third attempt never reaches the verifier


def test_settlement_reconciles_lost_reply_from_chain(client, monkeypatch):
    """Facilitator broadcast the tx but the HTTP reply was lost: the on-chain
    nonce state says 'used' → deliver + settled, never 'charged without content'."""
    from auto_service.celo import wallet
    calls = {"verify": 0}

    def verify(payload, req):
        calls["verify"] += 1
        return {"isValid": True, "payer": PAYER}
    monkeypatch.setattr(x402, "facilitator_verify", verify)
    monkeypatch.setattr(x402, "facilitator_settle",
                        lambda p, r: {"success": False, "errorReason": x402.ERR_FACILITATOR_DOWN,
                                      "transaction": "", "network": x402.NETWORK, "transport": True})
    monkeypatch.setattr(wallet, "authorization_state", lambda asset, payer, nonce, rpc_fn=None: True)
    monkeypatch.setattr(wallet, "find_transfer_tx", lambda asset, payer, to, rpc_fn=None, blocks=300: "0xfoundOnChain")
    _mock_llm(monkeypatch)
    req = x402.requirements("chat", "u")
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 200, r.text
    assert r.json()["payment"]["tx"] == "0xfoundOnChain"
    assert x402.recent()[0]["status"] == "settled" and x402.recent()[0]["tx"] == "0xfoundonchain"


def test_facilitator_errors_are_sanitized(monkeypatch):
    import httpx as _h

    def boom(*a, **k):
        raise _h.ConnectError("Connection refused to https://api.x402.celo.org/verify (secret-host)")
    monkeypatch.setattr(x402.httpx, "post", boom)
    v = x402.facilitator_verify({}, {})
    s = x402.facilitator_settle({}, {})
    assert v == {"isValid": False, "invalidReason": "facilitator unreachable", "transport": True}
    assert s["errorReason"] == "facilitator unreachable" and s["transport"] is True
    assert "secret-host" not in json.dumps(v) + json.dumps(s)


def test_deposit_scanner_skips_x402_settlements():
    from auto_service.services import deposits
    req = x402.requirements("insight", "u")
    pid = x402.begin(PAYER, "0xn9", "insight", req, "u")
    x402.finish(pid, "settled", tx="0xSALE")
    assert deposits._is_x402_settlement("0xsale") and deposits._is_x402_settlement("0xSALE")
    assert not deposits._is_x402_settlement("0xother") and not deposits._is_x402_settlement("")


def test_owner_share_rolls_back_marker_when_credit_fails(monkeypatch):
    agent = {"agent_id": "ag_1", "address": OWNER, "label": "A", "symbol": "xyz:NVDA"}

    def broken(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(points_model, "credit", broken)
    with pytest.raises(RuntimeError):
        x402.credit_owner(agent, 0.05, "0xT1", "insight", PAYER)
    assert db.query_one("SELECT 1 FROM bonus_grants WHERE address=? AND tag='x402:0xt1'", (OWNER,)) is None
    monkeypatch.undo()
    assert x402.credit_owner(agent, 0.05, "0xT1", "insight", PAYER) == 35.0      # retry succeeds


def test_activity_is_public_safe_and_excludes_operator(client, monkeypatch):
    monkeypatch.setenv("X402_OPERATOR_WALLETS", "0x" + "ee" * 20)
    agent = _sell_agent("ag_act")
    req = x402.requirements("insight", "u")
    p1 = x402.begin(PAYER, "0xa1", "insight", req, "u", agent_id="ag_act", meta={"ip": "9.9.9.9"})
    x402.finish(p1, "settled", tx="0xreal1")
    p2 = x402.begin("0x" + "ee" * 20, "0xa2", "chat", req, "u")
    x402.finish(p2, "settled", tx="0xoperator")
    from auto_service import admin_store
    admin_store.set("celo_platform_agent", {"agentId": 4242, "txhash": "0xplat"})
    r = client.get("/api/x402/activity")
    assert r.status_code == 200, r.text
    d = r.json()
    blob = json.dumps(d)
    assert "9.9.9.9" not in blob and PAYER not in blob and OWNER not in blob and "meta_json" not in blob
    assert d["summary"]["settled"] == 1 and d["summary"]["payers"] == 1
    assert [x["tx"] for x in d["recent"]] == ["0xreal1"] and d["recent"][0]["payer_short"] == "0x857b…6b66"
    assert d["recent"][0]["agent_code"] == agent_model.agent_code("ag_act")
    assert d["registrations"]["platform"]["agentId"] == 4242 and d["registrations"]["count"] == 1
    assert d["registrations"]["agents"][0]["celo_agent_id"] == 5150
    assert d["wallets"]["pay_to"] == TREASURY and d["links"]["repo"].startswith("https://github.com/")
    assert d["deposits"]["assets"] == ["USDC", "USD₮", "USDm", "USA₮"]
    cfg = client.get("/api/x402/config").json()
    assert cfg["settler"] == "facilitator" and set(cfg["assets"]) == {"USDC", "USAT"} and "analyst_ready" in cfg


def test_self_settler_mode_is_explicit_only(monkeypatch):
    assert x402.settler_mode() == "facilitator"
    monkeypatch.delenv("X402_API_KEY")
    assert x402.settler_mode() == "off" and not x402.enabled()
    monkeypatch.setenv("X402_SETTLER", "self")
    assert x402.settler_mode() == "off"                              # no registrar key → still off
    monkeypatch.setenv("CELO_REGISTRAR_KEY", "0x" + "44" * 32)
    assert x402.settler_mode() == "self" and x402.enabled()
    monkeypatch.delenv("X402_SETTLER")
    monkeypatch.setenv("X402_API_KEY", "k")
    assert x402.settler_mode() == "facilitator"                      # never auto-selects self
