"""ERC-8004 on Celo (auto_service/celo/agentid.py) — gating, registration of
ANY-model agents, idempotency, platform Analyst registration, backfill, and
the registration-v1 agent cards (public facts only).
"""
from __future__ import annotations

import time

import pytest

from tests.test_points_v1 import _TMP  # noqa: F401 — temp-DB bootstrap
from auto_service import db, admin_store, service_config
from auto_service.models import agent_model
from auto_service.celo import agentid as ca
from auto_service.celo import x402
from auto_service.services import zerog_agentid as zg

DEV_KEY = "0x" + "22" * 32
TREASURY = "0x26523f5cea5da5d9411749afefe741ba340f6566"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for t in ("agents", "admin_settings", "decisions"):
        db.execute(f"DELETE FROM {t}")
    monkeypatch.setattr(service_config, "_admin_setting", lambda k: "")
    monkeypatch.delenv("CELO_REGISTRAR_KEY", raising=False)
    monkeypatch.delenv("ZEROG_REGISTRAR_KEY", raising=False)
    monkeypatch.delenv("CELO_AGENTID_ENABLED", raising=False)
    monkeypatch.delenv("X402_API_KEY", raising=False)
    monkeypatch.delenv("X402_PAY_TO", raising=False)
    monkeypatch.setenv("CELO_TREASURY_ADDRESS", TREASURY)
    # the registrar balance cache (60s) is process-global — never let one
    # test's 'unfunded' reading leak into the next
    ca.wallet.invalidate_balance(); ca.wallet._bal_cache["wei"] = None
    ca._last_fail.clear()
    yield


def _agent(agent_id="ag_ce", model="deepseek/deepseek-chat", cid=0, zid=0, sell=0, deleted=0, status="running"):
    db.execute(
        "INSERT INTO agents(agent_id, address, symbol, model, persona, label, created_at, updated_at,"
        " celo_agent_id, zerog_agent_id, x402_sell, deleted_at, status)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, "0xowner", "xyz:NVDA", model, "navigator", "My NVDA", time.time(), time.time(),
         cid, zid, sell, deleted, status))
    return agent_model.get(agent_id)


def _fake_rpc(agent_id_out=5150, status="0x1", txh="0xceloTx"):
    def rpc(method, params):
        return {
            "eth_getTransactionCount": "0x7",
            "eth_gasPrice": hex(202_000_000_000),
            "eth_estimateGas": hex(183_000),
            "eth_sendRawTransaction": txh,
            "eth_getTransactionReceipt": {"status": status, "logs": [{
                "address": ca.REGISTRY,
                "topics": [zg.TOPIC_REGISTERED, "0x" + hex(agent_id_out)[2:].rjust(64, "0")]}]},
        }[method]
    return rpc


# ------------------------------------------------------------------ gating --

def test_disabled_without_any_key():
    assert not ca.enabled()
    assert ca.register_agent("whatever") == {"ok": False, "skipped": "disabled"}
    assert ca.register_platform() == {"ok": False, "skipped": "disabled"}


def test_key_fallback_and_kill_switch(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)     # one EVM key, every chain
    assert ca.enabled() and ca.registrar_address().startswith("0x")
    monkeypatch.setenv("CELO_REGISTRAR_KEY", "0x" + "33" * 32)
    assert ca.registrar_key() == "0x" + "33" * 32           # dedicated key wins
    monkeypatch.setenv("CELO_AGENTID_ENABLED", "off")
    assert not ca.enabled()


def test_same_registry_address_as_0g():
    assert ca.REGISTRY == zg.REGISTRY == "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    assert ca.REGISTRY_CAIP == "eip155:42220:" + ca.REGISTRY


# ----------------------------------------------------------- registration --

def test_register_any_model_agent_persists(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent()                                    # NOT a 0G model — still registers on Celo
    monkeypatch.setattr(ca, "_rpc", _fake_rpc())
    r = ca.register_agent("ag_ce")
    assert r["ok"] and r["agentId"] == 5150 and r["txhash"] == "0xceloTx"
    assert r["uri"] == "https://manekiai.io/api/agent-card/" + agent_model.agent_code("ag_ce")
    row = agent_model.get("ag_ce")
    assert row["celo_agent_id"] == 5150 and row["celo_agent_tx"] == "0xceloTx"
    assert row["celo_registered_at"] > 0
    assert row["zerog_agent_id"] == 0           # the 0G columns are untouched
    # idempotent
    # idempotent — and the admin "Celo⛓" button shows this tx, so it comes back too
    assert ca.register_agent("ag_ce") == {"ok": True, "already": True, "agentId": 5150, "txhash": "0xceloTx"}


def test_register_skips_deleted_and_reports_revert(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent("ag_gone", deleted=time.time())
    assert ca.register_agent("ag_gone")["skipped"] == "agent deleted"
    _agent("ag_rev")
    monkeypatch.setattr(ca, "_rpc", _fake_rpc(status="0x0"))
    r = ca.register_agent("ag_rev")
    assert not r["ok"] and "reverted" in r["error"]
    assert agent_model.get("ag_rev")["celo_agent_id"] == 0


def test_register_all_missing_backfills_only_live_unregistered(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent("ag_1"); _agent("ag_2", cid=9); _agent("ag_3", deleted=time.time())
    monkeypatch.setattr(ca, "_rpc", _fake_rpc(agent_id_out=77))
    r = ca.register_all_missing()
    assert r["registered"] == 1 and r["failed"] == 0
    assert agent_model.get("ag_1")["celo_agent_id"] == 77
    assert agent_model.get("ag_2")["celo_agent_id"] == 9
    assert agent_model.get("ag_3")["celo_agent_id"] == 0
    assert ca.registered_count() == 2


def test_platform_registration_once(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    assert ca.platform_agent() is None
    monkeypatch.setattr(ca, "_rpc", _fake_rpc(agent_id_out=4242, txh="0xplat"))
    r = ca.register_platform()
    assert r["ok"] and r["agentId"] == 4242 and r["txhash"] == "0xplat"
    assert r["uri"].endswith("/api/agent-card/maneki-analyst")
    assert ca.platform_agent()["agentId"] == 4242
    assert ca.register_platform()["already"] is True
    st = ca.public_status()
    assert st["platform_agent"]["agentId"] == 4242 and st["identity_registry"] == ca.REGISTRY


# ----------------------------------------------------------------- cards ---

def test_agent_card_dual_registration_and_x402_service(monkeypatch):
    monkeypatch.setenv("X402_API_KEY", "k")     # x402 on → services can advertise it
    a = _agent(cid=5150, zid=999, sell=1)
    db.execute("UPDATE agents SET celo_agent_tx='0xceloTx', zerog_agent_tx='0xogtx' WHERE agent_id='ag_ce'")
    card = ca.agent_card(agent_model.get("ag_ce"))
    assert card["type"].endswith("#registration-v1")
    regs = card["registrations"]
    assert regs[0] == {"agentId": 999, "agentRegistry": zg.REGISTRY_CAIP}
    assert regs[1] == {"agentId": 5150, "agentRegistry": ca.REGISTRY_CAIP}
    names = [s["name"] for s in card["services"]]
    assert "web" in names and "agent-card" in names and "x402-insight" in names
    svc = [s for s in card["services"] if s["name"] == "x402-insight"][0]
    assert svc["endpoint"].endswith(f"/api/x402/agents/{agent_model.agent_code('ag_ce')}/insight")
    assert svc["price"]["usd"] == 0.05 and svc["price"]["network"] == "eip155:42220"
    assert card["supportedTrust"] == ["reputation"]
    assert card["x402Support"] is True and card["active"] is True
    assert card["x-maneki"]["celo"]["agentId"] == 5150
    assert card["x-maneki"]["wallet"] == {"chainId": 42220, "address": TREASURY,
                                          "role": "x402 payTo / stablecoin Gas treasury"}
    blob = str(card)
    assert "0xowner" not in blob and "ag_ce" not in blob


def test_agent_card_without_optin_has_no_x402_service(monkeypatch):
    monkeypatch.setenv("X402_API_KEY", "k")
    _agent(cid=5150, sell=0, status="stopped")
    card = ca.agent_card(agent_model.get("ag_ce"))
    assert "x402-insight" not in [s["name"] for s in card["services"]]
    assert card["x402Support"] is False and card["active"] is False
    assert card["registrations"] == [{"agentId": 5150, "agentRegistry": ca.REGISTRY_CAIP}]


def test_platform_card(monkeypatch):
    monkeypatch.setenv("X402_API_KEY", "k")
    admin_store.set("celo_platform_agent", {"agentId": 4242, "txhash": "0xplat"})
    card = ca.platform_card()
    assert card["name"].startswith("ManekiAI Analyst")
    assert card["registrations"] == [{"agentId": 4242, "agentRegistry": ca.REGISTRY_CAIP}]
    names = {s["name"] for s in card["services"]}
    assert {"x402-chat", "x402-brief", "x402-catalog", "agent-card"} <= names
    chat = [s for s in card["services"] if s["name"] == "x402-chat"][0]
    assert chat["endpoint"].endswith("/api/x402/chat") and chat["price"]["usd"] == 0.02
    assert card["x402Support"] is True and x402.enabled()


# ------------------------------------------------- review fixes (09-11) ----

def test_unfunded_registrar_skips_without_sending(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent()
    sent = []

    def rpc(method, params):
        if method == "eth_getBalance":
            return hex(10 ** 15)                       # 0.001 CELO — below the floor
        sent.append(method)
        raise AssertionError("must not sign/send when unfunded")
    monkeypatch.setattr(ca, "_rpc", rpc)
    ca._bal_cache["at"] = 0.0
    r = ca.register_agent("ag_ce")
    assert not r["ok"] and "unfunded" in r["skipped"] and sent == []
    assert ca.register_platform()["skipped"].startswith("registrar unfunded")
    assert ca.register_all_missing_async()["skipped"].startswith("registrar unfunded")
    # a read failure is NOT a block: unknown balance → try anyway
    monkeypatch.setattr(ca, "_rpc", _fake_rpc(agent_id_out=8))
    ca._bal_cache["at"] = 0.0
    assert ca.register_agent("ag_ce")["agentId"] == 8


def test_edit_hook_backs_off_after_failure(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent("ag_bk")
    monkeypatch.setattr(ca, "_rpc", _fake_rpc(status="0x0"))
    ca._bal_cache["at"] = 0.0
    assert not ca.register_agent("ag_bk")["ok"]
    assert time.time() - ca._last_fail["ag_bk"] < 5
    started = []
    monkeypatch.setattr(ca.threading, "Thread",
                        lambda *a, **k: started.append(k.get("args")) or type("T", (), {"start": lambda self: None})())
    ca.maybe_register_async("ag_bk")
    assert started == []                                # inside the backoff window
    ca._last_fail["ag_bk"] = 0.0
    ca.maybe_register_async("ag_bk")
    assert started == [("ag_bk",)]                       # window elapsed → retried


# ------------------------------------------------- autopilot + receipts ----

def test_autopilot_tick_mints_platform_then_agents(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent("ag_p1"); _agent("ag_p2", cid=3)
    ids = iter([4242, 501])
    sent = []

    def rpc(method, params):
        if method == "eth_getBalance":
            return hex(2 * 10 ** 18)
        if method == "eth_sendRawTransaction":
            sent.append(params[0]); return f"0xtx{len(sent)}"
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "logs": [{"address": ca.REGISTRY,
                    "topics": [zg.TOPIC_REGISTERED, "0x" + hex(next(ids))[2:].rjust(64, "0")]}]}
        return {"eth_getTransactionCount": "0x1", "eth_gasPrice": hex(200_000_000_000),
                "eth_estimateGas": hex(180_000)}[method]
    monkeypatch.setattr(ca, "_rpc", rpc)
    r = ca.autopilot_tick()
    assert r["platform"]["agentId"] == 4242 and r["registered"] == 1 and r["pending"] == 0
    assert agent_model.get("ag_p1")["celo_agent_id"] == 501 and len(sent) == 2
    # second pass: nothing to do, nothing sent
    r2 = ca.autopilot_tick()
    assert "platform" not in r2 and r2.get("registered") is None and len(sent) == 2


def test_autopilot_skips_while_unfunded(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent("ag_u")
    monkeypatch.setattr(ca, "_rpc", lambda m, p: hex(10 ** 15) if m == "eth_getBalance" else (_ for _ in ()).throw(AssertionError(m)))
    r = ca.autopilot_tick()
    assert r["skipped"] == "unfunded" and r["pending"] == 1


def test_receipt_timeout_persists_hash_and_finalizes_later(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    monkeypatch.setattr(ca.wallet, "RECEIPT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(ca.wallet, "RECEIPT_POLL_S", 0.001)
    _agent("ag_t")
    sends = []
    state = {"mined": False}

    def rpc(method, params):
        if method == "eth_getBalance":
            return hex(10 ** 18)
        if method == "eth_sendRawTransaction":
            sends.append(1); return "0xslow"
        if method == "eth_getTransactionReceipt":
            return ({"status": "0x1", "logs": [{"address": ca.REGISTRY,
                     "topics": [zg.TOPIC_REGISTERED, "0x" + hex(31337)[2:].rjust(64, "0")]}]}
                    if state["mined"] else None)
        if method == "eth_getTransactionByHash":
            return {"hash": "0xslow"}                       # known, still pending
        return {"eth_getTransactionCount": "0x1", "eth_gasPrice": "0x1",
                "eth_estimateGas": hex(180_000)}[method]
    monkeypatch.setattr(ca, "_rpc", rpc)
    r = ca.register_agent("ag_t")
    assert r["skipped"] == "pending receipt" and r["txhash"] == "0xslow"
    row = agent_model.get("ag_t")
    assert row["celo_agent_tx"] == "0xslow" and row["celo_agent_id"] == 0 and row["celo_registered_at"] > 0
    # still pending → no second broadcast
    assert ca.register_agent("ag_t")["skipped"] == "pending receipt" and sends == [1]
    state["mined"] = True
    r3 = ca.register_agent("ag_t")
    assert r3["ok"] and r3["agentId"] == 31337 and r3["txhash"] == "0xslow" and sends == [1]


def test_repoint_uris_only_sends_where_different(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    monkeypatch.setenv("MANEKI_PUBLIC_BASE", "https://arena.manekiai.io")
    _agent("ag_r", cid=77)
    admin_store.set("celo_platform_agent", {"agentId": 4242, "txhash": "0xplat", "uri": "http://old/x"})
    code = agent_model.agent_code("ag_r")
    onchain = {77: f"https://arena.manekiai.io/api/agent-card/{code}", 4242: "http://34.68.151.4/api/agent-card/maneki-analyst"}
    sent = []

    def rpc(method, params):
        if method == "eth_getBalance":
            return hex(10 ** 18)
        if method == "eth_call":
            aid = int(params[0]["data"][10:], 16)
            b = onchain[aid].encode()
            return "0x" + (32).to_bytes(32, "big").hex() + len(b).to_bytes(32, "big").hex() + b.hex().ljust(((len(b) + 31) // 32) * 64, "0")
        if method == "eth_sendRawTransaction":
            sent.append(params[0]); return "0xset"
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "logs": []}
        return {"eth_getTransactionCount": "0x1", "eth_gasPrice": "0x1", "eth_estimateGas": hex(60_000)}[method]
    monkeypatch.setattr(ca, "_rpc", rpc)
    dry = ca.repoint_uris(dry_run=True)
    assert [c["onchain"] for c in dry["changed"]] == [4242] and dry["unchanged"] == 1 and sent == []
    r = ca.repoint_uris()
    assert len(r["changed"]) == 1 and r["changed"][0]["txhash"] == "0xset" and len(sent) == 1
    assert ca.platform_agent()["uri"].startswith("https://arena.manekiai.io")
    data = ca._encode_set_agent_uri(4242, "https://arena.manekiai.io/api/agent-card/maneki-analyst")
    assert data.startswith("0x" + ca.SEL_SET_AGENT_URI) and int(data[10:74], 16) == 4242


def test_reverted_pending_is_cleared_and_resent_after_backoff(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent("ag_rev2")
    db.execute("UPDATE agents SET celo_agent_tx='0xdead', celo_registered_at=? WHERE agent_id='ag_rev2'", (time.time(),))
    sent = []

    def rpc(method, params):
        if method == "eth_getBalance":
            return hex(10 ** 18)
        if method == "eth_getTransactionReceipt":
            return {"status": "0x0", "logs": []} if params[0] == "0xdead" else \
                   {"status": "0x1", "logs": [{"address": ca.REGISTRY, "topics": [zg.TOPIC_REGISTERED, "0x" + hex(9)[2:].rjust(64, "0")]}]}
        if method == "eth_sendRawTransaction":
            sent.append(1); return "0xfresh"
        if method == "eth_getTransactionByHash":
            return {"hash": params[0]}
        return {"eth_getTransactionCount": "0x1", "eth_gasPrice": "0x1", "eth_estimateGas": hex(180_000)}[method]
    monkeypatch.setattr(ca, "_rpc", rpc)
    r = ca.register_agent("ag_rev2")
    assert not r["ok"] and "cleared" in r["error"] and sent == []
    assert agent_model.get("ag_rev2")["celo_agent_tx"] == "" and ca._last_fail["ag_rev2"] > 0
    # autopilot respects the backoff; the admin batch forces the resend
    assert ca.register_all_missing(respect_backoff=True)["backoff"] == 1 and sent == []
    r2 = ca.register_all_missing()
    assert r2["registered"] == 1 and sent == [1] and agent_model.get("ag_rev2")["celo_agent_id"] == 9


def test_unknown_pending_tx_waits_then_resends_and_stuck_tx_is_bumped(monkeypatch):
    monkeypatch.setenv("ZEROG_REGISTRAR_KEY", DEV_KEY)
    _agent("ag_lag")
    db.execute("UPDATE agents SET celo_agent_tx='0xlag', celo_registered_at=? WHERE agent_id='ag_lag'", (time.time(),))
    sent = []

    def rpc(method, params):
        if method == "eth_getBalance":
            return hex(10 ** 18)
        if method == "eth_getTransactionReceipt":
            return None
        if method == "eth_getTransactionByHash":
            return None                                    # a lagging gateway does not know it
        if method == "eth_sendRawTransaction":
            sent.append(params[0]); return "0xnew"
        return {"eth_getTransactionCount": "0x2", "eth_gasPrice": "0x1", "eth_estimateGas": hex(180_000)}[method]
    monkeypatch.setattr(ca, "_rpc", rpc)
    monkeypatch.setattr(ca.wallet, "RECEIPT_TIMEOUT_S", 0.01); monkeypatch.setattr(ca.wallet, "RECEIPT_POLL_S", 0.001)
    # fresh + unknown → wait (no resend)
    assert ca.register_agent("ag_lag")["skipped"] == "pending receipt" and sent == []
    # old + unknown → cleared and re-sent
    db.execute("UPDATE agents SET celo_registered_at=? WHERE agent_id='ag_lag'", (time.time() - 7200,))
    r = ca.register_agent("ag_lag")
    assert r["skipped"] == "pending receipt" and r["txhash"] == "0xnew" and len(sent) == 1
    # old + known-but-stuck → replace-by-fee on the SAME nonce, never a fresh send
    _agent("ag_stuck")
    db.execute("UPDATE agents SET celo_agent_tx='0xstuck', celo_registered_at=? WHERE agent_id='ag_stuck'", (time.time() - 7200,))
    bumped = []

    def rpc2(method, params):
        if method == "eth_getBalance":
            return hex(10 ** 18)
        if method == "eth_getTransactionReceipt":
            return None
        if method == "eth_getTransactionByHash":
            return {"hash": "0xstuck", "nonce": "0x3", "gasPrice": hex(10 ** 9), "gas": hex(180_000),
                    "to": ca.REGISTRY, "value": "0x0", "input": "0x" + "ab" * 8, "blockNumber": None}
        if method == "eth_gasPrice":
            return hex(2 * 10 ** 9)
        if method == "eth_sendRawTransaction":
            bumped.append(params[0]); return "0xbumped"
        raise AssertionError(method)
    monkeypatch.setattr(ca, "_rpc", rpc2)
    ca.wallet._price_cache["at"] = 0.0
    r = ca.register_agent("ag_stuck")
    assert r["skipped"] == "pending receipt" and r["txhash"] == "0xbumped" and len(bumped) == 1
    assert agent_model.get("ag_stuck")["celo_agent_tx"] == "0xbumped"
