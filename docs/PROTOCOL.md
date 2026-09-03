# PROTOCOL — MinoNexus ⇄ MinoScout

> **本文在 MinoNexus 与 MinoScout 两个仓库各有一份，必须逐字相同。**
> 契约真源是 `tests/fixtures/protocol/*.json`（两仓字节相同）。改协议的流程见各仓 `CLAUDE.md` §5。
> 协议版本：`1`

---

## 1. 传输与连接

| 项 | 规定 |
|---|---|
| 传输 | WebSocket，文本帧，UTF-8 JSON，一帧一条消息 |
| 方向 | **Scout 主动 dial Nexus**。Scout 永不监听对外端口 |
| 端点 | `ws(s)://<nexus-host>:<port>/node` |
| 单机默认 | `ws://mino.local:10104/node` |
| 鉴权 | 首帧 `REGISTER` 携带 `token`（配对时由 Nexus 下发）。Nexus 校验失败即关闭连接，close code `1008` |
| 重连 | Scout 侧指数退避（1s → 2s → 4s → … 上限 30s），无限重试 |
| 心跳 | Scout 每 15s 发 `HEARTBEAT`；Nexus 45s 未收到视为节点掉线 |
| 大载荷 | 截图以 base64 放在 JSON 里。单帧上限 32 MiB；超限时 Scout 必须降质重发而不是分片 |

## 2. 消息信封

所有消息共用同一信封：

```json
{
  "v": 1,
  "type": "EXECUTE",
  "msg_id": "01J8ZQ...",
  "reply_to": null,
  "ts": "2026-09-02T15:30:01.123456+08:00",
  "payload": { }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `v` | int | 协议版本。收到不认识的版本：Nexus 关连接并在 UI 报错；Scout 关连接并退避重连 |
| `type` | str | 见 §3 |
| `msg_id` | str | ULID。发送方生成，全局唯一 |
| `reply_to` | str \| null | 应答类消息填对应请求的 `msg_id` |
| `ts` | str | ISO-8601 带时区 |
| `payload` | object | 各消息自己的载荷 |

**应答规则**：`ACK 必须` 的消息，接收方必须回一条 `reply_to` 指向它的消息；发送方超时未收到则按 §6 重试。

---

## 3. 消息总表

| # | 方向 | `type` | ACK | 说明 |
|---|---|---|---|---|
| 1 | S→N | `REGISTER` | 必须 | 上报身份、能力、挂载设备 |
| 2 | N→S | `REGISTERED` | — | `REGISTER` 的应答 |
| 3 | S→N | `HEARTBEAT` | 否 | 保活 + 连通性/设备变化增量 |
| 4 | N→S | `OBSERVE` | 必须 | 取屏幕状态 |
| 5 | N→S | `EXECUTE` | 必须 | 执行一个动作 |
| 6 | S→N | `RESULT` | — | `OBSERVE` / `EXECUTE` 的应答 |
| 7 | N→S | `PROBE` | 必须 | 重新探连通性 |
| 8 | N→S | `CANCEL_RUN` | 必须 | 取消一次 run 的所有在途动作 |
| 9 | S→N | `NODE_EVENT` | 否 | Scout 主动上报（设备掉线、引擎崩溃） |
| 10 | N→S | `NODE_COMMAND` | 必须 | 对已连接节点下发 stop / restart / update |

> **协议里没有 `DECIDE` / `STEP_EVENT` / `RUN_FINISH` / `NEED_HUMAN`。**
> 执行循环在 Nexus（见 [ARCHITECTURE.md](ARCHITECTURE.md)），决策、trace 落库、问人全部在 Nexus 本地完成，不过网。十条消息里七条是 N→S 的单向指令。

---

## 4. 各消息载荷

### 4.1 `REGISTER`（S→N，ACK 必须）

```json
{
  "node_id": "3f8a1c0e9b2d4f71",
  "token": "<pair-token>",
  "platform": "darwin",
  "arch": "arm64",
  "scout_version": "0.1.0",
  "protocol_version": 1,
  "hostname": "mac-studio.local",
  "studio_id": "a1b2c3d4e5f67890",
  "executors": [
    {"id": "adb", "available": true,
     "provides": ["system_shell","system_pkg_install","system_pkg_clear","read_system_data",
                  "ui_native_input","ui_input_text","ui_screenshot",
                  "app_launch_native","app_force_stop","clipboard_set","key_event"]},
    {"id": "remote", "available": true,
     "provides": ["ui_native_input","ui_input_text","ui_screenshot","ui_stream",
                  "app_launch_native","clipboard_set","key_event","system_pkg_install",
                  "read_system_data","exec_script"]},
    {"id": "ios_wda", "available": false, "provides": [], "reason": "no WDA runtime"},
    {"id": "playwright", "available": true, "provides": ["ui_native_input","ui_screenshot"]}
  ],
  "devices": [
    {"sn": "R5CT30xxxx", "platform": "android", "model": "SM-S9210",
     "channels": {"adb": "connected", "remote": "connected"}},
    {"sn": "claw-abc123", "platform": "android",
     "channels": {"adb": "disconnected", "remote": "connected"}},
    {"sn": "web3f8a1c0e9b2d4f71", "platform": "web", "channels": {"playwright": "connected"}}
  ]
}
```

| 字段 | 规定 |
|---|---|
| `node_id` | 节点稳定标识，**不等于任何 `sn`**。取值 `[a-z0-9]{16}`（Scout 持久化的 `scout_id`），重启后不变。Playwright 槽 `sn` 为 `web` + `node_id` |
| `hostname` | 本机主机名，可空。只作展示 |
| `studio_id` | 写入本机 Scout 配置的工作台 id（`[a-z0-9]{16}`），可空。Nexus 据此记归属 |
| `executors[].id` | 必须是 `adb` / `remote` / `ios_wda` / `playwright` 之一 |
| `executors[].provides` | **abstract cap id 列表**。取值域由 `abstract_caps.yaml` 定义（Nexus 侧真源），Scout 只报字符串 |
| `executors[].available` | `false` 时 `provides` 必须为空数组，并给 `reason` |
| `devices[].channels` | 取值：`connected` / `disconnected` / `unauthorized` / `not_applicable` |

Nexus 收到后：`provides` ∩ 能力目录 → 该节点可执行的 capability 集合；`devices[]` 写入设备表并标记 `node_id` 归属。

### 4.2 `REGISTERED`（N→S）

```json
{
  "accepted": true,
  "session_token": "<short-lived>",
  "heartbeat_interval_sec": 15,
  "nexus_version": "0.1.0",
  "protocol_version": 1,
  "warnings": ["device claw-abc123 未在 Nexus 侧登记，已自动创建"]
}
```

`accepted: false` 时必须给 `reason`，Scout 记录后按退避重连（不要立刻重试，避免打爆）。

### 4.3 `HEARTBEAT`（S→N，不需 ACK）

```json
{
  "node_id": "3f8a1c0e9b2d4f71",
  "uptime_sec": 3721,
  "busy": true,
  "active_runs": ["run_20260902_153001_R5CT30"],
  "device_delta": [
    {"sn": "R5CT30xxxx", "channels": {"adb": "disconnected", "remote": "connected"}}
  ]
}
```

`device_delta` 只报**变化**的设备；无变化时可省略。Nexus 据此更新连通性，并在下一次组装菜单时生效。

### 4.4 `OBSERVE`（N→S，ACK 必须）

```json
{
  "run_id": "run_20260902_153001_R5CT30",
  "sn": "R5CT30xxxx",
  "kind": "screenshot",
  "prefer": ["adb", "remote"],
  "force_fresh": true,
  "timeout_sec": 15,
  "compress_ratio": 2.0
}
```

| `kind` | 返回 |
|---|---|
| `screenshot` | PNG/JPEG base64 + 宽高 + 来源通道 |
| `hierarchy` | UI 层级 dump（Android=uiautomator XML / iOS=WDA source / web=DOM 摘要） |
| `app_version` | 目标包版本号 |
| `foreground_app` | 当前前台包名 / bundle id |

`prefer` 是通道优先顺序；Scout 按序尝试，实际用了哪个在 `RESULT.source` 里报回。

`compress_ratio`（默认 `2.0`，`1.0` = 不压缩）目前只对 **playwright 通道**生效：Web 截图
按此比例缩小后转 JPEG，省带宽与 VLM token。**这是 Nexus 侧的设置**（上游
`system_settings_service.get_ai_web_compress_ratio`，按 LLM provider 取值），所以必须随
`OBSERVE` 下发 —— Scout 不读设置。

**重要：`RESULT` 里的 `width` / `height` 始终报原图尺寸**，与 `compress_ratio` 无关。
坐标体系因此不受压缩影响（Nexus 拿到的千分比坐标仍对应真实视口）。

### 4.5 `EXECUTE`（N→S，ACK 必须，幂等）

```json
{
  "run_id": "run_20260902_153001_R5CT30",
  "step_idx": 7,
  "sn": "R5CT30xxxx",
  "capability_id": "tap_element",
  "params": {"x": 512, "y": 830},
  "executor_order": ["remote", "adb"],
  "low_level": {"command": "TAP", "params": {"x": "{x}", "y": "{y}"}},
  "selected_impl": {"id": "vlm_locate_remote_tap", "needs_vlm": true},
  "device_hint": {"adb_serial": "R5CT30xxxx", "password": "***", "ttl_sec": 3600},
  "timeout_sec": 30
}
```

| 字段 | 规定 |
|---|---|
| `capability_id` | 能力 id。Scout 不校验它是否存在于目录（目录在 Nexus），只看自己的 executor `supports()` |
| `params` | **坐标一律是 0–1000 归一化千分比，不是像素。** Scout 负责按实际分辨率换算 |
| `executor_order` | **由 Nexus 算好**（含 AI 的 `expected_executor` / `fallback_executors` + 连通性过滤）。Scout 严格按此顺序尝试，不自行改序、不自行追加 |
| `low_level` | 抄自能力目录 YAML 的 `low_level` 段。`{x}` 这类占位符由 Scout 用 `params` 填充 |
| `device_hint` | Nexus 注入的设备凭据，避免 Scout 回查。**含敏感字段，不得写入 Scout 的日志** |

**幂等**：`(run_id, step_idx)` 是唯一键。Scout 必须缓存已完成的 `(run_id, step_idx) → RESULT`（建议保留至该 run 结束或 10 分钟），重复收到时**直接返回缓存结果，不重新执行**。

### 4.6 `RESULT`（S→N）

`OBSERVE` 与 `EXECUTE` 共用应答类型，靠 `reply_to` 区分。

```json
{
  "run_id": "run_20260902_153001_R5CT30",
  "step_idx": 7,
  "status": "pass",
  "summary": "remote TAP (512,830) -> ok",
  "error": "",
  "executor_used": "remote",
  "source": "remote",
  "elapsed_ms": 412,
  "attempts": [
    {"executor": "remote", "status": "pass", "elapsed_ms": 412}
  ],
  "image_base64": "",
  "image_mime": "",
  "width": 0,
  "height": 0,
  "extra": {}
}
```

| 字段 | 规定 |
|---|---|
| `status` | **五态之一（小写）**：`pass` / `fail` / `skipped` / `blocked` / `declined`。取值与上游 `EventStatus` 逐字一致。Router 的处置见 §4.6.1 |
| `executor_used` | 实际执行的 executor id。**不可省略**，Nexus 的 trace 与覆盖度依赖它 |
| `attempts` | fallback 链的逐次记录。全部失败时 `status` 取最后一次的结果 |
| `image_*` / `width` / `height` | 仅 `OBSERVE(screenshot)` 填 |
| `error` | 失败原因，面向人。**不得包含 `device_hint` 里的凭据** |

Scout **任何情况下不得让异常逃出** —— 内部异常一律转成 `status: "fail"` + `error`。

#### 4.6.1 五态与 Router 处置

取值与上游 `server/services/ai/regression/schemas.py::EventStatus` 逐字一致（**小写**，且是 `pass` 而不是 `OK`），避免搬迁时到处翻译名字。

| status | Router 行为 | 语义 |
|---|---|---|
| `pass` | 立即返回 | 做成了 |
| `blocked` | **立即返回** | 需要人介入 —— 换 executor 没有意义 |
| `declined` | 试下一个 executor | 主动让位，**不算故障**（日志 info，不计失败） |
| `fail` | 试下一个 executor | 真故障，但仍兜底换通道再试一次 |
| `skipped` | 试下一个 executor | 本次未执行（前置不满足等） |

**`declined` 与 `fail` 都会走 fallback**，区别在**是否算故障**，不在"换个 executor 有没有用"。只有 `blocked` 会中断 fallback 链。

`skipped` 是 executor 也会返回的值（上游 executors 里出现 6 次），不是 Nexus 独有的编排态。

### 4.7 `PROBE`（N→S，ACK 必须）

```json
{ "sn": "R5CT30xxxx", "channels": ["adb", "remote", "ios_wda", "playwright"], "timeout_sec": 20 }
```

强制重探连通性，绕过 Scout 侧缓存。应答用 `RESULT`，探测结果放 `extra.channels`。用于 UI 上的"重新检测"按钮和跑前闸门。

### 4.8 `CANCEL_RUN`（N→S，ACK 必须）

```json
{ "run_id": "run_20260902_153001_R5CT30", "reason": "user cancelled" }
```

Scout 停止该 run 的在途动作，丢弃其幂等缓存，应答 `RESULT` 且 `status: "pass"`。**已经发给设备的动作不保证能撤回** —— 语义是"不再继续"，不是"回滚"。

### 4.9 `NODE_EVENT`（S→N，不需 ACK）

```json
{
  "node_id": "3f8a1c0e9b2d4f71",
  "event": "device_lost",
  "sn": "R5CT30xxxx",
  "detail": "adb device offline",
  "severity": "warn"
}
```

`event` 取值：`device_found` / `device_lost` / `channel_changed` / `engine_crashed` / `shutting_down`。
Nexus 据此更新设备状态并广播给 UI。可丢 —— 权威状态以 `HEARTBEAT` 为准。

`shutting_down` 只在人主动停（`mino-scout stop` / SIGTERM）时发。断线重连不发。Nexus 收到后立刻失败该节点上的在途 run，不要等到心跳超时。Scout 会在这条之前再发一帧 HEARTBEAT，把 `active_runs` 对齐。

### 4.10 `NODE_COMMAND`（N→S，ACK 必须）

```json
{ "command": "stop", "reason": "studio" }
```

| `command` | Scout 行为 |
|---|---|
| `stop` | 应答 `RESULT` 后按本机 `mino-scout stop` 路径退出（先 `shutting_down`） |
| `restart` | 应答 `RESULT` 后安排一次自拉起，再退出 |
| `update` | 若本节点没有远程装包路径，应答 `RESULT` 且 `status: "fail"`，原因写清 |

应答用 `RESULT`（与 `PROBE` / `CANCEL_RUN` 相同）。**不能靠这条启动一台已经离线的专机。**

---

## 5. 时序

```mermaid
sequenceDiagram
  participant N as MinoNexus
  participant S as MinoScout
  participant D as 设备

  S->>N: REGISTER
  N-->>S: REGISTERED
  loop 15s
    S->>N: HEARTBEAT
  end

  Note over N: UI 触发批次，AgentExecutor 循环在 Nexus 内启动
  loop 每一步
    N->>S: OBSERVE {kind: screenshot}
    S->>D: 截图
    S-->>N: RESULT {image_base64, source}
    N->>N: planner.decide_next_action (LLM，本地)
    Note over N: 若 cap ∈ {human_*, assert_visual, persona, wait_ms}<br/>→ Nexus 本地 executor 处理，不出网
    N->>S: EXECUTE {capability_id, params, executor_order}
    S->>D: adb / remote / ios_wda / playwright
    S-->>N: RESULT {status 五态, executor_used, attempts}
    N->>N: emit_agent_event → trace → UI WS
  end
```

---

## 6. 超时、重试、幂等

| 场景 | 规定 |
|---|---|
| Nexus 等 `RESULT` 超时 | 用 `payload.timeout_sec` + 5s 宽限。超时后按同一 `(run_id, step_idx)` 重发一次；再超时记 `fail`，原因 `scout timeout` |
| Scout 收到重复 `(run_id, step_idx)` | 返回缓存的 `RESULT`，**不重新执行** |
| 连接断开 | Scout 退避重连并重发 `REGISTER`。在途 `EXECUTE` 的结果若已产生，重连后 Nexus 重发同键请求即可取到缓存 |
| Nexus 重启 | Scout 的 `REGISTER` 会重建节点登记。在途 run 由 Nexus 侧判为中断，不尝试续跑 |
| `NODE_EVENT` / `HEARTBEAT` 丢失 | 允许。状态最终由下一次 `HEARTBEAT` 收敛 |

**Scout 侧不做业务重试。** 同一动作换 executor 重试由 `executor_order` 表达（这是 Nexus 的决定）；同一 executor 内的机械重试（如 adb 偶发 `device offline`）允许，但必须记进 `attempts`。

---

## 7. 兼容性

- **加字段**：允许，接收方必须忽略不认识的字段
- **加消息类型**：允许，接收方对未知 `type` 记 warn 并忽略（不可关连接）
- **改字段语义 / 删字段 / 改 `type` 名**：不允许，必须 `v` 加一
- 双方在 `REGISTER` / `REGISTERED` 交换 `protocol_version`，不一致时**低版本方拒绝连接并明确报错**，不做降级协商

---

## 8. Fixture 哈希

契约真源：`tests/fixtures/protocol/`。两仓必须一致。

```
fixtures_sha256 = 944140bd44ee74bee07928116baa380982c5ebf9e77963e1124afe8999291ac6
```

`verify_protocol_contract.py` 校验：① 本仓 `protocol.py` 能 round-trip 全部 fixture；② fixture 目录哈希与上面记录一致。
