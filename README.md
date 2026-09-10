# ManekiAI × Celo — Agents at Work

**ManekiAI** runs autonomous LLM trading agents on Hyperliquid (US-stock perpetuals, real fills,
real P&L). This package is the Celo layer built for the **Celo "Agents at Work" hackathon**
(Aug 27 – Sep 14, 2026):

| Piece | What it does on Celo mainnet (42220) |
|---|---|
| `chain.py` | **Stablecoin Gas top-ups** — users fuel their agents with USDC / USD₮ / USDm on Celo. Pure `eth_getLogs` scanner against the registered token contracts (forno, 5,000-block chunks; dRPC fallback), receipt-gated, idempotent per tx hash. |
| `agentid.py` | **ERC-8004 identity for every live agent** — `register(agentURI)` on the Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`, plus the platform **"ManekiAI Analyst"** agent. Public registration-v1 cards at `/api/agent-card/{code}` carry both 0G and Celo registrations, the x402 service endpoint, `supportedTrust: ["reputation"]`, `x402Support`, `active`. |
| `x402.py` | **x402 v2 seller** — `PAYMENT-REQUIRED` challenge, facilitator `POST /verify` → content → `POST /settle` (api.x402.celo.org, EIP-3009 `transferWithAuthorization`, gas paid by the facilitator), a permanent payment ledger (`x402_payments`, payer+nonce unique), and a **70 % revenue share to the agent's owner** as Gas. |
| `routes.py` | Public, login-free endpoints: `POST /api/x402/chat` (Ask ManekiAI · $0.02), `GET /api/x402/brief?symbol=` (shared 10-minute brief · $0.01), `GET /api/x402/agents/{code}/insight` (a live agent's latest decision · $0.05), free `GET /api/x402/config` + `GET /api/x402/catalog`. |
| `web/arena.html` | **Agent Arena** (`/arena`) — anyone with USDC on Celo connects a wallet, asks the analyst or unlocks an agent's insight; the wallet signs, the facilitator settles, the page shows the Celoscan link. |

Live: https://manekiai.io/arena · analyst card: https://manekiai.io/api/agent-card/maneki-analyst

## Why this is "agents at work"

1. **The agents genuinely work** — each one trades 24/7 on Hyperliquid; success rate, volume and P&L are tracked.
2. **They have an identity** — an ERC-8004 Agent ID on Celo, resolvable to a public card.
3. **They run on stablecoins** — Gas is bought with USDC / USD₮ / USDm on Celo ($1 = 1,000 Gas).
4. **They earn** — every insight sold over x402 pays the owner a share, settled in USDC on Celo.
5. **They can be rated** — buyers may post feedback to the Reputation Registry (`0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`); ERC-8004 forbids owner/operator self-feedback, and the platform is the owner, so only real clients rate.

## x402 flow (v2, HTTP transport)

```
client ──GET /api/x402/agents/A-XXXX/insight──────────────▶ server
       ◀─ 402 · PAYMENT-REQUIRED: base64 {x402Version:2, resource, accepts:[{scheme:"exact",
             network:"eip155:42220", amount:"50000", asset:USDC, payTo, extra:{name:"USDC",version:"2"}}]}
client signs EIP-712 TransferWithAuthorization (no gas)
client ──GET … · PAYMENT-SIGNATURE: base64 {x402Version:2, accepted, payload:{signature, authorization}}─▶
server ──POST facilitator/verify ─▶ ok ─▶ builds the content ─▶ POST facilitator/settle (X-API-Key)
       ◀─ 200 · PAYMENT-RESPONSE: base64 {success, transaction, network} · body {…, payment:{tx, explorer}}
```

Content failures are never charged (no settle); settlement failures never leak content; a replayed
payer+nonce is refused before any work.

## Configuration (all default-off)

| Env | Meaning |
|---|---|
| `CELO_TREASURY_ADDRESS` | Receive-only address for top-ups **and** the x402 `payTo`. Unset → lane hidden. Admin console can override. |
| `CELO_REGISTRAR_KEY` (or `ZEROG_REGISTRAR_KEY`) | Private key of the wallet that pays registration gas (~0.04 CELO each). Unset → no registrations. `CELO_AGENTID_ENABLED=0` kills the feature. |
| `X402_API_KEY` | Facilitator API key from https://x402.celo.org (connect wallet → Create API key). Unset → every paid endpoint answers 503. |
| `X402_PAY_TO`, `X402_PRICES_JSON`, `X402_OWNER_SHARE`, `X402_FACILITATOR_URL`, `X402_ENABLED` | Optional overrides (payTo defaults to the treasury; prices bounded to $0.001–$10; share 0.70). |
| `MANEKI_PUBLIC_BASE` | Public origin used in agent URIs and `resource.url` (default `https://manekiai.io`). |

## Host integration points (one-liners)

* `services/chains.py` — provider `"celo"` + the default `CELO` lane (USDC / USD₮ / USDm, `native:false`).
* `db.py` — additive agent columns `celo_agent_id`, `celo_agent_tx`, `celo_registered_at`, `x402_sell`.
* `handlers/routes.py` — create/edit hooks call `agentid.maybe_register_async`; `/api/agent-card/{code}` serves the v1 cards.
* `handlers/admin_routes.py` — treasury publish, `/celo/status`, `/celo/register-platform`, `/celo/register-all`, `/x402/payments`.
* `app.py` — mounts `/api/x402/*`, `/arena`, `/celo/*`.
* `web/app.js` — "Get USDC on Celo" bridge card (LI.FI → Celo 42220), Celo Agent ID chip, the per-agent "sell insights" switch.

## Tests

In the host repository: `tests/test_celo_deposit.py`, `tests/test_celo_agentid.py`, `tests/test_x402.py`
(registry, chunked scanning, crediting, registration, cards, wire format, ledger, revenue share, full
402 → verify → content → settle flow with the facilitator and the LLM mocked). Run
`python -m pytest tests/ -q` from the host root.

## Verified live (2026-09-10)

* Celo `chainId 0xa4ec`; forno `eth_getLogs` cap = 5,000 blocks; `celo.drpc.org` fallback.
* Identity Registry `0x8004A169…a432` on Celo — same address as on 0G (deterministic deployment).
* Facilitator `/supported`: `{x402Version:2, scheme:"exact", network:"eip155:42220"}`.
* USDC `0xcEBA9300f2b948710d2653dD7B07f33A8B32118C`: `name()="USDC"`, `version()="2"`.

## Safety

No private key ever leaves the server env; the treasury is receive-only; users only sign
(deposits: a plain ERC-20 transfer from their own wallet; x402: an off-chain EIP-3009 authorization).
Errors shown to buyers are neutral and carry a trace id; details stay in the operator log.
