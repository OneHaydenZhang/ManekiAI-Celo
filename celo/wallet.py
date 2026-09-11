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
MIN_CELO = 0.05                 # below this the wallet is treated as unfunded
RECEIPT_TIMEOUT_S = 90
RECEIPT_POLL_S = 2.0

_send_lock = threading.Lock()
_bal_cache: Dict[str, Any] = {"at": 0.0, "wei": None}

RpcFn = Callable[[str, list], Any]


def key() -> str:
    return (os.environ.get("CELO_REGISTRAR_KEY", "").strip()
            or os.environ.get("ZEROG_REGISTRAR_KEY", "").strip())


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


def funded(rpc_fn: Optional[RpcFn] = None, min_celo: float = MIN_CELO) -> bool:
    """False only when we KNOW the balance is below the floor; an unreadable
    balance is 'try anyway' so a flaky RPC never blocks the feature."""
    if not address():
        return False
    bal = balance_celo(rpc_fn)
    return True if bal is None else bal >= min_celo


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


def find_transfer_tx(asset: str, payer: str, to: str, rpc_fn: Optional[RpcFn] = None,
                     blocks: int = 300) -> str:
    """Most recent Transfer(payer → to) on `asset` within the last `blocks`
    (~5 min on Celo): the tx hash of a settlement whose reply we lost. '' if
    none found or the RPC fails."""
    try:
        call = rpc_fn or rpc
        latest = int(call("eth_blockNumber", []), 16)
        logs = call("eth_getLogs", [{
            "fromBlock": hex(max(0, latest - blocks)), "toBlock": hex(latest), "address": asset,
            "topics": [_TRANSFER_TOPIC,
                       "0x" + payer.lower().replace("0x", "").rjust(64, "0"),
                       "0x" + to.lower().replace("0x", "").rjust(64, "0")]}]) or []
        return str((logs[-1].get("transactionHash") if logs else "") or "")
    except Exception:
        return ""


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
    # module constants read at CALL time (tests shorten them)
    deadline = time.time() + (RECEIPT_TIMEOUT_S if receipt_timeout_s is None else receipt_timeout_s)
    while time.time() < deadline:
        receipt = call("eth_getTransactionReceipt", [txh])
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
