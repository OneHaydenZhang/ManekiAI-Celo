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
                               "payer": PAYER, "amount_usd": 0.02, "asset": "USDC", "network": "eip155:42220"}
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


def test_insight_without_decision_is_not_charged(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch)
    agent = _sell_agent("ag_new", with_decision=False)
    code = agent_model.agent_code("ag_new")
    req = x402.requirements("insight", "u")
    r = client.get(f"/api/x402/agents/{code}/insight",
                   headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 503 and calls["settle"] == 0
    assert points_model.balance(OWNER) == 0.0
