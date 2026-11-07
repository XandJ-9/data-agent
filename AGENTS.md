# Data Agent 服务边界

本目录是独立的多用户 Agent 服务，不属于 data-store 仓库。第一版不接入原数据平台的身份、菜单、数据库或工具授权；本文件定义本服务不可破坏的边界。

## 边界

- 第一版不导入 `backend/` 平台代码、不读取平台业务数据库、不接平台菜单或 SSO；后续平台跳转与 REST 工具连接必须显式新增边界，不得隐式直连。
- 账号仅由 `data-agent create-user` 管理，不开放注册；会话令牌只存摘要，Cookie HttpOnly + 同源 CSRF，所有变更请求校验精确 Origin。
- 原始消息与回复只追加不改写；压缩、修正、遗忘只能作用于派生记忆并登记来源排除，不得删除原文。
- Run 提交（输入 + queued）与完成（回复 + 投递 + 记忆脏标记）各自单事务原子；同会话跨进程按输入序号串行，跨进程互斥靠 `BEGIN IMMEDIATE` 认领 + 租约 fencing，过期执行者不得覆盖新状态；`interrupted` 不自动重放工具循环。
- 长期记忆更新用 revision CAS 同事务推进游标；冲突重读合并，失败退避不推进；并发用户编辑使进行中提取失效。
- Skill 由管理员部署：部署目录或管理界面上传（写入同一受校验目录，原子替换）；运行时快照 + 版本哈希；停用即时生效。SKILL.md 是纯提示词指引，系统不执行其中的代码或脚本。内置工具仅本人数据，凭模型不可指定用户身份。
- 会话支持重命名和归档：归档前检查活跃 Run（queued/running）和飞书绑定（channel_identities），存在即 409 拒绝；已归档会话禁止提交新消息和飞书 webhook 切换绑定；恢复归档仅取消时间戳标记。
- MCP 服务器仅管理员配置（`mcp_servers` 表）；工具统一以 `mcp_<server>_` 前缀注册、结果走 36000 字节统一截断；stdio 子进程与 http 端点视为受信管理面；env/headers 中的凭证不进日志、模型上下文或对普通用户的任何响应。单个服务器故障只影响对应工具调用，不得中断 Run。
- 飞书事件必须经官方 SDK 验签/解密，绑定码只存摘要、原子消费；投递经持久化 Delivery，unknown 不得盲目重试。
- 对外错误脱敏：不返回堆栈、SQL、密钥、绝对路径；凭证不进日志、模型上下文或快照。

## 分层架构（单向依赖，禁止反向 import）

```
cli / worker / api / channels        ← 进程编排与协议适配
         ↓
services / domains / skills          ← 业务用例与领域装配
         ↓
runtime / tools / providers          ← 可复用 Agent 核心（领域无关）
         ↓
storage                              ← 持久化子系统
```

- `runtime/` 是领域无关的编排核心（上下文预算、工具循环、Provider 协议）；只依赖 `tools/` 与标准库，禁止 import Settings、storage、domains、api。新增领域 = 在 `domains/<name>/` 装配 system prompt + 工具集 + Provider，不改 runtime。
- `tools/` 的 ToolRegistry 是唯一工具注册点：新工具 = 注册一个 ToolDefinition，不写 if/elif 分发。
- `tests/test_architecture.py` 用 AST 强制上述依赖规则，破坏分层即测试失败。
- 事务边界只在 services（及 worker/channels 的编排函数）开启；repository 方法收 `Connection`、永不自开事务；跨 repo 协作在单个事务内完成。

## 存储子系统（storage/）

- SQLAlchemy 2.0 Core（非 ORM）；`schema.py` 的 MetaData 是唯一 schema 事实来源，双后端共用。
- 双后端：默认 SQLite（`DATA_AGENT_DATABASE_PATH`），设 `DATA_AGENT_DATABASE_URL` 切 PostgreSQL（`postgresql+psycopg://...`）。
- 跨后端不变式：时间列一律 epoch 秒 Double（禁 DateTime）；布尔 0/1 SmallInteger；JSON 存 Text；部分唯一索引必须同时声明 `sqlite_where` 与 `postgresql_where`。
- 并发语义与后端无关：所有"读-判-写"折叠为带条件 UPDATE（+ rowcount/RETURNING）+ 租约 fencing（status/owner/generation/lease_until）；SQLite 用 `BEGIN IMMEDIATE` 串行化认领，PG 在 READ COMMITTED 下靠条件 UPDATE 保证同一结果。
- 一次性资源（绑定码）用 savepoint 原子消费，失败只回滚 savepoint 不中止外层事务。
- 迁移：第一版 `init_db()` = create_all + `_schema_version` 校验；schema 演进时再引入 Alembic baseline。

## 验证

```bash
uv run ruff check .
uv run pytest
cd web && npm run build && npm test
```

涉及真实模型或飞书联调时，在部署环境用真实凭证另行验证，不在测试中声称已通过。
