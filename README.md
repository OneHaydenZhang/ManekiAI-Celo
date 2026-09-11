# ManekiAI × Celo — Agents at Work

**ManekiAI** runs autonomous LLM trading agents on Hyperliquid (US-stock perpetuals, real fills,
real P&L). This package is the Celo layer built for the **Celo "Agents at Work" hackathon**
(Aug 27 – Sep 14, 2026):

| Piece | What it does on Celo mainnet (42220) |
|---|---|
| `chain.py` | **Stablecoin Gas top-ups** — users fuel their agents with USDC / USD₮ / USDm / USA₮ on Celo. Pure `eth_getLogs` scanner against the registered token contracts (forno, 5,000-block chunks; dRPC fallback), receipt-gated, idempotent per tx hash. |
| `agentid.py` | **ERC-8004 identity for every live agent** — `register(agentURI)` on the Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`, plus the platform **"ManekiAI Analyst"** agent. Public registration-v1 cards at `/api/agent-card/{code}` carry both 0G and Celo registrations, the x402 service endpoint, `supportedTrust: ["reputation"]`, `x402Support`, `active`. |
| `x402.py` | **x402 v2 seller** — `PAYMENT-REQUIRED` challenge (USDC and USA₮ accepted), facilitator `POST /verify` → content → `POST /settle` (api.x402.celo.org, EIP-3009 `transferWithAuthorization`, gas paid by the facilitator), a permanent payment ledger (`x402_payments`, payer+nonce unique), and a **70 % revenue share to the agent's owner** as Gas. |
| `routes.py` | Public, login-free endpoints: `POST /api/x402/chat` (Ask ManekiAI · $0.02), `GET /api/x402/brief?symbol=` (shared 10-minute brief · $0.01), `GET /api/x402/agents/{code}/insight` (a live agent's latest decision · $0.05), free `GET /api/x402/config`, `GET /api/x402/catalog` and `GET /api/x402/activity` (public settlement / registration / deposit summary, 60 s cache). |
| `web/arena.html` | **Agent Arena** (`/arena`) — anyone with USDC or USA₮ on Celo connects a wallet, asks the analyst or unlocks an agent's insight; the wallet signs, the facilitator settles, the page shows the Celoscan link. |

Live: https://manekiai.io/arena · analyst card: https://manekiai.io/api/agent-card/maneki-analyst ·
activity: https://manekiai.io/api/x402/activity

## Why this is "agents at work"

1. **The agents genuinely work** — each one trades 24/7 on Hyperliquid; success rate, volume and P&L are tracked.
2. **They have an identity** — an ERC-8004 Agent ID on Celo, resolvable to a public card.
3. **They run on stablecoins** — Gas is bought with USDC / USD₮ / USDm / USA₮ on Celo ($1 = 1,000 Gas).
4. **They earn** — every insight sold over x402 pays the owner a share, settled in USDC (or USA₮) on Celo.
5. **They can be rated** — buyers may post feedback to the Reputation Registry (`0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`); ERC-8004 forbids owner/operator self-feedback, and the platform is the owner, so only real clients rate.

## x402 flow (v2, HTTP transport)

```
client ──GET /api/x402/agents/A-XXXX/insight──────────────▶ server
       ◀─ 402 · PAYMENT-REQUIRED: base64 {x402Version:2, resource, accepts:[
             {scheme:"exact", network:"eip155:42220", amount:"50000", asset:USDC, payTo, extra:{name:"USDC",version:"2"}},
             {scheme:"exact", network:"eip155:42220", amount:"50000", asset:USA₮, payTo, extra:{name:"Tether America USD",version:"1"}}]}
client picks the entry whose asset it holds ≥ amount of (USDC preferred) and signs
       EIP-712 TransferWithAuthorization (domain = extra.name / extra.version / 42220 / asset; no gas)
client ──GET … · PAYMENT-SIGNATURE: base64 {x402Version:2, accepted:<chosen entry>, payload:{signature, authorization}}─▶
server ──POST facilitator/verify ─▶ ok ─▶ builds the content ─▶ POST facilitator/settle (X-API-Key)
       ◀─ 200 · PAYMENT-RESPONSE: base64 {success, transaction, network} · body {…, payment:{tx, explorer}}
```

Content failures are never charged (no settle); settlement failures never leak content; a replayed
payer+nonce is refused before any work.

### Accepted assets

| Asset | Contract (Celo 42220) | EIP-712 domain | Used for |
|---|---|---|---|
| USDC | `0xcEBA9300f2b948710d2653dD7B07f33A8B32118C` | `USDC` / `2` | Gas top-ups, x402 (first `accepts[]` entry) |
| USD₮ (USDT) | `0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e` | — | Gas top-ups |
| USDm | registered lane token | — | Gas top-ups |
| **USA₮** (Tether America USD) | `0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771` · 6 decimals | `Tether America USD` / `1` | Gas top-ups **and** x402 (second `accepts[]` entry) |

### Settlement paths

* **Default — official Celo facilitator** (`X402_SETTLER=facilitator`, `https://api.x402.celo.org`).
  Needs an API key from https://x402.celo.org (connect wallet → Create API key) in `X402_API_KEY`.
  The facilitator verifies the EIP-3009 authorization, broadcasts it and pays the gas; our server
  only ever sees a signature. This is the path the hackathon leaderboard counts.
* **Optional — self-settlement** (`X402_SETTLER=self`). Our registrar wallet broadcasts the buyer's
  `transferWithAuthorization` itself (the buyer's signature is all EIP-3009 needs, so this is
  spec-legal and the buyer still pays no gas). **Documented fallback only**: the leaderboard may not
  count builder-broadcast settlements as facilitator settlements, so it exists to keep the Arena
  working if the facilitator is unreachable, never as the default.
* **Off** (`X402_SETTLER=off`): every paid endpoint answers 503 and nothing is charged.

Selector note for the self-settlement calldata: `0xcf092995` is the **`bytes signature`** variant of
`transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)`; `0xe3ee160e` is the
**`v,r,s`** variant `transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)`.
Both take the same EIP-3009 authorization; only the signature encoding differs. Nothing in this package
calls `receiveWithAuthorization` (`0xef55bec6`) — the buyer authorizes a plain transfer to `payTo`.

### Costs (Celo mainnet, ≈200 gwei)

| Action | Gas | CELO | Who pays |
|---|---|---|---|
| ERC-8004 `register(agentURI)` | ≈180k | ≈0.037 CELO | our registrar wallet (`CELO_REGISTRAR_KEY`) |
| x402 settlement via facilitator (`transferWithAuthorization`) | ≈85k | ≈0.017 CELO | the facilitator — billed as ≈$0.001 facilitator credit per settlement once the free credits are used |
| x402 self-settlement | ≈85k | ≈0.017 CELO | our registrar wallet |
| Buyer (Arena) | 0 | 0 | signs only, never broadcasts |

## Configuration (all default-off)

| Env | Meaning |
|---|---|
| `CELO_TREASURY_ADDRESS` | Receive-only address for top-ups **and** the x402 `payTo`. Unset → lane hidden. Admin console can override. |
| `CELO_REGISTRAR_KEY` (or `ZEROG_REGISTRAR_KEY`) | Private key of the wallet that pays registration gas (≈0.037 CELO each; registration is skipped below 0.05 CELO and backs off 10 min after a failure). Unset → no registrations. `CELO_AGENTID_ENABLED=0` kills the feature. |
| `X402_API_KEY` | Facilitator API key from https://x402.celo.org. Unset (with the facilitator settler) → every paid endpoint answers 503. |
| `X402_SETTLER` | `facilitator` (default) · `self` (fallback, see *Settlement paths*) · `off`. |
| `X402_PAY_TO`, `X402_PRICES_JSON`, `X402_OWNER_SHARE`, `X402_FACILITATOR_URL`, `X402_ENABLED` | Optional overrides (payTo defaults to the treasury; prices bounded to $0.001–$10; share 0.70). |
| `MANEKI_PUBLIC_BASE` | Public origin used in agent URIs and `resource.url` (default `https://manekiai.io`). |

## Host integration points (one-liners)

* `services/chains.py` — provider `"celo"` + the default `CELO` lane (USDC / USD₮ / USDm / USA₮, `native:false`).
* `db.py` — additive agent columns `celo_agent_id`, `celo_agent_tx`, `celo_registered_at`, `x402_sell`.
* `handlers/routes.py` — create/edit hooks call `agentid.maybe_register_async`; `/api/agent-card/{code}` serves the v1 cards.
* `handlers/admin_routes.py` — treasury publish, `/celo/status`, `/celo/register-platform`, `/celo/register-all`, `/x402/payments`.
* `app.py` — mounts `/api/x402/*`, `/arena`, `/celo/*`.
* `web/app.js` — "Get USDC on Celo" bridge card (LI.FI → Celo 42220), Celo Agent ID chip, the per-agent "sell insights" switch.

## Tests

In the host repository: `tests/test_celo_deposit.py`, `tests/test_celo_agentid.py`, `tests/test_x402.py`,
`tests/test_x402_selfsettle.py` (registry, chunked scanning, crediting, registration, cards, wire format,
ledger, revenue share, full 402 → verify → content → settle flow with the facilitator and the LLM mocked,
self-settlement calldata). Run `python -m pytest tests/ -q` from the host root.

## Built during the hackathon

Everything in this repository was written after the last pre-hackathon host commit `36acfac`
(2026-08-31, HL rejected-order fix — unrelated to Celo). Host commits `36acfac..HEAD` on
`hackathon/2026-09` (Asia/Shanghai time):

| Date | Host | Subject |
|---|---|---|
| 2026-09-11 01:26 | `cb66cfe` | add: Celo Agents-at-Work layer — USDC/USD₮/USDm Gas lane, ERC-8004 identity for every agent, x402 seller + Arena API (`auto_service/celo`) |
| 2026-09-11 01:26 | `f303039` | add: Celo front-end — Arena link, Get-USDC-on-Celo bridge card (LI.FI → 42220), Celo Agent ID chip, per-agent 'sell insights' switch, admin Celo panel |
| 2026-09-11 01:27 | `44dd477` | docs: Celo Agents-at-Work plan / submission draft + TEST_VERSIONS 2.74 + public-repo sync script |
| 2026-09-11 11:16 | `6f2edc7` | fix(celo): review fixes — unfunded-registrar guard + edit-hook backoff, register-all runs in background, owner share keyed on nonce when settle returns no tx |
| 2026-09-11 11:20 | `6d00dfa` | docs: 2026-09-10/11 iteration notes + TEST_VERSIONS 2.74 correction (production rolled back, preview host deployed) |
| 2026-09-11 → | … | USA₮ as second x402 asset, self-settlement fallback, `/api/x402/activity`, Arena error copy, public-repo replay + git hook (this and later commits) |

**Public-repo history = host history replayed.** This repository is a filtered mirror of the private
host repo: `scripts/publish_celo_public.sh --replay` walks every host commit since `36acfac` that
touches `auto_service/celo`, the Celo/x402 tests or `docs/hackathon`, checks those paths out at that
commit and commits them here with the **original author date and subject**, suffixed `(host <hash>)`.
Day-to-day commits arrive through a `post-commit` hook (`sync: <subject> (host <hash>)`). Commit
timestamps here are therefore the real ones, not the time of the mirror run.

## Verified live (2026-09-10 / 11)

* Celo `chainId 0xa4ec`; forno `eth_getLogs` cap = 5,000 blocks; `celo.drpc.org` fallback.
* Identity Registry `0x8004A169…a432` on Celo — same address as on 0G (deterministic deployment).
* Facilitator `/supported`: `{x402Version:2, scheme:"exact", network:"eip155:42220"}`.
* USDC `0xcEBA9300f2b948710d2653dD7B07f33A8B32118C`: `name()="USDC"`, `version()="2"`.
* USA₮ `0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771`: 6 decimals, EIP-712 domain `Tether America USD` / `1` (as configured in `/api/x402/config` → `assets.USAT`).
* Preview host (GCP, `http://34.68.151.4`): `/api/x402/config` and `/arena` reachable; registration cleanly skipped while the registrar holds 0 CELO.

## Safety

No private key ever leaves the server env; the treasury is receive-only; users only sign
(deposits: a plain ERC-20 transfer from their own wallet; x402: an off-chain EIP-3009 authorization).
Errors shown to buyers are neutral and carry a trace id; details stay in the operator log.
