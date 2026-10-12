# DEVICE_SETUP — MinoScout

一台机器要成为可用的 Scout 节点，需要哪些环境。Scout 启动时会探一遍，探不到的通道在 `REGISTER` 里报 `available: false` + `reason`。

## 1. 通道总览

| 通道 | 必需外部依赖 | 探测方式 | 探不到的后果 |
|---|---|---|---|
| `adb` | `adb` 在 PATH；设备开 USB 调试并已授权 | `adb -s <sn> shell echo ok` 含 `ok` | 该 Android 设备只能走 `remote`，清缓存 / 读 SIM / 静默装包不可用 |
| `remote` | 设备装 ClawNode App 并已与本 Scout 配对 | WS 连接状态为 `Authenticated` | 生产手机（无 USB 调试）完全不可控 |
| `ios_wda` | usbmuxd；WDA 已装并可拉起；真机需签名 | 能建 WDA session | iOS 设备不可控 |
| `playwright` | `playwright install chromium` 已执行 | `playwright_hub` 能拉起 Chromium | Web 用例不可跑 |

## 2. adb

```bash
adb devices -l        # 应看到设备且状态为 device，不是 unauthorized / offline
```

| 常见问题 | 处理 |
|---|---|
| `unauthorized` | 手机上确认 RSA 指纹弹窗；`REGISTER` 里该设备 `channels.adb = "unauthorized"` |
| 多台机器抢同一台设备 | adb server 是单机独占的。**同一台设备不要挂在两个 Scout 节点上** —— Nexus 的 `node_id` 归属唯一，重复挂载行为未定义 |
| TCP adb 断连 | Scout 会在 `HEARTBEAT.device_delta` 里报 `disconnected`，不自行反复重连 |

## 3. ClawNode（`remote` 通道）

**这是相对上游 MiniOrangeServer 的行为变化：ClawNode 连 Scout，不再连 server。**

```
ClawNode App ──WS──► MinoScout 的监听口（局域网）
```

配对流程（沿用上游 `PAIR_CONFIG` 语义，端点换成 Scout）：

1. Scout 启动时通过 mDNS 广播自己（服务类型沿用 `_miniorange-gw._tcp`，或按需另定）
2. ClawNode 发现并请求配对
3. 人在 UI 上确认 → Nexus 通知 Scout → Scout 下发 `PAIR_CONFIG`（`ws_url` 指向 Scout 自己、`auth_token`）
4. ClawNode 用该配置连上 Scout，状态推进到 `Authenticated`
5. Scout 向 ClawNode 请求 `GET_CAPABILITIES`，把结果并入自己的 manifest 上报给 Nexus

**待协同事项**：上述第 3 步的 `ws_url` 从"server 地址"改为"Scout 地址"，需要 `../ClawNode` 仓库配合。这是拆分的已知跨仓依赖，见 [MIGRATION.md](MIGRATION.md) §已知行为变化。

ClawNode 能力受设备授予的运行时权限影响：

| 能力 | 条件 |
|---|---|
| `system_pkg_clear`（清数据） | 需 `device_owner`。生产机多数没有 → **返回 `declined`**，让位给 Nexus 侧的 `ai_persona`（拟人化走设置页） |
| `system_pkg_install` | 需用户在 UI 上点"允许安装" |
| `exec_script` | ClawNode ≥ 1.8.0 |
| `ui_stream` | ClawNode 独有，adb 只能轮询截图 |

## 4. iOS（`ios_wda` 通道）

| 项 | 要求 |
|---|---|
| 真机 | usbmuxd 可用；WDA 已用有效签名安装；设备已信任本机 |
| 模拟器 | `simctl` 可用；UDID 形态与真机不同，由 `ios_ids` 区分 |
| Appium | 可选。走的是同一个 `Engine` 协议的另一个实现，**不是 `if backend` 分支**（见 [ENGINES.md](ENGINES.md) §4） |
| WDA 挂了 | Scout 发 `EXECUTE {capability_id: node.engine_crashed}`，下次 `get()` 重建 |

`../WebDriverAgent` 是本地的 WDA 工程副本，可用于重新签名与安装。

## 5. Playwright（Web 通道）

```bash
playwright install chromium
```

- Chromium 由 `playwright_hub` 在 Scout **进程内**拉起，和 adb 平级
- 设备列表里的 `web` + `scout_id`（如 `web3f8a1c0e9b2d4f71`）是**虚拟槽**，不是真实设备行
- Chrome 走 CDP、Firefox 走 BiDi 都由 Playwright 消化。**不要另做 CDP / BiDi 驱动**
- Web 场景优先按按钮 / 链接名字点，坐标兜底 —— 因此 `playwright` 的实现 `needs_vlm: false`，比 VLM 路径 cost 更低

## 6. 网络

| 项 | 规定 |
|---|---|
| Scout → Nexus | 出向 WS。默认 `ws://mino.local:10104/node`（Nexus 启动时注册该名） |
| Scout 监听 | 只为 ClawNode，绑局域网，配对 token 鉴权 |
| 系统代理 | 必须绕过 `mino.local` 与 loopback。本仓 `configure_proxy_bypass` 写入 `no_proxy` |

## 7. 自检

```bash
mino-scout probe          # 只探测并打印 manifest，不连 Nexus
```

输出即 `REGISTER` 里将要上报的 `executors[]` + `devices[]`。**部署一台新节点时先跑这个**，确认矩阵符合预期再接入 Nexus。


## V4 PC 桌面通道

每台宿主登记一个 `desktop:<node_id>:console`，与 `web<node_id>` 和手机设备并存。Scout 重启会更新 `session_generation`，不会增加设备。窗口/显示器不创建额外目标。

| 宿主 | 当前执行范围 | 未就绪时 |
|---|---|---|
| macOS arm64/x86_64 | 可选 Cua Driver 0.34.1 适配，visual 截图/捕获坐标点击/输入/按键/启动及聚焦目标应用；实际硬件与权限仍需验收 | 分别报告无会话、锁屏、权限不足、插件缺失/版本不符或 native 探测失败 |
| Windows x64/arm64 | Cua Driver 0.34.1 visual 截图/捕获坐标点击/输入/按键/启动及聚焦目标应用；发行包目前为 x64，arm64 需源码安装；真实 Windows 验收仍待完成 | 无交互登录、RDP 断开、Session 0、锁屏、SDK 缺失/不兼容和 native 探测失败均禁止输入 |
| Linux | 宿主与图形会话环境可见；原生 Desktop 执行未开放 | `no_session` 或 `unsupported` |

源码安装按需要使用 `pip install ".[desktop]"`，只接受锁定的 SDK 版本。macOS/Windows 发布流水线包含该 extra，并在冻结前检查原生库可导入；冻结包同时收集 SDK 原生库及 distribution metadata；运行层 ABI 升级到 4，旧运行层不可冒充已具备 Desktop。构建配置不等于实际发布/安装验收。

macOS 必须在已登录的交互会话内启动，并给实际运行 Scout 的程序授予屏幕录制和辅助功能权限。可在 Workbench 的节点页展开“处理权限”；按系统提示重启程序。探测只读，不代用户授权或解锁。仅安装 Scout/SDK 不会使桌面自动变成可选状态。

Windows 计划任务使用安装用户的 `Interactive` 登录态和 `Limited` 权限，在该用户登录时启动；不再用 SYSTEM/Session 0 操作桌面。重跑安装会更新同名任务；原有 SYSTEM 部署须在实际登录用户内重新运行安装。RDP 会话必须保持连接。探测读取输入桌面名称，不切换桌面，不操作锁屏或 UAC 安全桌面；权限边界不足时由驱动拒绝，不能默认提升权限。

任务应用标识：macOS 使用 bundle ID；Windows 使用完整 EXE 路径或打包应用 AUMID（例如 `Microsoft.WindowsNotepad_8wekyb3d8bbwe!App`），不使用模糊进程名。启动、聚焦后回读前台身份；输入前再次确认任务目标，不能切换到另一个应用。按键支持 `key` 与字符串列表 `modifiers`，或 `Ctrl+C` 形式；`primary` 在 macOS 映射 Command、Windows 映射 Ctrl。

桌面操作共用一个持久 SDK、事件循环和串行线程；截图带 capture ID，动作尝试消费截图引用。输入要求服务端任务租约及相同会话代际。Windows 会话身份包含登录 AuthenticationId，锁屏/注销/会话切换都会使旧引用失效。超时结果未知时停止接纳该 native runtime 的新操作，查看真实状态并重启 Scout；不自动重试、不创建另一条 native lane。取消立即使引用失效并串行结束 SDK 会话；进程退出关闭 SDK。

当前完成的是协议、HTTP/WebSocket 与 SDK 替身的隔离联调，尚未验收真实原生应用截图、点击、输入和跨 OS 部署，不能标为全部 PC 已支持。

## 0.1.76 连接与桌面诊断

`mino-scout status` 的 `running` 仅表示进程存活；网页在线需要该进程成功连接并注册到 Nexus。检查 `connection.state`、`connected`、`registered`、`last_error` 与最近心跳。`registered` / `heartbeat` 表示完成注册；`registration_rejected` 保留拒绝原因，`stale` 表示诊断超过 90 秒未更新，旧进程未上报时显示 `unknown`。升级磁盘载荷后还需要重启才能使用新诊断。不要因此重新建库、重绑账号或清空配置。

`mino-scout probe` 显示完整设备描述及桌面会话、权限和执行能力。macOS 需要当前登录桌面以及辅助功能、屏幕录制权限；Windows 需要目标用户的交互桌面，SYSTEM 后台任务不能执行 PC 操作。Linux 本版本仅显示真实可用能力，不宣称已实现原生桌面执行。

## 0.1.77 旧插件隔离与节点在线

本版本修正已接受 REGISTER 后因本地旧插件配置待认领而断线的问题：保留节点身份和旧插件文件，节点继续连接并上报心跳；旧插件保持停用，不上传历史插件清单或消息，插件操作返回 blocked。管理员需通过明确的插件归属维护流程处理旧配置，重装或删除配置不是解法。
