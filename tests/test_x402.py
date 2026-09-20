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
    db.execute("DELETE FROM x402_receipts")
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
    celo_routes._payer_attempts.clear()
    celo_routes._content_cache.clear()
    celo_routes._verifying.clear()
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


_REAL_KEY = "0x" + "22" * 32  # fixed test key — only used where a REAL EIP-712
                              # signature must recover (x402.signature_matches_payer)


def _real_signed_payload(req, nonce="0x" + "ab" * 32, key_hex=_REAL_KEY):
    """A payload whose signature genuinely recovers to its `from` address, for
    exercising the already-settled redelivery path (x402.signature_matches_payer
    does a real ecrecover — a placeholder signature like _payload()'s default
    cannot pass it)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    acct = Account.from_key(key_hex)
    extra = req.get("extra") or {}
    value = int(str(req["amount"]))
    valid_before = int(time.time()) + 600
    domain = {"name": str(extra.get("name") or ""), "version": str(extra.get("version") or ""),
              "chainId": x402.CHAIN_ID, "verifyingContract": req["asset"]}
    message = {"from": acct.address, "to": req["payTo"], "value": value,
               "validAfter": 0, "validBefore": valid_before, "nonce": nonce}
    signable = encode_typed_data(domain_data=domain, message_types=x402._EIP3009_TYPES, message_data=message)
    sig = Account.sign_message(signable, private_key=acct.key).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    payload = _payload(req, payer=acct.address, nonce=nonce, value=str(value), to=req["payTo"])
    payload["payload"]["signature"] = sig
    payload["payload"]["authorization"]["validBefore"] = str(valid_before)
    return payload, acct.address


def _mock_facilitator(monkeypatch, verify_ok=True, settle_ok=True, tx="0xsettled"):
    calls = {"verify": 0, "settle": 0}

    def verify(payload, req):
        calls["verify"] += 1
        return ({"isValid": True, "payer": PAYER} if verify_ok
                else {"isValid": False, "invalidReason": "insufficient_funds", "payer": PAYER})

    def settle(payload, req):
        calls["settle"] += 1
        return ({"success": True, "payer": PAYER, "transaction": tx, "network": x402.NETWORK} if settle_ok
                else {"success": False, "errorReason": "nonce_already_used", "transaction": "", "network": x402.NETWORK})
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
    assert x402.recent() == [] and calls["settle"] == 0          # unverified → no permanent row
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
    monkeypatch.setattr(wallet, "find_settlement_tx",
                        lambda asset, payer, to, nonce, min_value, rpc_fn=None, blocks=900: "0xfoundOnChain")
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


# ------------------------------------------ settlement lifecycle (review) ----

def test_lost_reply_parks_row_and_same_signature_retry_is_free(client, monkeypatch):
    """Facilitator reply lost and the chain is undecided → 202 settle_pending;
    once the chain shows the settlement, the SAME signed request gets the
    content with no second charge, and the owner share is paid exactly once."""
    from auto_service.celo import wallet
    agent = _sell_agent("ag_pend")
    code = agent_model.agent_code("ag_pend")
    req = x402.requirements("insight", "u")
    payload, payer = _real_signed_payload(req)
    monkeypatch.setattr(x402, "facilitator_verify", lambda p, r: {"isValid": True, "payer": payer})
    settles = {"n": 0}

    def settle(p, r):
        settles["n"] += 1
        return {"success": False, "errorReason": x402.ERR_FACILITATOR_DOWN, "transaction": "",
                "network": x402.NETWORK, "transport": True}
    monkeypatch.setattr(x402, "facilitator_settle", settle)
    chain = {"used": False}
    monkeypatch.setattr(wallet, "authorization_state", lambda a, p, n, rpc_fn=None: chain["used"])
    monkeypatch.setattr(wallet, "find_settlement_tx",
                        lambda a, p, to, n, mv, rpc_fn=None, blocks=900: "0xlate" if chain["used"] else "")
    hdr = {"PAYMENT-SIGNATURE": x402.b64e(payload)}
    r = client.get(f"/api/x402/agents/{code}/insight", headers=hdr)
    assert r.status_code == 202 and r.json()["status"] == "settle_pending"
    row = x402.recent()[0]
    assert row["status"] == "settle_pending" and points_model.balance(OWNER) == 0.0
    assert not any(celo_routes._payer_fails.values())           # not the buyer's fault
    # retry while still undecided → 202 again, no second settle attempt
    assert client.get(f"/api/x402/agents/{code}/insight", headers=hdr).status_code == 202
    assert settles["n"] == 1
    # the chain now shows the settlement → the autopilot finaliser flips it
    chain["used"] = True
    out = x402.finalize_pending()
    assert out == {"checked": 1, "settled": 1, "shares": 0}
    row = x402.recent()[0]
    assert row["status"] == "settled" and row["tx"] == "0xlate" and row["owner_credits"] == 35.0
    assert points_model.balance(OWNER) == 35.0
    # same signature again → content delivered, nothing charged, share unchanged
    r3 = client.get(f"/api/x402/agents/{code}/insight", headers=hdr)
    assert r3.status_code == 200 and r3.json()["decision"]["action"] == "open_long"
    assert r3.json()["payment"]["tx"] == "0xlate" and settles["n"] == 1
    assert points_model.balance(OWNER) == 35.0
    assert x402.get_payment(payer, "0x" + "ab" * 32)["meta"]["delivered"] is True
    # a forged retry with the SAME (payer, nonce) but no real signature must
    # still be refused — this is exactly the auth-bypass the check closes.
    forged = _payload(req, payer=payer, nonce="0x" + "ab" * 32)
    forged["payload"]["signature"] = "0x" + "11" * 65
    r4 = client.get(f"/api/x402/agents/{code}/insight",
                    headers={"PAYMENT-SIGNATURE": x402.b64e(forged)})
    assert r4.status_code == 402
    # a delivered payment cannot be replayed
    assert client.get(f"/api/x402/agents/{code}/insight", headers=hdr).status_code == 402


def test_reconcile_needs_the_exact_settlement_not_just_a_used_nonce(monkeypatch):
    from auto_service.celo import wallet
    req = x402.requirements("chat", "u")
    f = {"payer": PAYER, "nonce": "0xn1"}
    monkeypatch.setattr(wallet, "authorization_state", lambda a, p, n, rpc_fn=None: True)
    monkeypatch.setattr(wallet, "find_settlement_tx", lambda *a, **k: "")
    s = x402.reconcile(f, req, {"success": False, "transaction": ""})
    assert not s["success"] and s["pending"] is True
    # a tx already on the ledger (previous sale) is never re-used
    pid = x402.begin(PAYER, "0xold", "chat", req, "u"); x402.finish(pid, "settled", tx="0xprev")
    monkeypatch.setattr(wallet, "find_settlement_tx", lambda *a, **k: "0xprev")
    s = x402.reconcile(f, req, {"success": False, "transaction": ""})
    assert not s["success"] and s["pending"] is True
    monkeypatch.setattr(wallet, "find_settlement_tx", lambda *a, **k: "0xexact")
    s = x402.reconcile(f, req, {"success": False, "transaction": ""})
    assert s["success"] and s["transaction"] == "0xexact" and s["reconciled"]
    monkeypatch.setattr(wallet, "authorization_state", lambda a, p, n, rpc_fn=None: None)
    assert x402.reconcile(f, req, {"success": False})["pending"] is True


def test_buyer_cli_signs_a_payload_the_server_accepts(client, monkeypatch):
    # auto_service/celo/tools/x402_buyer.py is the programmatic Arena client
    # (agent buys agent research). Its signature must recover to the payer
    # exactly like the browser's, and the wire shape must pass _gate().
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "x402_buyer", Path(celo_routes.__file__).resolve().parent / "tools" / "x402_buyer.py")
    buyer = importlib.util.module_from_spec(spec); spec.loader.exec_module(buyer)
    req = x402.requirements("brief", "u")
    offer = {"resource": {"url": "http://t/api/x402/brief?symbol=NVDA"}, "accepts": [req]}
    payload, payer = buyer.build_payment_payload(_REAL_KEY, offer, req, nonce="0x" + "cd" * 32)
    assert payload["x402Version"] == 2 and payload["payload"]["authorization"]["from"] == payer
    assert x402.signature_matches_payer(payload, req)                # real ecrecover
    calls = _mock_facilitator(monkeypatch)
    _mock_llm(monkeypatch)
    r = client.get("/api/x402/brief?symbol=NVDA", headers={"PAYMENT-SIGNATURE": x402.b64e(payload)})
    assert r.status_code == 200 and r.json()["payment"]["tx"] and calls["settle"] == 1
    row = x402.get_payment(payer.lower(), "0x" + "cd" * 32)      # ledger row keyed on OUR payer + nonce
    assert row and row["status"] == "settled" and row["product"] == "brief"
    # --dry-run style: an unsigned request yields the offer the CLI parses
    r = client.get("/api/x402/brief?symbol=NVDA")
    assert r.status_code == 402 and buyer.b64d(r.headers["PAYMENT-REQUIRED"])["accepts"][0]["payTo"] == req["payTo"]


def test_public_pages_and_guide_link(client, monkeypatch):
    # /arena and /hackathon are served by app.py (not mounted in this bare
    # router fixture), so check the page files themselves; the guide is
    # advertised in config.links so the host app can show its strip.
    from pathlib import Path
    web = Path(celo_routes.__file__).resolve().parent / "web"
    arena = (web / "arena.html").read_text(encoding="utf-8")
    guide = (web / "guide.html").read_text(encoding="utf-8")
    # The page is the two-entry workbench now (paid consultation + create an
    # agent), and both entries plus the guide link must survive any redesign.
    assert 'id="consultPanel"' in arena and 'id="createPanel"' in arena
    assert '/api/x402/tasks' in arena and 'href="/hackathon"' in arena
    # The guide is now just the journeys and the live data (2026-09-20), so it
    # reads the activity feed; it no longer needs the catalog.
    assert 'id="journeys"' in guide and 'id="recent"' in guide
    # (the "no jE" check is gone: E is a journey again since 2026-09-21, now
    # meaning "swap CELO in the page" — what matters is where the page points)
    assert "/api/x402/activity" in guide
    assert "/#agent" not in guide and "/#settings" not in guide
    cfg = client.get("/api/x402/config").json()
    assert cfg["links"]["guide"].endswith("/hackathon")
    monkeypatch.setenv("CELO_GUIDE_ENABLED", "0")
    assert "guide" not in client.get("/api/x402/config").json()["links"]


def test_verified_purchases_never_count_toward_the_attempt_cap(client, monkeypatch):
    # Review 2026-09-13: the cap is for signature spam, not for a real buyer —
    # one demo wallet must be able to buy more than 10 times in 10 minutes.
    _mock_facilitator(monkeypatch)
    _mock_llm(monkeypatch)
    req = x402.requirements("chat", "u")
    for i in range(12):
        r = client.post("/api/x402/chat", json={"message": "hi"},
                        headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + f"{i + 60:02d}" * 32))})
        assert r.status_code == 200, (i, r.text)
    assert not any(celo_routes._payer_attempts.values())        # every attempt was forgiven
    # ...while a spammer with bad signatures is still capped at 10
    celo_routes._payer_attempts.clear()
    _mock_facilitator(monkeypatch, verify_ok=False)
    for i in range(10):
        r = client.post("/api/x402/chat", json={"message": "hi"},
                        headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + f"{i + 80:02d}" * 32))})
        assert r.status_code == 402
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + "98" * 32))})
    assert r.status_code == 429
    celo_routes._payer_fails.clear(); celo_routes._payer_attempts.clear()


def test_content_failures_never_strike_and_attempts_are_capped(client, monkeypatch):
    calls = _mock_facilitator(monkeypatch, verify_ok=False)
    req = x402.requirements("chat", "u")
    # 10 unverified attempts allowed per 10 min, the 11th is 429; none strikes the payer
    for i in range(10):
        r = client.post("/api/x402/chat", json={"message": "hi"},
                        headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + f"{i + 20:02d}" * 32))})
        assert r.status_code == 402
    r = client.post("/api/x402/chat", json={"message": "hi"},
                    headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + "99" * 32))})
    assert r.status_code == 429 and "attempts" in r.json()["detail"]
    assert calls["verify"] == 10 and x402.recent() == []
    # verify-time failures cost no model time → no strikes (the cap did the work)
    assert not any(celo_routes._payer_fails.values())
    celo_routes._payer_fails.clear(); celo_routes._payer_attempts.clear()
    _mock_facilitator(monkeypatch)
    _mock_llm(monkeypatch, billable=False)
    for i in range(3):
        r = client.post("/api/x402/chat", json={"message": "hi"},
                        headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req, nonce="0x" + f"{i + 40:02d}" * 32))})
        assert r.status_code == 503, r.text                       # model busy: never 429
    assert not any(celo_routes._payer_fails.values())


def test_hash_persists_before_owner_share_and_scanner_matches_by_amount(client, monkeypatch):
    from auto_service.services import deposits
    _mock_facilitator(monkeypatch, tx="0xSALEtx")
    agent = _sell_agent("ag_scan")
    code = agent_model.agent_code("ag_scan")
    req = x402.requirements("insight", "u")
    r = client.get(f"/api/x402/agents/{code}/insight", headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 200
    # by hash, by (payer, asset, amount) while the hash is unknown, and never for a different amount
    assert deposits._is_x402_settlement("0xsaletx")
    assert deposits._is_x402_settlement("0xunknown", PAYER, "USDC", 0.05)
    assert not deposits._is_x402_settlement("0xunknown", PAYER, "USDC", 5.0)
    assert not deposits._is_x402_settlement("0xunknown", OWNER, "USDC", 0.05)
    assert not deposits._is_x402_settlement("0xunknown", PAYER, "USDT", 0.05)   # not an x402 asset


def test_owner_share_failure_is_retried_by_finalizer(client, monkeypatch):
    _mock_facilitator(monkeypatch, tx="0xshare")
    _sell_agent("ag_share")
    code = agent_model.agent_code("ag_share")
    req = x402.requirements("insight", "u")
    orig = points_model.credit
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return orig(*a, **k)
    monkeypatch.setattr(points_model, "credit", flaky)
    r = client.get(f"/api/x402/agents/{code}/insight", headers={"PAYMENT-SIGNATURE": x402.b64e(_payload(req))})
    assert r.status_code == 200 and points_model.balance(OWNER) == 0.0
    row = x402.recent()[0]
    assert row["status"] == "settled" and row["tx"] == "0xshare" and row["owner_credits"] == 0
    assert x402.get_payment(PAYER, "0x" + "ab" * 32)["meta"]["owner_share_failed"] is True
    out = x402.finalize_pending()
    assert out["shares"] == 1 and points_model.balance(OWNER) == 35.0
    assert x402.recent()[0]["owner_credits"] == 35.0


def test_client_ip_trusts_cloudflare_header_only_when_fronted(monkeypatch):
    from auto_service import admin_auth
    from starlette.requests import Request

    def req(headers):
        scope = {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                 "client": ("10.0.0.9", 1234), "method": "GET", "path": "/", "query_string": b"",
                 "scheme": "http", "server": ("h", 80), "http_version": "1.1"}
        return Request(scope)
    monkeypatch.delenv("MANEKI_BEHIND_CLOUDFLARE", raising=False)
    assert admin_auth.client_ip(req({"cf-connecting-ip": "1.2.3.4", "x-real-ip": "5.6.7.8", "x-forwarded-for": "9.9.9.9, 5.6.7.8"})) == "5.6.7.8"
    assert admin_auth.client_ip(req({"x-forwarded-for": "9.9.9.9"})) == "10.0.0.9"
    monkeypatch.setenv("MANEKI_BEHIND_CLOUDFLARE", "1")
    assert admin_auth.client_ip(req({"cf-connecting-ip": "1.2.3.4", "x-real-ip": "5.6.7.8"})) == "1.2.3.4"
    assert admin_auth.client_ip(req({"x-real-ip": "5.6.7.8"})) == "5.6.7.8"
