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

`desktop` 只处理 `platform=desktop`，不在 Android/Web 执行器中 fallback。支持 `screenshot/tap_xy/tap_element/input_text/press_key/launch_app/get_foreground_app`，其中截图复用 ScreenCapture 的 Desktop 分支。Cua Driver 0.34.1 是可选 native 依赖；manifest 消费 Cua `health_report` 的版本、会话、权限和 AX 状态，实际窗口捕获还须取得有效捕获引用。

Desktop 首版只接受 visual 流程。截图坐标按 Mino 的千分比转换成当前窗口图像像素，再带 capture ID 和 `ActionTarget.WINDOW(pid, window_id)` 交给 SDK；窗口到屏幕及 Retina/缩图坐标映射由 Cua 处理。输入与按键也需要本任务的新截图、未变化的会话代际和独占租约；精确窗口的前台激活、投递及校验由 Cua `delivery_mode=foreground` 负责。macOS 启动使用已配置 bundle ID，Windows 使用完整 EXE 路径或 AUMID，经结构化 launch 调用确认目标进程并绑定窗口，不要求启动后保持前台，不执行 shell。组合键和 modifiers 按目标 OS 规范化。动作证据保留 Cua effect/route/delivery/error.code；拒绝为 blocked，疑似无效果或部分执行为 fail，不标成功；无业务回读的动作仍需后续截图确认。

能力实现配置由 Nexus 的目录决定；Scout 不读取 YAML，也不让“在线”替代 readiness。

## Desktop 输入契约（0.1.76）

Nexus 将 Desktop 渠道映射为 `desktop_visual`，PC 输入只派给 `desktop`，不进入 Android / ADB 前台恢复。支持 `current_focus` 下的 `replace` 与 `append`；成功点击后由 Nexus 提供 `focus:<run_id>:<seq>`，Scout 只保存字段落点、窗口目标和窗口尺寸。输入前以新窗口截图的 capture ID 在 Cua 中重新点击该字段；窗口或尺寸变化、代际变化、取消、输入后或超时使引用失效。`replace` 经 Cua 发送 macOS Command+A / Windows Ctrl+A；`append` 发送 Command+Down / Ctrl+End 定位末尾，再输入正文。全部输入绑定相同窗口与 foreground 模式，不自行读取原生键盘焦点。

Windows 被测应用标识为完整 EXE 路径或 AUMID，macOS 为 bundle ID；不执行任意启动命令。Android 清缓存、force-stop、reset 等恢复动作不用于 Desktop。

Session Log 的 `tool/result` 包含节点、实际 executor、尝试链、capture_id、session_generation、驱动 effect/route 和安全的原生 evidence 类型；恢复子动作同样记录执行诊断。`observe/screen` 保留捕获引用，Workbench 可关联前后图和请求落点。自由文本 evidence detail 不展示，避免泄露输入内容。驱动局部 verify_state 未采集时明确标注，不能把动作投递成功等同于业务断言通过。

### 2026-10-12 看图模式兼容性修正

macOS 和 Windows 启动均通过 Cua `launch_app` 确认正整数 pid，再有界等待目标进程的普通窗口并绑定 `pid + window_id`；启动调用遵循已锁定 SDK 的工具 schema，不附加不支持的 session。启动准入不调用 `bring_to_front`，不要求 `activated=true` 或全局前台归属目标应用。仅请求受理且缺少有效 pid、目标窗口不存在或无法唯一选窗时仍阻断；启动成功不代表窗口截图、输入或业务断言已成功。

前台身份只消费 Cua `list_apps.active` 和配置的应用标识，不再调用自研 NSWorkspace/AX/Win32 探针。前台结果不能唯一确认或缺少应用身份时阻塞；不会把未知状态当作应用已离开前台。健康检查统一调用 Cua `health_report`，跳过 `screen_capture_capability`，避免健康探测顺带抓整屏或申请权限。

启动执行 Cua launch_app（已绑定且进程仍在则复用）→ 等待有效目标窗口 → 保存运行窗口绑定。窗口列表非空但没有可绑定普通窗口时继续有界等待，不重复启动。launch 诊断保留 stage、launched、activated、window_verified、foreground_verified 和目标 pid/window_id；成功 stage 为 window_ready，window_verified=true 表示窗口身份已确认，foreground_source=not_required、activated=false、foreground_verified=false 表示没有请求或验证持续前台激活，不表示启动失败。多个窗口优先选择当前可见窗口，并仅在完整 z_index 可唯一确定最前窗口时自动选择；否则要求明确 window_id，不随意选择列表首项。

点击的 typed `ActionResult` 与输入/按键的 `ToolResult.action`、JSON 回执分别解析。`unverifiable` 表示动作已投递但效果待回看，不视作已完成业务，也不当作投递失败；额外返回 `verification_required=true`。输入摘要明确标记“已写入当前焦点”，与 Nexus 的输入投递契约一致，业务结论由下一张截图确认。拒绝、部分执行、疑似无效果仍保留各自的驱动证据。启动失败的诊断保留 stage、pid、窗口与前台标识。

Desktop 图像的 `compress_ratio` 为 1–10，默认 1。通过 SDK 通用 `get_window_state(pid, window_id, include_accessibility_tree=false, include_screenshot=true, display_only=false)` 获取注册窗口捕获，保留 typed 输出未暴露的 capture_id。比值大于 1 时先校准原尺寸，再由 Cua `max_image_dimension` 生成并注册最终缩图；Scout 不自行缩放、裁切或重编码图像。最终图像尺寸等于 capture_width/capture_height，原尺寸记录在 native_width/native_height。截图和动作诊断保留 capture_scope=window、target、pid/window_id、capture_id、snapshot_id 和 session_generation。新截图失败会使旧观察引用失效，窗口丢失、所有者改变或捕获拒绝不回退整屏。

窗口截图不要求被测应用此刻占据全局前台。get_foreground_app 继续返回真实诊断；该结果不能推翻已确认的进程和窗口绑定，Nexus 的全局前台自动恢复也不用于 Desktop。后续输入的窗口存在、所有者、捕获几何与前台投递仍由 SDK 独立校验。此项不承诺所有应用均支持完全后台输入：点击、文本和按键继续使用 delivery_mode=foreground，让 Cua 在每个动作中处理所需激活和恢复；截图失败或 SDK 拒绝投递保留真实失败，不回退整屏、不改为成功。
