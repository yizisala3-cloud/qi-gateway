# qi-gateway

统一网关 — 积温 + Eventide + 记忆注入

## 当前状态

**Phase 1**: 透传骨架，能正常聊天

## 架构

```
橘瓣 → qi-gateway(/v1/chat/completions) → 上游 LLM → qi-gateway → 橘瓣
```

后续 Phase 会逐步加入：
- Phase 2: 积温引擎（主动意识 + 语气注入）
- Phase 3: Eventide（身体状态卡注入）
- Phase 4: 记忆注入（替代 OB Gateway）
- Phase 5: 主动消息
- Phase 6: 对话后情绪分析

## 部署

### 环境变量

| 变量 | 说明 |
|------|------|
| `GATEWAY_TOKEN` | 网关鉴权 token（橘瓣填的 API Key） |
| `UPSTREAM_BASE_URL` | 上游 LLM API 地址（如中转站） |
| `UPSTREAM_API_KEY` | 上游 API Key |
| `UPSTREAM_MODEL` | 默认模型名 |
| `SUPABASE_URL` | Supabase 项目地址 |
| `SUPABASE_KEY` | Supabase anon key |
| `PORT` | 端口（默认 8000） |

### Zeabur 部署

1. 连接 GitHub 仓库 `yizisala3-cloud/qi-gateway`
2. 配置环境变量
3. 部署后访问 `/health` 确认运行

### 橘瓣连接

- 提供商格式：OpenAI
- API Base URL：`https://你的域名/v1`
- API Key：填 `GATEWAY_TOKEN` 的值
- 模型名：填 `UPSTREAM_MODEL` 的值

## 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 核心聊天接口，OpenAI 兼容 |
| `/v1/models` | GET | 模型列表 |
| `/health` | GET | 健康检查（无需鉴权） |
| `/status` | GET | 网关状态（需鉴权） |

## 开发

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入实际值
uvicorn gateway.main:app --reload --port 8000
```

