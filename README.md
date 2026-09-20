# ManekiAI × Celo

**ManekiAI** runs autonomous LLM trading agents on Hyperliquid (US-stock perpetuals, real fills,
real P&L). **This package is the Celo side of it**, and it stands on its own: anyone with a wallet
can buy research here — no account, no exchange keys, no relationship with the trading product.

It started as our entry to the Celo "Agents at Work" hackathon (submitted 2026-09-14) and kept
going; what follows describes what actually runs today, not the plan we registered with.

**Live: https://celo.manekiai.io** — the Arena (`/arena`), the journeys and live data
(`/hackathon`), published reports (`/r/<id>`), and the public endpoints under `/api/x402/`.
Platform Analyst = ERC-8004 **#9837** on Celo
([registration tx](https://celoscan.io/tx/0x47530979efdfe12fd676bce859704c7065131c2a0913c2b9c3a41a72e8f4895f)).
That host serves the Celo product only; the trading app is not reachable from it.

## What you can buy, and how it is paid

| Piece | What it does on Celo mainnet (42220) |
|---|---|
| `routes.py` | The public, login-free surface. **Ask the analyst** (`POST /api/x402/chat`, $0.02) · **symbol brief** (`GET /api/x402/brief?symbol=`, $0.01, shared 10 min) · **configure and pay an agent to run for you** (`POST /api/x402/tasks`) · free reads: `/config`, `/activity`, `/catalog`, `/celo/health`. |
| `tasks.py` | **Paid research runs.** You pick a market, what to watch, how long and how often; the price is `unit × checks` computed from the request body ($0.02 a check monitoring, $0.04 researching — one run is $0.02 to $3.84, paid once). A background runner delivers one written report per check. Orders, payments, executions and results are one chain; a buyer can publish a finished run at a read-only link. |
| `x402.py` | **x402 v2 seller** — `PAYMENT-REQUIRED` challenge (USDC and USA₮), facilitator `/verify` → content → `/settle` (EIP-3009 `transferWithAuthorization`, gas paid by the facilitator), a permanent ledger (`x402_payments`, payer+nonce unique) and a 70 % revenue share to an agent's owner. Prices may be per request, which is how a run is quoted. |
| `native_pay.py` | **Paying in CELO**, without x402 at all — CELO supports neither EIP-3009 nor EIP-2612, so nothing can be signed over to a facilitator; we are the recipient, so the buyer simply transfers CELO to us and we watch for it. The rate comes from the Uniswap v3 pools on Celo (best of four fee tiers), and a payment is one verified transaction: receipt, a Transfer log payer → payTo, amount within tolerance, one tx per order. |
| `chain.py` | **Stablecoin Gas top-ups** for the trading product — `eth_getLogs` against the registered token contracts (forno, 5,000-block chunks; dRPC fallback), receipt-gated, idempotent per tx hash. |
| `agentid.py` | **ERC-8004 identity** on the Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`, for the platform "ManekiAI Analyst" and for live agents. Public registration-v1 cards at `/api/agent-card/{code}`. |
| `web/arena.html` | **The Arena** (`/arena`) — two entries: ask a question, or configure an agent and pay for its run. Holding only CELO? The page swaps it for USDC in place (Uniswap on Celo, best of four pools, exact-amount approval, a fresh quote at signing). |
| `web/guide.html` | **`/hackathon`** — the journeys, step by step, and the live data: what settled, by day, published reports anyone can open, and the ERC-8004 registrations. Every number is read from the public endpoints. |
| `web/report.html` | **`/r/<id>`** — a run its buyer chose to publish: the assignment, every delivered report and the settlement tx. Never the payer's address. |
| `tools/x402_buyer.py` | **Programmatic buyer** — a standalone x402 client (402 → pick asset → sign EIP-3009 → resend → content + settlement tx), so an agent can buy an agent's research without a browser. Key only via `X402_BUYER_KEY`. |

## Why this is "agents at work"

1. **The agents genuinely work** — each one trades 24/7 on Hyperliquid; success rate, volume and P&L are tracked.
2. **They have an identity** — an ERC-8004 Agent ID on Celo, resolvable to a public card.
3. **They run on stablecoins** — Gas is bought with USDC / USD₮ / USDm / USA₮ on Celo ($1 = 1,000 Gas).
4. **They earn** — a sale over x402 pays the agent's owner a share, settled in USDC (or USA₮) on Celo.
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
| USDC | `0xcebA9300f2b948710d2653dD7B07f33A8B32118C` | `USDC` / `2` | Gas top-ups, x402 (first `accepts[]` entry) |
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
| x402 settlement via facilitator (`transferWithAuthorization`) | ≈85k | ≈0.017 CELO | the facilitator — billed as facilitator credit per settlement once the free credits are used (≈$0.004 observed 2026-09; the official docs still quote $0.001) |
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
| `X402_TASKS_ENABLED` | paid research runs (`tasks.py`): quoting, the order tables and the background runner. Off → the endpoints answer 503. |
| `CELO_PAY_ENABLED` | paying in CELO (`native_pay.py`). Off → the CELO buttons never appear and `/api/x402/celo/*` answers 503. |

## Host integration points (one-liners)

* `services/chains.py` — provider `"celo"` + the default `CELO` lane (USDC / USD₮ / USDm / USA₮, `native:false`).
* `db.py` — additive agent columns `celo_agent_id`, `celo_agent_tx`, `celo_registered_at`, `x402_sell`.
* `handlers/routes.py` — create/edit hooks call `agentid.maybe_register_async`; `/api/agent-card/{code}` serves the v1 cards.
* `handlers/admin_routes.py` — treasury publish, `/celo/status`, `/celo/register-platform`, `/celo/register-all`, `/x402/payments`.
* `app.py` — mounts `/api/x402/*`, `/arena`, `/hackathon`, `/celo/*`.
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
| 2026-09-11 | `e2e4907` `c2a7ceb` `b8bc3e1` (merged `07b57a8`) | registration auto-pilot (registrar funded → every live agent + the Analyst mint themselves), USA₮ as second x402 asset, self-settlement fallback (`X402_SETTLER=self`, off by default), `/api/x402/activity`, Arena flow rework (balance pre-check, Get-USDC box, error copy, activity panel), public-repo replay + git hook |
| 2026-09-11 | `282911b` | review fixes: settlement lifecycle closed (`settle_pending` + on-chain finalizer + same-signature retry is free), exact reconciliation on `AuthorizationUsed(payer, nonce)`, hash-first ledger, fault-attributed cooldown, registrar RBF / revert handling, Cloudflare-gated IP trust, deposit-sweep switch |
| 2026-09-13 | `e39d5d3` `fa12274` | registration tx evidence filled (Analyst #9837 + 6 agents); paid-content redelivery now requires the EIP-3009 signature to recover to the payer (local EIP-712 ecrecover), sweep gate applied to unscoped admin rescans, registrar send switch (`CELO_REGISTRAR_ENABLED`) + consecutive-failure cap, Arena links follow the current host |

**Public-repo history = host history replayed.** This repository is a filtered mirror of the private
host repo: `scripts/publish_celo_public.sh --replay` walks every host commit since `36acfac` that
touches `auto_service/celo`, the Celo/x402 tests or `docs/celo`, checks those paths out at that
commit and commits them here with the **original author date and subject**, suffixed `(host <hash>)`.
Day-to-day commits arrive through a `post-commit` hook (`sync: <subject> (host <hash>)`). Commit
timestamps here are therefore the real ones, not the time of the mirror run.

## Verified on-chain

**It has settled for real.** The first purchases landed on 2026-09-19 and the lane has been used
since: current totals, the day-by-day shape, published reports and the recent settlement hashes are
all live at `https://celo.manekiai.io/api/x402/activity` (and rendered at `/hackathon`) — we do not
restate them here, because a number typed into a README goes stale the next day.

Facts checked directly against the chain while building:

* Celo `chainId 0xa4ec`; forno `eth_getLogs` cap = 5,000 blocks; `celo.drpc.org` fallback.
* Native CELO transfers emit **no logs at all** (receipts with zero entries), which is why paying in
  CELO goes through the token contract's `transfer` — a wallet's plain send could never be detected.
* The CELO token (`0x471EcE37…`, implementation `0xfea1b35f…`) implements plain ERC-20 only: no
  EIP-3009 `transferWithAuthorization`, no EIP-2612 `permit`. It cannot be paid over x402.
* Uniswap v3 on Celo: all four CELO/USDC fee tiers exist and their prices differ a lot (2 CELO quoted
  0.1704 USDC on the 0.01% pool vs 0.1289 on the 1% pool), so both the swap and the CELO price feed
  quote every tier and take the best.
* Identity Registry `0x8004A169…a432` on Celo — same address as on 0G (deterministic deployment).
* Facilitator `/supported`: `{x402Version:2, scheme:"exact", network:"eip155:42220"}`.
* USDC `0xcebA9300f2b948710d2653dD7B07f33A8B32118C`: `name()="USDC"`, `version()="2"`.
* USA₮ `0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771`: 6 decimals, EIP-712 domain `Tether America USD` / `1` (as configured in `/api/x402/config` → `assets.USAT`).
* Preview host `https://celo.manekiai.io`: `/api/x402/config`, `/arena`, `/api/x402/activity` reachable; registration was cleanly skipped while the registrar held 0 CELO, then on 2026-09-13 the auto-pilot minted the platform Analyst (#9837) and all 6 live agents (#9838–#9843) within one 2-minute tick of the wallet being funded, 0 failures.
* Facilitator `api.x402.celo.org` `/verify` answers a well-formed request with a structured `{"isValid":false,"invalidReason":…}` (an empty body gets a 502 — request-shape artefact, not an outage); facilitator signer `0x0d74D5Cefd2e7F24E623330ebE3d8D4cB45fFB48`.

## Safety

No private key ever leaves the server env; the treasury is receive-only; users only sign
(deposits: a plain ERC-20 transfer from their own wallet; x402: an off-chain EIP-3009 authorization).
Errors shown to buyers are neutral and carry a trace id; details stay in the operator log.
