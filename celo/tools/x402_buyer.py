#!/usr/bin/env python3
"""x402 buyer — a programmatic client for the ManekiAI Arena on Celo.

An "agent buys an agent's research" client: it does exactly what the Arena
page does in a browser, without a browser —

    GET/POST the paid endpoint        → 402 + PAYMENT-REQUIRED (the offer)
    pick the asset you hold           → USDC first, USA₮ second
    sign one EIP-3009 authorization   → EIP-712, off-chain, no gas
    resend with PAYMENT-SIGNATURE     → the server verifies, generates the
                                        content, settles via the Celo
                                        facilitator (which pays the gas)
    200 + PAYMENT-RESPONSE            → content + settlement tx (Celoscan)

Standalone: only `eth_account` (already in auto_service/requirements.txt)
plus the standard library. Never imports the host app.

Usage (the key comes ONLY from the environment, never from argv):

    export X402_BUYER_KEY=0x...            # a wallet holding USDC/USA₮ on Celo
    python auto_service/celo/tools/x402_buyer.py --base http://34.68.151.4 brief --symbol NVDA
    python auto_service/celo/tools/x402_buyer.py --base http://34.68.151.4 chat --symbol TSLA --message "Is TSLA setting up for a breakout?"
    python auto_service/celo/tools/x402_buyer.py --base http://34.68.151.4 insight --agent A-G1TFNJ
    python auto_service/celo/tools/x402_buyer.py --base http://34.68.151.4 brief --symbol NVDA --dry-run   # only fetch the offer

Exit codes: 0 paid & delivered · 2 offer only (--dry-run) · 3 payment
rejected (402: insufficient funds, bad signature, cooldown…) · 4 still
confirming after the retry budget (safe: retry later with the same nonce is
not possible from here, but the server serves an already-settled payment
free on the Arena) · 5 server/other error.

Rules reminder: purchases from the team's own wallets are a smoke test and a
demo of machine-to-machine payment — they do NOT count for the hackathon
leaderboard (only independent, non-builder-funded wallets do).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

EIP3009_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
    ]
}
ASSET_NAMES = {"USDC": "usdc", "USAT": "tether america usd", "USA₮": "tether america usd"}
CELO_RPC = "https://forno.celo.org"
EXPLORER_TX = "https://celoscan.io/tx/"


def b64e(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def b64d(s: str) -> Any:
    return json.loads(base64.b64decode(s).decode())


def http(method: str, url: str, body: Optional[Dict[str, Any]] = None,
         headers: Optional[Dict[str, str]] = None, timeout: int = 150) -> Tuple[int, Dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Accept": "application/json", "User-Agent": "maneki-x402-buyer/1.0"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, method=method, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, {k.lower(): v for k, v in r.headers.items()}, _json(raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, {k.lower(): v for k, v in e.headers.items()}, _json(raw)


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode() or "{}")
    except Exception:
        return {"_raw": raw[:400].decode(errors="replace")}


def balance_of(asset: str, account: str, rpc: str = CELO_RPC) -> Optional[int]:
    """ERC-20 balanceOf via eth_call — a courtesy pre-check like the Arena's."""
    data = "0x70a08231" + account[2:].lower().rjust(64, "0")
    try:
        st, _, d = http("POST", rpc, {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                      "params": [{"to": asset, "data": data}, "latest"]}, timeout=20)
        return int(d.get("result") or "0x0", 16) if st == 200 else None
    except Exception:
        return None


def pick_accept(accepts: List[Dict[str, Any]], want: str, account: str, check_balance: bool) -> Optional[Dict[str, Any]]:
    """Prefer the asset the caller asked for; with --check-balance, the first
    asset the wallet actually holds ≥ amount of wins (USDC before USA₮)."""
    want_name = ASSET_NAMES.get(want.upper(), want.lower())
    ordered = sorted(accepts, key=lambda a: 0 if str((a.get("extra") or {}).get("name", "")).lower() == want_name else 1)
    for acc in ordered:
        if check_balance:
            bal = balance_of(acc["asset"], account)
            if bal is not None and bal < int(str(acc["amount"])):
                continue
        return acc
    return None


def sign_authorization(key_hex: str, accept: Dict[str, Any], valid_s: int = 120,
                       nonce: Optional[str] = None) -> Tuple[Dict[str, Any], str, str]:
    """Build + sign the EIP-3009 TransferWithAuthorization for one offer entry.
    Returns (authorization-as-strings, signature hex, payer address)."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    acct = Account.from_key(key_hex)
    extra = accept.get("extra") or {}
    chain_id = int(str(accept.get("network", "eip155:42220")).split(":")[-1])
    value = int(str(accept["amount"]))
    valid_before = int(time.time()) + max(90, min(valid_s, int(accept.get("maxTimeoutSeconds") or valid_s)))
    nonce = nonce or ("0x" + secrets.token_hex(32))
    domain = {"name": str(extra.get("name") or ""), "version": str(extra.get("version") or ""),
              "chainId": chain_id, "verifyingContract": accept["asset"]}
    message = {"from": acct.address, "to": accept["payTo"], "value": value,
               "validAfter": 0, "validBefore": valid_before, "nonce": nonce}
    signable = encode_typed_data(domain_data=domain, message_types=EIP3009_TYPES, message_data=message)
    sig = Account.sign_message(signable, private_key=acct.key).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    auth = {"from": acct.address, "to": accept["payTo"], "value": str(value),
            "validAfter": "0", "validBefore": str(valid_before), "nonce": nonce}
    return auth, sig, acct.address


def build_payment_payload(key_hex: str, offer: Dict[str, Any], accept: Dict[str, Any],
                          valid_s: int = 120, nonce: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    auth, sig, payer = sign_authorization(key_hex, accept, valid_s=valid_s, nonce=nonce)
    return ({"x402Version": 2, "resource": offer.get("resource") or {}, "accepted": accept,
             "payload": {"signature": sig, "authorization": auth}}, payer)


def summarize(body: Dict[str, Any]) -> str:
    st = body.get("structured") or {}
    if body.get("product") == "insight" or body.get("decision"):
        dec = body.get("decision") or {}
        return (f"decision: {dec.get('action')} · confidence {dec.get('confidence')} · round {dec.get('round')}\n"
                f"reasoning: {str(dec.get('reasoning') or '')[:400]}")
    head = st.get("headline") or body.get("reply") or body.get("brief") or ""
    pts = "\n".join(f"  · {p.get('label')}: {p.get('text')}" for p in (st.get("points") or [])[:6])
    return f"{head}\n{pts}".strip()


def main(argv: Optional[List[str]] = None) -> int:
    # The global flags are accepted both before and after the sub-command
    # (`brief --symbol NVDA --dry-run` reads naturally); SUPPRESS keeps a
    # sub-parser from clobbering a value given before it.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--base", help="server origin (default: $X402_BASE or http://34.68.151.4)")
    common.add_argument("--asset", help="USDC (default) or USAT")
    common.add_argument("--check-balance", action="store_true", help="skip assets the wallet cannot cover (forno eth_call)")
    common.add_argument("--dry-run", action="store_true", help="fetch and print the 402 offer, sign nothing")
    common.add_argument("--json", action="store_true", help="print the raw response body")
    common.add_argument("--valid-s", type=int, help="authorization validity window in seconds (default 120; server needs ≥ 60 s left)")
    ap = argparse.ArgumentParser(description="x402 buyer for the ManekiAI Arena (Celo)", parents=[common])
    sub = ap.add_subparsers(dest="product", required=True)
    p = sub.add_parser("chat", help="Ask ManekiAI ($0.02)", parents=[common]); p.add_argument("--symbol", required=True); p.add_argument("--message", required=True)
    p = sub.add_parser("brief", help="symbol brief ($0.01)", parents=[common]); p.add_argument("--symbol", required=True)
    p = sub.add_parser("insight", help="an agent's latest decision ($0.05)", parents=[common]); p.add_argument("--agent", required=True, help="agent code, e.g. A-G1TFNJ")
    ns = ap.parse_args(argv)
    a = argparse.Namespace(base=getattr(ns, "base", os.environ.get("X402_BASE", "http://34.68.151.4")),
                           asset=getattr(ns, "asset", "USDC"), check_balance=getattr(ns, "check_balance", False),
                           dry_run=getattr(ns, "dry_run", False), json=getattr(ns, "json", False),
                           valid_s=getattr(ns, "valid_s", 120), product=ns.product,
                           symbol=getattr(ns, "symbol", ""), message=getattr(ns, "message", ""), agent=getattr(ns, "agent", ""))

    base = a.base.rstrip("/")
    if a.product == "chat":
        method, url, body = "POST", f"{base}/api/x402/chat", {"message": a.message, "symbol": a.symbol}
    elif a.product == "brief":
        method, url, body = "GET", f"{base}/api/x402/brief?symbol={a.symbol}", None
    else:
        method, url, body = "GET", f"{base}/api/x402/agents/{a.agent}/insight", None

    # 1. the offer
    st, hdr, d = http(method, url, body)
    if st != 402:
        print(f"[x402] expected 402, got {st}: {json.dumps(d)[:300]}")
        return 5 if st >= 500 else 3
    offer = b64d(hdr["payment-required"]) if hdr.get("payment-required") else d
    accepts = [x for x in (offer.get("accepts") or []) if x.get("asset") and x.get("amount") is not None]
    print(f"[x402] offer: {offer.get('resource', {}).get('url')} · "
          + " | ".join(f"{(x.get('extra') or {}).get('name')} {int(str(x['amount'])) / 1e6:.2f} → {x.get('payTo')}" for x in accepts))
    if a.dry_run or not accepts:
        return 2 if accepts else 5

    key = os.environ.get("X402_BUYER_KEY", "").strip()
    if not key:
        print("[x402] set X402_BUYER_KEY (hex private key of a wallet holding USDC/USA₮ on Celo) — never pass it on the command line")
        return 5
    from eth_account import Account
    payer = Account.from_key(key).address
    acc = pick_accept(accepts, a.asset, payer, a.check_balance)
    if not acc:
        print(f"[x402] wallet {payer} holds none of the offered assets in sufficient amount — nothing signed")
        return 3

    # 2. sign (off-chain) and resend
    payload, payer = build_payment_payload(key, offer, acc, valid_s=a.valid_s)
    print(f"[x402] signed {(acc.get('extra') or {}).get('name')} {int(str(acc['amount'])) / 1e6:.2f} from {payer} (no gas) · nonce {payload['payload']['authorization']['nonce'][:10]}…")
    headers = {"PAYMENT-SIGNATURE": b64e(payload)}
    st, hdr, d = http(method, url, body, headers)
    tries = 0
    while st == 202 and tries < 8:      # settlement still confirming — same signature, never charged twice
        wait = max(3, min(20, int(d.get("retry_after_s") or 8)))
        print(f"[x402] settle pending, retrying in {wait}s with the same signature…")
        time.sleep(wait)
        st, hdr, d = http(method, url, body, headers)
        tries += 1

    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    if st == 200:
        pay = d.get("payment") or {}
        print(f"[x402] PAID & DELIVERED · {pay.get('amount_usd')} {pay.get('asset')} · tx {pay.get('tx')} · {pay.get('explorer') or (EXPLORER_TX + str(pay.get('tx') or ''))}")
        if hdr.get("payment-response"):
            print(f"[x402] PAYMENT-RESPONSE: {json.dumps(b64d(hdr['payment-response']))}")
        if not a.json:
            print(summarize(d))
        return 0
    if st == 202:
        print("[x402] still confirming on Celo after the retry budget — the payment is safe; the server serves it once settled")
        return 4
    print(f"[x402] {st}: {d.get('error') or d.get('detail') or json.dumps(d)[:300]}"
          + (f" (ref {d['log_id']})" if d.get('log_id') else ""))
    return 3 if st in (402, 409, 429) else 5


if __name__ == "__main__":
    sys.exit(main())
