"""托管 LangBot 子进程：安装、启动、守护、下发平台配置、状态上报。

LangBot 只当「IM 渠道层」用：不配模型，收到消息由 mino-bridge 插件转给 Scout 的
`im_bridge` 回环入口，回复由 Scout 通过 LangBot 的 HTTP API 发出。
密钥规则：
  - LangBot 的 global_api_key 每次启动随机生成，只放内存 + 系统钥匙串，不写日志、不写文件。
  - 平台 secret 从钥匙串读出，只通过回环 HTTP 交给 LangBot 创建 bot，不写日志。
  - 子进程 stdout/stderr 只写到插件目录的 langbot.log，写入前把已知密钥替换成 ***。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from mino_scout.log import SLog
from mino_scout.plugins import im_bridge
from mino_scout.plugins import progress as PP
from mino_scout.plugins.state import IM_PLATFORMS, _item, plugin_dir, plain_value, remember_plain, secret_value

TAG = "LangBot"
KIND, PID = "im", "langbot"
# 实测：langbot 4.10.11 精确依赖 langbot-plugin==0.5.8（0.7.10 会 ResolutionImpossible）
LANGBOT_VERSION = "4.10.11"
LANGBOT_PLUGIN_VERSION = "0.5.8"
BRIDGE_AUTHOR, BRIDGE_NAME = "mino", "mino-bridge"
HOLDER_IM = "im"
PIPELINE_NAME = "mino-passthrough"
MAX_FAILS = 5
BACKOFF = (2, 5, 15, 30, 60)

_lock = threading.RLock()
_sup: threading.Thread | None = None
_stop = threading.Event()
_proc: subprocess.Popen | None = None
_port = 0
_api_key = ""
_state = "stopped"  # stopped|installing|starting|running|error
_error = ""
_restarts = 0
_started_at = 0.0
_holds: dict[str, bool] = {}  # platform id -> 是否被 Nexus 仲裁停掉（False=停）
_bots: dict[str, str] = {}  # platform id -> bot uuid
_plat_err: dict[str, str] = {}
_last_msg: dict[str, int] = {}  # channel -> ts
_llm_warning = ""
_node_id = ""


# ---------------- 路径 ----------------

def root() -> Path:
    return plugin_dir(KIND, PID)


def venv_dir() -> Path:
    return root() / "venv"


def venv_python() -> Path:
    return venv_dir() / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def data_dir() -> Path:
    return root() / "data"


def log_path() -> Path:
    return root() / "langbot.log"


def bridge_src() -> Path:
    return Path(__file__).resolve().parent / "langbot_bridge"


def is_installed() -> bool:
    return (root() / ".installed").is_file() and venv_python().is_file()


# ---------------- 安装 ----------------

def _run(cmd: list[str], *, timeout: int, label: str, percent: int) -> None:
    PP.emit("install", kind=KIND, plugin_id=PID, label=label, percent=percent)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{label}超时") from exc
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"{label}失败: {' | '.join(tail)[:300]}")


def _base_python() -> str:
    """建 venv 用的解释器。冻结包里 sys.executable 是 Scout 本身，不能拿来建 venv。"""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python3.12", "python3.11", "python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("找不到系统 Python（需要 3.11 及以上）来安装 LangBot")


def bridge_package() -> bytes:
    """把 langbot_bridge 目录打成 .lbpkg（zip）。直接复制进 data/plugins 会被 LangBot 4.10 拒绝
    （"did not provide a trusted plugin installation capability"，实测），必须走 install/local。"""
    import io
    import zipfile

    buf = io.BytesIO()
    src = bridge_src()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(src.rglob("*")):
            if fp.is_file() and "__pycache__" not in fp.parts and fp.suffix != ".pyc":
                zf.write(fp, fp.relative_to(src).as_posix())
    return buf.getvalue()


def ensure_bridge() -> None:
    """LangBot 起来后确认 mino-bridge 已装且版本一致，没有就经 HTTP 安装。"""
    want = _bridge_version()
    for pl in (_api("GET", "/api/v1/plugins") or {}).get("plugins") or []:
        meta = ((pl.get("manifest") or {}).get("manifest") or pl.get("manifest") or {}).get("metadata") or {}
        if meta.get("author") == BRIDGE_AUTHOR and meta.get("name") == BRIDGE_NAME:
            if str(meta.get("version")) == want:
                return
            _api("DELETE", f"/api/v1/plugins/{BRIDGE_AUTHOR}/{BRIDGE_NAME}")
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{_port}/api/v1/plugins/install/local",
            headers={"X-API-Key": _api_key}, trust_env=False, timeout=30,
            files={"file": (f"{BRIDGE_NAME}.lbpkg", bridge_package(), "application/zip")},
        )
        task_id = (resp.json().get("data") or {}).get("task_id")
    except Exception as exc:
        raise ApiError(f"安装 mino-bridge 失败: {type(exc).__name__}") from exc
    if resp.status_code >= 400 or task_id is None:
        raise ApiError(f"安装 mino-bridge 失败: HTTP {resp.status_code}")
    deadline = time.time() + 120
    while time.time() < deadline:
        t = _api("GET", f"/api/v1/system/tasks/{task_id}") or {}
        st = str(t.get("status") or "")  # API key 视角：running|succeeded|failed|cancelled
        if st == "succeeded":
            return
        if st in ("failed", "cancelled"):
            raise ApiError(f"安装 mino-bridge {st}")
        time.sleep(1)
    raise ApiError("安装 mino-bridge 超时")


def _retry(fn: Any, *, tries: int = 40, delay: float = 2.0) -> None:
    for i in range(tries):
        try:
            fn()
            return
        except ApiError:
            if i == tries - 1 or _stop.is_set():
                raise
            _stop.wait(delay)


def _bridge_version() -> str:
    for line in (bridge_src() / "manifest.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("  version:"):
            return line.split(":", 1)[1].strip()
    return ""


def install(node_id: str) -> str:
    """创建独立 venv，pip 装锁定版本，复制 mino-bridge。"""
    global _state
    with _lock:
        _state = "installing"
    try:
        py = _base_python()
        r = root()
        r.mkdir(parents=True, exist_ok=True)
        PP.emit("plan", kind=KIND, plugin_id=PID, label="创建独立环境", percent=5)
        if not venv_python().is_file():
            if venv_dir().exists():
                shutil.rmtree(venv_dir())
            _run([py, "-m", "venv", str(venv_dir())], timeout=180, label="创建 venv", percent=8)
        _run(
            [str(venv_python()), "-c", "import sys; sys.exit(0 if (3,11)<=sys.version_info[:2]<(4,0) else 1)"],
            timeout=30, label="检查 Python 版本（需要 3.11 及以上）", percent=10,
        )
        uv = shutil.which("uv")
        pkgs = [f"langbot=={LANGBOT_VERSION}", f"langbot-plugin=={LANGBOT_PLUGIN_VERSION}"]
        if uv:
            cmd = [uv, "pip", "install", "--python", str(venv_python()), *pkgs]
        else:
            cmd = [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", *pkgs]
        _run(cmd, timeout=3600, label="安装 LangBot（体积较大，请耐心等待）", percent=20)
        if not bridge_src().is_dir():
            raise RuntimeError("缺少 mino-bridge 插件目录")
        (r / ".installed").write_text(f"langbot={LANGBOT_VERSION}\n", encoding="utf-8")
        PP.emit("done", kind=KIND, plugin_id=PID, label="安装完成", percent=100, done=True)
        SLog.i(TAG, f"installed langbot {LANGBOT_VERSION}")
        return "安装完成"
    except Exception as exc:
        text = str(exc).strip() or "安装失败"
        PP.emit("done", kind=KIND, plugin_id=PID, label=text[:80], percent=100, error=text[:300], done=True)
        with _lock:
            _state = "stopped"
        raise


# ---------------- 配置 → 期望状态 ----------------

def _enabled(item: dict[str, Any], plat: str) -> bool:
    return str(item.get(f"{plat}.enabled") or "") == "1"


def wanted_platforms() -> list[str]:
    """已启用、必填项填全、且没被 Nexus 仲裁停掉的平台。"""
    from mino_scout.plugins.state import platform_filled

    item = _item(KIND, PID)
    out = []
    for p in IM_PLATFORMS:
        pid = p["id"]
        if _enabled(item, pid) and platform_filled(item, pid) and _holds.get(pid, True):
            out.append(pid)
    return out


def _persisted_hold(plat: str) -> bool:
    return plain_value(KIND, PID, f"{plat}.im_hold") != "0"


def _adapter_config(node_id: str, plat: dict[str, Any]) -> dict[str, Any]:
    """Workbench 填的值 → LangBot 适配器配置。字段名以 LangBot 适配器 manifest 为准。"""
    cfg: dict[str, Any] = {}
    item = _item(KIND, PID)
    for fld in plat["fields"]:
        key = f"{plat['id']}.{fld['key']}"
        if fld.get("secret"):
            cfg[fld["key"]] = secret_value(node_id, KIND, PID, key)
        else:
            cfg[fld["key"]] = str(item.get(key) or "")
    extra = _ADAPTER_DEFAULTS.get(plat["adapter"], {})
    for k, v in extra.items():
        cfg.setdefault(k, v)
    return cfg


# 适配器里必填但 Workbench 不让用户填的字段，给安全默认值（长连接模式、关流式）。
_ADAPTER_DEFAULTS: dict[str, dict[str, Any]] = {
    "lark": {"enable-webhook": False, "encrypt-key": "", "enable-stream-reply": False},
    "dingtalk": {"enable-stream-reply": False, "card_template_id": ""},
    "wecombot": {"enable-webhook": False},
}


# ---------------- LangBot HTTP API ----------------

class ApiError(Exception):
    pass


def _api(method: str, path: str, body: Any = None, *, timeout: float = 15.0) -> Any:
    if not _port:
        raise ApiError("LangBot 未运行")
    try:
        resp = httpx.request(
            method, f"http://127.0.0.1:{_port}{path}",
            json=body, headers={"X-API-Key": _api_key}, timeout=timeout, trust_env=False,
        )
    except httpx.HTTPError as exc:
        raise ApiError(f"{type(exc).__name__}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("code", 0) not in (0, None)):
        msg = ""
        if isinstance(data, dict):
            msg = str(data.get("msg") or data.get("message") or "")
        raise ApiError(f"HTTP {resp.status_code} {msg}"[:200])
    return data.get("data") if isinstance(data, dict) else data


def _ensure_pipeline() -> str:
    """复用 / 创建一个不接模型的 pipeline。"""
    rows = _api("GET", "/api/v1/pipelines") or {}
    for p in rows.get("pipelines") or []:
        if p.get("name") == PIPELINE_NAME:
            return str(p["uuid"])
    created = _api("POST", "/api/v1/pipelines", {"name": PIPELINE_NAME, "description": "Mino 透传，不调模型"})
    return str((created or {}).get("uuid") or "")


def sync_bots(node_id: str) -> None:
    """让 LangBot 里的 bot 与期望状态一致：期望启用的 enable=true，其余 enable=false。"""
    want = set(wanted_platforms())
    pipeline = _ensure_pipeline()
    existing = {}
    for b in (_api("GET", "/api/v1/platform/bots") or {}).get("bots") or []:
        existing[str(b.get("name"))] = b
    for plat in IM_PLATFORMS:
        pid = plat["id"]
        name = f"mino-{pid}"
        on = pid in want
        cur = existing.get(name)
        if cur is None and not on:
            continue
        body = {
            "name": name,
            "description": f"Mino {plat['label']}",
            "adapter": plat["adapter"],
            "adapter_config": _adapter_config(node_id, plat) if on or cur is None else {},
            "enable": on,
            "use_pipeline_uuid": pipeline,
        }
        try:
            if cur is None:
                created = _api("POST", "/api/v1/platform/bots", body)
                _bots[pid] = str((created or {}).get("uuid") or "")
            else:
                if not on:
                    body.pop("adapter_config")
                    cfg_now = cur.get("adapter_config")
                    if isinstance(cfg_now, dict):
                        body["adapter_config"] = cfg_now
                _api("PUT", f"/api/v1/platform/bots/{cur['uuid']}", body)
                _bots[pid] = str(cur["uuid"])
            _plat_err.pop(pid, None)
        except ApiError as exc:
            _plat_err[pid] = str(exc)
            SLog.w(TAG, f"同步 bot {pid} 失败: {exc}")


def check_no_llm() -> str:
    """LangBot 里不应启用任何大模型。发现就返回告警文本（不替用户删）。"""
    try:
        data = _api("GET", "/api/v1/provider/models/llm") or {}
    except ApiError as exc:
        return f"无法校验大模型配置: {exc}"
    models = data.get("models") or []
    if models:
        return f"LangBot 里配置了 {len(models)} 个大模型，会绕过 Nexus，请在 LangBot 中删除"
    return ""


# ---------------- 子进程 ----------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_DARWIN_SHIM = '''\
# Scout 写的补丁（仅 macOS）：LangBot 插件运行时把依赖环境目录 chmod 0555 再 os.rename，
# macOS 上重命名只读目录会 EACCES，导致任何插件都装不上。重命名前后临时放开权限。
import os as _os, stat as _st
_orig = _os.rename
def _rename(src, dst, *a, **k):
    try:
        return _orig(src, dst, *a, **k)
    except PermissionError:
        try:
            mode = _st.S_IMODE(_os.stat(src).st_mode)
            if not _os.path.isdir(src) or mode & 0o200:
                raise
            _os.chmod(src, mode | 0o200)
            try:
                return _orig(src, dst, *a, **k)
            finally:
                try:
                    _os.chmod(dst, mode)
                except OSError:
                    pass
        except OSError:
            raise
_os.rename = _rename
'''


def _shim_dir() -> str:
    """darwin 才需要的 PYTHONPATH 补丁目录；其它平台返回空串。"""
    if sys.platform != "darwin":
        return ""
    d = root() / "shim"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sitecustomize.py").write_text(_DARWIN_SHIM, encoding="utf-8")
    return str(d)


def _ca_bundle() -> str:
    """venv 里 certifi 的根证书。python.org 安装的 macOS Python 默认没有 CA 文件，
    不给的话 LangBot 连飞书（wss://）和任何 HTTPS 都是 CERTIFICATE_VERIFY_FAILED，而且是静默失败。"""
    try:
        out = subprocess.run(
            [str(venv_python()), "-c", "import certifi,sys;sys.stdout.write(certifi.where())"],
            capture_output=True, text=True, timeout=20,
        )
        path = out.stdout.strip()
        return path if out.returncode == 0 and os.path.isfile(path) else ""
    except Exception:
        return ""


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(0.5)
        return sk.connect_ex(("127.0.0.1", port)) == 0


# 插件运行时的调试口，LangBot 以 stdio 拉起它时写死 5401，改不了；被占用则运行时起不来，
# 表现为 mino-bridge 装不上、平台一直未连接。
RUNTIME_DEBUG_PORT = 5401


def _build_env(url: str, token: str) -> dict[str, str]:
    env = dict(os.environ)
    shim = _shim_dir()
    if shim:
        env["PYTHONPATH"] = shim + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    ca = _ca_bundle()
    if ca:
        env.setdefault("SSL_CERT_FILE", ca)
        env.setdefault("REQUESTS_CA_BUNDLE", ca)
        env.setdefault("CURL_CA_BUNDLE", ca)
    env.update({
        "LANGBOT_DATA_ROOT": str(data_dir()),
        "API__PORT": str(_port),
        "API__GLOBAL_API_KEY": _api_key,
        "BOX__ENABLED": "false",
        "SPACE__DISABLE_TELEMETRY": "true",
        "MINO_BRIDGE_URL": url,
        "MINO_BRIDGE_TOKEN": token,
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
    })
    return env


def _pump_log(proc: subprocess.Popen) -> None:
    """把子进程输出落盘，替换掉 API key 与平台密钥。"""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass
    with open(path, "a", encoding="utf-8", errors="replace") as fh:
        for line in proc.stdout or []:
            if _api_key:
                line = line.replace(_api_key, "***")
            fh.write(line)
            fh.flush()


def _wait_ready(proc: subprocess.Popen, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline and not _stop.is_set():
        if proc.poll() is not None:
            return False
        try:
            r = httpx.get(f"http://127.0.0.1:{_port}/healthz", timeout=2, trust_env=False)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def _sync_once() -> bool:
    """装 mino-bridge、同步 bot、刷新各平台真实状态。成功返回 True；失败写进 _error，调用方会重试。"""
    global _error, _llm_warning
    try:
        _retry(ensure_bridge, tries=20, delay=2.0)  # healthz 先于插件运行时就绪，首次要等一会儿
        sync_bots(_node_id)
        refresh_platform_health()
        _llm_warning = check_no_llm()
    except ApiError as exc:
        SLog.w(TAG, f"同步失败，稍后重试: {exc}")
        with _lock:
            _error = f"同步配置失败: {exc}"[:200]
        return False
    with _lock:
        bad = [f"{pid}: {e}" for pid, e in _plat_err.items() if e]
        _error = _llm_warning or ("；".join(bad)[:200] if bad else "")
    return True


def refresh_platform_health() -> None:
    """读每个 bot 的事件日志，看适配器有没有报错。连不上飞书时错误会出现在这里，
    不能只因为 LangBot 进程活着就报「已连接」。"""
    for pid, bot in list(_bots.items()):
        if not bot:
            continue
        try:
            data = _api("POST", f"/api/v1/platform/bots/{bot}/logs", {"from_index": -1, "max_count": 20}) or {}
        except ApiError:
            continue
        errs = [str(l.get("text") or "") for l in (data.get("logs") or []) if str(l.get("level")) == "error"]
        if errs:
            _plat_err[pid] = errs[-1][:160]
        elif _plat_err.get(pid, "").startswith("适配器"):
            _plat_err.pop(pid, None)


def _supervise() -> None:
    global _proc, _port, _api_key, _state, _error, _restarts, _started_at, _llm_warning
    fails = 0
    first_run_exit_ok = True  # 首次自动 pip 装依赖后 sys.exit(0) 要求重启，不算失败
    from mino_scout.power import get_guard

    get_guard().acquire(HOLDER_IM)
    try:
        while not _stop.is_set():
            if not wanted_platforms():
                with _lock:
                    _state = "stopped"
                break
            url, token = im_bridge.start_loopback()
            try:
                import secrets as _sec
                with _lock:
                    _port = _free_port()
                    _api_key = _sec.token_urlsafe(32)
                _remember_key()
                with _lock:
                    _state, _error = "starting", ""
                data_dir().mkdir(parents=True, exist_ok=True)
                if _port_in_use(RUNTIME_DEBUG_PORT):
                    raise RuntimeError(
                        f"端口 {RUNTIME_DEBUG_PORT} 被别的进程占用（LangBot 插件运行时需要它），"
                        "请先关掉占用它的程序，Scout 会自动重试")
                proc = subprocess.Popen(
                    [str(venv_python()), "-m", "langbot"],
                    cwd=str(data_dir()), env=_build_env(url, token),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                with _lock:
                    _proc = proc
                    _started_at = time.time()
                threading.Thread(target=_pump_log, args=(proc,), daemon=True).start()
                ready = _wait_ready(proc)
                if ready:
                    synced = _sync_once()
                    with _lock:
                        _state = "running" if synced else "error"
                    fails = 0
                    SLog.i(TAG, f"started port={_port} synced={synced}")
                    last_try = time.time()
                    while proc.poll() is None and not _stop.is_set():
                        time.sleep(1)
                        # 没同步成功就每 15 秒再试一次，直到成功；成功后每分钟核对一次平台连接
                        every = 60 if synced else 15
                        if time.time() - last_try >= every:
                            last_try = time.time()
                            synced = _sync_once()
                            with _lock:
                                if _state in ("running", "error"):
                                    _state = "running" if synced else "error"
                else:
                    proc.wait(timeout=1) if proc.poll() is not None else None
                code = proc.poll()
                if code is None:
                    _terminate(proc)
                    code = proc.poll()
                if _stop.is_set():
                    break
                if code == 0 and first_run_exit_ok and not ready:
                    first_run_exit_ok = False  # 只豁免一次
                    SLog.i(TAG, "LangBot 首次安装依赖后退出，立即重新拉起")
                    continue
            except Exception as exc:
                SLog.e(TAG, f"启动异常: {type(exc).__name__}: {exc}")
                code = -1
            fails += 1
            with _lock:
                _restarts += 1
                _error = f"LangBot 退出 (code={code})，第 {fails} 次失败"
            if fails >= MAX_FAILS:
                with _lock:
                    _state = "error"
                    _error = f"LangBot 连续 {fails} 次启动失败，已停止重试；查看 langbot.log"
                SLog.e(TAG, _error)
                _notify()
                break
            _stop.wait(BACKOFF[min(fails - 1, len(BACKOFF) - 1)])
    finally:
        with _lock:
            _proc = None
        get_guard().release(HOLDER_IM)


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def _remember_key() -> None:
    """API key 只进钥匙串（best effort），不写文件、不打日志。"""
    try:
        from mino_scout.plugins.secret_store import account_name, put_secret

        put_secret(account_name(_node_id, KIND, PID, "api_key"), _api_key)
    except Exception as exc:
        SLog.w(TAG, f"API key 未能存入钥匙串，仅保留在内存: {type(exc).__name__}")


def _notify() -> None:
    """进入 error 时让心跳尽快带上状态（心跳每次都会读 channel_status）。"""
    return


# ---------------- 对外 ----------------

def reconcile(node_id: str) -> None:
    """配置 / 安装 / 仲裁变化后调用：该起就起，该停就停，已在跑就同步 bot。"""
    global _node_id, _sup, _state, _error
    _node_id = node_id or _node_id
    for p in IM_PLATFORMS:
        _holds.setdefault(p["id"], _persisted_hold(p["id"]))
    if not is_installed():
        return
    want = wanted_platforms()
    with _lock:
        alive = _sup is not None and _sup.is_alive()
    if not want:
        if alive:
            if _port and _state in ("running", "error"):
                try:
                    sync_bots(_node_id)  # 全部 enable=false
                except ApiError as exc:
                    SLog.w(TAG, f"停用 bot 失败: {exc}")
            shutdown()
        return
    if alive and _state in ("running", "error") and _port:
        try:
            sync_bots(_node_id)
        except ApiError as exc:
            SLog.w(TAG, f"同步 bot 失败: {exc}")
        return
    if not alive:
        with _lock:
            _stop.clear()
            _state, _error = "starting", ""
            _sup = threading.Thread(target=_supervise, name="langbot-host", daemon=True)
            _sup.start()


def set_hold(platform: str | None, hold: bool) -> None:
    """Nexus 仲裁。platform=None 表示整个 LangBot。hold=False：对应 bot enable=false。"""
    targets = [platform] if platform else [p["id"] for p in IM_PLATFORMS]
    known = {p["id"] for p in IM_PLATFORMS}
    for t in targets:
        if t not in known:
            raise ValueError(f"未知平台 {t}")
        _holds[t] = hold
        remember_plain(KIND, PID, {f"{t}.im_hold": "" if hold else "0"})
    SLog.i(TAG, f"im_hold platform={platform or '*'} hold={hold}")
    reconcile(_node_id)


def shutdown() -> None:
    global _sup, _state
    _stop.set()
    with _lock:
        proc, sup = _proc, _sup
    if proc is not None and proc.poll() is None:
        _terminate(proc)
    if sup is not None and sup is not threading.current_thread():
        sup.join(timeout=15)
    with _lock:
        _sup = None
        _state = "stopped"


def restart(node_id: str) -> None:
    shutdown()
    reconcile(node_id)


def note_message(channel: str) -> None:
    _last_msg[channel] = int(time.time())


def channel_status() -> dict[str, Any]:
    """随插件快照上报。不含密钥 / 正文。"""
    item = _item(KIND, PID)
    from mino_scout.plugins.state import platform_filled

    running = _state == "running"
    platforms: dict[str, Any] = {}
    for p in IM_PLATFORMS:
        pid = p["id"]
        enabled = _enabled(item, pid)
        err = _plat_err.get(pid, "")
        if enabled and not platform_filled(item, pid):
            err = err or "必填项未填完"
        platforms[pid] = {
            "enabled": enabled,
            "holding": _holds.get(pid, True),
            "connected": bool(running and pid in wanted_platforms() and not err),
            "last_message_at": _last_msg.get(p["channel"], 0),
            "error": err[:200],
        }
    return {
        "status": {
            "state": _state,
            "healthy": running and not _llm_warning,
            "installed": is_installed(),
            "version": LANGBOT_VERSION,
            "restarts": _restarts,
            "error": _error[:200],
            "uptime_s": int(time.time() - _started_at) if running else 0,
        },
        "platforms": platforms,
    }


# ---------------- 出站 ----------------

def _bot_uuid(ctx: dict[str, str]) -> str:
    bot = str(ctx.get("bot_uuid") or "")
    if not bot or not _port or _state not in ("running", "error"):
        raise RuntimeError("LangBot 未运行，无法发送")
    return bot


def _send_text(chat_id: str, sender_id: str, ctx: dict[str, str], text: str) -> None:
    bot = _bot_uuid(ctx)
    target = str(ctx.get("target_id") or chat_id or sender_id)
    ttype = str(ctx.get("target_type") or "person")
    body = {
        "target_type": ttype, "target_id": target,
        "message_chain": [{"type": "Plain", "text": str(text)}],
    }
    try:
        _api("POST", f"/api/v1/platform/bots/{bot}/send_message", body, timeout=30)
    except ApiError as exc:
        raise RuntimeError(f"LangBot 发送失败: {exc}") from exc


def _send_image(chat_id: str, sender_id: str, ctx: dict[str, str], data: bytes, mime: str) -> None:
    import base64

    bot = _bot_uuid(ctx)
    target = str(ctx.get("target_id") or chat_id or sender_id)
    ttype = str(ctx.get("target_type") or "person")
    body = {
        "target_type": ttype, "target_id": target,
        "message_chain": [{"type": "Image", "base64": f"data:{mime};base64,{base64.b64encode(data).decode()}"}],
    }
    try:
        _api("POST", f"/api/v1/platform/bots/{bot}/send_message", body, timeout=60)
    except ApiError as exc:
        raise RuntimeError(f"LangBot 发送图片失败: {exc}") from exc


for _ch in ("feishu", "wecom", "dingtalk"):
    im_bridge.register_sender(_ch, _send_text, _send_image)
