# MinoScout

**执行器。** 接收 MinoNexus 下发的动作，操作真实设备，回报结果。

MinoScout 是 [MiniOrangeServer](../MiniOrangeServer) 拆分出的「手脚」那一半，与 [MinoNexus](../MinoNexus)（「大脑」）配对使用。

## 是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 唯一碰设备的进程：adb / WDA / ClawNode / Playwright | 不做决策 —— 下一步做什么由 Nexus 定 |
| 截图、UI 树、连通性探测的提供方 | 不调大模型 —— 没有任何 LLM 调用 |
| 按 Nexus 给的 `executor_order` 依次尝试执行 | 不碰数据库 —— 没有 ORM、没有落库 |
| 主动 dial Nexus 的常驻节点（反向连接） | 不读能力目录 YAML —— 能力声明在 Nexus |
| 上报「我这台机器能干什么、挂了哪些设备」 | 不监听对外端口 |

**一句话边界：MinoScout 只回答两个问题 —— 「屏幕现在是什么样」和「这个动作做完了没有」。**

## 快速开始

```bash
uv sync                       # 或 pip install -e .
mino-scout --nexus ws://mino.local:10104/node --token <pair-token>
```

也可以不传参数：Studio 会把 `{ nexus_url, token }` 写到本机配置，Scout 自己读，并在首次运行时写入 `scout_id`。

| OS | 配置路径 |
|---|---|
| macOS | `~/Library/Application Support/MinoScout/config.json` |
| Windows | `%APPDATA%\MinoScout\config.json` |
| Linux | `~/.config/minoscout/config.json` |

`--nexus` / `--token` 覆盖文件。`nexus_url` 是 HTTP 源站，Scout 会推导 `ws(s)://…/node`。

单机和跨机都连 `mino.local`（Nexus 启动时注册）。`--nexus` 或配置里的 `nexus_url` 仅在要覆盖缺省时才改。

## 发布安装包

安装包在 **GitHub Releases**，不进 Nexus 数据目录。

1. 确认 `pyproject.toml` 的 `version`（例如 `0.1.0`）
2. `git tag v0.1.0 && git push origin v0.1.0`
3. Actions 工作流 `.github/workflows/release.yml` 打 zip：`darwin-arm64`（macos-latest）、`darwin-x64`（macos-15-intel）、`win32-x64`、`linux-x64`，算 sha256、上传 Release，并挂上 `manifest.json`
4. 稳定地址（Studio 用这个找最新包）：

```
https://github.com/<owner>/MinoScout/releases/latest/download/manifest.json
```

`<owner>` 与本仓 `github.repository` 相同。`workflow_dispatch` 会打 `dev-<sha>` 预发布，**不会**改写 `latest`。

本机只打当前系统的 zip：

```bash
python scripts/build_binary.py --check   # 冻结，目标机器不需要 Python
python scripts/pack_release.py --out dist --binary
```

装好后由 `install.sh` / `install.ps1` 注册 launchd / Scheduled Task / systemd。
专机用 root 装会走 LaunchDaemon / AtStartup；Studio 以当前用户装走 LaunchAgent / AtLogOn。
Scout 是独立守护进程，关掉 Studio 不停。控制：`mino-scout status` / `mino-scout stop`。

启动后 Scout 会：

1. 探一遍本机通道（adb / ios_wda / playwright / ClawNode）
2. `REGISTER` 上报 manifest 与挂载设备
3. 常驻，等 Nexus 的 `OBSERVE` / `EXECUTE` / `PROBE` / `CANCEL_RUN`

## 怎么和 Nexus 连

九条消息，定义见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。要点：

- **Scout 主动连 Nexus**，不监听端口 —— 穿 NAT、免开防火墙
- Nexus 下发的 `EXECUTE` **自带全部上下文**（`executor_order` / `low_level` / `device_hint`），Scout 不回查任何东西
- 幂等键是 `run_id + step_idx`，断连重发由 Scout 去重
- Scout 只返回五态：`pass` / `fail` / `skipped` / `blocked` / `declined`，**任何情况下不向外抛异常**

## 文档

| 文档 | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | 硬约束、目录与命名约定、改动前必读 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层、一次 EXECUTE 的完整路径 |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | 九条消息的字段级定义（与 Nexus 同一份） |
| [docs/EXECUTORS.md](docs/EXECUTORS.md) | 四个 executor 的能力矩阵、怎么加一个 |
| [docs/ENGINES.md](docs/ENGINES.md) | `EngineFactory`、Remote / iOS 引擎 |
| [docs/DEVICE_SETUP.md](docs/DEVICE_SETUP.md) | adb / WDA / Playwright / ClawNode 配对的环境要求 |
| [packaging/README.txt](packaging/README.txt) | zip 安装说明（launchd / Scheduled Task） |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | 日志、异常、守门脚本约定 |
| [docs/MIGRATION.md](docs/MIGRATION.md) | 从 MiniOrangeServer 搬哪些文件、怎么改 |

## 守门

```bash
python scripts/verify_all.py
```

四条硬约束由脚本静态断言，CI 必跑：不含 ORM、不含 LLM 调用、不读能力目录 YAML、不 import Nexus。
