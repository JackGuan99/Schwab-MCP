# 设计说明

## 目标

让 agent（Claude、GPT……）**安全地读取**你的 Schwab 持仓和实时行情，并且在 token 预算内拿到"可以直接回答问题"的数据。

不在范围内：下单交易、WebSocket 流式行情、多用户托管。

## 架构

```
agent (Claude / GPT)
   │  MCP: stdio 或 Streamable HTTP (+Bearer)
   ▼
server.py      工具定义、参数校验（pydantic / JSON Schema）、错误翻译
   │
   ├── formatting.py   原始 JSON → 精简、派生好的字典（纯函数）
   ├── indicators.py   K 线 → 技术指标（纯函数）
   ├── accounts.py     账号 ↔ hash 映射、脱敏、按尾号/昵称解析
   └── client.py       httpx 异步客户端，只有 GET；401 自动刷新，429/5xx 退避重试
          │
          └── auth.py  OAuth 授权码流程、token 文件、刷新锁
                 │
                 ▼
        api.schwabapi.com  (/v1/oauth, /trader/v1, /marketdata/v1)
```

| 模块 | 职责 |
|---|---|
| `config.py` | 环境变量 / `.env` → `Settings` |
| `auth.py` | `schwab-mcp auth` 的授权 URL 构造与回调解析；`TokenStore`（0600 原子写入）；`TokenManager`（提前 60 秒刷新、`asyncio.Lock` 保证并发只刷一次、检测到 token 文件被改写就重新加载） |
| `client.py` | Trader API 和 Market Data API 的只读端点；日期格式（ISO `…000Z`、epoch 毫秒）集中在这里处理 |
| `accounts.py` | `/accounts/accountNumbers` + `/userPreference`（昵称）→ `Account`；`scrub()` 递归替换所有残留的完整账号 |
| `formatting.py` | 持仓派生（成本、浮盈亏 %、仓位占比、期权 ×100 乘数、空头方向）、组合汇总、报价/K 线/期权链/订单/流水的精简 |
| `indicators.py` | SMA / EMA / RSI / MACD / 布林带 / ATR / 已实现波动率、交叉检测，以及单个标的的技术面快照 |
| `server.py` | 15 个工具 + 1 个 prompt；`lifespan` 里创建共享的 httpx 客户端 |
| `cli.py` | `auth` / `status` / `serve`，以及 HTTP 模式的 Bearer 中间件 |

## 关键设计决策

### 1. 只读，而且是"物理上"只读
参考项目都把交易放在"审批"后面（jkoelker 用 Discord 审批，sudowealth 标记为实验功能）。本项目的需求是"让 agent 知道
我持有什么、现在值多少钱"，所以干脆**不实现**任何 POST/PUT/DELETE。prompt injection 再成功，也不可能导致下单。
所有工具都标注了 `readOnlyHint=true`，客户端可以放心地免审批自动调用。

### 2. 自己写一个很薄的 API 客户端，不依赖 schwab-py
schwab-py 很成熟，但它的体量主要花在交易、订单构造和流式行情上。只读场景只需要约 15 个 GET 端点，自己实现的好处是：
依赖少；可以用 `httpx.MockTransport` 做完整的端到端测试；401 刷新、429 退避、错误信息提取都能按 agent 的需要定制。
各端点的参数名和日期格式对照 schwab-py 的实现逐一核对过。

### 3. 面向 LLM 的输出
- **预先派生**：成本 = 市值 − 浮盈；浮盈 %、仓位占比、组合配置、跨账户合并同一代码。LLM 算术不可靠，这些由代码来算。
- **现金用差额算**：组合汇总里的现金 = 账户总值 − 所有持仓市值，而不是 `cashBalance + moneyMarketFund`。
  Schwab 可能把 sweep / 货币基金同时报成余额和一个 `CASH_EQUIVALENT` 持仓，直接相加会重复计算，资产配置合计超过 100%。
- **精简**：去掉 null 和空容器，数字按类型取整（金额 2 位，其余 4 位），字段一律 snake_case，`_pct` 后缀表示百分数。
- **可截断**：K 线默认只返回最近 120 根（摘要仍覆盖全区间），期权链默认 80 个合约，超出时明确告诉模型如何缩小范围。
- **统一时间**：时间戳全部转换为美东时间（交易所时钟），另提供 `get_current_time`，避免模型自己猜"今天"。
- **可操作的错误**：登录过期时，工具错误里直接写"运行 `schwab-mcp auth`"，模型可以原样转告用户。

### 4. 账号隐私（借鉴 sudowealth/schwab-mcp 的 account scrubbing）
Schwab 在 URL 中使用加密 hash，在响应体中却返回明文账号（有时是字符串，有时是整数）。本项目的做法：
输出只使用 `****1234` 标签；工具参数接受标签、尾号、昵称，或者完整账号；所有工具输出最后都会经过 `scrub()` 兜底，
把任何残留的完整账号（包括自由文本里的）替换成标签。测试 `test_no_raw_account_number_leaks` 会覆盖所有账户类工具。

### 5. Token 生命周期
Schwab：access token 30 分钟，refresh token 自**首次登录**起 7 天，不能续期。因此：
- `refresh_token_issued_at` 固定为交互式登录的时间，刷新时不重置，这样 `get_auth_status` 报告的剩余时间是真实的；
- 剩余不到 24 小时时给出警告；
- 重新 `schwab-mcp auth` 会改写 token 文件，运行中的 server 通过比较 mtime 自动加载，无需重启；
- 登录采用"粘贴回调 URL"的手动流程（同 schwab-py 的 manual flow）：不需要在本机起 HTTPS 服务、不需要自签证书，
  在无图形界面的服务器上也能用。

### 6. 技术指标在服务端计算（借鉴 jkoelker/schwab-mcp 的 technical 工具）
"RSI 是多少""是不是跌破了 200 日线"这类问题，如果让模型从几百根 K 线里自己算，既费 token 又容易出错。
`get_technical_indicators` 取 2 年日线（或 10 年周线），只返回最新值和几个可以直接引用的事实（与均线的偏离、
MACD / 均线最近一次交叉的日期、52 周区间位置）。口径与主流看盘软件一致：EMA 用前 n 根的 SMA 做种子，
RSI 和 ATR 用 Wilder 平滑，布林带用总体标准差；RSI 的实现用 StockCharts 公开的算例逐值校验过。
多个代码并发请求，但限制并发数为 4，避免触发 Schwab 约 120 次/分钟的频率限制。

### 7. 传输与部署
- 默认 stdio：Claude Desktop、Claude Code、Codex CLI、OpenAI Agents SDK 都支持，数据不经过网络监听端口。
- HTTP（Streamable HTTP，`/mcp`）用于 OpenAI Responses API 这类只能连远程 MCP 的场景：强制 Bearer token（`hmac.compare_digest`
  比较），不配 token 就拒绝启动；绑定回环地址时保留 SDK 的 DNS rebinding 防护，通过 `--allowed-host` 放行隧道域名。
- Docker：两阶段构建（uv 安装锁定的依赖 → 精简运行镜像），非 root 用户运行；`XDG_CONFIG_HOME=/config`，
  token 放在命名卷里，`auth` 和 server 两个容器共享，热加载依然有效。基础镜像只用 Docker Hub 的官方 `python`，
  uv 从 PyPI 安装，不依赖 GHCR。

## 参考项目

| 项目 | 借鉴点 | 不同之处 |
|---|---|---|
| [jkoelker/schwab-mcp](https://github.com/jkoelker/schwab-mcp) | `auth` / `server` 两段式 CLI；`https://127.0.0.1:8182` 默认回调；去掉空字段以节省 token 的 `--json` 模式；stdio + HTTP 两种传输；工具按"行情 / 账户"分组；服务端技术指标；容器镜像 + token 卷 | 它支持交易（Discord 审批，另有绕过审批的开关）；本项目完全不含交易 |
| [sudowealth/schwab-mcp](https://github.com/sudowealth/schwab-mcp) | 账号脱敏（用显示名替代账号）；日志中的密钥脱敏；token 在过期前主动刷新 | 它部署在 Cloudflare Workers + Durable Objects 上，由服务端托管 OAuth；本项目是本地优先的单用户进程 |
| [acidsolution/schwab-mcp-server](https://github.com/acidsolution/schwab-mcp-server) | 同样坚持只读；给出 Claude / ChatGPT / Codex 等多种客户端的配置示例 | 工具更少，没有组合汇总和账号脱敏 |
| [alexgolec/schwab-py](https://github.com/alexgolec/schwab-py) | OAuth 端点与 Basic 认证方式、授权码需 URL 解码、各端点参数名、日期格式、`Approved - Pending` 状态等注意事项 | 本项目只实现只读子集，没有把它作为依赖 |

## 可能的后续工作

- 可选的 WebSocket 流式报价（Schwab Streamer），作为 MCP resource 订阅
- 更多派生分析：行业/板块暴露（需要额外数据源）、已实现盈亏统计、分红日历
- `schwab-mcp auth --local-server`：自签证书本地回调，免去粘贴 URL
- 在 CI 里发布镜像到容器仓库（目前只在 CI 里构建和冒烟测试）
