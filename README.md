# Data Agent 独立 AI 工作台

独立的多用户 Agent 服务（仓库外独立项目，与 data-store 平台目录并列）。第一版不接入原数据平台的身份、菜单、数据库或工具授权；模型调用、账号、会话、执行记录与长期记忆均由本服务独立持有。设计背景见 data-store 仓库的 [docs/极简Agent架构设计.md](../data-store/docs/极简Agent架构设计.md)。

## 功能范围（第一版）

- 独立多用户登录：管理员命令创建账号，服务端会话 Cookie + CSRF 保护，无自助注册。
- Web 对话工作台：会话管理、Markdown 回答、执行状态轮询、取消。
- 持久化执行：原始消息与回复不可改写保存；Run 状态机 `queued → running → succeeded / failed / cancelled / interrupted`，跨进程原子认领 + 租约 fencing，中断不自动重放。
- 可信 Skill：管理员部署目录，运行时快照，用户可启停；内置工具仅限本人历史检索与受限统计计算。
- 长期记忆：后台按完整轮次提炼跨会话记忆，revision CAS 合并，支持查看、修正、遗忘（遗忘排除派生记忆与旧来源，不删除原文）。
- 飞书私聊（可选）：HTTP 事件订阅 + 官方 SDK 验签解密，一次性绑定码绑定，回复经持久化投递记录分片发送。

## 快速开始

```bash
cd data-agent
uv sync --extra dev
uv run data-agent init-db
uv run data-agent create-user alice
uv run data-agent serve        # API + 静态工作台 http://127.0.0.1:8100
uv run data-agent worker       # 另开终端：执行 Run、投递与长期记忆任务
```

模型需在 `data-agent/.env` 配置 OpenAI 兼容 `DATA_AGENT_LLM_API_KEY` 与 `DATA_AGENT_LLM_MODEL`；未配置时服务可启动，Run 会以 `configuration_required` 失败。飞书接入配置见 `.env.example`。

### 存储：SQLite（默认）或 PostgreSQL

默认使用 SQLite（`DATA_AGENT_DATABASE_PATH`），零配置即可开发。生产或多人部署设：

```bash
# .env
DATA_AGENT_DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/data_agent
```

两个后端共用同一套 schema 与并发语义（条件 UPDATE + 租约 fencing），切换只改这一行；PG 需 `uv sync --extra postgres` 并先 `init-db`。

## 目录结构

```
src/
  runtime/        # 领域无关 Agent 核心：上下文预算、工具循环、Provider 协议
  tools/          # ToolRegistry：工具定义统一注册与分发
  providers/      # 模型 Provider 适配（OpenAI 兼容）
  domains/data/   # 数据助手领域装配：system prompt、领域工具、记忆提炼
  services/       # 业务用例层（唯一事务边界）
  storage/        # SQLAlchemy Core 双后端：schema、Database、repositories
  api/            # FastAPI 网关（路由 / schemas / 依赖）
  channels/feishu/# 飞书 webhook 验签 + 投递泵（outbox）
  worker.py       # Run / 记忆 / 投递 三个后台泵
  cli.py          # 进程入口
```

依赖单向：`api / channels / worker → services / domains → runtime / tools / providers → storage`；`runtime` 不依赖任何领域与存储模块（`tests/test_architecture.py` 强制）。

## 开发

```bash
uv run ruff check .
uv run pytest                                   # 默认 SQLite；PG 集成测试见 tests/test_postgres.py
cd web && npm ci && npm run build && npm test
```

架构约束见 [AGENTS.md](AGENTS.md)。
