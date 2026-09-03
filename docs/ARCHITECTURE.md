# ARCHITECTURE — MinoScout

## 1. 在整体里的位置

```
┌──────────┐   HTTP :10104   ┌───────────────────────┐   WS /node   ┌──────────────────┐
│ Electron │ ──────────────► │      MinoNexus        │ ◄──────────  │    MinoScout     │
│    UI    │ ◄── WS 推送 ──── │  大脑：循环 / LLM /    │   (Scout     │  手脚：设备操作   │
└──────────┘                 │  能力目录 / 数据库     │    主动连)    └────────┬─────────┘
                             └───────────────────────┘                       │
                                                              adb / WDA / ClawNode / Playwright
                                                                             │
                                                                    ┌────────▼─────────┐
                                                                    │  Android / iOS   │
                                                                    │  / 浏览器        │
                                                                    └──────────────────┘
```

**UI 只跟 Nexus 说话。** Scout 不对 UI 暴露任何接口。

## 2. 四层套娃里 Scout 占哪两层

上游 MiniOrangeServer 的执行是四层：

| 层 | 归属 |
|---|---|
| ① 任务编排（多设备、多用例、闸门、落库） | Nexus |
| ② 单用例循环（observe → decide → dispatch） | **Nexus**（决策在这层） |
| ③ 动作分发（选 executor、fallback） | **拆开**：选路在 Nexus，尝试在 Scout |
| ④ 设备动作 | **Scout** |

③ 为什么拆：选哪个 executor 需要知道能力目录和当前连通性（Nexus 有），而依次尝试、处理失败、返回五态是纯执行（Scout 做）。所以 Nexus 算出 `executor_order` 塞进 `EXECUTE`，Scout 照单执行。

## 3. 内部分层

```
transport/node.py           ← WS 客户端，收发九条消息，幂等缓存
        │
        ▼
core.py  ScoutCore          ← execute() / observe() / manifest()
        │                      纯本地、零网络、可单测
        ├─────────────► router.py         按 executor_order 依次尝试
        │                     │
        │                     ▼
        │              executors/         adb / remote / ios_wda / playwright
        │                     │           + low_level 通用执行
        │                     ▼
        │              engines/           EngineFactory → Remote / iOS engine 对象
        │
        ├─────────────► screen.py         四通道截图
        ├─────────────► hierarchy.py      UI 树
        └─────────────► probe/            连通性探测
```

**`ScoutCore` 里禁止出现连接形态的分支**（`if 本地 / if 远程`）。core 只认 `ExecuteRequest → ExecuteResult`，transport 是外层的事。

## 4. 一次 `EXECUTE` 的完整路径

以 `tap_element`、`executor_order: ["remote","adb"]` 为例：

```
1. transport/node.py 收到 EXECUTE 帧
2. 幂等检查：(run_id, step_idx) 已完成 → 直接回缓存的 RESULT，结束
3. 交给 ScoutCore.execute(ExecuteRequest)
4. router.dispatch:
   4.1 取 executor_order[0] = "remote"
   4.2 remote_executor.supports("tap_element") → True
   4.3 params 里的 0–1000 千分比坐标 → 按设备实际分辨率换算成像素
   4.4 EngineFactory.get(sn) → RemoteEngine（复用已有连接）
   4.5 按 low_level.command = "TAP" 发命令
   4.6 拿到结果（五态，小写）：
       - pass     → 返回
       - blocked  → 立即返回，中断 fallback（需要人介入，换 executor 没意义）
       - declined → 记入 attempts，取 executor_order[1] = "adb" 重来（不计失败）
       - fail     → 记入 attempts，取下一个；全试完返回最后一次的 fail
       - skipped  → 同 fail 的走向，但语义是「压根没动手」
5. 组装 RESULT：status / executor_used / attempts / elapsed_ms
6. 写幂等缓存
7. transport 回 RESULT 帧（reply_to = 请求的 msg_id）
```

**第 4.6 步的五态语义是本仓的核心契约。** 只有 `blocked` 中断 fallback；`declined` / `fail` / `skipped` 都继续试下一个。见 [CONVENTIONS.md](CONVENTIONS.md) §2。

## 5. 一次 `OBSERVE(screenshot)` 的路径

```
transport → ScoutCore.observe → screen.py
  按 prefer 顺序尝试：
    adb        → subprocess `adb -s <serial> exec-out screencap -p`
    remote     → EngineFactory.get(sn) → ClawNode GET_SCREENSHOT
    ios_wda    → WDA session screenshot
    playwright → playwright_hub 里的 page.screenshot()
  空白帧检测（shot_is_blank）→ 判空则换下一个通道
  → RESULT { image_base64, image_mime, width, height, source }
```

**缩略图不在 Scout 侧做。** Scout 回原图，Nexus 负责压缩成 trace 用的 thumb —— 因为 thumb 只服务 UI，而 UI 是 Nexus 的事。

## 6. ClawNode 在哪一侧

ClawNode（被测手机上的 App）是**被 `remote` executor 驱动的设备**，因此它连 **Scout**，不连 Nexus。

```
ClawNode App ──WS──► MinoScout (clawnode/ 模块，WS 服务端)
                         ▲
                         │ remote_executor 通过 EngineFactory 拿到该连接
```

这是与上游 MiniOrangeServer 的一个**行为变化**：上游 ClawNode 直连 server 的 `/ws`。拆分后配对配置（`ws_url` / `auth_token` / `gateway_id`）要指向 Scout。**需要与 ClawNode 仓库协同改动**，见 [DEVICE_SETUP.md](DEVICE_SETUP.md) §ClawNode 与 [MIGRATION.md](MIGRATION.md) §已知行为变化。

> 例外：Scout 是这条链路上唯一需要**监听**端口的地方（ClawNode 无法反向被连）。这不违反"Scout 不监听对外端口"的约束 —— 那条约束针对的是 Nexus↔Scout 方向。ClawNode 监听口应绑局域网、用配对 token 鉴权。

## 7. 状态与生命周期

| 状态 | 存在哪 | 重启后 |
|---|---|---|
| 设备连接 / engine 对象 | 进程内存（`EngineFactory` 缓存） | 重建 |
| 连通性探测结果 | 进程内存 + 短 TTL | 重探 |
| `(run_id, step_idx) → RESULT` 幂等缓存 | 进程内存，run 结束或 10 分钟后清 | 丢失（Nexus 重发会重新执行，可接受） |
| 截图原图 | 临时文件，用后即删 | — |
| **任何业务数据** | **不存**，全在 Nexus | — |

**Scout 可以随时被 kill 并重启**，代价上限是"当前那一步重跑一次"。这是硬要求，不要引入需要优雅关闭才能保住的状态。
