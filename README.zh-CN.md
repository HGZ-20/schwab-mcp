# schwab-mcp (嘉信理财 MCP Server)

**生产级、开源的 Charles Schwab（嘉信理财）Market Data v1 & Trader v1 Model Context Protocol (MCP) 服务端。**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![MCP Protocol](https://img.shields.io/badge/MCP-2026--07--28%20%7C%20SDK%20v2.3-10b981.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.md) | 简体中文

纯直连嘉信理财官方 REST API 异步封装（零非官方第三方 Schwab SDK 依赖），完整覆盖 **股票/ETF + 单腿/多腿期权行情、持仓查询与风控交易**，并内置 **两种丝滑鉴权模式**，开箱适配 Cursor、Codex、Claude Desktop、Claude Code、Antigravity、Windsurf 等任意 MCP Agent。

---

## 核心亮点

| 特性 | 说明 |
| :--- | :--- |
| **方式 1：远程托管（类 Cloudflare 一键 OAuth 2.1）** | 部署在带公网 HTTPS 域名的服务器上，Cursor / Codex / Claude 客户端**只需填一个 URL**。原生实现最新 **MCP `2026-07-28`** OAuth 2.1 授权服务器（`/.well-known/oauth-protected-resource`、动态客户端注册 DCR、PKCE S256、RFC 8707 资源校验、Refresh Token 轮换与吊销），自动代理上游嘉信 OAuth 2.0，并支持多用户 Token 与账户缓存严格隔离。 |
| **方式 2：纯本地任意 Agent（`uvx schwab-mcp`）** | 携带开发者 `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET` 环境变量或命令行参数即可直接运行。内置 **对话内快捷鉴权工具 `start_auth` / `complete_auth`**（原生支持 **MCP URL-mode Elicitation** 弹窗授权 + 自动拉起浏览器 + 本地临时 HTTPS 回调捕获 + 粘贴 URL 兜底），Token 默认持久化在 `~/.config/schwab-mcp/token.json`（`0600` 权限），本机所有 Agent 共享登录态。 |
| **全面拥抱最新 MCP 协议（`2026-07-28` / SDK v2.3）** | 原生支持无状态 `server/discover` 与按请求 `_meta` 协议协商、URL-mode Elicitation、**Streamable HTTP** 与 **stdio** 双传输；全部 30 个工具配置标准 `ToolAnnotations`；提供只读 **MCP Resources** 与标准化 **MCP Prompts**。 |
| **四重实盘安全风控** | 默认全局开启 `SCHWAB_DRY_RUN=true`（强制走嘉信官方 `previewOrder` 模拟预演，AI 参数无法绕过），叠加 `SCHWAB_READ_ONLY`、单笔名义金额上限 `SCHWAB_MAX_ORDER_USD`、标的黑白名单、强制 `confirm_live=True` 以及 `0600` 权限 JSONL 交易审计日志。 |

---

## 前置准备

1. 在 [Charles Schwab Developer Portal](https://developer.schwab.com/) 创建开发者 App，并勾选 **Accounts and Trading Production** 与 **Market Data Production**。
2. 等待 App 审批状态变为 **`Ready for Use`**。
3. 在开发者后台配置 **Callback URL**：
   - **方式 2（纯本地 stdio）**：`https://127.0.0.1:8182`
   - **方式 1（远程 OAuth 2.1）**：`https://schwab-mcp.你的域名/schwab/callback`

> **嘉信 OAuth 说明**：嘉信官方强制 OAuth 2.0 授权码流程，Access Token 有效期 30 分钟（自动无感续期），Refresh Token 固定 **7 天硬过期**，每 7 天点一次授权即可。

---

## 快速开始：方式 2 — 纯本地任意 Agent（`uvx schwab-mcp`）

无需克隆仓库、无需手动建 `.env`、无需切终端敲命令，在任意支持 MCP 的 Agent 中直接配置开发者 App 参数即可运行：

```json
{
  "mcpServers": {
    "schwab": {
      "command": "uvx",
      "args": ["schwab-mcp"],
      "env": {
        "SCHWAB_APP_KEY": "你的_App_Key",
        "SCHWAB_APP_SECRET": "你的_App_Secret",
        "SCHWAB_CALLBACK_URL": "https://127.0.0.1:8182"
      }
    }
  }
}
```

也支持直接通过命令行参数传入（适配不支持 `env` 字段的客户端）：

```bash
uvx schwab-mcp \
  --app-key "你的_App_Key" \
  --app-secret "你的_App_Secret" \
  --callback-url "https://127.0.0.1:8182"
```

### 对话内一键快捷鉴权流程

1. 直接在聊天窗口问 Agent：*「查一下我的嘉信持仓和 NVDA 实时报价」*。
2. 若尚未登录或 7 天 Token 已过期，Agent 会自动调用 `start_auth`：
   - 后台自动在 `https://127.0.0.1:8182` 启动临时 HTTPS 回调监听；
   - 自动在系统浏览器打开嘉信登录页。
3. 在浏览器登录并同意授权：
   - **自动捕获**：回调完成后 Token 自动落盘至 `~/.config/schwab-mcp/token.json`（`0600`），本机 Cursor / Codex / Claude / Antigravity 全部共享，直接进入使用；
   - **兜底方式（无头服务器或不想点本地自签名证书提示）**：复制浏览器地址栏跳转后的完整 URL（`https://127.0.0.1:8182/?code=...`）发给 Agent，Agent 调用 `complete_auth` 即可秒级完成。

> 💡 **进阶零警告技巧**：本机执行一次 `mkcert 127.0.0.1 localhost` 并配置 `SCHWAB_TLS_CERT_PATH` / `SCHWAB_TLS_KEY_PATH`，之后本地自动回调**完全不会再弹浏览器证书警告**，详见 [docs/authentication.md](docs/authentication.md)。

---

## 快速开始：方式 1 — 远程托管（类 Cloudflare 一键 OAuth 2.1）

在带公网 HTTPS 域名的 VPS 上部署一次，随后在 **Cursor / Codex / Claude** 中只需填一个 URL——客户端无需保存任何 App Secret，自动弹窗“Connect”，零复制粘贴、零证书警告！

### 1. 一键启动服务端（Docker Compose + Caddy 自动 HTTPS）

```bash
cp .env.example .env
# 编辑 .env，填入：
#   SCHWAB_APP_KEY=...
#   SCHWAB_APP_SECRET=...
#   MCP_PUBLIC_URL=https://schwab-mcp.你的域名
#   MCP_PUBLIC_DOMAIN=schwab-mcp.你的域名
docker compose up -d
```

或直接通过命令行运行：

```bash
uvx schwab-mcp serve \
  --host 0.0.0.0 \
  --port 8765 \
  --public-url https://schwab-mcp.你的域名
```

> 当设置 `MCP_PUBLIC_URL` 时，`SCHWAB_CALLBACK_URL` 会自动推导为 `https://schwab-mcp.你的域名/schwab/callback`（请确保与嘉信开发者后台填写的一致）。

### 2. 在 Cursor / Codex / Claude 中只配 URL 接入

```json
{
  "mcpServers": {
    "schwab": {
      "url": "https://schwab-mcp.你的域名/mcp"
    }
  }
}
```

连接时客户端会自动发现 MCP OAuth 2.1 元数据并弹出授权按钮，点开登录嘉信后自动回跳客户端完成连接。完整 Nginx / Caddy / Cloudflare Tunnel 配置见 [docs/deployment.md](docs/deployment.md)。

---

## 工具、资源与提示词一览

- **鉴权管理（3）**：`get_auth_status`、`start_auth`、`complete_auth`
- **实时与历史行情（7）**：`get_quotes`、`get_price_history`、`get_option_expirations`、`get_option_chain`、`get_market_movers`、`get_market_hours`、`search_instruments`
- **账户与订单查询（7）**：`list_accounts`、`get_account_details`、`get_positions`、`get_orders`、`get_order`、`get_transactions`、`get_user_preferences`
- **风控与交易执行（13）**：`get_risk_config`、`build_option_symbol`、`place_equity_order`、`place_option_order`、`place_option_spread_order`、`place_trailing_stop_order`、`place_oco_order`、`place_bracket_order`、`place_trigger_order`、`place_raw_order`、`preview_raw_order`、`replace_order`、`cancel_order`
- **MCP Resources（3）**：`schwab://auth/status`、`schwab://risk/config`、`schwab://accounts`
- **MCP Prompts（3）**：`safe_order_checklist`、`analyze_position`、`option_chain_review`

---

## 常用 CLI 命令

```bash
schwab-mcp               # 方式 2：启动本地 stdio MCP Server（默认）
schwab-mcp serve         # 方式 1：启动 Streamable HTTP + OAuth 2.1 服务
schwab-mcp auth login    # 终端交互登录（支持本地 HTTPS 自动回调或 --no-auto-callback）
schwab-mcp auth status   # 查看 Token 有效期与健康状态
schwab-mcp auth refresh  # 手动刷新 30 分钟 Access Token
schwab-mcp auth logout   # 清除本地 Token
schwab-mcp doctor        # 一键体检（凭证、回调 URL、TLS 证书、Token 状态、风控参数）
```

---

## 风险提示与免责声明

本项目为社区开源项目，与 Charles Schwab & Co., Inc. 无官方隶属或背书关系。股票与期权交易存在重大资金风险，实盘前请务必在默认 `SCHWAB_DRY_RUN=true` 状态下充分预演验证，并妥善配置 `SCHWAB_MAX_ORDER_USD` 等风控上限。安全机制详见 [docs/security.md](docs/security.md)。

## License

[MIT](LICENSE)
