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
| WDA 挂了 | Scout 发 `NODE_EVENT {engine_crashed}`，下次 `get()` 重建 |

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
