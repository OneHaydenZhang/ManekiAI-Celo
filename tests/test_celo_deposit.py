"""Celo stablecoin deposit lane (auto_service/celo/chain.py) — registry entry
(native hidden, three stables), forno 5000-block chunking, log decode, $1-par
crediting through the shared scan pipeline.

Run: python3 -m pytest tests/ -q   (repo root)
"""
from __future__ import annotations

import time

import pytest

from tests.test_points_v1 import _TMP  # noqa: F401 — temp-DB bootstrap
from auto_service import db, service_config
from auto_service.models import points_model
from auto_service.services import chains, deposits, pricing
from auto_service.celo import chain as celo_chain

ME = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
TREASURY = "0x26523f5cea5da5d9411749afefe741ba340f6566"
USDC, USDT, USDM = celo_chain.USDC, celo_chain.USDT, celo_chain.USDM


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for t in ("points_ledger", "points_tx", "bonus_grants", "deposit_senders",
              "users", "admin_settings", "notifications"):
        try:
            db.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    monkeypatch.setattr(service_config, "_admin_setting", lambda k: "")
    for env in ("INJ_TREASURY_ADDRESS", "ZEROG_TREASURY_ADDRESS",
                "HYPE_TREASURY_ADDRESS", "XLAYER_TREASURY_ADDRESS", "INJ_PRICING_MODE"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("CELO_TREASURY_ADDRESS", TREASURY)
    chains.invalidate_cache()
    pricing.invalidate_cache()
    yield
    chains.invalidate_cache()
    pricing.invalidate_cache()


# ---------------------------------------------------------------- registry ---

def test_registry_default_celo_entry():
    ce = chains.all_chains()["CELO"]
    assert ce["provider"] == "celo" and ce["token"] == "CELO"
    assert ce["enabled"] is True and ce["treasury"] == TREASURY
    assert ce["native"] is False
    assert ce["erc20"]["USDC"] == {"address": USDC, "decimals": 6, "display": "USDC"}
    assert ce["erc20"]["USDT"]["address"] == USDT and ce["erc20"]["USDT"]["decimals"] == 6
    assert ce["erc20"]["USDM"]["address"] == USDM and ce["erc20"]["USDM"]["decimals"] == 18
    pub = [c for c in chains.public_list() if c["id"] == "CELO"][0]
    assert pub["kind"] == "evm" and pub["treasury"] == TREASURY
    assert pub["evm"]["chain_id_hex"] == "0xa4ec" and pub["evm"]["rpc"] == "https://forno.celo.org"
    assert pub["evm"]["explorer_tx"] == "https://celoscan.io/tx/"
    # stablecoin-only: no native CELO offered, every token at par
    assert [t["symbol"] for t in pub["tokens"]] == ["USDC", "USDT", "USDM", "USAT"]
    assert all(t["kind"] == "erc20" and t["price_usd"] == 1.0 for t in pub["tokens"])
    assert [t["display"] for t in pub["tokens"]] == ["USDC", "USD₮", "USDm (cUSD)", "USA₮"]
    assert ce["erc20"]["USAT"] == {"address": celo_chain.USAT, "decimals": 6, "display": "USA₮"}
    # Celo sits right after the promoted 0G lane in the UI order
    assert list(chains.all_chains().keys())[:2] == ["0G", "CELO"]


def test_registry_celo_hides_without_treasury(monkeypatch):
    monkeypatch.delenv("CELO_TREASURY_ADDRESS")
    chains.invalidate_cache()
    assert chains.all_chains()["CELO"]["enabled"] is False
    assert not [c for c in chains.public_list() if c["id"] == "CELO"]


def test_registry_accepts_operator_published_usat():
    # USA₮ (or any new stable) is a config publish, not a code change.
    out = chains.validate_chains({"CELO": {
        "provider": "celo", "treasury": TREASURY, "enabled": True, "native": False,
        "erc20": {"USDC": {"address": USDC, "decimals": 6},
                  "USAT": {"address": "0x" + "ab" * 20, "decimals": 6, "display": "USA₮"}}}})
    assert out["CELO"]["erc20"]["USAT"]["display"] == "USA₮"
    assert pricing.token_price_usd("USAT") == 1.0 and pricing.token_price_usd("USDM") == 1.0


# -------------------------------------------------------------- log decode ---

_LOG_IDX = iter(range(1000))


def _log(contract, sender, to, value_units, txh, block=100, extra=None):
    lg = {"address": contract,
          "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                     "0x" + sender[2:].rjust(64, "0"),
                     "0x" + to[2:].rjust(64, "0")],
          "data": hex(value_units), "transactionHash": txh,
          "blockNumber": hex(block), "logIndex": hex(next(_LOG_IDX))}
    lg.update(extra or {})
    return lg


def test_fetch_incoming_decodes_and_aggregates(monkeypatch):
    logs = [
        _log(USDC, ME, TREASURY, 25_000_000, "0xa", extra={"blockTimestamp": hex(1757500000)}),
        _log(USDT, OTHER, TREASURY, 10_000_000, "0xb"),
        # USDm has 18 decimals — 2.5 USDm
        _log(USDM, ME, TREASURY, 2_500_000_000_000_000_000, "0xm"),
        # two matching logs inside ONE tx must sum (ledger credits once per txhash)
        _log(USDC, ME, TREASURY, 1_000_000, "0xc"),
        _log(USDC, ME, TREASURY, 2_000_000, "0xc"),
        # self-send from the treasury, zero-value dust and unregistered tokens are dropped
        _log(USDC, TREASURY, TREASURY, 5_000_000, "0xd"),
        _log(USDT, ME, TREASURY, 0, "0xe"),
        _log("0x" + "77" * 20, ME, TREASURY, 9_000_000, "0xz"),
    ]
    monkeypatch.setattr(celo_chain, "_rpc",
                        lambda url, method, params: hex(1000) if method == "eth_blockNumber" else None)
    monkeypatch.setattr(celo_chain, "_get_logs",
                        lambda url, frm, to, contracts, topic: list(logs))
    rows = {(r["txhash"], r["token"]): r for r in celo_chain.fetch_incoming(
        "mainnet", TREASURY, limit=40,
        erc20={"USDC": {"address": USDC, "decimals": 6},
               "USDT": {"address": USDT, "decimals": 6},
               "USDM": {"address": USDM, "decimals": 18}})}
    assert set(rows) == {("0xa", "USDC"), ("0xb", "USDT"), ("0xm", "USDM"), ("0xc", "USDC")}
    assert rows[("0xa", "USDC")]["amount"] == pytest.approx(25.0)
    assert rows[("0xa", "USDC")]["sender_eth"] == ME
    assert rows[("0xa", "USDC")]["ts"] == 1757500000
    assert rows[("0xm", "USDM")]["amount"] == pytest.approx(2.5)
    assert rows[("0xc", "USDC")]["amount"] == pytest.approx(3.0)


def test_scan_is_chunked_to_forno_limit(monkeypatch):
    """forno caps eth_getLogs at 5,000 blocks (measured live 2026-09-10): a
    90,000-block admin rescan must be split into 19 range queries."""
    ranges = []
    monkeypatch.setattr(celo_chain, "_rpc",
                        lambda url, method, params: hex(200_000) if method == "eth_blockNumber" else None)

    def get_logs(url, frm, to, contracts, topic):
        ranges.append((frm, to))
        assert to - frm + 1 <= 5000
        return []
    monkeypatch.setattr(celo_chain, "_get_logs", get_logs)
    celo_chain.fetch_incoming("mainnet", TREASURY, limit=1000,
                              erc20={"USDC": {"address": USDC, "decimals": 6}})
    assert len(ranges) == 19                       # ceil((90,000 + 1) / 5,000)
    assert ranges[0][0] == 200_000 - 90_000 and ranges[-1][1] == 200_000


def test_fetch_incoming_falls_back_to_second_rpc(monkeypatch):
    calls = []

    def rpc(url, method, params):
        calls.append(url)
        if url == celo_chain.SCAN_RPCS[0][0]:
            raise RuntimeError("rate limited")
        return hex(1000) if method == "eth_blockNumber" else None
    monkeypatch.setattr(celo_chain, "_rpc", rpc)
    monkeypatch.setattr(celo_chain, "_get_logs",
                        lambda url, frm, to, contracts, topic:
                        [_log(USDC, ME, TREASURY, 5_000_000, "0xf")])
    rows = celo_chain.fetch_incoming("mainnet", TREASURY, limit=40,
                                     erc20={"USDC": {"address": USDC, "decimals": 6}})
    assert len(rows) == 1 and rows[0]["amount"] == pytest.approx(5.0)
    assert celo_chain.SCAN_RPCS[1][0] in calls


# ------------------------------------------------------------- deposit leg ---

def _wire_chain(monkeypatch, txs=None, failed=()):
    monkeypatch.setattr(celo_chain, "fetch_incoming",
                        lambda network, treasury, limit=40, erc20=None: list(txs or []))
    monkeypatch.setattr(celo_chain, "tx_succeeded", lambda txh: txh not in failed)


def test_celo_stablecoin_deposit_leg(monkeypatch):
    _wire_chain(monkeypatch, txs=[
        {"txhash": "0xok", "sender_eth": ME, "amount": 25.0, "token": "USDC"},
        {"txhash": "0xusdm", "sender_eth": ME, "amount": 3.0, "token": "USDM"},
        {"txhash": "0xother", "sender_eth": OTHER, "amount": 50.0, "token": "USDT"},
        {"txhash": "0xfailed", "sender_eth": ME, "amount": 70.0, "token": "USDC"},
    ], failed={"0xfailed"})
    r = deposits.scan_for_user(ME)
    # $1 par: 25 × 1.01 × 1000 = 25250 and 3 × 1.01 × 1000 = 3030; the ≥$10
    # cumulative deposit also lands the one-time first-top-up promo (+1000).
    assert r["credited"] == 2
    assert r["credited_points"] == pytest.approx(25250.0 + 3030.0)
    assert points_model.balance(ME) == pytest.approx(25250.0 + 3030.0 + 1000.0)
    row = db.query_one("SELECT * FROM points_tx WHERE txhash='0xok'")
    assert row["chain"] == "CELO" and row["token"] == "USDC"
    assert row["price_usd"] == pytest.approx(1.0)
    row2 = db.query_one("SELECT * FROM points_tx WHERE txhash='0xusdm'")
    assert row2["chain"] == "CELO" and row2["token"] == "USDM"
    # idempotent; the failed tx never minted Gas
    assert deposits.scan_for_user(ME)["credited"] == 0
    assert db.query_one("SELECT 1 FROM points_tx WHERE txhash='0xfailed'") is None


def test_background_sweep_credits_registered_login_user(monkeypatch):
    # An unscoped sweep (only=None) needs the CELO gate open — see
    # deposits._sweep_gate_ok (2026-09-13, closes the admin-rescan bypass).
    monkeypatch.setenv("DEPOSIT_SWEEP_ENABLED", "1")
    db.execute("INSERT OR REPLACE INTO users(address, created_at) VALUES(?,?)", (ME, time.time()))
    _wire_chain(monkeypatch, txs=[
        {"txhash": "0xbg1", "sender_eth": ME, "amount": 5.0, "token": "USDC"},
        {"txhash": "0xbg2", "sender_eth": OTHER, "amount": 5.0, "token": "USDC"},
    ])
    r = deposits.scan_once()
    assert r["credited"] == 1
    assert points_model.balance(ME) == pytest.approx(5 * 1.01 * 1000)
    assert points_model.balance(OTHER) == 0.0


def test_unscoped_sweep_skips_celo_when_gate_closed(monkeypatch):
    """The admin's manual rescan button calls scan_once() with no `only` —
    without DEPOSIT_SWEEP_ENABLED, that must NOT touch Celo (the 2026-09-11
    mis-credit incident this gate exists to prevent)."""
    monkeypatch.delenv("DEPOSIT_SWEEP_ENABLED", raising=False)
    db.execute("INSERT OR REPLACE INTO users(address, created_at) VALUES(?,?)", (ME, time.time()))
    _wire_chain(monkeypatch, txs=[{"txhash": "0xgated", "sender_eth": ME, "amount": 5.0, "token": "USDC"}])
    assert deposits.scan_once()["credited"] == 0
    assert points_model.balance(ME) == 0.0
    # an explicit, scoped request is exempt from the gate (a deliberate admin action)
    assert deposits.scan_once(only={"CELO"})["credited"] == 1
