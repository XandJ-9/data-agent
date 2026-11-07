# Worker 机制详解与并发性能提升方案

## 一、当前架构：基于数据库的 Durable Worker

### 1.1 核心设计原则

Worker 是**无状态执行器 + 有状态数据库**的组合。Worker 进程本身不持有任何内存队列或缓存——所有状态（任务排队、执行进度、租约）都在 `runs` 表中，重启不丢任务。

```
┌─────────────────────────────────────────────────────┐
│ Worker 进程                                         │
│                                                     │
│  run_forever() {                                    │
│    loop {                                           │
│      run_once()     // 认领并执行一个 run             │
│      memory_once()  // 提取记忆                      │
│      deliver_once() // 投递飞书                       │
│      sleep(poll_s)  // 全空才休眠                     │
│    }                                                │
│  }                                                  │
│                                                     │
│  依赖注入: runtime=, mcp_gateway= (测试可控)          │
└─────────────────────────────────────────────────────┘
         │                │              │
         ▼                ▼              ▼
   ┌──────────┐   ┌────────────┐  ┌──────────────┐
   │  SQLite  │   │ LLM API    │  │  Feishu API  │
   │  (状态)  │   │ (推理)     │  │  (消息投递)   │
   └──────────┘   └────────────┘  └──────────────┘
```

### 1.2 Run 状态机

```
           submit_message()
                 │
                 ▼
            ┌─────────┐
            │ queued  │ ← 用户提交消息后立即写入
            └────┬────┘
                 │ claim_run()
                 ▼
            ┌─────────┐
            │ running │ ← 被 Worker 认领，持有租约
            └────┬────┘
                 │ finish_run()
        ┌────────┼────────┐
        ▼        ▼        ▼
   succeeded  failed  interrupted  ← expired lease → 重新变成 running
                 ▲
                 │ cancel_run()
                 │
             cancelled
```

### 1.3 租约认领机制（Claim）

`claim_run()` 是整个并发控制的核心，在一个数据库事务内完成四件事：

```
claim_run(owner, lease_seconds, now):
  Step 1. 清理死 Worker
    UPDATE runs SET status='interrupted'
    WHERE status='running' AND lease_until <= now

  Step 2. 找到候选 run
    SELECT 最早的 queued run
    WHERE 同 session 没有 running 或更早的 queued
    → 保证同 session 串行执行

  Step 3. 锁住 session 行
    SELECT ... FROM sessions WHERE id=? FOR UPDATE SKIP LOCKED
    → 多 Worker 并发 claim 时，只有一个能拿到锁

  Step 4. 条件认领
    UPDATE runs SET status='running', owner=?, generation=generation+1, lease_until=?
    WHERE id=? AND status='queued'
      AND 同 session 仍无 running/更早 queued（双重检查）
    RETURNING *
```

关键点：
- **同 session 串行**：子查询保证一个 session 同时只有一个 run 在执行，避免回复顺序错乱
- **`SKIP LOCKED`**：多 Worker 同时 claim 时，拿不到锁的 Worker 跳过不阻塞
- **generation 递增**：每次状态变更都递增 generation，作为 fencing 校验的一部分

### 1.4 Fencing 机制

所有写操作都经过条件 UPDATE，校验四元组：

```
fence_check = {
  id == run.id,
  status == 'running',
  owner == run.owner,
  generation == run.generation,
  lease_until > now()
}
```

```
_if_fenced_update(run):
  UPDATE runs SET ...
  WHERE id=? AND status='running' AND owner=? AND generation=? AND lease_until>?

heartbeat(run):     _if_fenced_update(run) SET lease_until=now+lease_s
mark_finished(run): _if_fenced_update(run) SET status='succeeded'/'failed'

_cancelled(run):    SELECT 1 FROM runs WHERE <fence条件>
                    → 在工具循环每次迭代前检查
```

**一旦 fence 失败（被取消、租约到期被接管），后续所有操作立即无效。** 这保证了即使心跳线程死了、Worker 崩溃了，也不会出现重复执行或脏写。

### 1.5 心跳线程

```
run_once():
  stop = Event()
  thread = Thread(target=heartbeat_loop, daemon=True)
  thread.start()
  try:
    ... 执行 LLM 调用 + 工具循环 ...
  finally:
    stop.set()
    thread.join(timeout=2)

heartbeat_loop(run, stop):
  while not stop.wait(lease_s / 3):  # 每 lease/3 秒续一次
    store.heartbeat(run, lease_s)     # 条件 UPDATE lease_until
    if failed: return                  # fence 失败 → 停止续约
```

### 1.6 工具调度与执行追踪

```
_respond():
  runtime.respond(
    messages=..., memories=..., skills=...,
    tool_handler=_tool,          # 领域工具分发
    cancelled=_cancelled,        # 每轮检查 fence
    on_event=_step_recorder(run) # 实时写入 run_steps
  )

_tool(run, name, args):
  if _cancelled(run): raise 409   # 已被取消/接管
  if skill_disabled:  raise 403   # 技能被停用
  if name == 'search_history':
    return search_history(...)
  raise 403                        # 未知工具
```

每个事件独立事务写入 `run_steps`，前端可以**实时看到执行进度**——不等 run 完成。

### 1.7 三段式主循环

```
run_forever():
  while True:
    worked  = run_once()      // 优先级1: 用户对话
    worked |= memory_once()   // 优先级2: 记忆提取
    worked |= deliver_once()  // 优先级3: 飞书投递
    if not worked:
      sleep(poll_seconds)
```

`|=` 保证：只要任意一个做了事，立即进入下一轮不 sleep。只有全部空闲才休眠。

---

## 二、当前并发能力分析

### 2.1 并发模型

当前是**多进程串行**模型：

```
┌──────────┐  ┌──────────┐  ┌──────────┐
│ Worker 1 │  │ Worker 2 │  │ Worker 3 │  ← 水平扩展
│ 单线程   │  │ 单线程   │  │ 单线程   │
│ 一次一个 │  │ 一次一个 │  │ 一次一个 │
│ run      │  │ run      │  │ run      │
└──────────┘  └──────────┘  └──────────┘
     │              │              │
     └──────────────┴──────────────┘
                    │
              ┌──────────┐
              │  SQLite  │  ← 写锁瓶颈
              └──────────┘
```

### 2.2 瓶颈量化分析

| 瓶颈 | 类型 | 严重程度 | 说明 |
|------|------|----------|------|
| Worker 内串行 | IO 等待 | **高** | LLM 调用 10-60s，期间 Worker 完全空闲 |
| SQLite 写锁 | 锁竞争 | **中** | 多 Worker 同时 finish_run 需排队，耗时 ~1-10ms |
| Session 串行 | 设计约束 | **低** | 同一用户对话天然需要顺序，不应绕过 |
| claim 竞争 | 锁竞争 | **低** | SKIP LOCKED + 事务内操作，耗时 <1ms |

**典型场景吞吐量估算：**

```
单 Worker，LLM 平均响应 15s：
  吞吐 = 3600 / (15 + 心跳 + 持久化) ≈ 240 runs/小时

5 Worker 进程，SQLite 模式：
  理论 = 5 × 240 = 1200 runs/小时
  实际 ≈ 800-1000 runs/小时  (写锁竞争导致 ~20-30% 衰减)

PostgreSQL 模式：
  5 Worker 进程:
  实际 ≈ 1100-1200 runs/小时  (行级锁，竞争小)
```

### 2.3 核心矛盾

**LLM 调用是 IO 密集型（等远程 API），但 Worker 同步等待时 CPU 空闲。** 这是吞吐量上不去的主因。

```
run_once 的时间分布（典型值）:
  claim_run:         < 5ms    (数据库操作)
  prompt_messages:   < 10ms   (数据库查询)
  LLM round 1:       8-30s    ← 等待！CPU 空闲
  search_history:    < 5ms    (数据库查询)
  LLM round 2:       5-20s    ← 等待！CPU 空闲
  finish_run:         < 10ms   (数据库写入)
  ──────────────────────────
  总耗时: 13-50s，其中 99% 在等 IO
```

---

## 三、并发性能提升方案

### 3.1 方案对比

| 方案 | 改造范围 | 单 Worker 吞吐提升 | 复杂度 | 风险 |
|------|----------|-------------------|--------|------|
| A. 多进程水平扩展 | 无代码改动 | 线性（受 SQLite 锁衰减） | 低 | 低 |
| B. asyncio 协程化 run | Provider + Worker 循环 | 3-10x | 中 | 中 |
| C. 线程池执行 run | Worker 循环 | 3-5x | 低-中 | 中 |
| D. 异步任务队列（Celery/ARQ） | 整体架构 | 弹性伸缩 | 高 | 高 |

### 3.2 推荐方案：线程池并发（方案 C）

**为什么选线程池而非 asyncio：**
- 当前 `runtime`、`tools`、`storage` 全是同步代码，改造成本大
- Python GIL 在 IO 等待（LLM API 调用）时会释放，线程池效果接近 asyncio
- 不需要改 Provider、loop、tools 等核心模块

**架构改造：**

```
当前:
  run_forever():
    while True:
      worked = run_once()   ← 阻塞等 LLM
      worked |= memory_once()
      worked |= deliver_once()

改造后:
  run_forever():
    pool = ThreadPoolExecutor(max_workers=N)
    futures = set()
    while True:
      # 补充到并发上限
      while len(futures) < max_workers:
        run = store.claim_run(owner, lease_s)  # claim 仍串行
        if run is None: break
        future = pool.submit(_execute, run)    # 提交到线程池
        futures.add(future)
      # 等待任意一个完成
      done, futures = wait(futures, return_when=FIRST_COMPLETED, timeout=poll_s)
      for f in done:
        f.result()  # 捕获异常
      # 后台任务照常
      memory_once()
      deliver_once()
```

**改动清单：**

| 文件 | 改动 | 说明 |
|------|------|------|
| `src/worker.py` | `run_forever` 改造为线程池调度 | 核心改动 |
| `src/worker.py` | 提取 `_execute(run)` 静态方法 | 避免线程间状态共享 |
| `src/storage/__init__.py` | `Store` 构造函数支持连接池 | SQLite 需 `NullPool` → `QueuePool` |
| `src/settings.py` | 新增 `worker_concurrency` 配置项 | 默认 3，可调 |

**并发安全保证：**

1. **每个线程独立的 Store 实例**：`_execute()` 内部创建自己的 `Store`，不共享连接
2. **claim 仍串行**：主线程做 claim，线程池只负责执行
3. **Fencing 不变**：每个 run 的 `owner + generation + lease_until` 校验在线程池中照样生效
4. **SQLite WAL 模式**：支持多读单写，已启用

### 3.3 SQLite vs PostgreSQL 并发差异

```
SQLite (WAL 模式):
  读写:  无限并发读 + 1 个写
  影响:  finish_run 涉及多表写入，瞬时持锁

PostgreSQL (READ COMMITTED):
  读写:  行级锁 + MVCC
  影响:  同一 session 的 run 串行（SKIP LOCKED），不同 session 完全并行
```

**建议**：并发 Worker ≥ 3 时切换 PostgreSQL。

### 3.4 渐进式落地路径

| 阶段 | 内容 | 预期吞吐提升 |
|------|------|-------------|
| Phase 1 | `worker_concurrency` 配置 + 线程池执行 | 3-5x |
| Phase 2 | Provider 层连接复用（httpx connection pool） | 额外 1.2x |
| Phase 3 | PostgreSQL 迁移（可选） | 消除 SQLite 写锁衰减 |

---

## 四、Worker 配置参数调优

| 参数 | 默认值 | 说明 | 调优建议 |
|------|--------|------|----------|
| `lease_seconds` | 120 | Run 租约时长 | LLM 慢时适当加大，避免误判超时 |
| `poll_seconds` | 1 | 空闲轮询间隔 | 低负载可加大到 3-5s |
| `worker_concurrency` | 3（新增） | 单 Worker 并发 run 数 | = LLM API 并发限制 / Worker 数 |
| `llm_timeout` | 60 | 单次 LLM 调用超时 | 应 < lease_seconds |
| `max_tool_rounds` | 8 | 最大工具调用轮数 | 每轮一次 LLM 调用，8 轮可能很长 |

---

## 五、监控与运维

### 5.1 关键日志

```
# Worker 生命周期
Worker started (owner=xxx poll=1s lease=120s)
Run xxx claimed (model glm-5-3-flash)
Run xxx tool search_history({"query":"..."})
Run xxx succeeded in 23.5s
Run xxx failed after 15.2s (exc_info=True)

# 并发相关（新增）
Worker pool status: active=3/5 queued=12
```

### 5.2 健康检查

```sql
-- 卡住的 run（租约过期但未标记 interrupted）
SELECT id, status, lease_until, owner
FROM runs WHERE status='running' AND lease_until < unixepoch('now');

-- Worker 负载分布
SELECT owner, COUNT(*) FROM runs WHERE status='running' GROUP BY owner;

-- 排队深度
SELECT COUNT(*) FROM runs WHERE status='queued';
```

### 5.3 优雅关闭

```
SIGTERM →
  1. 停止 claim 新 run
  2. 等待所有 running run 完成或超时
  3. 清理线程池
  4. store.dispose()
```