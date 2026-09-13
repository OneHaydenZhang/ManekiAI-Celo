# Celo「Agents at Work」提交材料（草稿 · 随进度更新）

> 截止 2026-09-14 09:00 GMT（北京 17:00）。赛事页 https://www.risein.com/celo/celo-agents-at-work-hackathon
> 方案：`docs/hackathon/CELO_AGENTS_AT_WORK_方案.md` · 代码：`auto_service/celo/`（公开仓库 ManekiAI-Celo 同步）

## 报名表字段

| 字段 | 值 | 状态 |
|---|---|---|
| Project | ManekiAI — AI trading agents that work for you, identified on Celo | ✓ |
| Public GitHub | https://github.com/OneHaydenZhang/ManekiAI-Celo | ✓ Public（2026-09-13 切回）。仓库只含黑客松新增代码（`celo/` 包 + Celo/x402 测试 + 本文档），不含宿主私有代码 |
| ERC-8004 Agent ID（平台 Analyst） | `#9837`（Celo Identity Registry `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`，注册 tx [`0x47530979efdfe12fd676bce859704c7065131c2a0913c2b9c3a41a72e8f4895f`](https://celoscan.io/tx/0x47530979efdfe12fd676bce859704c7065131c2a0913c2b9c3a41a72e8f4895f)） | ✓ 已铸造 |
| Agent 钱包地址 | 收款/x402 payTo：`0x26523f5cea5da5d9411749afefe741ba340f6566`（只收款）；注册钱包（付 gas）：`0xaf6fA147e8F85781196627765FcaFC1044F89308` | ✓ |
| Telegram handle | 已由负责人在报名表填写（不在此记录） | ✓ |
| Primary track | Real World Adoption | ✓ |
| Secondary（一句话） | Stablecoin Adoption — Gas top-ups in USDC/USD₮/USDm/USA₮ on Celo + every Arena purchase is an x402 settlement in USDC; Judges' Favorite — ERC-8004 identity × x402 revenue share = agents that earn for their owners | ✓（报名表所填，与 /hackathon 第 5 节一致） |
| Distribution channel | Existing audience (manekiai.io users, X) + the public login-free Arena for the Celo community | ✓ |
| Demo | **指南/证明/动线：http://34.68.151.4/hackathon** · Arena http://34.68.151.4/arena · http://34.68.151.4/api/agent-card/maneki-analyst · http://34.68.151.4/api/x402/activity | ✓ 在线（桌面浏览器钱包可用；移动端 MiniPay 需 HTTPS，暂不可用） |

## 链上证据（部署后逐项填 · `scripts/fill_celo_submission.py --activity <url|file>` 可自动回填）

- 平台 Analyst 注册 tx：[`0x47530979efdfe12fd676bce859704c7065131c2a0913c2b9c3a41a72e8f4895f`](https://celoscan.io/tx/0x47530979efdfe12fd676bce859704c7065131c2a0913c2b9c3a41a72e8f4895f)（Agent ID #9837）
- 用户 Agent 注册 tx（前 5 个）：A-NDDDMS·NVDA #9838 [`0x0e01dfaf4890de2912e9ee68f61cdab88a1b3720144b99238a965eaa19d5ff03`](https://celoscan.io/tx/0x0e01dfaf4890de2912e9ee68f61cdab88a1b3720144b99238a965eaa19d5ff03)；A-5MYM3T·NVDA #9839 [`0xc048ffc7187d557e4c3d89bffc40b91f7e7c60c5c8fbd2e8be92b3f370ac2b40`](https://celoscan.io/tx/0xc048ffc7187d557e4c3d89bffc40b91f7e7c60c5c8fbd2e8be92b3f370ac2b40)；A-5YG13T·SNDK #9840 [`0x98747168ad411c952ee9a8b1e224bdd965512f67eb0309396ff2088d33d6f287`](https://celoscan.io/tx/0x98747168ad411c952ee9a8b1e224bdd965512f67eb0309396ff2088d33d6f287)；A-8FWAZV·UNITREE #9841 [`0xe1137cd692de26a5567ecf888a90d907fba8e1124acd62dcf54d0e67ada81e81`](https://celoscan.io/tx/0xe1137cd692de26a5567ecf888a90d907fba8e1124acd62dcf54d0e67ada81e81)；A-5GHAFP·UNITREE #9842 [`0x7e10799775b3f49af15ea4132c8d6b66164ab383d7ac4ea369ffe9e6c76897a1`](https://celoscan.io/tx/0x7e10799775b3f49af15ea4132c8d6b66164ab383d7ac4ea369ffe9e6c76897a1)
- 首笔 Celo 稳定币充值 tx：`TBD`
- 首笔 x402 结算 tx（facilitator 签名者 `0x0d74D5Cefd2e7F24E623330ebE3d8D4cB45fFB48`）：`TBD`
- 公开活动端点（免登录、60 s 缓存，结算笔数/付款人/注册/充值汇总 + 最近结算 tx）：http://34.68.151.4/api/x402/activity
- Agent ID 铸造：所有 ERC-8004 注册都由注册钱包 `0xaf6fA147e8F85781196627765FcaFC1044F89308` 签名付 gas（可在 Celoscan 该地址的交易列表回查全部铸造）；agentURI 指向 `http://34.68.151.4/api/agent-card/{code}`。如后续更换公共域名，用 `setAgentURI` 重定向（admin「URI 重定向」），ID 不变、不重铸。
- Dune 查询（distinct 钱包 / 日；USDC + USD₮ + USDm + USA₮，按各自精度换算；payTo 同时收 x402 结算，所以结果 = 充值 + Arena 购买）：

```sql
select date_trunc('day', evt_block_time) d,
       count(distinct "from") wallets, count(*) txs,
       sum(case when contract_address = 0x765DE816845861e75A25fCA122bb6898B8B1282a then cast(value as double) / 1e18   -- USDm: 18 decimals
                else cast(value as double) / 1e6 end) usd                                                              -- USDC / USD₮ / USA₮: 6 decimals
from erc20_celo.evt_Transfer
where "to" = 0x26523f5cea5da5d9411749afefe741ba340f6566
  and contract_address in (0xcebA9300f2b948710d2653dD7B07f33A8B32118C,   -- USDC
                           0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e,   -- USD₮ (USDT)
                           0x765DE816845861e75A25fCA122bb6898B8B1282a,   -- USDm (Mento Dollar, ex-cUSD)
                           0xD2ab3C9A02DBBAB236BfEC45D1d755DF4267F771)   -- USA₮ (Tether America USD)
group by 1 order by 1
```

## 赛期内新增工作（commit 范围）

- 2026-09-10：方案 + `auto_service/celo/`（充值 lane、ERC-8004 注册、x402 卖方、Arena 页）+ 34 项测试 + 前后台接线（见 `git log hackathon/2026-09`）。
- 后续每日：见 TEST_VERSIONS 2.74 起。
- 公开仓库 ManekiAI-Celo 的历史由 `scripts/publish_celo_public.sh --replay` 从宿主 `36acfac..HEAD` 逐 commit 回放（保留原作者日期与标题，附 `(host <hash>)`），日常提交则由 post-commit hook 增量同步（`scripts/install_git_hooks.sh`）。

## 英文叙事

> **ManekiAI — AI trading agents that work for you, identified on Celo.**
> Every ManekiAI agent is an autonomous LLM trader running 24/7 on Hyperliquid with real fills and real P&L. During Agents at Work we brought the fleet on-chain on Celo: each agent mints an ERC-8004 identity, is fueled by Celo stablecoins (USDC / USD₮ / USDm / USA₮ top-ups become agent "Gas"), and sells its latest market insight over x402 — anyone with USDC on Celo can ask ManekiAI or unlock an agent's reasoning for a few cents, no login, no exchange keys, settled by the Celo facilitator. Owners earn 70 % of what their agents sell; buyers can rate agents in the Reputation Registry. Primary track: Real World Adoption (+ Stablecoin Adoption via x402 settlement in USDC / USA₮ and stablecoin Gas top-ups); secondary: Judges' Favorite (ERC-8004 × x402 agent economy).

## 分发邀请文案（发黑客松 Telegram / Celo & MiniPay 社区 / X；参与者必须用自己的钱，我们不能转钱给任何人）

> **中文**
> 🐱 ManekiAI 上了 Celo：6 个真在 Hyperliquid 交易美股永续的 AI Agent，现在把它们的最新决策放到链上卖，一份 $0.05 USDC，问一次平台分析师 $0.02，个股简报 $0.01。
> 不用注册、不用交易所 key、不用 CELO 付 gas：连上 Celo 钱包 → 点一下 → 签一个授权 → 内容和 Celoscan 结算链接一起回来（x402 微支付，官方 facilitator 结算）。
> 30 秒体验：http://34.68.151.4/arena （桌面浏览器 + MetaMask / Rabby / OKX；钱包里要有 Celo 上的一点 USDC，没有就用 Jumper 换：https://jumper.exchange/?toChain=42220&toToken=0xcebA9300f2b948710d2653dD7B07f33A8B32118C ）
> 这是 Celo「Agents at Work」黑客松参赛作品，指南和链上证明：http://34.68.151.4/hackathon
>
> **English**
> 🐱 ManekiAI is on Celo: 6 AI agents that really trade US-stock perps on Hyperliquid now sell their latest decision on-chain — $0.05 USDC per insight, $0.02 to ask the platform analyst, $0.01 for a symbol brief.
> No sign-up, no exchange keys, no CELO for gas: connect a Celo wallet → click → sign one authorization → the content comes back with its Celoscan settlement link (x402 micro-payment, settled by the official facilitator).
> 30-second try: http://34.68.151.4/arena (desktop browser + MetaMask / Rabby / OKX; you need a little USDC on Celo — swap with Jumper: https://jumper.exchange/?toChain=42220&toToken=0xcebA9300f2b948710d2653dD7B07f33A8B32118C )
> Built for the Celo “Agents at Work” hackathon — guide & on-chain proofs: http://34.68.151.4/hackathon

## 程序化买家（Agent 买 Agent 的研究）

`auto_service/celo/tools/x402_buyer.py`：不用浏览器的 x402 客户端，拿 402 → 选资产 → 签 EIP-3009 → 重发 → 打印内容与结算 tx。私钥只从环境变量 `X402_BUYER_KEY` 读，永不进命令行。用途：团队冒烟测试（不计分）与"机器付费给机器"的演示。
`python auto_service/celo/tools/x402_buyer.py --base http://34.68.151.4 brief --symbol NVDA --dry-run` 只看报价；去掉 `--dry-run` 即真实购买。

## 演示视频脚本（2–3 分钟，可选）

1. 创建/编辑 Agent → 打开「在 Celo 竞技场出售洞察」→ 详情页出现 Celo Agent ID 链接（celoscan）。
2. 设置页「获取 Celo USDC」→ Agent 中心充值卡选 Celo · USDC → 燃料到账（台账 txhash 链接 celoscan）。
3. 换一个钱包打开 /arena → 问 ManekiAI（签名、无 gas）→ 答案 + 结算 tx → 解锁一个 Agent 的最新洞察 → 主人 Inbox 收到 "+35 Gas" 分成通知。
4. 收尾：admin Celo 面板（注册数、x402 销售）+ Dune 查询。
