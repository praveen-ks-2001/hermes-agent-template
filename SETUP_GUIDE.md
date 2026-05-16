# Hermes Agent 部署配置指南

本文档说明在 **Render**（免费版，无持久化磁盘）上部署 Hermes Agent 时所需的所有环境变量，以及如何用 **Backblaze B2** 实现数据持久化。

---

## 目录

1. [Render 部署步骤](#1-render-部署步骤)
2. [LLM 模型提供商变量](#2-llm-模型提供商变量)
3. [自定义 API 站点（自建）变量](#3-自定义-api-站点自建变量)
4. [消息渠道变量](#4-消息渠道变量)
5. [工具类变量](#5-工具类变量)
6. [Backblaze B2 持久化配置（重点）](#6-backblaze-b2-持久化配置重点)
7. [管理后台变量](#7-管理后台变量)
8. [完整变量清单示例](#8-完整变量清单示例)
9. [常见问题](#9-常见问题)

---

## 1. Render 部署步骤

### 1.1 准备工作

- 注册 [Render](https://render.com/) 账号
- 注册 [Backblaze](https://www.backblaze.com/) 账号（免费 10GB）
- Fork 或推送本项目到你的 GitHub

### 1.2 创建 Web Service

1. 在 Render Dashboard 点击 **New +** → **Web Service**
2. 连接你的 GitHub 仓库
3. 填写以下信息：
   - **Name**: `hermes-agent`（任意）
   - **Region**: 选离你最近的（如 Singapore）
   - **Branch**: `main`
   - **Runtime**: `Docker`
   - **Build Command**: 留空（用 Dockerfile）
   - **Start Command**: 留空（用 Dockerfile）

### 1.3 设置环境变量

在 **Environment Variables** 中添加下文列出的变量（至少需要 LLM 提供商 + 1 个消息渠道 + B2 配置）。

### 1.4 部署

点击 **Create Web Service**，等待构建完成（约 5-10 分钟）。

### 1.5 访问后台

打开 `https://你的服务名.onrender.com/setup`，用 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。

---

## 2. LLM 模型提供商变量

**至少需要配置一个。** 推荐用 OpenRouter（一个 Key 访问所有模型）。

### OpenRouter（推荐）

```
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxx
LLM_MODEL=openai/gpt-4o-mini
```

### OpenAI

```
OPENAI_API_KEY=sk-xxxxxxxxxxxx
LLM_MODEL=gpt-4o-mini
```

### Anthropic Claude

```
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
LLM_MODEL=claude-sonnet-4-5
```

### DeepSeek

```
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx
LLM_MODEL=deepseek/deepseek-chat
```

### 阿里通义千问 (DashScope)

```
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxx
LLM_MODEL=qwen-plus
```

### 智谱 GLM / Z.AI

```
GLM_API_KEY=xxxxxxxxxxxx
```

### Kimi (月之暗面)

```
KIMI_API_KEY=xxxxxxxxxxxx
```

### MiniMax

```
MINIMAX_API_KEY=xxxxxxxxxxxx
```

### 更多国内模型 (阶跃星辰)

```
STEPFUN_API_KEY=xxxxxxxxxxxx
```

### Google Gemini

```
GEMINI_API_KEY=AIzaxxxxxxxxxxxx
```

### Hugging Face

```
HF_TOKEN=hf_xxxxxxxxxxxx
```

### NVIDIA NIM

```
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxx
```

---

## 3. 自定义 API 站点（自建变量）

如果你有自己的 OpenAI 兼容 API 站点（如 one-api、new-api 等），用这个：

```
OPENAI_API_KEY=sk-你的自定义密钥
OPENAI_API_BASE=https://你的api站点.com/v1
LLM_MODEL=gpt-3.5-turbo
```

> `OPENAI_API_BASE` 的格式必须是 `https://域名/v1`，末尾不要带斜杠。

---

## 4. 消息渠道变量

**至少需要配置一个**，Hermes Agent 通过消息渠道与你交互。

### Telegram（推荐，最简单）

1. 在 Telegram 中搜索 **@BotFather**，发送 `/newbot` 创建机器人
2. 复制 Bot Token
3. 可选：发送 `/mybots` → 你的 bot → **Bot Settings** → **Group Privacy** → **Turn off**（如果要在群组中使用）

```
TELEGRAM_BOT_TOKEN=7234567890:AAHxxxxxxxxxxxx
TELEGRAM_ALLOWED_USERS=123456789      # 允许的用户 ID（可选，不设则所有人都能发消息）
```

### Discord

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用
2. 在 **Bot** 页面创建 Token，开启 Privileged Gateway Intents

```
DISCORD_BOT_TOKEN=xxxxxxxxxxxx
DISCORD_ALLOWED_USERS=123456789       # 可选
```

### Slack

```
SLACK_BOT_TOKEN=xoxb-xxxxxxxxxxxx
SLACK_APP_TOKEN=xapp-xxxxxxxxxxxx
```

### WhatsApp

```
WHATSAPP_ENABLED=true
```

### Email

```
EMAIL_ADDRESS=your@email.com
EMAIL_PASSWORD=xxxxxxxxxxxx
EMAIL_IMAP_HOST=imap.example.com
EMAIL_SMTP_HOST=smtp.example.com
```

### Mattermost

```
MATTERMOST_URL=https://mattermost.example.com
MATTERMOST_TOKEN=xxxxxxxxxxxx
```

### Matrix

```
MATRIX_HOMESERVER=https://matrix.example.com
MATRIX_ACCESS_TOKEN=xxxxxxxxxxxx
MATRIX_USER_ID=@user:example.com
```

---

## 5. 工具类变量

可选配置，增强 Hermes 的能力。

### 网络搜索（至少选一个）

```
TAVILY_API_KEY=tvly-xxxxxxxxxxxx
PARALLEL_API_KEY=xxxxxxxxxxxx
FIRECRAWL_API_KEY=fc-xxxxxxxxxxxx    # 网页抓取
EXA_API_KEY=xxxxxxxxxxxx
```

### 图片生成

```
FAL_KEY=xxxxxxxxxxxx                  # FAL.ai 图片生成
```

### 浏览器自动化

```
BROWSERBASE_API_KEY=xxxxxxxxxxxx
BROWSERBASE_PROJECT_ID=xxxxxxxxxxxx
```

### 语音 / TTS

```
VOICE_TOOLS_OPENAI_KEY=sk-xxxxxxxxxxxx  # OpenAI Whisper/TTS
```

### 记忆存储

```
HONCHO_API_KEY=xxxxxxxxxxxx           # 跨会话用户记忆
```

### GitHub

```
GITHUB_TOKEN=ghp_xxxxxxxxxxxx         # 提升代码工具的 API 频率限制
```

---

## 6. Backblaze B2 持久化配置（重点）

> **为什么需要？** Render 免费版没有持久化磁盘，容器重启后所有数据丢失（API 配置、用户配对、代理记忆等）。用 B2 存储桶可以实现启动时自动恢复、运行时自动备份。

### 6.1 创建 B2 存储桶

1. 注册 [Backblaze](https://www.backblaze.com/)（免费 10GB）
2. 登录后进入 **B2 Cloud Storage**
3. 点击 **Buckets** → **Create a Bucket**
   - **Bucket Name**: `hermes-data`（任意）
   - **Files in Bucket are**: `Private`
   - 其他保持默认
4. 创建完成后，记下 **Bucket Name** 和 **Endpoint**（如 `https://s3.us-west-002.backblazeb2.com`）

### 6.2 创建 Application Key

1. 进入 **Application Keys** → **Add New Application Key**
2. 设置：
   - **Name of Key**: `hermes-render`
   - **Allow access to bucket(s)**: 勾选刚创建的 bucket（如 `hermes-data`）
   - **Type of Access**: `Read and Write`
3. 点击 **Create New Key**
4. **立即复制保存** 显示的 Key ID 和 Application Key（关闭后不再显示）

### 6.3 添加环境变量

在 Render 的环境变量中添加：

```
# B2 配置（填你自己的值）
S3_ACCESS_KEY_ID=0045xxxxxxxxxxxx        # B2 Application Key ID
S3_SECRET_ACCESS_KEY=K002xxxxxxxxxxxx     # B2 Application Key
S3_BUCKET=hermes-data                      # B2 存储桶名称
S3_ENDPOINT=https://s3.us-west-002.backblazeb2.com  # B2 端点地址
```

### 6.4 可选参数

```
S3_REGION=auto                           # 区域（默认 auto）
S3_PREFIX=hermes-data                    # 存储桶内路径前缀（默认 hermes-data）
S3_SYNC_INTERVAL=300                     # 自动同步间隔（秒，默认 300=5分钟）
```

### 6.5 同步机制说明

| 时机 | 操作 | 说明 |
|---|---|---|
| **容器启动时** | 从 B2 拉取数据 | 恢复配置、配对数据、记忆等 |
| **运行期间** | 每 5 分钟自动推送 | 确保数据实时备份 |
| **容器关闭时** | 推送最终数据 | 确保最近变更不丢失 |

> 只要配置了 B2，即使容器意外崩溃或你手动重新部署，所有数据都在 B2 中安全保存。

---

## 7. 管理后台变量

```
ADMIN_USERNAME=admin                     # 管理员用户名（默认 admin）
ADMIN_PASSWORD=your-strong-password      # 管理员密码（必设，否则自动生成随机密码）
```

---

## 8. 完整变量清单示例

以下是一个完整的配置示例（部署到 Render，用自定义 API + Telegram + B2）：

```
# ── 管理员 ──
ADMIN_USERNAME=admin
ADMIN_PASSWORD=MySecurePassword123

# ── LLM 提供商（自定义 API） ──
OPENAI_API_KEY=sk-my-custom-key
OPENAI_API_BASE=https://my-api.example.com/v1
LLM_MODEL=gpt-3.5-turbo

# ── 消息渠道 ──
TELEGRAM_BOT_TOKEN=7234567890:AAHxxxxxxxxxxxx
TELEGRAM_ALLOWED_USERS=123456789

# ── B2 持久化 ──
S3_ACCESS_KEY_ID=0045xxxxxxxxxxxx
S3_SECRET_ACCESS_KEY=K002xxxxxxxxxxxx
S3_BUCKET=hermes-data
S3_ENDPOINT=https://s3.us-west-002.backblazeb2.com

# ── 工具（可选） ──
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

---

## 9. 常见问题

### Q: 容器重启后配置丢失？

**检查 B2 配置是否正确。** 确保 `S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`、`S3_BUCKET`、`S3_ENDPOINT` 四个变量都设置正确。启动日志中会看到：

```
[s3-sync] pulled 5 files from s3://hermes-data/hermes-data
```

如果看到 `pull failed`，说明 B2 配置有误。

### Q: 第一次部署 B2 是空的？

正常。第一次部署时 B2 中没有数据，`pull` 不会报错。你在管理后台保存配置后，`push` 会自动把数据写入 B2。

### Q: 更换 B2 存储桶？

直接把 `S3_BUCKET` 改成新的桶名，重启后会从新桶拉取数据。

### Q: 后续想迁移到 Railway（有 Volume）？

删除所有 `S3_*` 环境变量，在 Railway 上挂载 Volume 到 `/data` 即可。
