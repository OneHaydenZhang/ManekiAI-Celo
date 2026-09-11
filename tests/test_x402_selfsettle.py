"""Self-hosted x402 settlement (auto_service/celo/settle.py + wallet.py):
EIP-3009 calldata encoding, structural checks, simulation-based verify, and
settle that never broadcasts a call that would revert. RPC is mocked; the
encoding was proven against Celo mainnet USDC on 2026-09-11 (a real signature
reverts only with 'transfer amount exceeds balance').
"""
from __future__ import annotations

import secrets
import time

import pytest
from eth_account import Account

from tests.test_points_v1 import _TMP  # noqa: F401 — temp-DB bootstrap
from auto_service.celo import settle, wallet

DEV_KEY = "0x" + "44" * 32
PAY_TO = "0x26523f5cea5da5d9411749afefe741ba340f6566"
TYPES = {"TransferWithAuthorization": [
    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}
DOMAIN = {"name": "USDC", "version": "2", "chainId": 42220, "verifyingContract": settle.USDC}


@pytest.fixture(autouse=True)
def _registrar(monkeypatch):
    monkeypatch.setenv("CELO_REGISTRAR_KEY", DEV_KEY)
    wallet.invalidate_balance()
    wallet._bal_cache["wei"] = None
    yield


def _signed(value=20000, to=PAY_TO, valid_before=None, valid_after=0):
    acct = Account.create()
    auth = {"from": acct.address, "to": to, "value": str(value), "validAfter": str(valid_after),
            "validBefore": str(valid_before if valid_before is not None else int(time.time()) + 600),
            "nonce": "0x" + secrets.token_hex(32)}
    msg = {**auth, "value": int(auth["value"]), "validAfter": int(auth["validAfter"]),
           "validBefore": int(auth["validBefore"]), "nonce": bytes.fromhex(auth["nonce"][2:])}
    sig = Account.sign_typed_data(acct.key, DOMAIN, TYPES, msg).signature.hex()
    sig = sig if sig.startswith("0x") else "0x" + sig
    return {"x402Version": 2, "payload": {"signature": sig, "authorization": auth}}, acct


REQ = {"scheme": "exact", "network": settle.NETWORK, "amount": "20000",
       "asset": settle.USDC, "payTo": PAY_TO}


class _Rpc:
    """Scriptable node: estimateGas outcome + tx flow, records every call."""

    def __init__(self, revert="", balance_wei=10 ** 18, receipt_status="0x1"):
        self.revert, self.balance_wei, self.receipt_status = revert, balance_wei, receipt_status
        self.calls = []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "eth_getBalance":
            return hex(self.balance_wei)
        if method == "eth_estimateGas":
            if self.revert:
                data = "0x08c379a0" + (32).to_bytes(32, "big").hex() + len(self.revert).to_bytes(32, "big").hex() \
                       + self.revert.encode().hex().ljust(64, "0")
                raise wallet.RpcError(method, {"code": 3, "message": "execution reverted", "data": data})
            return hex(70_000)
        if method == "eth_getTransactionCount":
            return "0x9"
        if method == "eth_gasPrice":
            return hex(200_000_000_000)
        if method == "eth_sendRawTransaction":
            return "0xselfsettled"
        if method == "eth_getTransactionReceipt":
            return {"status": self.receipt_status, "gasUsed": hex(68_000), "logs": []}
        raise AssertionError(method)


# ------------------------------------------------------------- encoding ----

def test_calldata_layout_and_signature_split():
    payload, acct = _signed()
    auth = payload["payload"]["authorization"]
    data = settle.encode_transfer_with_authorization(auth, payload["payload"]["signature"])
    assert data.startswith("0x" + settle.SEL_TWA_VRS)
    body = bytes.fromhex(data[10:])
    assert len(body) == 9 * 32
    assert body[12:32].hex() == acct.address[2:].lower()
    assert body[44:64].hex() == PAY_TO[2:]
    assert int.from_bytes(body[64:96], "big") == 20000
    assert int.from_bytes(body[128:160], "big") == int(auth["validBefore"])
    assert body[160:192].hex() == auth["nonce"][2:]
    assert int.from_bytes(body[192:224], "big") in (27, 28)
    with pytest.raises(ValueError):
        settle.encode_transfer_with_authorization(auth, "0x1234")


# ----------------------------------------------------------- structural ----

def test_structural_rejections():
    payload, _ = _signed(to="0x" + "11" * 20)
    assert settle.verify(payload, REQ)["invalidReason"] == "authorization.to != payTo"
    payload, _ = _signed(value=19999)
    assert "below required" in settle.verify(payload, REQ)["invalidReason"]
    payload, _ = _signed(valid_before=int(time.time()) + 5)
    assert settle.verify(payload, REQ)["invalidReason"] == "authorization_expired"
    payload, _ = _signed(valid_after=int(time.time()) + 100)
    assert settle.verify(payload, REQ)["invalidReason"] == "authorization_not_yet_valid"
    payload, _ = _signed()
    assert "unsupported asset" in settle.verify(payload, {**REQ, "asset": "0x" + "22" * 20})["invalidReason"]
    payload["payload"]["signature"] = "0xzz"
    assert "malformed" in settle.verify(payload, REQ)["invalidReason"]


# --------------------------------------------------------------- verify ----

def test_verify_uses_simulation_and_classifies_reverts():
    payload, acct = _signed()
    ok = settle.verify(payload, REQ, rpc_fn=_Rpc())
    assert ok["isValid"] and ok["payer"] == acct.address.lower() and ok["settler"] == "self"
    for reason, code in (("ERC20: transfer amount exceeds balance", "insufficient_funds"),
                         ("ECRecover: invalid signature 'v' value", "invalid_signature"),
                         ("FiatTokenV2: authorization is used or canceled", "nonce_already_used"),
                         ("FiatTokenV2: authorization is expired", "authorization_expired")):
        r = settle.verify(payload, REQ, rpc_fn=_Rpc(revert=reason))
        assert not r["isValid"] and r["invalidReason"] == code, reason


def test_verify_without_wallet_key(monkeypatch):
    monkeypatch.delenv("CELO_REGISTRAR_KEY")
    monkeypatch.delenv("ZEROG_REGISTRAR_KEY", raising=False)
    payload, _ = _signed()
    assert settle.verify(payload, REQ)["invalidReason"] == "self-settler has no wallet"


# --------------------------------------------------------------- settle ----

def test_settle_broadcasts_only_after_clean_simulation():
    payload, acct = _signed()
    node = _Rpc()
    r = settle.settle(payload, REQ, rpc_fn=node)
    assert r["success"] and r["transaction"] == "0xselfsettled" and r["network"] == settle.NETWORK
    assert r["payer"] == acct.address.lower() and r["gas_used"] == 68_000
    assert node.calls.index("eth_estimateGas") < node.calls.index("eth_sendRawTransaction")


def test_settle_never_broadcasts_a_reverting_call():
    payload, _ = _signed()
    node = _Rpc(revert="ERC20: transfer amount exceeds balance")
    r = settle.settle(payload, REQ, rpc_fn=node)
    assert not r["success"] and r["errorReason"] == "insufficient_funds"
    assert "eth_sendRawTransaction" not in node.calls


def test_settle_refuses_when_registrar_unfunded():
    payload, _ = _signed()
    node = _Rpc(balance_wei=10 ** 15)                  # 0.001 CELO
    r = settle.settle(payload, REQ, rpc_fn=node)
    assert not r["success"] and "unfunded" in r["errorReason"]
    assert "eth_sendRawTransaction" not in node.calls


def test_settle_reports_onchain_revert():
    payload, _ = _signed()
    r = settle.settle(payload, REQ, rpc_fn=_Rpc(receipt_status="0x0"))
    assert not r["success"] and "reverted" in r["errorReason"]


def test_wallet_serializes_sends(monkeypatch):
    """Two concurrent sends must not interleave nonce/broadcast (one key, one chain)."""
    import threading
    order = []
    node = _Rpc()
    orig = node.__call__

    def slow(method, params):
        if method == "eth_getTransactionCount":
            order.append("nonce"); time.sleep(0.05)
        if method == "eth_sendRawTransaction":
            order.append("send")
        return orig(method, params)
    ts = [threading.Thread(target=lambda: wallet.send_and_wait(settle.USDC, "0x" + "00" * 4, rpc_fn=slow))
          for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert order == ["nonce", "send", "nonce", "send"]
