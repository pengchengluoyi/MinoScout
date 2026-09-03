# ENGINES — MinoScout

`EngineFactory` 是本仓对"一个能操作某台设备的对象"的唯一入口。

## 1. 它从哪来

上游 MiniOrangeServer 里，这件事是 `driver/agent/Crawl/device_bootstrap.bootstrap_mobile_engine()` 干的，只有两个调用点（`screen.py:381` 和 `remote_executor.py:138`），却拉进了整个 `driver/tentacle` 引擎层约 6,000 行。

搬迁时（[MIGRATION.md](MIGRATION.md) 的 **E3**）把它提炼成本仓的 `mino_scout/engines/factory.py`。**`driver/agent/Crawl` 这个位置在本仓不存在** —— 引擎工厂不该长在爬虫模块里。

## 2. 接口

```python
class EngineFactory:
    def get(self, sn: str, platform: str = "", *, reuse: bool = True) -> Engine: ...
    def drop(self, sn: str) -> None:      # 设备掉线 / 引擎崩溃时清理
    def active(self) -> list[str]:        # 当前持有连接的 sn，供 manifest 上报
```

`reuse=True` 时复用已建立的连接 —— 建 engine 是重操作（ClawNode 要握手、WDA 要拉会话），每步重建会显著拖慢一条用例。

## 3. 引擎种类

| Engine | 上游来源 | 对什么设备 | 连接方式 |
|---|---|---|---|
| `AdbEngine` | `driver/tentacle/engine/mobile/mAdb.py`（1,246 行） | Android，USB / TCP adb | `adbutils` + `uiautomator2` |
| `RemoteEngine` | `mRemote.py`（464 行） | 装了 ClawNode App 的手机 | ClawNode 反连 Scout 的 WS（见 §5） |
| `IosEngine` | `mIOS.py`（805 行）+ `ios_runtime.py` + `ios_appium_runtime.py` + `ios_config.py` | iOS 真机 / 模拟器 | usbmuxd + WDA，可选 Appium |
| `PcEngine` | `engine/pc/{mMac,mWindows}.py` | 本机桌面 | AppleScript / WinAPI |
| `WebEngine` | `engine/web/mChrome.py` | 浏览器 | 由 `playwright_hub` 持有 |

## 4. 多方案必须可插拔

**硬规矩：不允许 `if backend == "appium"` 这类分叉。**

iOS 一侧尤其容易踩 —— 它同时有 WDA 直连、Appium、simctl 三条路径。正确做法是三个实现类满足同一个 `Engine` 协议，由 `EngineFactory` 按探测结果选一个返回；错误做法是一个 `IosEngine` 里面到处 `if self.backend == ...`。

判据：**加第四种 iOS 后端时，如果需要修改现有类的方法体，说明抽象错了。**

## 5. ClawNode 连的是 Scout，不是 Nexus

```
ClawNode App ──WS──► MinoScout  mino_scout/clawnode/  (WS 服务端)
                          │
                          ▼
                    RemoteEngine ◄── EngineFactory.get(sn)
                          │
                          ▼
                   remote_executor
```

ClawNode 是**被 `remote` executor 驱动的设备**，所以它连 Scout。

这是相对上游的**行为变化**：上游 ClawNode 直连 server 的 `/ws`（`server/websocket/rWebsocket.py`）。拆分后配对配置里的 `ws_url` / `auth_token` / `gateway_id` 要指向 Scout。**需要与 `../ClawNode` 仓库协同改动。** 详见 [DEVICE_SETUP.md](DEVICE_SETUP.md) §ClawNode。

> Scout 因此需要**监听**一个局域网端口（ClawNode 无法被反向连接）。这不违反"Scout 不监听对外端口"—— 那条约束针对 Nexus↔Scout 方向。ClawNode 监听口必须绑局域网 + 配对 token 鉴权。

上游 `server/websocket/device_manager.py`（1,623 行）里的 ClawNode 连接管理、配对、`send_command`、capability manifest 摄取整段搬到 `mino_scout/clawnode/manager.py`；UI 广播与节点登记那半边留在 Nexus。这是全仓最大的一处需人工劈开的文件。

## 6. 生命周期与崩溃

| 事件 | 处理 |
|---|---|
| 设备掉线 | `EngineFactory.drop(sn)` + 发 `EXECUTE {capability_id: node.device_lost}` |
| 引擎崩溃（WDA 挂了 / u2 agent 死了） | `drop(sn)` + `EXECUTE {capability_id: node.engine_crashed}`，下次 `get()` 时重建 |
| Scout 进程重启 | 全部 engine 重建。**这是可接受的** —— Scout 无持久状态，代价上限是当前那一步重跑 |
| 长时间空闲 | 可以主动 `drop`，但要在 `HEARTBEAT.device_delta` 里如实反映连通性 |

**不要为了保住 engine 而引入优雅关闭依赖。** Scout 必须能被随时 kill。
