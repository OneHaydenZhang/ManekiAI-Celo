"""The Celo layer's one hot wallet — the REGISTRAR.

It pays gas for two things and never holds user funds: ERC-8004 registrations
(agentid.py) and, when no facilitator API key is configured, self-settled
x402 payments (settle.py — it submits the buyer's signed EIP-3009
authorization; the USDC moves buyer → treasury, the registrar only pays gas).

One key, one nonce chain: every send goes through `_send_lock`, so a
registration and a settlement can never race each other into a nonce
collision. Balance reads are cached for 60s.

Key resolution: CELO_REGISTRAR_KEY, else ZEROG_REGISTRAR_KEY (one EVM key
works on every chain). No key → address() is '' and nothing can be sent.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

RPC_URLS = ["https://forno.celo.org", "https://celo.drpc.org"]
CHAIN_ID = 42220
MIN_CELO = 0.05                 # absolute floor; funded() also requires one tx worth of gas
FLOOR_GAS = 250_000             # one registration incl. the 1.3× estimate margin
_price_cache: Dict[str, Any] = {"at": 0.0, "wei": 0}
RECEIPT_TIMEOUT_S = 90
RECEIPT_POLL_S = 2.0

_send_lock = threading.Lock()
_bal_cache: Dict[str, Any] = {"at": 0.0, "wei": None}

RpcFn = Callable[[str, list], Any]


def key() -> str:
    return (os.environ.get("CELO_REGISTRAR_KEY", "").strip()
            or os.environ.get("ZEROG_REGISTRAR_KEY", "").strip())


def sending_enabled() -> bool:
    """False only when this host is explicitly told not to broadcast from the
    registrar key (CELO_REGISTRAR_ENABLED=0/false/off). Default true.

    Exists because the SAME registrar key can be configured on more than one
    host (e.g. a preview box and, later, production) — `_send_lock` below only
    serializes sends WITHIN one process, so two hosts broadcasting at once
    could both read the same 'pending' nonce and race. There is no shared
    nonce store to lock across hosts, so the safe default is: designate one
    host as the sender and set this to '0' everywhere else before ever running
    this branch on a second host concurrently."""
    return os.environ.get("CELO_REGISTRAR_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")


def address() -> str:
    k = key()
    if len(k) < 32:
        return ""
    try:
        from eth_account import Account
        return Account.from_key(k).address
    except Exception:
        return ""


class ReceiptTimeout(RuntimeError):
    """Broadcast succeeded but no receipt within the wait — the tx may still
    land. Carries the hash so callers can persist and finalise it later."""

    def __init__(self, txhash: str):
        super().__init__(f"receipt timeout for {txhash}")
        self.txhash = txhash


def receipt_of(txhash: str, rpc_fn: Optional[RpcFn] = None) -> Optional[Dict[str, Any]]:
    """Receipt for a hash, None while pending; raises if the tx is unknown to
    the node (dropped) so callers can decide to re-send."""
    call = rpc_fn or rpc
    rec = call("eth_getTransactionReceipt", [txhash])
    if rec:
        return rec
    if call("eth_getTransactionByHash", [txhash]) is None:
        raise LookupError(f"tx unknown to the node: {txhash}")
    return None


class RpcError(RuntimeError):
    """The node ANSWERED with a JSON-RPC error (e.g. a revert) — not a transport
    failure, so it must not fall through to the next gateway."""

    def __init__(self, method: str, error: Any):
        super().__init__(f"{method}: {error}")
        self.error = error


def rpc(method: str, params: list) -> Any:
    """JSON-RPC against the first gateway that answers (forno, then dRPC).
    Transport trouble → next gateway; a JSON-RPC error → RpcError right away
    (dRPC wraps reverts in HTTP 400, so the body is parsed before the status)."""
    last: Exception | None = None
    for url in RPC_URLS:
        try:
            r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params}, timeout=20.0)
            try:
                body = r.json() or {}
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("error"):
                raise RpcError(method, body["error"])
            r.raise_for_status()
            return body.get("result") if isinstance(body, dict) else None
        except RpcError:
            raise
        except Exception as e:
            last = e
    raise RuntimeError(f"celo rpc failed on all gateways: {last!r}")


def balance_celo(rpc_fn: Optional[RpcFn] = None) -> Optional[float]:
    """Registrar CELO balance (60s cache). None = read failed (unknown)."""
    addr = address()
    if not addr:
        return None
    now = time.time()
    if _bal_cache["wei"] is not None and now - _bal_cache["at"] < 60:
        return _bal_cache["wei"] / 1e18
    try:
        wei = int((rpc_fn or rpc)("eth_getBalance", [addr, "latest"]), 16)
    except Exception:
        return None
    _bal_cache["wei"], _bal_cache["at"] = wei, now
    return wei / 1e18


def invalidate_balance() -> None:
    _bal_cache["at"] = 0.0


def gas_price_wei(rpc_fn: Optional[RpcFn] = None) -> int:
    """Current gas price (60s cache); 0 when unreadable."""
    now = time.time()
    if _price_cache["wei"] and now - _price_cache["at"] < 60:
        return int(_price_cache["wei"])
    try:
        wei = int((rpc_fn or rpc)("eth_gasPrice", []), 16)
    except Exception:
        return int(_price_cache["wei"] or 0)
    _price_cache["wei"], _price_cache["at"] = wei, now
    return wei


def gas_floor_celo(rpc_fn: Optional[RpcFn] = None) -> float:
    """What ONE registration-sized tx costs up front at today's price (×1.2
    price multiplier) — the wallet must hold at least this, else the node
    rejects the broadcast with 'insufficient funds' on every attempt."""
    gp = gas_price_wei(rpc_fn)
    return max(MIN_CELO, FLOOR_GAS * gp * 1.2 / 1e18) if gp else MIN_CELO


def funded(rpc_fn: Optional[RpcFn] = None, min_celo: Optional[float] = None) -> bool:
    """False only when we KNOW the balance is below the floor (the larger of
    MIN_CELO and one tx of gas at the current price); an unreadable balance
    is 'try anyway' so a flaky RPC never blocks the feature."""
    if not address():
        return False
    bal = balance_celo(rpc_fn)
    floor = gas_floor_celo(rpc_fn) if min_celo is None else min_celo
    return True if bal is None else bal >= floor


def revert_reason(err: Any) -> str:
    """Human reason out of a JSON-RPC revert error (Error(string) ABI)."""
    try:
        data = err.get("data") if isinstance(err, dict) else None
        if isinstance(data, dict):
            data = data.get("data") or data.get("originalError", {}).get("data")
        if isinstance(data, str) and data.startswith("0x08c379a0"):
            b = bytes.fromhex(data[10:])
            ln = int.from_bytes(b[32:64], "big")
            return b[64:64 + ln].decode(errors="replace")
        msg = (err.get("message") if isinstance(err, dict) else str(err)) or ""
        return str(msg)[:200]
    except Exception:
        return str(err)[:200]


def simulate(to: str, data: str, rpc_fn: Optional[RpcFn] = None,
             value: int = 0) -> Tuple[bool, str, int]:
    """eth_estimateGas from the registrar: (ok, reason, gas). A revert comes
    back as (False, reason, 0) — the tx must NOT be sent."""
    addr = address()
    call = {"from": addr, "to": to, "data": data}
    if value:
        call["value"] = hex(value)
    try:
        gas = int((rpc_fn or rpc)("eth_estimateGas", [call]), 16)
        return True, "", gas
    except RpcError as e:
        return False, revert_reason(e.error), 0
    except Exception as e:
        # an injected rpc_fn (tests) may raise plain exceptions carrying the
        # error dict; anything else is a transport failure, not a revert
        err = getattr(e, "error", None)
        if err is not None:
            return False, revert_reason(err), 0
        return False, f"rpc_unavailable: {str(e)[:120]}", 0


# keccak("authorizationState(address,bytes32)")[:4] — EIP-3009 view on FiatToken
_SEL_AUTH_STATE = "e94a0102"
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# keccak("AuthorizationUsed(address,bytes32)") — emitted by transferWithAuthorization
AUTH_USED_TOPIC = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"


def authorization_state(asset: str, payer: str, nonce: str, rpc_fn: Optional[RpcFn] = None) -> Optional[bool]:
    """True when the payer's EIP-3009 nonce is already used on `asset` — i.e. a
    settlement DID land even if the facilitator's reply was lost. None = unknown."""
    try:
        data = "0x" + _SEL_AUTH_STATE + payer.lower().replace("0x", "").rjust(64, "0") \
               + nonce.lower().replace("0x", "").rjust(64, "0")
        res = (rpc_fn or rpc)("eth_call", [{"to": asset, "data": data}, "latest"])
        return int(str(res or "0x0"), 16) == 1
    except Exception:
        return None


def find_settlement_tx(asset: str, payer: str, to: str, nonce: str, min_value: int,
                       rpc_fn: Optional[RpcFn] = None, blocks: int = 900) -> str:
    """The tx that settled ONE specific authorization: a Transfer(payer → to)
    on `asset` worth ≥ min_value whose receipt also carries
    AuthorizationUsed(payer, nonce). Never a previous sale, never a plain
    top-up. '' when not found (or the RPC fails)."""
    try:
        call = rpc_fn or rpc
        latest = int(call("eth_blockNumber", []), 16)
        p_topic = "0x" + payer.lower().replace("0x", "").rjust(64, "0")
        n_topic = "0x" + nonce.lower().replace("0x", "").rjust(64, "0")
        logs = call("eth_getLogs", [{
            "fromBlock": hex(max(0, latest - blocks)), "toBlock": hex(latest), "address": asset,
            "topics": [_TRANSFER_TOPIC, p_topic,
                       "0x" + to.lower().replace("0x", "").rjust(64, "0")]}]) or []
        for lg in reversed(logs):
            try:
                if int(str(lg.get("data") or "0x0"), 16) < int(min_value):
                    continue
            except (TypeError, ValueError):
                continue
            txh = str(lg.get("transactionHash") or "")
            if not txh:
                continue
            rec = call("eth_getTransactionReceipt", [txh]) or {}
            for l2 in rec.get("logs") or []:
                t = [str(x).lower() for x in (l2.get("topics") or [])]
                if (str(l2.get("address") or "").lower() == asset.lower() and len(t) >= 3
                        and t[0] == AUTH_USED_TOPIC and t[1] == p_topic and t[2] == n_topic):
                    return txh
        return ""
    except Exception:
        return ""


def bump_pending(txhash: str, rpc_fn: Optional[RpcFn] = None, bump: float = 1.15) -> str:
    """Replace-by-fee for a stuck registrar tx: re-sign the SAME nonce/calldata
    with a higher gas price (a fresh nonce would only queue behind it). Returns
    the new hash; the old one when already mined; raises LookupError when the
    node no longer knows the tx (dropped → the caller may re-send)."""
    from eth_account import Account
    call = rpc_fn or rpc
    tx = call("eth_getTransactionByHash", [txhash])
    if tx is None:
        raise LookupError(f"tx unknown to the node: {txhash}")
    if tx.get("blockNumber"):
        return txhash
    acct = Account.from_key(key())
    old_price = int(str(tx.get("gasPrice") or "0x0"), 16)
    cur = gas_price_wei(call) or old_price
    price = max(int(old_price * bump) + 1, int(cur * 1.2))
    with _send_lock:
        signed = acct.sign_transaction({
            "chainId": CHAIN_ID, "nonce": int(str(tx["nonce"]), 16), "to": tx.get("to"),
            "value": int(str(tx.get("value") or "0x0"), 16), "gas": int(str(tx.get("gas")), 16),
            "gasPrice": price, "data": tx.get("input") or "0x"})
        raw = signed.raw_transaction.hex()
        raw = raw if raw.startswith("0x") else "0x" + raw
        return str(call("eth_sendRawTransaction", [raw]))


def send_and_wait(to: str, data: str, *, rpc_fn: Optional[RpcFn] = None, value: int = 0,
                  gas_fallback: int = 0, gas_mult: float = 1.3, price_mult: float = 1.2,
                  receipt_timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """Sign, broadcast and wait for the receipt of one legacy (type-0) tx from
    the registrar. Serialized on `_send_lock` (one nonce chain).

    gas_fallback=0 (settlements): a failed gas estimate means the call would
    revert → raise BEFORE broadcasting, never burn gas on a doomed tx.
    gas_fallback>0 (registrations): estimate failures fall back to that limit
    (the 0G registrar's proven behaviour).
    Raises RuntimeError on revert / receipt timeout; returns
    {txhash, receipt, gas_used}.
    """
    if not sending_enabled():
        raise RuntimeError("registrar sending disabled on this host (CELO_REGISTRAR_ENABLED=0)")
    from eth_account import Account
    from eth_utils import to_checksum_address
    call = rpc_fn or rpc
    acct = Account.from_key(key())
    to = to_checksum_address(to)          # eth_account rejects a wrong-case mixed address
    with _send_lock:
        ok, reason, gas = simulate(to, data, call, value=value)
        if not ok:
            if gas_fallback <= 0:
                raise RuntimeError(f"would revert: {reason}")
            gas = gas_fallback
        else:
            gas = int(gas * gas_mult)
        nonce = int(call("eth_getTransactionCount", [acct.address, "pending"]), 16)
        gas_price = int(call("eth_gasPrice", []), 16)
        tx = {"chainId": CHAIN_ID, "nonce": nonce, "to": to, "value": value,
              "gas": gas, "gasPrice": int(gas_price * price_mult), "data": data}
        signed = acct.sign_transaction(tx)
        raw = signed.raw_transaction.hex()
        raw = raw if raw.startswith("0x") else "0x" + raw
        txh = call("eth_sendRawTransaction", [raw])
        invalidate_balance()
    receipt = None
    # module constants read at CALL time (tests shorten them). After the
    # broadcast NOTHING may lose the hash: a failing poll is retried until the
    # deadline and the only ways out are a receipt or ReceiptTimeout(txh).
    deadline = time.time() + (RECEIPT_TIMEOUT_S if receipt_timeout_s is None else receipt_timeout_s)
    while time.time() < deadline:
        try:
            receipt = call("eth_getTransactionReceipt", [txh])
        except Exception:
            receipt = None
        if receipt:
            break
        time.sleep(RECEIPT_POLL_S)
    if not receipt:
        raise ReceiptTimeout(txh)
    if (receipt.get("status") or "").lower() != "0x1":
        raise RuntimeError(f"tx reverted on-chain: {txh}")
    try:
        gas_used = int(str(receipt.get("gasUsed") or "0x0"), 16)
    except (TypeError, ValueError):
        gas_used = 0
    return {"txhash": txh, "receipt": receipt, "gas_used": gas_used}
