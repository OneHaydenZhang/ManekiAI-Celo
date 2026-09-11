"""Celo mainnet (chain id 42220) — incoming ERC20 stablecoin transfer detection
for Gas top-ups. Registered in services/chains.py as provider "celo".

Same design as the HyperEVM / X Layer lanes: pure JSON-RPC `eth_getLogs`
against the REGISTERED stablecoin contracts only (USDC — Circle native,
USD₮ — Tether native, USDm — Mento dollar, formerly cUSD). ERC20 Transfer logs
only exist for transactions that SUCCEEDED, and nothing unregistered is ever
read. The lane is stablecoin-only on purpose: a native-CELO send emits no log,
so the registry entry hides the native token (`native: false`).

RPC endpoints are tried in order. Forno (the Celo Foundation gateway) caps
`eth_getLogs` at 5,000 blocks per query — measured live 2026-09-10 — so scans
are chunked; the public dRPC gateway is the fallback. Celo blocks are ~1s, so
the default user-refresh window (3,600 blocks ≈ 1h) is a single call.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import httpx

RPC_URL = "https://forno.celo.org"                    # Celo Foundation gateway
# (url, max eth_getLogs block range) — tried in order for scans.
SCAN_RPCS: List[Tuple[str, int]] = [
    (RPC_URL, 5000),
    ("https://celo.drpc.org", 5000),
]

CHAIN_ID = 42220                                      # 0xa4ec
EXPLORER = "https://celoscan.io"
EXPLORER_TX_PREFIX = "https://celoscan.io/tx/"

# Canonical stablecoins on Celo mainnet (verified on-chain 2026-09-10 via
# eth_call name()/decimals()). These are the registry DEFAULTS in
# services/chains.py — the operator can add more (e.g. USA₮) from the admin
# console without a code change.
USDC = "0xceba9300f2b948710d2653dd7b07f33a8b32118c"   # Circle native USDC, 6 dec, EIP-3009
USDT = "0x48065fbbe25f71c9282ddf5e1cd6d6a887483d5e"   # Tether USD (USD₮), 6 dec
USDM = "0x765de816845861e75a25fca122bb6898b8b1282a"   # Mento Dollar (ex-cUSD), 18 dec
USAT = "0xd2ab3c9a02dbbab236bfec45d1d755df4267f771"   # Tether America USD (USA₮), 6 dec, EIP-3009

# keccak("Transfer(address,address,uint256)") — the ERC20 transfer event.
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# What an EVM wallet needs to switch/add this chain + link out to txs. Served
# to the UI through chains.public_list() — the deposit frontend is generic
# over any registry chain with kind:"evm".
KIND = "evm"
EVM_PARAMS = {
    "chain_id_hex": "0xa4ec",
    "rpc": RPC_URL,
    "explorer": EXPLORER,
    "explorer_tx": EXPLORER_TX_PREFIX,
    "symbol": "CELO",
    "decimals": 18,
}


def _rpc(url: str, method: str, params: list) -> Any:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params}, timeout=15.0)
    r.raise_for_status()
    body = r.json() or {}
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def _get_logs(url: str, from_block: int, to_block: int,
              contracts: List[str], treasury_topic: str) -> List[Dict[str, Any]]:
    return _rpc(url, "eth_getLogs", [{
        "fromBlock": hex(from_block), "toBlock": hex(to_block),
        "address": contracts,
        "topics": [_TRANSFER_TOPIC, None, treasury_topic],
    }]) or []


def _scan_window(limit: int) -> int:
    """How far back to look, in blocks (~1s each on Celo). The default
    user-refresh limit (40) covers ~1h; the admin manual rescan passes a larger
    limit for deposits that slid out of the window."""
    return min(90_000, max(3600, int(limit) * 90))


def fetch_incoming(network: str, treasury: str, limit: int = 40,
                   erc20: Dict[str, Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    """Recent incoming ERC20 transfers TO `treasury` from the registered
    stablecoin contracts: [{txhash, sender_eth, amount, token, ts?, block}].

    Multiple matching logs inside one tx (contract batching) are summed per
    (txhash, token) — the ledger credits once per txhash, so the row must
    carry the tx's full amount. `network` is part of the registry provider
    signature; Celo has one mainnet."""
    t = (treasury or "").lower()
    by_contract = {str(tc.get("address") or "").lower(): (sym, int(tc.get("decimals") or 18))
                   for sym, tc in (erc20 or {}).items() if tc.get("address")}
    if not t or not by_contract:
        return []
    treasury_topic = "0x" + t[2:].rjust(64, "0")
    contracts = sorted(by_contract)
    window = _scan_window(limit)
    logs: List[Dict[str, Any]] | None = None
    last_err: Exception | None = None
    for url, max_range in SCAN_RPCS:
        try:
            latest = int(_rpc(url, "eth_blockNumber", []), 16)
            start = max(0, latest - window)
            got: List[Dict[str, Any]] = []
            frm = start
            while frm <= latest:
                to = min(frm + max_range - 1, latest)
                got.extend(_get_logs(url, frm, to, contracts, treasury_topic))
                frm = to + 1
            logs = got
            break
        except Exception as e:                     # try the next RPC
            last_err = e
    if logs is None:
        raise RuntimeError(f"celo scan failed on all RPCs: {last_err!r}")
    # (txhash, token) -> row, amounts summed across a tx's matching logs.
    # (txhash, logIndex) dedup guards against the same log arriving twice
    # (an overlapping window or a gateway retry must never double-count).
    rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
    seen_logs: set = set()
    for lg in logs:
        li = lg.get("logIndex")
        if li is not None:
            lkey = ((lg.get("transactionHash") or "").strip(), str(li))
            if lkey in seen_logs:
                continue
            seen_logs.add(lkey)
        contract = (lg.get("address") or "").lower()
        if contract not in by_contract:
            continue                               # unregistered — never credited
        sym, dec = by_contract[contract]
        topics = lg.get("topics") or []
        if len(topics) < 3:
            continue
        sender = "0x" + str(topics[1])[-40:].lower()
        if not sender or sender == t:
            continue
        txh = (lg.get("transactionHash") or "").strip()
        try:
            amt = int(str(lg.get("data") or "0x0"), 16) / (10 ** dec)
        except (TypeError, ValueError):
            continue
        if not txh or amt <= 0:
            continue
        key = (txh, sym)
        if key in rows:
            rows[key]["amount"] += amt
            continue
        row: Dict[str, Any] = {"txhash": txh, "sender_eth": sender, "amount": amt,
                               "token": sym}
        try:
            row["block"] = int(str(lg.get("blockNumber")), 16)
        except (TypeError, ValueError):
            pass
        try:   # non-standard field, present on some gateways (dRPC) — optional
            row["ts"] = int(str(lg.get("blockTimestamp")), 16)
        except (TypeError, ValueError):
            pass
        rows[key] = row
    return list(rows.values())


def tx_succeeded(txhash: str) -> bool:
    """Receipt check: only status 0x1 counts. A Transfer log already implies a
    succeeded tx, but the belt-and-braces receipt gate is one cheap call per
    NEW deposit. Any RPC trouble returns False — the scan retries later; we
    never credit on uncertainty."""
    for url, _ in SCAN_RPCS:
        try:
            rec = _rpc(url, "eth_getTransactionReceipt", [txhash]) or {}
            return (rec.get("status") or "").lower() == "0x1"
        except Exception:
            continue
    return False
