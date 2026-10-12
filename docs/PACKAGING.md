# 打包与更新

Scout 的发布产物是**分层**的。改动打包、安装、更新逻辑前先读这里。

---

## 1. 为什么分层

实测一份 darwin-arm64 的冻结产物（`build/dist/mino-scout/`）：

| 层 | 内容 | 解压 | 占比 | 什么时候才会变 |
|---|---|---|---|---|
| **browser** | `ms-playwright/`：chromium 583M + chromium_headless_shell 196M + ffmpeg 2.5M | 780 MB | 77% | playwright 换 browser revision，几个月一次 |
| **runtime** | `mino-scout` 可执行 + `_internal/`：playwright driver 的 `node` 115M、Python 30M、PIL 11M、lxml 8.8M、numpy 6.9M、u2 5.8M… | 228 MB | 23% | `pyproject.toml` 依赖 / Python / 冻结配方变 |
| **app** | `app/mino_scout/**.py` 纯源码，25 个文件 | 0.2 MB | 0.02% | **每次发版** |

合并成一个 zip 是 **439 MB**，而每次发版真正变的那一层 zip 后只有 **90 KB**。
分层前，改一行代码也要让每个节点重下 439 MB。

## 2. app 层刻意不进 PYZ

这是整个方案的前提，也是最容易被无意破坏的地方。

PyInstaller 默认把应用代码编成字节码塞进可执行文件里的 PYZ 归档。那样的话「改一行代码」
的最小更新单位就是 9.2 MB 的可执行文件 —— 而它和 `_internal/` 是**同一次 build 的绑定
产物**（exe 里的 PKG TOC 引用 `_internal/` 下的具体文件名），不能单独换。

所以 `scripts/build_binary.py` 做了两件事：

1. 入口用 `importlib.import_module("mino_scout.cli")`，并加 `--exclude-module mino_scout`
2. 冻结后把 `mino_scout/` 源码拷进 `app/`，可执行文件按 `sys.executable` 的同级目录去找

**写成 `from mino_scout.cli import main` 会让整个包被拖回 PYZ，而 FrozenImporter 的优先级
高于文件系统 —— 外挂的 app 层会被无声忽略**，症状是改了代码却没生效。`build_binary.py`
的 `_assert_pkg_not_frozen()` 和 `scripts/verify_layer_mapping.py` 各守一道。

代价：mino_scout 退出了 PyInstaller 的分析范围，第三方与 stdlib 依赖都不会再被自动发现。
`scan_external_imports()` 用 ast 把 mino_scout 里所有外部 import 扒出来当 hiddenimports 顶上。
mino_scout 内没有动态 import（`importlib` / `__import__` 全仓零引用），所以 ast 扫描是完备的 ——
**新增动态 import 会打破这个前提**。

## 3. 层指纹

| 层 | 指纹 | 怎么算 |
|---|---|---|
| app | 版本号，如 `0.1.7` | `pyproject.toml` 的 `version` |
| runtime | `rt-<10 hex>` | sha256(依赖**声明** + Python 次版本 + `RUNTIME_ABI` + os/arch) |
| browser | `bw-<10 hex>` | sha256(`ms-playwright/` 下 revision 目录名排序后) |

runtime 指纹刻意用**依赖声明**而不是 `pip freeze` 的锁定版本：pyproject 用的是 `>=`，
任何传递依赖发新版都会让锁定表变化。若指纹跟着变，几乎每次发版 runtime 都"变了"，
app-only 更新就永远用不上，分层作废。声明集恰好回答了唯一要紧的问题 ——
**我这个 runtime 能不能满足那个 app**。

> 改了 `build_binary.py` 的 `COLLECT_ALL` / `EXTRA_HIDDEN` / `EXCLUDES` / 入口脚本，
> 要把 `scripts/layers.py` 的 `RUNTIME_ABI` +1。否则依赖声明没变、产物变了，
> 指纹却不动，客户端会误以为自己那份 runtime 还能配新 app。

browser 指纹取所有 revision 目录名而不只是 chromium 的 —— ffmpeg 有自己的 revision，
可以独立滚动。

## 4. 产物

`python scripts/pack_release.py --out dist --binary` 产出 4 个 zip：

| zip | 内容 | 用途 |
|---|---|---|
| `MinoScout-<ver>-<os>-<arch>.zip` | 三层齐全，439 MB | 全新安装。**名字刻意不变**，分层前的 Studio 只认这一个 |
| `MinoScout-app-<ver>-<os>-<arch>.zip` | 只有 app 层，90 KB | 只改了代码的更新 |
| `MinoScout-runtime-<rtkey>-<os>-<arch>.zip` | 只有 runtime 层，82 MB | 依赖 / Python / 冻结配方变了 |
| `MinoScout-browser-<bwkey>-<os>-<arch>.zip` | 只有 browser 层，357 MB | playwright 换 revision |

四者结构相同：根目录下 `layers.json` + `layers.txt` + 若干载荷目录（`runtime/` / `app/`
/ `browser/`）+ 安装脚本。层包只是少几个载荷目录，所以 **install 脚本只有一条代码路径**：
present 的层就装，不 present 的层原地不动。

层包是**增量**，不替代合并包 —— 全新安装本来就要全部字节，拆开只会多几次往返。
代价是每平台每次发布上传约两倍字节（合并包 + 三个层包）。

## 5. 安装布局

`bootstrap.sh`（macOS/Linux）和 `bootstrap.ps1`（Windows PowerShell）随 Release 同时发布，使用 manifest 的精确系统/架构条目，缺包或不支持的架构明确失败，不回退为其他架构。两种入口均在启动服务前合并配置，保留已有身份、长期凭证和其他设置；短期安装凭证只写入待绑定字段，注册确认后才替换长期凭证。Windows 配置以 APPDATA 为默认目录，兼容已存在的 LOCALAPPDATA 配置；计划任务明确使用同一配置目录。安装成功仅代表文件和服务完成，账号接入结果在 Workbench 查看。

```
$PREFIX/bin/
├── mino-scout          ← runtime 层
├── _internal/          ← runtime 层
├── app/mino_scout/     ← app 层
├── ms-playwright/      ← browser 层
└── layers.txt          ← 已装的三层指纹，<层> <指纹> 两列
```

**安装根刻意仍是 `$PREFIX/bin`**：Studio 的 `scoutBinCandidates()` 和 launchd plist
都硬编码了 `$PREFIX/bin/mino-scout` 与 `$PREFIX/bin/ms-playwright`，换根等于跨仓破坏。

层 → 目录的映射真源在 `scripts/layers.py` 的 `PAYLOAD_DIRS`，另有三份副本
（`install.sh`、`install.ps1`、冻结入口）—— bash / PowerShell / 冻结产物都没法 import 它。
修改时需同步检查四处映射。分层安装回归脚本只在本地使用，不随仓库或发布上传。

**install 脚本不再整体 `rm -rf $PREFIX/bin`。** 那一句会连 780 MB 浏览器一起删，
等于每次更新都重下重铺 —— 这是包体问题的一半。

## 6. 版本闸门

app 层的 `layers.txt` 带 `requires_runtime=<rtkey>`。只装 app 层时，install 脚本先比对
本机 `bin/layers.txt` 的 runtime 指纹，不一致就**在动任何文件之前**拒绝并退出非 0，
提示改用合并包。

没有这道闸门，"只更新脚本"迟早会更新出一个起不来的 Scout —— 新代码引用了本机 runtime
里没有的依赖，症状只是启动时一句 ImportError。

## 7. manifest

`scripts/write_manifest.py` 产出的 `manifest.json`：顶层字段（`url` / `sha256` /
`filename`）**一个不动**，仍指向合并包，分层前的 Studio 行为完全不变；新增的 `layers`
带每层的 `key` / `url` / `sha256` / `bytes`，新客户端拿本机 `bin/layers.txt` 逐层比对，
只下指纹不一致的层。`bytes` 用来在下载前告诉用户这次要下 90 KB 还是 439 MB。

## 8. 验证

```bash
python scripts/build_binary.py --check                         # 冻结 + 真跑一次 probe
```

CI 保留 Windows 安装器语法检查、PC SDK 原生库加载、冻结程序真实 probe 与发布包生成。mock、测试脚本及本地回归脚本由 `.gitignore` 屏蔽，不再作为 CI 的仓库文件依赖。

## 0.1.76 Windows 更新与运行身份

Windows 根据系统选择 `install.ps1`。当 Scout 仍在运行时，先下载并校验 SHA256，再由独立助手等待原进程退出后替换载荷，避免覆盖正在使用的 EXE / DLL。同一安装只允许一个待更新任务；`status.update` 展示 `awaiting_exit`、`installing`、`completed` 或 `failed`。安装失败保留备份和失败载荷，恢复受影响层，不修改 config、节点身份和凭据。

服务停止使用 PID 与创建时间绑定的协作请求，超时才终止同一进程；存活检查使用 psutil，Windows 不使用 POSIX `kill(pid, 0)`。冻结版本重启直接运行 `mino-scout.exe run`。计划任务按安装目录区分，以安装用户的 Interactive / Limited 身份启动；旧任务保留 XML 备份，权限不足时提示并使用当前用户 Startup。原 SYSTEM 任务不能无凭据转成另一用户。源码安装在 macOS / Windows 一并安装 Desktop extra。

0.1.75 的 Windows 进程可能仍有选择 install.sh 的旧逻辑；首次升级建议从 Workbench 为该电脑重新复制安装命令，在原登录用户的 PowerShell 执行，复用现有安装目录和身份。不要删除配置来升级。

本版本提升 runtime ABI 到 5，发布需包含对应 runtime，不能只手工替换 app。Windows CI 额外验证原生子进程协作停止以及安装器和延后更新助手的 PowerShell 语法。真实 GUI 输入、锁屏恢复与权限弹窗需要分别在 Mac / Windows 实机验收。
