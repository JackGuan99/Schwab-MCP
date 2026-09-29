# Schwab MCP

一个**只读**的 Charles Schwab（嘉信理财）[Model Context Protocol](https://modelcontextprotocol.io) 服务器。
Claude、GPT 等 AI agent 可以通过它查询你的**持仓、账户余额、实时报价**、K 线、期权链、订单和交易记录。

> 本项目**不能下单、改单、撤单**，代码里也没有任何交易接口，只负责把 Schwab 的数据安全地交给 agent。
> 非 Schwab 官方项目，也不构成投资建议。

## 特性

- **持仓 + 实时价格**：`get_positions(include_quotes=true)` 一次调用返回每个持仓的成本、市值、浮盈亏、当日盈亏、
  仓位占比，并附带实时报价
- **组合总览**：`get_portfolio_summary` 汇总所有账户的总资产、现金、当日盈亏、资产类别配置和前 N 大持仓
  （同一只股票在多个账户里的持仓会合并）
- **行情**：实时报价（股票、ETF、指数、期货、期权及希腊值）、K 线、期权链、开闭市时间、涨跌榜、基本面搜索
- **技术指标**：`get_technical_indicators` 在服务端算好 SMA 20/50/200、RSI、MACD、布林带、ATR、波动率、
  52 周区间和金叉/死叉，模型不用再拿几百根 K 线自己算（也就不会算错）
- **隐私**：账号默认脱敏成 `****1234`，Schwab 的 account hash 不会出现在输出里；agent 可以用尾号或昵称指定账户
- **为 LLM 优化的输出**：snake_case 字段、去掉空值、数字取整、时间统一为美东时间、预先算好百分比，既省 token 又不容易算错
- **自动续期**：access token（30 分钟）自动刷新；refresh token 7 天后过期，重新执行 `schwab-mcp auth` 即可，
  **正在运行的 server 不需要重启**
- **两种传输方式**：stdio（Claude Desktop / Claude Code / Codex CLI / OpenAI Agents SDK）和 Streamable HTTP
  （必须配置 Bearer token）
- **Docker**：提供 Dockerfile，token 保存在命名卷里，容器里登录、容器里运行都可以

## 工具一览

| 工具 | 说明 |
|---|---|
| `get_portfolio_summary` | 全部账户总览：总资产、现金、当日/浮动盈亏、资产配置、前 N 大持仓、各账户占比 |
| `get_positions` | 各账户持仓明细；`include_quotes=true` 附带实时价格；可按账户、按代码过滤（期权按标的匹配） |
| `list_accounts` | 账户列表：类型（MARGIN/CASH）、昵称、总资产、现金、购买力等 |
| `get_quotes` | 实时报价：最新价、买卖盘、涨跌幅、成交量、日内/52 周区间；期权带希腊值；可选基本面 |
| `get_price_history` | K 线（1 分钟到月线）+ 区间统计（涨跌幅、最高最低、均量），支持预设周期或起止日期 |
| `get_technical_indicators` | 一次最多 25 个代码的技术面快照：均线及偏离度、RSI(14)、MACD(12,26,9) 及最近交叉、布林带、ATR、1 周至 1 年涨跌幅、年化波动率、52 周区间；日线或周线 |
| `get_option_chain` | 期权链：bid/ask/mark、成交量、未平仓量、IV、希腊值，按到期日排序 |
| `get_option_expirations` | 某个标的的所有期权到期日 |
| `get_market_hours` | 开闭市时间（盘前/盘中/盘后），并标出**当前**所处时段 |
| `get_movers` | 指数/交易所的涨跌榜、成交量榜 |
| `search_instruments` | 按代码或公司名搜索；`fundamental` 模式返回市盈率、市值、beta 等 |
| `get_orders` | 最近 60 天内的订单（含成交明细和平均成交价），只读 |
| `get_transactions` | 交易流水：买卖、分红、利息、出入金、费用 |
| `get_auth_status` | 登录状态，以及距离 7 天过期还剩多久 |
| `get_current_time` | 当前美东时间和 UTC 时间（方便模型做日期计算） |

另外提供一个 prompt 模板 `portfolio_review`，一键生成组合复盘。

## 快速开始

### 1. 申请 Schwab 开发者 App（只需一次）

1. 用你的 Schwab 账号登录 <https://developer.schwab.com>，进入 **Dashboard → Apps → Create App**。
2. API Product 选 **Accounts and Trading Production**（如果能选，再加上 **Market Data Production**）。
3. Callback URL 填 `https://127.0.0.1:8182`（**末尾不要加 `/`**，后续配置必须与这里一字不差）。
4. 创建后的状态通常是 `Approved - Pending`，要等变成 **`Ready For Use`**（可能需要几天）才能使用。
5. 在 App 详情页拿到 **App Key** 和 **Secret**。

### 2. 安装

需要 Python ≥ 3.10，推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/<you>/Schwab-MCP.git
cd Schwab-MCP
uv sync            # 或者：python -m venv .venv && .venv/bin/pip install -e .
```

### 3. 配置凭据

```bash
mkdir -p ~/.config/schwab-mcp
cp .env.example ~/.config/schwab-mcp/.env
chmod 600 ~/.config/schwab-mcp/.env
# 编辑该文件，填入 SCHWAB_CLIENT_ID（App Key）和 SCHWAB_CLIENT_SECRET（Secret）
```

配置的读取顺序（先读到的优先）：真实环境变量 → `--env-file` / `$SCHWAB_MCP_ENV_FILE` → 当前目录的 `.env`
→ `~/.config/schwab-mcp/.env`。

### 4. 登录（每 7 天一次）

```bash
uv run schwab-mcp auth
```

浏览器会打开 Schwab 登录页 → 登录并勾选要授权的账户 → 浏览器跳转到 `https://127.0.0.1:8182/?code=...`，
**页面打不开是正常的** → 把地址栏里的完整 URL 粘贴回终端。Token 保存在 `~/.config/schwab-mcp/token.json`（权限 600）。

```bash
uv run schwab-mcp status     # 查看登录状态，并测试 API 连通性
```

### 5. 接入你的 agent

把下面的 `/ABS/PATH/Schwab-MCP` 换成仓库的绝对路径。如果凭据已经写在 `~/.config/schwab-mcp/.env` 里，
`env` 部分可以省略。

<details open>
<summary><b>Claude Desktop</b></summary>

编辑 `claude_desktop_config.json`（macOS 位于 `~/Library/Application Support/Claude/`）：

```json
{
  "mcpServers": {
    "schwab": {
      "command": "uv",
      "args": ["--directory", "/ABS/PATH/Schwab-MCP", "run", "schwab-mcp"],
      "env": {
        "SCHWAB_CLIENT_ID": "your-app-key",
        "SCHWAB_CLIENT_SECRET": "your-app-secret"
      }
    }
  }
}
```

Claude Desktop 可能找不到 `uv`。如果启动失败，把 `"command"` 改成 `which uv` 输出的绝对路径。
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add schwab --scope user -- uv --directory /ABS/PATH/Schwab-MCP run schwab-mcp
```
</details>

<details>
<summary><b>OpenAI Codex CLI（GPT）</b></summary>

`~/.codex/config.toml`：

```toml
[mcp_servers.schwab]
command = "uv"
args = ["--directory", "/ABS/PATH/Schwab-MCP", "run", "schwab-mcp"]
```
</details>

<details>
<summary><b>OpenAI Agents SDK（GPT，本地 stdio）</b></summary>

```python
import asyncio
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async def main():
    async with MCPServerStdio(
        params={"command": "uv", "args": ["--directory", "/ABS/PATH/Schwab-MCP", "run", "schwab-mcp"]},
    ) as schwab:
        agent = Agent(name="Portfolio assistant", mcp_servers=[schwab],
                      instructions="用中文回答，只使用工具返回的数据。")
        result = await Runner.run(agent, "我今天的持仓表现怎么样？哪只股票拖累最大？")
        print(result.final_output)

asyncio.run(main())
```
</details>

<details>
<summary><b>Docker</b></summary>

```bash
docker build -t schwab-mcp .
docker volume create schwab-mcp          # 保存 token 的命名卷

# 登录（每 7 天一次）：容器里没有浏览器，把打印出来的链接复制到本机浏览器打开，再把跳转后的 URL 粘贴回来
docker run -it --rm -v schwab-mcp:/config \
  -e SCHWAB_CLIENT_ID=your-app-key -e SCHWAB_CLIENT_SECRET=your-app-secret \
  schwab-mcp auth --no-browser
```

Claude Desktop 里用 Docker 启动（`-e NAME` 不带值，表示从下面的 `env` 继承）：

```json
{
  "mcpServers": {
    "schwab": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-v", "schwab-mcp:/config",
               "-e", "SCHWAB_CLIENT_ID", "-e", "SCHWAB_CLIENT_SECRET", "schwab-mcp"],
      "env": {
        "SCHWAB_CLIENT_ID": "your-app-key",
        "SCHWAB_CLIENT_SECRET": "your-app-secret"
      }
    }
  }
}
```

HTTP 模式：`docker run -d -p 127.0.0.1:8765:8765 -v schwab-mcp:/config -e SCHWAB_CLIENT_ID -e SCHWAB_CLIENT_SECRET
-e SCHWAB_MCP_HTTP_TOKEN schwab-mcp serve --transport http`（端口只映射到本机回环地址）。

镜像以非 root 用户（uid 10001）运行，`/config/schwab-mcp/` 下的 token 与 `.env` 在 `auth` 和 server 之间共享，
重新登录后正在运行的容器同样无需重启。
</details>

<details>
<summary><b>远程 HTTP（OpenAI Responses API 等）</b></summary>

```bash
export SCHWAB_MCP_HTTP_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
uv run schwab-mcp serve --transport http --port 8765          # 端点：http://127.0.0.1:8765/mcp
# 通过隧道或反向代理暴露时，加上 --allowed-host your.domain.com
```

所有请求都必须带 `Authorization: Bearer $SCHWAB_MCP_HTTP_TOKEN`，否则返回 401。

```python
from openai import OpenAI

resp = OpenAI().responses.create(
    model="gpt-5",  # 任意支持远程 MCP 工具的模型
    tools=[{
        "type": "mcp",
        "server_label": "schwab",
        "server_url": "https://your.domain.com/mcp",
        "headers": {"Authorization": "Bearer <SCHWAB_MCP_HTTP_TOKEN>"},
        "require_approval": "never",
    }],
    input="列出我的前五大持仓和它们今天的涨跌幅",
)
print(resp.output_text)
```

⚠️ 这相当于把你的券商数据暴露到公网。只在确实需要时使用：token 要足够长，放在 HTTPS 后面，用完就关。
ChatGPT 网页版的自定义连接器只支持 OAuth 或无认证，所以**不建议**直接接入。GPT 用户请优先使用 Codex CLI 或
Agents SDK（本地 stdio，数据只会发给模型，不会暴露在公网上）。
</details>

### 6. 试一试

- 「我的组合今天表现如何？」→ `get_portfolio_summary`
- 「列出我所有持仓的实时价格和浮盈」→ `get_positions(include_quotes=true)`
- 「NVDA 和 AAPL 现在多少钱？」→ `get_quotes`
- 「AAPL 过去 6 个月的走势」→ `get_price_history(period="6M")`
- 「我的持仓里哪些 RSI 超买了？哪些跌破了 200 日均线？」→ `get_positions` + `get_technical_indicators`
- 「我的 Roth IRA 这个月收到多少分红？」→ `get_transactions(account="Roth IRA", types=["DIVIDEND_OR_INTEREST"])`
- 「现在美股开盘了吗？」→ `get_market_hours`

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `SCHWAB_CLIENT_ID` | — | App Key（必填） |
| `SCHWAB_CLIENT_SECRET` | — | App Secret（必填） |
| `SCHWAB_CALLBACK_URL` | `https://127.0.0.1:8182` | 必须与开发者后台填写的完全一致 |
| `SCHWAB_TOKEN_PATH` | `~/.config/schwab-mcp/token.json` | Token 文件位置 |
| `SCHWAB_MCP_MASK_ACCOUNTS` | `true` | 账号脱敏；设为 `false` 显示完整账号 |
| `SCHWAB_MCP_HTTP_TOKEN` | — | HTTP 传输使用的 Bearer token（HTTP 模式必填） |
| `SCHWAB_MCP_TIMEOUT` | `20` | 单次请求超时（秒） |
| `SCHWAB_MCP_LOG_LEVEL` | `INFO` | 日志级别（日志只写 stderr，不会干扰 stdio 协议） |

命令行：`schwab-mcp [serve] [--transport stdio|http] [--host] [--port] [--allowed-host HOST]`、
`schwab-mcp auth [--no-browser]`、`schwab-mcp status`，以及全局参数 `--env-file PATH`。

## 安全与隐私

- **只读**：客户端只实现了 GET 接口，所有工具都带 MCP 的 `readOnlyHint` 注解。
- **Token 存储**：`token.json` 以 `0600` 权限原子写入，所在目录为 `0700`。不要把它和 `.env` 提交到 git（`.gitignore` 已排除）。
- **账号脱敏**：输出中的完整账号会被替换成 `****1234`，交易描述之类的自由文本也不例外；hash 值从不输出。
- **数据流向**：agent 调用工具后，返回的数据会发给你所用的模型提供方（Anthropic、OpenAI 等），请按自己的隐私要求取舍。
- **HTTP 模式**：未设置 `SCHWAB_MCP_HTTP_TOKEN` 时拒绝启动；`--insecure-no-auth` 只允许绑定回环地址。
  绑定 127.0.0.1 时默认开启 DNS rebinding 防护。

## 常见问题

| 现象 | 处理方法 |
|---|---|
| 工具提示 `Schwab login problem` 或 refresh token 过期 | 运行 `schwab-mcp auth` 重新登录（每 7 天一次，server 无需重启） |
| 登录时 Schwab 提示 callback/redirect 不匹配 | `SCHWAB_CALLBACK_URL` 必须与后台填写的完全一致，包括末尾有没有 `/` |
| HTTP 401 `invalid_client` | App 还没到 `Ready For Use`，或者 Key/Secret 填错了 |
| HTTP 403 | App 没有开通对应的 API Product（账户或行情） |
| HTTP 429 | 触发了 Schwab 的频率限制（约 120 次/分钟），server 会自动退避重试 |
| Claude Desktop 里看不到工具 | 检查 `command` 是否为绝对路径，并查看 Claude Desktop 的 MCP 日志 |

## 开发

```bash
uv sync
uv run pytest                 # 针对模拟的 Schwab API 运行，不需要真实账户
uv run ruff check src tests
npx @modelcontextprotocol/inspector uv run schwab-mcp   # 在浏览器里交互式调试工具
```

代码结构和设计取舍见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 参考项目

设计参考了以下开源项目，具体借鉴点见 [`docs/DESIGN.md`](docs/DESIGN.md#参考项目)：

- [jkoelker/schwab-mcp](https://github.com/jkoelker/schwab-mcp)：Python，基于 schwab-py；交易需要审批，`--json` 精简输出，
  技术指标工具，容器镜像
- [sudowealth/schwab-mcp](https://github.com/sudowealth/schwab-mcp)：TypeScript + Cloudflare Workers；账号脱敏、日志脱敏
- [alexgolec/schwab-py](https://github.com/alexgolec/schwab-py)：社区 Schwab API 客户端，OAuth 流程和接口参数的权威参考
- [acidsolution/schwab-mcp-server](https://github.com/acidsolution/schwab-mcp-server)：Python，同样只读；多客户端配置示例

## License

MIT
