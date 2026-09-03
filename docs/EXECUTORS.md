# EXECUTORS — MinoScout

本仓有且只有**四个** executor。它们的共同点是：直接操作设备。

上游 MiniOrangeServer 有八个，另外四个（`hitl` / `vlm` / `ai_persona` / `internal`）**零设备访问**，归 MinoNexus，不出网。

## 1. 能力矩阵

`provides` 是 abstract cap（词表真源在 Nexus 的 `abstract_caps.yaml`）。Scout 在 `REGISTER` 时上报这些字符串。

| abstract cap | adb | remote (ClawNode) | ios_wda | playwright |
|---|:--:|:--:|:--:|:--:|
| `system_shell` | ✅ | 受限 | — | — |
| `system_pkg_install` | ✅ 静默 | ⚠️ 需用户点"允许" | — | — |
| `system_pkg_clear` | ✅ | ⚠️ 需 device_owner | — | — |
| `read_system_data` | ✅ | 受限 | — | — |
| `ui_native_input` | ✅ | ✅ | ✅ | ✅ |
| `ui_input_text` | ✅ | ✅ | ✅ | ✅ |
| `ui_screenshot` | ✅ | ✅ | ✅ | ✅ |
| `ui_stream` | — | ✅ 独有 | — | — |
| `app_launch_native` | ✅ | ✅ | ✅ | — |
| `app_force_stop` | ✅ | ⚠️ | ✅ | — |
| `clipboard_set` | ✅ | ✅ | — | — |
| `key_event` | ✅ | ✅ | ✅ | — |
| `exec_script` | — | ✅（ClawNode ≥ 1.8.0） | — | — |

⚠️ = 条件可用。**条件不满足时必须返回 `declined`（让位），不是 `fail`** —— 否则这类「本来就做不到」会被计入失败率。

平台：`adb` / `remote` → Android（`remote` 也覆盖 iOS 的 ClawNode 形态）；`ios_wda` → iOS 真机与模拟器；`playwright` → Web，与 adb 平级（不再单做 CDP / BiDi 驱动）。

## 2. Executor 接口

```python
class Executor(Protocol):
    id: str   # "adb" | "remote" | "ios_wda" | "playwright"

    def supports(self, capability_id: str) -> bool:
        """Router 的硬校验。便宜、纯判断、不碰设备。"""

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        """跑这条动作。必须捕获全部异常，返回五态之一，永不抛。"""
```

五态语义见 [CONVENTIONS.md](CONVENTIONS.md) §2 —— **这是本仓最容易写错的地方**。

`ExecutorContext` 携带：当前屏幕（若 Nexus 已在 `EXECUTE` 前 `OBSERVE` 过并回传）、`capture_prefer` 通道优先序、`selected_impl`（Nexus 从能力目录抄来的 impl 元数据）、`shared` KV（同 run 内 executor 之间传递，如上一次定位结果）。

## 3. `low_level`：不写 Python 就能加能力

`EXECUTE` 载荷里的 `low_level` 段直接抄自 Nexus 能力目录 YAML：

```json
{"low_level": {"command": "TAP", "params": {"x": "{x}", "y": "{y}"}}}
{"low_level": {"shell": "input tap {x} {y}"}}
```

`executors/low_level.py` 负责：占位符 `{x}` 用 `params` 填充 → 按 `shell` / `command` 分派到对应通道 → 组装五态结果。

**因此绝大多数新能力在 Scout 侧零改动**：Nexus 加一条 capability YAML + 一条 `implementations`，Scout 自动能执行。只有引入**新原语**（现有 `command` / `shell` 表达不了的动作）时才需要动 Python。

这是上游 Skill Pack 方案定下的规矩，本仓继续遵守：**能用 `low_level` 表达的，不要写 `if capability_id == ...`。**

## 4. 加一个 executor

以假设的 `mac_applescript` 为例：

1. **Nexus 侧**（不在本仓）：`plugins/executors/mac_apple_script.yaml` 声明 `provides`；相关 capability 补一条 `implementations`
2. **本仓** `mino_scout/executors/mac_applescript_executor.py`：实现 `supports()` + `execute()`
3. **本仓** `mino_scout/probe/`：加连通性探测（这台机器上这个通道到底能不能用）
4. **本仓** `mino_scout/core.py::manifest()`：把它加进上报列表
5. `docs/EXECUTORS.md` 的矩阵补一列
6. 跑 `python scripts/verify_all.py`

**注意第 1 步在另一个仓库。** 只在 Scout 加 executor 不会被用到 —— Nexus 不知道它存在，就不会把它放进 `executor_order`。

## 5. 加一种"新原语"

如果新能力需要现有 `low_level` 表达不了的动作（例如"录屏 10 秒"）：

1. 先问：能不能拆成已有原语的组合？能就在 Nexus 侧用 `expands_to_events` 表达，本仓零改动
2. 不能，才在对应 executor 里加一个 `command` 分支，并在 `docs/EXECUTORS.md` §1 矩阵登记新的 abstract cap
3. 新 abstract cap 必须同时在 Nexus 的 `abstract_caps.yaml` 登记，否则 `REGISTER` 上报的字符串在 Nexus 侧无处匹配

## 6. 反模式

| 不要 | 为什么 |
|---|---|
| 在 executor 里判断"这一步该不该做" | 决策在 Nexus。Scout 只执行 |
| 在 executor 里调 LLM 兜底 | 违反硬约束 2，`verify_no_llm.py` 会拦 |
| 在 executor 里自行更换通道 | 换通道由 `executor_order` 表达，那是 Nexus 的决定 |
| 条件不满足时返回 `fail` | 应返回 `declined`，否则失败统计被污染 |
| 为一个能力写 `if capability_id == "xxx"` 长链 | 用 `low_level`。上游 `adb_executor.py` 的 if-chain 是被明确批评过的反模式 |
| 省略 `executor_used` | Nexus 的 trace 与覆盖度依赖它 |
