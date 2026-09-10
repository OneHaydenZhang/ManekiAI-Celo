"""ManekiAI × Celo — the "Agents at Work" integration layer.

Everything Celo-specific lives in this package so it can be published as its
own repository (git subtree of `auto_service/celo/`) while the host app keeps
one-line glue points:

  chain.py    Celo mainnet stablecoin deposit provider (USDC / USD₮ / USDm →
              agent Gas), plugged into the multi-chain registry.
  agentid.py  ERC-8004 identity for every agent on Celo (Identity Registry
              0x8004A169…a432) + the platform "Analyst" agent + public
              registration-v1 agent cards.
  x402.py     x402 v2 seller: PaymentRequired challenge, facilitator
              verify/settle, a permanent payment ledger and owner revenue share.
  routes.py   Public pay-per-request endpoints (Ask ManekiAI, symbol brief,
              agent insight, catalog) + the admin backfill endpoints.
  web/        The standalone "Agent Arena" page (no login, wallet pays in USDC).

Defaults are safe: with no treasury the deposit lane is hidden, with no
registrar key nothing is registered, with no facilitator API key every x402
endpoint answers 503 — the host app behaves exactly as before.
"""
