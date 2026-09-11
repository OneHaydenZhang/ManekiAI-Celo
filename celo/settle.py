"""Self-hosted x402 settlement for the `exact` EVM scheme on Celo.

The x402 facilitator is a convenience relayer, not a requirement of the
protocol: an `exact` payment is just an EIP-3009 `transferWithAuthorization`
signed by the buyer, and ANY funded wallet may submit it. This module lets the
resource server be its own facilitator — the registrar wallet pays ~70k gas
(≈ 0.015 CELO at 200 gwei) and the USDC moves buyer → payTo exactly as it would
through api.x402.celo.org.

Verified on Celo mainnet 2026-09-11 (eth_estimateGas with a real signature
from an unfunded key): USDC 0xcEBA…118C accepts both the (v,r,s) variant
(selector 0xe3ee160e) and the bytes variant; a valid signature reverts only
with "ERC20: transfer amount exceeds balance", a corrupted one with
"ECRecover: invalid signature 'v' value" — so the node's simulation is an
authoritative verifier.

verify(): structural checks + eth_estimateGas simulation from the registrar.
settle(): simulate again, then broadcast via wallet.send_and_wait (never
broadcasts a call that would revert). Both return the facilitator's response
shapes so routes.py does not care who settled.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from ..services import oplog
from . import wallet

ERR_UNAVAILABLE = "settlement temporarily unavailable"

USDC = "0xcebA9300f2b948710d2653dD7B07f33A8B32118C"   # EIP-55 checksum (the docs page mis-cases it)
USAT = "0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771"   # Tether America USD, 6 dec, EIP-3009 (verified 2026-09-11)
ASSETS = {USDC.lower(), USAT.lower()}
NETWORK = "eip155:42220"
# keccak("transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)")[:4]
SEL_TWA_VRS = "e3ee160e"
MIN_VALID_S = 30            # authorization must stay valid at least this long


def _w(hexstr: str) -> str:
    return hexstr.rjust(64, "0")


def _split_signature(sig_hex: str):
    s = sig_hex[2:] if sig_hex.startswith("0x") else sig_hex
    b = bytes.fromhex(s)
    if len(b) != 65:
        raise ValueError("signature must be 65 bytes")
    v = b[64]
    if v < 27:
        v += 27
    if v not in (27, 28):
        raise ValueError("invalid signature v")
    return v, b[:32], b[32:64]


def encode_transfer_with_authorization(auth: Dict[str, Any], signature_hex: str) -> str:
    """calldata for transferWithAuthorization(from,to,value,validAfter,validBefore,nonce,v,r,s)."""
    v, r, s = _split_signature(signature_hex)
    frm = str(auth["from"]).lower().replace("0x", "")
    to = str(auth["to"]).lower().replace("0x", "")
    nonce = str(auth["nonce"]).lower().replace("0x", "")
    if len(frm) != 40 or len(to) != 40 or len(nonce) != 64:
        raise ValueError("bad address or nonce length")
    return ("0x" + SEL_TWA_VRS + _w(frm) + _w(to)
            + _w(hex(int(str(auth["value"])))[2:])
            + _w(hex(int(str(auth["validAfter"])))[2:])
            + _w(hex(int(str(auth["validBefore"])))[2:])
            + nonce + _w(hex(v)[2:]) + r.hex() + s.hex())


def _classify(reason: str) -> str:
    r = (reason or "").lower()
    if "exceeds balance" in r or "insufficient" in r:
        return "insufficient_funds"
    if "signature" in r or "ecrecover" in r:
        return "invalid_signature"
    if "authorization is used" in r or "nonce" in r:
        return "nonce_already_used"
    if "not yet valid" in r:
        return "authorization_not_yet_valid"
    if "expired" in r:
        return "authorization_expired"
    if r.startswith("rpc_unavailable"):
        return "verifier_unavailable"
    return f"simulation_reverted: {reason[:80]}" if reason else "simulation_reverted"


def _structural(payload: Dict[str, Any], req: Dict[str, Any]) -> Optional[str]:
    p = payload.get("payload") or {}
    auth = p.get("authorization") or {}
    sig = str(p.get("signature") or "")
    try:
        if str(auth.get("to") or "").lower() != str(req["payTo"]).lower():
            return "authorization.to != payTo"
        if int(str(auth.get("value") or "0")) < int(req["amount"]):
            return "authorization.value below required amount"
        now = int(time.time())
        if int(str(auth.get("validAfter") or "0")) > now:
            return "authorization_not_yet_valid"
        if int(str(auth.get("validBefore") or "0")) < now + MIN_VALID_S:
            return "authorization_expired"
        nonce = str(auth.get("nonce") or "")
        if not (nonce.startswith("0x") and len(nonce) == 66):
            return "nonce must be 32 bytes hex"
        _split_signature(sig)
    except (TypeError, ValueError) as e:
        return f"malformed authorization: {e}"
    if str(req.get("asset") or "").lower() not in ASSETS or req.get("network") != NETWORK:
        return "unsupported asset/network for self-settlement"
    return None


def _calldata(payload: Dict[str, Any]) -> str:
    p = payload.get("payload") or {}
    return encode_transfer_with_authorization(p.get("authorization") or {}, str(p.get("signature") or ""))


def verify(payload: Dict[str, Any], req: Dict[str, Any], rpc_fn=None) -> Dict[str, Any]:
    payer = str(((payload.get("payload") or {}).get("authorization") or {}).get("from") or "").lower()
    bad = _structural(payload, req)
    if bad:
        return {"isValid": False, "invalidReason": bad, "payer": payer, "settler": "self"}
    if not wallet.address():
        return {"isValid": False, "invalidReason": "self-settler has no wallet", "payer": payer, "settler": "self"}
    try:
        data = _calldata(payload)
    except ValueError as e:
        return {"isValid": False, "invalidReason": f"malformed authorization: {e}", "payer": payer, "settler": "self"}
    ok, reason, gas = wallet.simulate(str(req["asset"]), data, rpc_fn)
    if not ok:
        code = _classify(reason)
        if code == "verifier_unavailable":
            oplog.error("x402.self_verify", reason[:300])
        return {"isValid": False, "invalidReason": code, "payer": payer, "settler": "self",
                "transport": code == "verifier_unavailable"}
    return {"isValid": True, "payer": payer, "settler": "self", "gas": gas}


def settle(payload: Dict[str, Any], req: Dict[str, Any], rpc_fn=None) -> Dict[str, Any]:
    payer = str(((payload.get("payload") or {}).get("authorization") or {}).get("from") or "").lower()
    base = {"success": False, "payer": payer, "transaction": "", "network": NETWORK, "settler": "self"}
    bad = _structural(payload, req)
    if bad:
        return {**base, "errorReason": bad}
    if not wallet.funded(rpc_fn):
        return {**base, "errorReason": "self-settler wallet unfunded (needs CELO for gas)"}
    try:
        data = _calldata(payload)
        r = wallet.send_and_wait(str(req["asset"]), data, rpc_fn=rpc_fn, gas_fallback=0)
    except ValueError as e:
        return {**base, "errorReason": f"malformed authorization: {e}"}
    except wallet.ReceiptTimeout as e:
        # Broadcast but unconfirmed: report the hash; the caller reconciles
        # via authorization_state before deciding.
        return {**base, "errorReason": "receipt_timeout", "transaction": e.txhash}
    except Exception as e:
        msg = str(e)
        if "would revert" in msg:
            return {**base, "errorReason": _classify(msg)}
        if "reverted on-chain" in msg:
            oplog.error("x402.self_settle", msg[:200])
            return {**base, "errorReason": "settlement_reverted", "transaction": msg.rsplit(" ", 1)[-1]}
        # transport / node trouble: the real text goes to the operator log only
        oplog.error("x402.self_settle", repr(e)[:300])
        return {**base, "errorReason": ERR_UNAVAILABLE, "transport": True}
    return {**base, "success": True, "transaction": r["txhash"], "gas_used": r.get("gas_used", 0)}
