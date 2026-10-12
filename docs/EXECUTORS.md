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

`ExecutorContext` 携带：当前屏幕（若 Nexus 已在本步前 `EXECUTE screenshot` 并把帧带下来）、`capture_prefer` 通道优先序、`selected_impl`（Nexus 从能力目录抄来的 impl 元数据）、`shared` KV（同 run 内 executor 之间传递，如上一次定位结果）。

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


## Desktop 执行器（V4）

`desktop` 只处理 `platform=desktop`，不在 Android/Web 执行器中 fallback。支持 `screenshot/tap_xy/tap_element/input_text/press_key/launch_app/get_foreground_app`，其中截图复用 ScreenCapture 的 Desktop 分支。Cua Driver 0.34.1 是可选 native 依赖；权限、会话、版本和实际捕获未就绪时 manifest 明确报告不可用。

Desktop 首版只接受 visual 流程。截图坐标按 Mino 的千分比转换成捕获像素，再带 capture ID 交给 SDK。输入与按键也需要本任务的新截图、未变化的会话代际、独占租约及目标应用前台确认。macOS 启动使用已配置 bundle ID，Windows 使用完整 EXE 路径或 AUMID，经结构化 launch/focus 调用并回读前台，不执行 shell。组合键和 modifiers 按目标 OS 规范化。动作证据保留 Cua effect/route/delivery/error.code；拒绝为 blocked，疑似无效果或部分执行为 fail，不标成功；无业务回读的动作仍需后续截图确认。

能力实现配置由 Nexus 的目录决定；Scout 不读取 YAML，也不让“在线”替代 readiness。

## Desktop 输入契约（0.1.76）

Nexus 将 Desktop 渠道映射为 `desktop_visual`，PC 输入只派给 `desktop`，不进入 Android / ADB 前台恢复。支持 `current_focus` 下的 `replace` 与 `append`；`replace` 使用 macOS Command+A / Windows Ctrl+A，再发送正文。成功点击后由 Nexus 提供 `focus:<run_id>:<seq>`，Scout 保存当时的原生键盘焦点并在输入前重验。焦点变化、代际变化、取消、输入后或超时会使引用失效；无法确认焦点时拒绝发送正文。

Windows 被测应用标识为完整 EXE 路径或 AUMID，macOS 为 bundle ID；不执行任意启动命令。Android 清缓存、force-stop、reset 等恢复动作不用于 Desktop。

Session Log 的 `tool/result` 包含节点、实际 executor、尝试链、capture_id、session_generation、驱动 effect/route 和安全的原生 evidence 类型；恢复子动作同样记录执行诊断。`observe/screen` 保留捕获引用，Workbench 可关联前后图和请求落点。自由文本 evidence detail 不展示，避免泄露输入内容。驱动局部 verify_state 未采集时明确标注，不能把动作投递成功等同于业务断言通过。

### 2026-10-12 看图模式兼容性修正

macOS 启动先等待目标进程的普通窗口，再按 `pid + window_id` 调用 `bring_to_front`；调用参数遵循已锁定 SDK 的工具 schema，不向不接受 session 的启动和激活工具附加该字段。检查激活回执，再在有界等待内读回目标应用的前台状态，不重复提交启动或激活请求。Windows 继续按系统支持的 EXE/AUMID 目标及进程激活路径执行。

macOS 前台读回使用 AXFocusedApplication 的原生 PID 与 Cua 返回的应用身份交叉匹配，不仅依赖 list_apps 的 active 标记；启动验证、get_foreground_app 及每次输入前采用同一读取路径，返回来源为 macos_ax。原生读回失败就阻塞，不回退到陈旧的 active 标记，也不读取控件正文。

点击的 typed `ActionResult` 与输入/按键的 `ToolResult.action`、JSON 回执分别解析。`unverifiable` 表示动作已投递但效果待回看，不视作已完成业务，也不当作投递失败；额外返回 `verification_required=true`。输入摘要明确标记“已写入当前焦点”，与 Nexus 的输入投递契约一致，业务结论由下一张截图确认。拒绝、部分执行、疑似无效果仍保留各自的驱动证据。启动失败的诊断保留 stage、pid、窗口与前台标识。

Desktop 图像的 `compress_ratio` 为 1–10，作用于输出图像；运行时保存的 capture 像素尺寸保持不变。截图结果保留 capture_id、session_generation、capture_width、capture_height 与实际压缩比。新截图失败会使旧观察引用失效，不能继续按陈旧图点击。
