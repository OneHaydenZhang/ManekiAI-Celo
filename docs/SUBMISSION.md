# Celo「Agents at Work」提交材料（草稿 · 随进度更新）

> 截止 2026-09-14 09:00 GMT（北京 17:00）。赛事页 https://www.risein.com/celo/celo-agents-at-work-hackathon
> 方案：`docs/hackathon/CELO_AGENTS_AT_WORK_方案.md` · 代码：`auto_service/celo/`（公开仓库 ManekiAI-Celo 同步）

## 报名表字段

| 字段 | 值 | 状态 |
|---|---|---|
| Project | ManekiAI — AI trading agents that work for you, identified on Celo | ✓ |
| Public GitHub | https://github.com/OneHaydenZhang/ManekiAI-Celo | 待创建/同步 |
| ERC-8004 Agent ID（平台 Analyst） | `#TBD`（Celo Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`，注册 tx `TBD`） | 待注册钱包充 CELO 后铸造 |
| Agent 钱包地址 | 收款/x402 payTo：`0x26523f5cea5da5d9411749afefe741ba340f6566`（只收款）；注册钱包（付 gas）：`0xaf6fA147e8F85781196627765FcaFC1044F89308` | ✓ |
| Telegram handle | `@TBD`（用户填写） | 待填 |
| Primary track | Real World Adoption | ✓ |
| Secondary（一句话） | Stablecoin Adoption — Gas top-ups in USDC/USD₮/USDm on Celo + every Arena purchase is an x402 settlement in USDC; Judges' Favorite — ERC-8004 identity × x402 revenue share = agents that earn for their owners | ✓ |
| Distribution channel | Existing audience (manekiai.io users, X) + the public login-free Arena for the Celo community | ✓ |
| Demo | https://manekiai.io/arena · https://manekiai.io/api/agent-card/maneki-analyst | 部署后生效 |

## 链上证据（部署后逐项填 · `scripts/fill_celo_submission.py --activity <url|file>` 可自动回填）

- 平台 Analyst 注册 tx：`TBD`
- 用户 Agent 注册 tx（前 5 个）：`TBD`
- 首笔 Celo 稳定币充值 tx：`TBD`
- 首笔 x402 结算 tx（facilitator 签名者 `0x0d74D5Cefd2e7F24E623330ebE3d8D4cB45fFB48`）：`TBD`
- 公开活动端点（免登录、60 s 缓存，结算笔数/付款人/注册/充值汇总 + 最近结算 tx）：生产 https://manekiai.io/api/x402/activity · 预览机 http://34.68.151.4/api/x402/activity
- Agent ID 铸造主机：所有 ERC-8004 注册都由注册钱包 `0xaf6fA147e8F85781196627765FcaFC1044F89308` 签名付 gas；该私钥同时配在预览机（GCP manekiaipeper，`MANEKI_PUBLIC_BASE=http://34.68.151.4`）与实盘（Azure manekiaicore1，`https://manekiai.io`），**谁先跑谁铸**——预览期铸出的 ID 其 agentURI 指向 34.68.151.4，正式提交以实盘部署后铸出的 ID 为准（两套 ID 都在同一 Identity Registry，可从注册钱包在 Celoscan 的交易列表回查）。
- Dune 查询（distinct 钱包 / 日；USDC + USD₮ + USA₮）：

```sql
select date_trunc('day', evt_block_time) d,
       count(distinct "from") wallets, count(*) txs, sum(value)/1e6 usd
from erc20_celo.evt_Transfer
where "to" = 0x26523f5cea5da5d9411749afefe741ba340f6566
  and contract_address in (0xcEBA9300f2b948710d2653dD7B07f33A8B32118C,   -- USDC
                           0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e,   -- USD₮ (USDT)
                           0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771)   -- USA₮ (Tether America USD)
group by 1 order by 1
```

## 赛期内新增工作（commit 范围）

- 2026-09-10：方案 + `auto_service/celo/`（充值 lane、ERC-8004 注册、x402 卖方、Arena 页）+ 34 项测试 + 前后台接线（见 `git log hackathon/2026-09`）。
- 后续每日：见 TEST_VERSIONS 2.74 起。
- 公开仓库 ManekiAI-Celo 的历史由 `scripts/publish_celo_public.sh --replay` 从宿主 `36acfac..HEAD` 逐 commit 回放（保留原作者日期与标题，附 `(host <hash>)`），日常提交则由 post-commit hook 增量同步（`scripts/install_git_hooks.sh`）。

## 英文叙事

> **ManekiAI — AI trading agents that work for you, identified on Celo.**
> Every ManekiAI agent is an autonomous LLM trader running 24/7 on Hyperliquid with real fills and real P&L. During Agents at Work we brought the fleet on-chain on Celo: each agent mints an ERC-8004 identity, is fueled by Celo stablecoins (USDC / USD₮ / USDm top-ups become agent "Gas"), and sells its latest market insight over x402 — anyone with USDC on Celo can ask ManekiAI or unlock an agent's reasoning for a few cents, no login, no exchange keys, settled by the Celo facilitator. Owners earn 70 % of what their agents sell; buyers can rate agents in the Reputation Registry. Primary track: Real World Adoption (+ Stablecoin Adoption via x402 settlement and USD₮/USDC); secondary: Judges' Favorite (ERC-8004 × x402 agent economy).

## 演示视频脚本（2–3 分钟，可选）

1. 创建/编辑 Agent → 打开「在 Celo 竞技场出售洞察」→ 详情页出现 Celo Agent ID 链接（celoscan）。
2. 设置页「获取 Celo USDC」→ Agent 中心充值卡选 Celo · USDC → 燃料到账（台账 txhash 链接 celoscan）。
3. 换一个钱包打开 /arena → 问 ManekiAI（签名、无 gas）→ 答案 + 结算 tx → 解锁一个 Agent 的最新洞察 → 主人 Inbox 收到 "+35 Gas" 分成通知。
4. 收尾：admin Celo 面板（注册数、x402 销售）+ Dune 查询。
