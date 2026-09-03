# CONVENTIONS — MinoScout

## 1. 日志

沿用上游 `SLog` 的形态：模块级 `TAG` 常量 + `SLog.{d,i,w,e}(TAG, msg)`。

```python
TAG = "AdbExecutor"
SLog.i(TAG, f"tap ({x},{y}) serial={serial}")
```

| 规则 | 说明 |
|---|---|
| TAG 用驼峰模块名 | `AdbExecutor` / `EngineFactory` / `NodeTransport`，不带路径 |
| 一行一事 | 不要多行拼接；trace 归 Nexus，Scout 的日志只服务本机排障 |
| **不记凭据** | `EXECUTE.device_hint` 里的 `password`、`session_token`、`auth_token` 一律不入日志。需要时打 `password=***` |
| **不记截图 base64** | 打长度即可：`shot bytes=182344` |
| 每条 `EXECUTE` 记一行结果 | `run_id` 前 8 位 + `step_idx` + cap + executor + status + 耗时，便于和 Nexus 的 trace 对齐 |

## 2. 异常与五态（本仓最重要的约定）

**Executor 永不抛异常。** `execute()` 必须捕获自身全部异常，转成五态之一。取值**小写**，且是 `pass` 不是 `OK` —— 与上游 `EventStatus` 逐字一致。

| 状态 | 什么时候用 | 例子 |
|---|---|---|
| `pass` | 动作做成了 | 不要在"发出去了但不知道成没成"时用 `pass` |
| `fail` | 真故障 | 设备离线、坐标越界、shell 返回非 0 |
| `declined` | 我不适合做这条，让位 | `playwright_executor` 收到 Android 专属 cap；`remote` 缺 `device_owner` 做不了清数据 |
| `blocked` | 需要人介入才能继续 | 需人工确认的系统弹窗、设备被锁且无密码 |
| `skipped` | 本次未执行 | 前置条件不满足，压根没动手 |

三条推论：

- **只有 `blocked` 中断 fallback。** Router 收到 `blocked` 立即返回 —— 换 executor 不能让人凭空出现。`declined` / `fail` / `skipped` 都会继续试下一个。
- **`declined` 与 `fail` 的区别是"算不算故障"**，不是"换个 executor 有没有用"。写错不改变 fallback 走向，但会污染失败统计和归因。
- **`declined` 必须便宜。** 判断要在真正操作设备**之前**做出（`supports()` 或 `execute()` 开头），不要先折腾 20 秒再 `declined`。

对照上游实现：`server/services/regression/router.py:150-170`。

## 3. 坐标

**协议里的坐标一律是 0–1000 归一化千分比，不是像素。**

换算在 Scout 侧做，靠当前屏幕实际分辨率：

```python
px = round(x_permille * width  / 1000)
py = round(y_permille * height / 1000)
```

不要在 Nexus 侧换算 —— Nexus 不该知道设备分辨率。不要把像素坐标回传到 `RESULT` 里当权威值。

## 4. 超时

| 层 | 规定 |
|---|---|
| 协议给的 `timeout_sec` 是**硬上限** | Scout 内部所有等待加起来不得超过它 |
| 每个 executor 内部再留 20% 余量 | 便于在超时前返回一个有意义的 `fail` 而不是被外层掐断 |
| 机械重试计入总时长 | 不允许"重试 3 次每次 30 秒"把 30 秒的预算撑到 90 秒 |

## 5. 幂等

`(run_id, step_idx)` 是唯一键。实现要求：

- 完成后写缓存 `(run_id, step_idx) → RESULT`
- 重复收到同键 `EXECUTE`：**直接回缓存，不重新执行**
- 缓存生命周期：该 run 结束或 10 分钟，取先到者
- `CANCEL_RUN` 要清掉该 run 的全部缓存

## 6. 守门脚本

`scripts/verify_*.py`，沿用上游 45 个 `verify_*.py` 的写法：**纯静态、无需起服务、退出码非 0 即失败、失败时打出具体文件与行号**。

```bash
python scripts/verify_all.py     # 跑全部
```

| 脚本 | 断言 |
|---|---|
| `verify_no_orm.py` | 无 `sqlalchemy` / ORM 模型 / 迁移 |
| `verify_no_llm.py` | 无 LLM 调用与 prompt 常量 |
| `verify_no_yaml_catalog.py` | 不解析能力目录 YAML |
| `verify_no_nexus_import.py` | 无 `mino_nexus` / `server.*` 引用 |
| `verify_protocol_contract.py` | `protocol.py` 能 round-trip 全部 fixture + fixture 哈希一致 |

新增一条硬约束时，同时新增对应的 verify 脚本 —— **写在文档里的约束会漂移，写在脚本里的不会。**

## 7. 提交与 PR

- 协议改动必须两仓同 PR 周期完成，四步流程见 `CLAUDE.md` §5
- 从上游搬文件的 commit message 注明原路径：`port(scout): screen.py from MiniOrangeServer server/services/regression/screen.py`
- 不在同一个 commit 里既搬迁又重构。**先原样搬（保证行为一致），再单独 commit 改造**
