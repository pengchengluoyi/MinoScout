# CLAUDE.md — MinoScout

本文件是给 Claude Code / 任何在本仓库工作的人的**硬约束清单**。写代码前先读这里。

---

## 0. 本仓库的定位

MinoScout 是执行器。它接收 [MinoNexus](../MinoNexus) 下发的动作，操作真实设备，回报结果。

上游来源是 [MiniOrangeServer](../MiniOrangeServer)（**已停止维护，只读参考，禁止改动**）。搬迁映射见 [docs/MIGRATION.md](docs/MIGRATION.md)。

---

## 1. 四条硬约束（违反即 CI 失败）

| # | 约束 | 守门脚本 | 为什么 |
|---|---|---|---|
| 1 | **不碰数据库** —— 不 import `sqlalchemy`、不定义 ORM 模型、不写迁移 | `scripts/verify_no_orm.py` | 数据归属方是 Nexus。Scout 有状态就没法随时重启、没法多节点 |
| 2 | **不调大模型** —— 不 import `openai` / `httpx` 打 LLM endpoint、不含 prompt 常量 | `scripts/verify_no_llm.py` | 决策权在 Nexus。Scout 一旦能"想"，两边就会各想一半 |
| 3 | **不读能力目录** —— 不解析 `plugins/**.yaml`、不含 `abstract_caps` 逻辑 | `scripts/verify_no_yaml_catalog.py` | 能力目录唯一真源在 Nexus。Scout 只按 `EXECUTE` 载荷里给定的 `executor_order` 执行 |
| 4 | **不 import Nexus** —— 任何 `mino_nexus` / `server.*` 引用 | `scripts/verify_no_nexus_import.py` | 依赖必须单向 |

跑 `python scripts/verify_all.py` 一次性检查。

---

## 2. 两条铁律

### 2.1 Executor 永不抛异常

```python
def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
    ...
```

必须捕获自身所有异常并返回五态之一。**取值是小写，且是 `pass` 不是 `OK`** —— 与上游 `EventStatus` 逐字一致，避免搬迁时到处翻译名字。

| 状态 | 语义 | Router 行为 |
|---|---|---|
| `pass` | 做成了 | 立即返回 |
| `blocked` | 需要人介入（Scout 不问人，上报 Nexus） | **立即返回，中断 fallback** |
| `declined` | 我不适合做这条，让位 | 试下一个 executor |
| `fail` | 真故障（设备报错、超时、元素找不到） | 试下一个 executor |
| `skipped` | 本次未执行（前置不满足等） | 试下一个 executor |

**最容易写错的地方**：`declined` 和 `fail` **都会**走 fallback，区别在**是否算故障** —— `declined` 记 info、不计失败；`fail` 记为真故障。只有 `blocked` 会中断 fallback 链。

（早期文档曾写成"四态、`FAIL` = 换谁都白搭"，与上游 `router.py:150-170` 不符，已修正。）

### 2.2 不做隐式回退到"另一种连接方式"

`ScoutCore` 内部**禁止出现 `if 本地 / if 远程`、`if 单机 / if 集群` 这类分支**。连接形态是外层 transport 的事，core 只认 `ExecuteRequest → ExecuteResult`。

同理，设备驱动多方案（Remote / adb / WDA / Appium）必须**可插拔**，不允许 `if backend == "appium"` 式的分叉；新增一种驱动是新增一个 executor 或 engine，不是在现有分支里加一个 `elif`。

---

## 3. 目录约定

```
mino_scout/
├── protocol.py          十条消息 + EventStatus（stdlib dataclasses，零依赖，见 §5）
├── schemas.py           PlanEvent / EventResult（pydantic，源自上游 schemas.py）
├── core.py              ScoutCore：execute / observe / manifest
├── transport/
│   ├── node.py          daemon 形态：反向 dial Nexus 的 WS 客户端
│   └── base.py          Transport 协议
├── router.py            按 executor_order 依次尝试 + 五态 + fallback（≈150 行，不含选路决策）
├── executors/
│   ├── base.py          Executor Protocol + ExecutorContext + make_event_result
│   ├── adb_executor.py
│   ├── remote_executor.py     ClawNode
│   ├── ios_wda_executor.py
│   ├── playwright_executor.py
│   ├── low_level.py     YAML 里 low_level 段的通用执行
│   └── multi_tap.py
├── screen.py            四通道截图
├── hierarchy.py         UI 树 dump / 解析
├── engines/             EngineFactory + Remote / iOS 引擎（源自 driver/tentacle）
├── probe/               连通性探测（adb / ios / playwright / remote）
└── clawnode/            ClawNode WS 服务端 + 命令编解码
```

命名沿用上游习惯：engine 层文件用 `m` 前缀（`mAdb.py` / `mIOS.py` / `mRemote.py`），executor 用 `*_executor.py`。

---

## 4. 改动前必读

| 你要做什么 | 先读 |
|---|---|
| 加一个能力 / 改一个动作的执行方式 | [docs/EXECUTORS.md](docs/EXECUTORS.md)。**注意：capability 的声明在 Nexus 的 `plugins/`，本仓只实现** |
| 加一种设备驱动 | [docs/ENGINES.md](docs/ENGINES.md) |
| 改与 Nexus 的通信 | [docs/PROTOCOL.md](docs/PROTOCOL.md) + §5。**协议改动必须两仓同步** |
| 从上游搬代码 | [docs/MIGRATION.md](docs/MIGRATION.md) |
| 调 ClawNode 相关 | [docs/DEVICE_SETUP.md](docs/DEVICE_SETUP.md) §ClawNode，以及 [ClawNode 仓库](../ClawNode) |
| 改打包 / 安装 / 更新逻辑 | [docs/PACKAGING.md](docs/PACKAGING.md)。**发布产物是分层的**，改之前先看那里的层指纹与闸门 |

---

## 5. 协议同步规则

协议在两个仓库各有一份 `protocol.py`（本仓 `mino_scout/protocol.py`，Nexus `mino_nexus/protocol.py`）。**没有共享包** —— 只有两个仓库，不引入第三个。

契约真源是 **golden fixtures**：`tests/fixtures/protocol/*.json`，两仓字节相同。

改协议的流程，**四步必须同一个 PR 周期内完成**：

1. 改 `docs/PROTOCOL.md`（两仓同一份文本）
2. 改/加 `tests/fixtures/protocol/*.json`
3. 两仓各自改 `protocol.py`，使其能 round-trip 全部 fixture
4. 两仓各自跑 `scripts/verify_protocol_contract.py`

`verify_protocol_contract.py` 会同时校验 fixture 目录的内容哈希，哈希记录在 `docs/PROTOCOL.md` 末尾。两仓哈希不一致 = 协议漂移，CI 失败。

---

## 6. 不要做的事

- 不要为了"顺手"在 Scout 里读一次数据库 —— 需要的数据让 Nexus 塞进 `EXECUTE` 载荷
- 不要在 Scout 里判断"这一步该不该做" —— 那是 Nexus 的循环
- 不要把 `plugins/*.yaml` 复制进本仓
- 不要改 `../MiniOrangeServer` 的任何文件
- 不要新增对外监听端口；Scout 永远是 dial 出去的一方
- 不要在 `RESULT` 里省略 `executor_used` —— Nexus 的 trace 和覆盖度统计依赖它
