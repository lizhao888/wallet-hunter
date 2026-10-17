# wallet-hunter

**从"上一轮火过的币"反查最早买家 → 筛出还在交易的真人 → 找多个钱包同时买入的共识信号。**

零依赖（只用 Python 标准库），不需要 API key，可在任何装了 Python 3.9+ 的机器上跑。

```
launchpad  →  新币列表（谁刚发）
   scan    →  一个币跑完 7 步：最早买家 → 活跃度 → 去机器人 → 共识 → 打分
```

---

## 为什么用这个方法

一个币从 0 涨到 1000 倍，最早进场的那 20 个钱包里，**通常只有 1 个几年后还在交易**（我们实测 PEPE、MOG 都是 20 → 1）。所以"早期买家"这个身份本身不值钱，值钱的是**还活着的那几个**——以及他们现在在买什么。

单个钱包买某个币，可能只是运气。**3 个互不相关的钱包在同一周买同一个币，才是信号。**

---

## 快速开始

```bash
python wallet_hunter.py doctor                       # 体检：哪些数据源可用
python wallet_hunter.py launchpad --source fourmeme --hours 6 --limit 20
python wallet_hunter.py scan --chain bsc --launchpad fourmeme --token 0xabc...
python wallet_hunter.py scan --chain eth --token 0x6982508145454ce325ddbe47a25d4ec3d2311933 --first 20
```

输出：终端报告 + `out/scan-<币>-<时间>.json`（含每个钱包的判定依据，便于复核）。

### 环境变量（都可选）

| 变量 | 用途 |
|---|---|
| `WH_RPC_ETH` / `WH_RPC_BSC` | 换成自己的节点（公共节点限流时） |
| `WH_BS_ETH` | 换成自己的 Blockscout 实例 |
| `WH_OUT` | 输出目录（默认 `./out`） |

---

## 七步法 → 实现 → **怎么验证**

| 步 | 做什么 | 实现 | 怎么验证 |
|---|---|---|---|
| ① 挑币 | 上轮涨过（4500x/750x 都行，归零更好） | 给合约地址；或用 `launchpad` 列新币 | 市值/持有人与行情软件对得上；`dexscreener` 有交易对 |
| ② 前 20 个买家 | 最早进场的真人钱包 | EVM：从部署区块滚 `Transfer`；发射平台：直接读平台的 `TokenPurchase` 事件 | 首个买家应紧邻部署区块；`eth_getCode=0x` 才是真人；两条通道名单重合 |
| ③ 还在用 | 近 30 天有交易 | ETH：`token-transfers` + `transactions` 双通道；BSC：发射平台近 N 天买卖事件 | 双通道都看；空投不算活跃；30/60 天各跑一遍 |
| ④ 去机器人 | 秒级连续交易 | 间隔中位数 <60s、30 天 >500 笔 | 至少两条证据；阈值 30/60/120s 各跑 |
| ⑤ 在买什么 | 近 30 天收到的币 | 按**合约地址**聚合 | 过滤市值 <$20 万且持有人 <500 的空投；只看真买入 |
| ⑥ 共识 | ≥3 个钱包同期买同一币 | 合约地址去重 + 7 天时间窗 | 2/3/4 三档阈值；**查发送方：同一个合约给多个钱包发同一代币 = 空投，剔除**；排除同一人、同一 bot 集群 |
| ⑦ 打分 | 钱包分 / 币分 | 共识 40 + 市值 20 + 持有人 20 + 新鲜度 20 | **分数只用于排序，不是胜率**；要证明必须回测 |

---

## 要不要从 Meme 发射平台找币？——要，而且更准

这是本工具支持 `--launchpad` 的原因。

| | 老币（Transfer 扫描） | 发射平台（four.meme 等） |
|---|---|---|
| 部署区块 | 要反查（可能查不到） | **事件里直接带** |
| 谁是买家 | 从 ERC20 转账猜（池子/路由/转账会混进来） | **平台自己的 `TokenPurchase` 事件，`account` 就是买家** |
| 噪音 | 低 | 高（99% 归零、机器人多） |
| 适合 | 复盘"上一轮谁赢过" | **找正在发生的事**（新币 + 谁在抢） |

**已支持**

- `fourmeme` — BNB Chain，`TokenManager2` `0x5c952063c7fc8610FFDB798152D69F0B9550762b`
  - `TokenCreate(creator, token, ...)` → 新币列表
  - `TokenPurchase(token, account, price, ...)` → 最早买家
- 任何 EVM 工厂：把合约与事件签名加到 `LAUNCHPADS` 即可（签名会自动算 topic0，不用硬编码哈希）

**待接入（已调研，未验证）**

| 平台 | 链 | 接入方式 |
|---|---|---|
| 蝴蝶协议 / Flap.sh | BNB | 需先确定其 Factory 合约地址（`butterflyprotocol.xyz` 由 Flap.sh 提供动力），再按 four.meme 同样方式接 |
| pump.fun | Solana | 无 EVM 事件可读。路线：`getSignaturesForAddress(mint)` + `getTransaction` 解析 pre/postTokenBalances（慢）；或 PumpPortal 实时接口 / SolanaTracker 的首批买家 API（需 key） |
| SunPump | Tron | TronGrid API（需 key） |

> Solana 的难点不是"能不能"，而是"要不要"：链上交易是 slot 级别的，翻到最早一笔需要分页到底，公共 RPC 会限流。我们目前把 Solana 放在路线图，而不是假装支持。

---

## 数据源（2026-10 实测）

| 源 | 状态 | 说明 |
|---|---|---|
| Blockscout v2 `eth.blockscout.com` | ✅ | 地址/转账/代币市值与持有人 |
| Blockscout 旧版 `module=logs` | ⚠️ 易 429 | 重负载会整轮被封 → 自动切 RPC |
| 公共 RPC `eth_getLogs` | ✅ | `eth.drpc.org`、`rpc.mevblocker.io` 可查；`publicnode` 对 getLogs 返回 403 |
| `bsc.publicnode.com` | ✅ | BSC 的 getLogs 可用（`bsc-dataseed` 会报 limit exceeded） |
| DexScreener | ✅ | 免 key，行情/流动性兜底 |
| Etherscan / BscScan | ❌ 需 key | 无 key 报 `Missing/Invalid API Key` |
| GMGN | ❌ | Cloudflare 人机验证，接口要签名 |
| Blockscout Base | ❌ 403 | — |

---

## 已知限制

- **空投会伪装成"共识"**（实测踩过）：12 个幸存钱包的"共识"榜单里，USDT/USDC 之外几乎全部是同一个合约批量分发（ODYSSEY 5 个钱包、ALPHA 5 个…），看着像"5 个人都买了"，其实是空投。工具现在会打印"空投/单源分发 → 剔除"，但**仍需人工看一眼发送方**。
- **BSC 没有 Blockscout 实例**：钱包活跃度改由发射平台的买卖事件推断（近 N 天），不是全链活动。
- **公共 RPC 会限流**：默认窗口 1 天、单次最多 1.5 万条日志，结果按天缓存到 `out/activity-*.json`。
- **共识 ≠ 胜率**：没有回测之前，任何"信号"都只是"观察到"。
- **不构成投资建议**：输出只描述"观察到 X 个钱包在买 Y"。

## 合规

本项目只读取公开链上数据，不发起任何交易，不托管私钥。请遵守所在地区法律与平台规则；不要用它刷量、操纵市场或规避风控。

## License

MIT
