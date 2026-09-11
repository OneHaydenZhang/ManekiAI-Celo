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
    assert ca.register_agent("ag_ce") == {"ok": True, "already": True, "agentId": 5150}


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
