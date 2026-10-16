"""PyCharm development operations. Nexus owns business state; receipts prevent replay."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

from mino_scout.config import config_path, load_config, save_config
from mino_scout.ide_client import Client, IDEError, configuration_names, connection_spec, local_url

_lock = threading.RLock()


@contextmanager
def machine_lock(endpoint):
    # Same user / same IDE across formal and test Scouts. No network listener.
    if os.name != "posix":
        raise IDEError("此平台的跨进程开发互斥尚未验收，请在 IDE 人工执行")
    import fcntl
    origin = urlsplit(endpoint)
    key = hashlib.sha256(f"{origin.hostname}:{origin.port}".encode()).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"mino-ide-{os.getuid()}-{key}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_nlink != 1:
            raise IDEError("IDE 锁文件身份不符")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IDEError("另一 Scout 正在准备同一 IDE，请等待其完成")
        yield
    finally:
        os.close(fd)


def _save_receipt(path, data):
    from mino_scout.secret_redaction import sanitize
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sanitize(data), ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _connection(cid, revision=None):
    config = load_config()
    row = next((r for r in config.get("ide_connections", []) if r["connection_id"] == cid), None)
    if not row:
        raise IDEError("此 Scout 尚未保存该 IDE 连接")
    if revision is not None and revision != row["revision"]:
        raise IDEError("连接配置已变更，请重新检查")
    return row


def _listeners(port):
    from mino_scout.ide_stop import listeners
    return listeners(port)


def _old_process(binding, endpoint):
    if sys.platform not in {"darwin", "linux"}:
        raise IDEError("自动停止目前仅支持 macOS/Linux；请在 IDE 中 Rerun")
    target = urlsplit(local_url(binding["target_url"]))
    control = urlsplit(str(load_config().get("nexus_url", "")))
    if target.port in {10104, 5273, urlsplit(endpoint).port, control.port}:
        raise IDEError("测试地址与正式/控制端口冲突，拒绝停止")
    pids = _listeners(target.port)
    if not pids:
        return None
    if len(pids) != 1:
        raise IDEError("测试端口有多个监听进程，请在 IDE 人工处理")
    proc = psutil.Process(pids.pop())
    if proc.pid == os.getpid() or proc.pid in {p.pid for p in psutil.Process().parents()}:
        raise IDEError("禁止停止 Scout 自身或其父进程")
    if proc.uids().real != os.getuid() or Path(proc.cwd()).resolve() != Path(binding["source_dir"]).resolve():
        raise IDEError("端口占用进程的用户或源码目录不匹配")
    ide_pids = _listeners(urlsplit(endpoint).port)
    parents = proc.parents()
    if not any(p.pid in ide_pids and "pycharm" in (p.name() + " " + p.exe()).lower() for p in parents):
        raise IDEError("无法证明应用由当前已连接的 PyCharm 启动")
    return proc, proc.create_time(), target.port


def _stop(client, binding, old):
    if old is None:
        return
    proc, created, port = old
    # Fixed generated command: no user-provided command, PID+creation time rechecked in IDE.
    args = ["--pid", str(proc.pid), "--created", repr(created), "--port", str(port),
            "--ide-port", str(urlsplit(client.url).port), "--source", binding["source_dir"]]
    command = [sys.executable, "internal-ide-stop"] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).with_name("ide_stop.py"))]
    if "execute_terminal_command" in client.tools:
        result = client.call("execute_terminal_command", {"projectPath": binding["project_path"],
            "command": shlex.join(command + args),
            "timeout": 10000, "maxLinesCount": 30, "truncateMode": "END", "executeInShell": True})
        if not isinstance(result, dict) or result.get("command_exit_code", result.get("exitCode")) != 0:
            raise IDEError("IDE 停止命令未确认成功；请人工检查，不重复发送", effect_unknown=True)
    else:
        from mino_scout.ide_stop import main as stop_main
        stop_main(args)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE) and not _listeners(port):
            return
        time.sleep(.2)
    raise IDEError("旧进程或端口未释放；未使用 SIGKILL，也未启动新进程", effect_unknown=True)


def probe(binding):
    url = binding.get("target_url")
    if not url:
        return {"state": "awaiting_developer", "reason": "未配置服务就绪地址，请读取 IDE 输出并人工核对", "target_ready": False}
    url = local_url(url)
    with httpx.Client(timeout=3, trust_env=False, follow_redirects=False) as http:
        end = time.monotonic() + 15
        last = "目标尚未就绪"
        while time.monotonic() < end:
            try:
                with http.stream("GET", url) as response:
                    if response.status_code == 200:
                        expected = binding.get("expected_build", "")
                        if not expected:
                            return {"state": "ready", "target_ready": True, "build_verified": False,
                                    "reason": "服务可访问；未证明本轮代码已生效，也不代表测试通过"}
                        content = b""
                        for part in response.iter_bytes():
                            content += part
                            if len(content) > 65536:
                                raise IDEError("版本探测响应过大")
                        value = json.loads(content)
                        for key in binding.get("build_field", "build_id").split("."):
                            value = value.get(key) if isinstance(value, dict) else None
                        if str(value) == expected:
                            return {"state": "ready", "target_ready": True, "build_verified": True, "build": expected}
                        return {"state": "failed", "target_ready": False, "reason": "实际 build 与预期不匹配"}
                    last = f"目标 HTTP {response.status_code}"
            except (httpx.HTTPError, ValueError):
                last = "目标暂不可访问或版本响应格式无效"
            time.sleep(.5)
        return {"state": "awaiting_developer", "target_ready": False, "reason": last}


def perform(params):
    action = params["action"]
    cid = str(params.get("connection_id", ""))
    if action == "save_connection":
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", cid):
            raise IDEError("connection_id 无效")
        spec = connection_spec(params["connection"])
        with _lock:
            path = config_path()
            if path.exists():
                # Do not replace an unreadable/corrupt config with an empty one.
                cfg = json.loads(path.read_text(encoding="utf-8-sig"))
            else:
                cfg = {}
            rows = cfg.get("ide_connections", [])
            old = next((r for r in rows if r["connection_id"] == cid), {})
            if params.get("connection_revision", 0) != old.get("revision", 0):
                raise IDEError("连接已被修改，请刷新后保存")
            row = {**spec, "connection_id": cid, "revision": old.get("revision", 0) + 1}
            cfg["ide_connections"] = [r for r in rows if r["connection_id"] != cid] + [row]
            save_config(cfg)
            return {"state": "ready", "connection": row, "reason": "已保存到节点，尚未检查连接"}
    if action == "list_connections":
        return {"state": "ready", "connections": load_config().get("ide_connections", [])}
    binding = params["binding"]
    connection = _connection(binding["connection_id"], binding["connection_revision"])
    if action == "probe":
        return probe(binding)
    if action == "logs":
        source = Path(binding["source_dir"]).resolve(strict=True)
        path = Path(binding.get("service_log_path", "")).resolve(strict=True)
        if not path.is_relative_to(source) or not path.is_file():
            raise IDEError("只允许读取本应用源码目录内已登记的服务日志")
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 32768))
            text = stream.read(32768).decode("utf-8", errors="replace")
        from mino_scout.secret_redaction import register_inputs, sanitize
        register_inputs(load_config())
        text = sanitize(text)
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
        return {"state": "ready", "receipt": {"source": "configured_service_log", "output": text,
                "truncated": size > 32768, "byte_offset": max(0, size - 32768)}}
    if action == "install":
        if not binding.get("execution_allowed") or not binding.get("test_parameters_confirmed"):
            raise IDEError("尚未授权应用安装或确认测试参数")
        from mino_scout.ide_apk import install
        return install(binding)
    lock = nullcontext() if action == "check" else machine_lock(connection["url"])
    with lock, Client(connection) as client:
        result = client.call("get_run_configurations", {"projectPath": binding["project_path"]})
        names = configuration_names(result)
        check = {"connected": True, "project_matched": True, "configurations": names,
                 "server": client.server, "tools": sorted(client.tools), "brave": "unknown",
                 "checked_at": time.time(), "connection_revision": connection["revision"]}
        if action == "check":
            return {"state": "ready", **check, "target_ready": False}
        if action not in {"execute", "restart", "build", "install"}:
            raise IDEError("不支持的开发准备动作")
        name = binding.get("build_configuration") if action == "build" else binding.get("run_configuration")
        if not name or name not in names:
            raise IDEError("所选运行配置不在当前项目中，请重新检查")
        if not binding.get("execution_allowed") or not binding.get("test_parameters_confirmed"):
            raise IDEError("尚未授权应用执行或确认测试参数")
        if action == "restart":
            _stop(client, binding, _old_process(binding, connection["url"]))
        result = client.call("execute_run_configuration", {"configurationName": name,
            "projectPath": binding["project_path"], "timeout": 5000, "maxLinesCount": 200, "truncateMode": "END"})
        if not isinstance(result, dict):
            raise IDEError("IDE 执行回执格式不支持")
        output = str(result.get("output", ""))
        from mino_scout.secret_redaction import register_inputs, sanitize
        register_inputs(load_config())
        output = sanitize(output)
        output = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", output)
        output = re.sub(r"(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", output)
        receipt = {"source": "pycharm.execute_run_configuration", "output": output[-20000:],
                   "truncated": len(output) > 20000 or len(output.splitlines()) >= 200,
                   "timedOut": result.get("timedOut"), "exitCode": result.get("exitCode")}
        if result.get("exitCode") not in (None, 0):
            return {"state": "failed", "reason": "运行配置非零退出；请检查输出和端口占用", "receipt": receipt}
        if action == "build":
            return {"state": "ready" if result.get("exitCode") == 0 else "unknown",
                    "reason": "构建回执；尚未安装或执行测试", "receipt": receipt}
        return {**probe(binding), "receipt": receipt}


def dispatch(core, req):
    from mino_scout.core import _event_from_execute
    from mino_scout.executors.base import make_event_result, now_iso
    from mino_scout.schemas import EventStatus
    from mino_scout.secret_redaction import sanitize
    params = req.params
    opid = str(params.get("operation_id", ""))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", opid):
        data = {"state": "failed", "reason": "缺少稳定 operation_id"}
    else:
        directory = config_path().parent / "ide_receipts"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / (opid + ".json")
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        with _lock:
            if path.exists():
                old = json.loads(path.read_text())
                data = old.get("result") or {"state": "unknown", "reason": "已有请求未确认，不重复执行"}
                if old["digest"] != digest:
                    data = {"state": "failed", "reason": "operation_id 参数冲突"}
                return make_event_result(_event_from_execute(req), status=EventStatus.PASS, executor_used="ide",
                    started_at=now_iso(), elapsed_ms=0, summary=data.get("reason", data["state"]), raw_response={"development": data})
            try:
                with path.open("x") as marker:
                    os.chmod(path, 0o600)
                    json.dump({"digest": digest}, marker)
                    marker.flush()
                    os.fsync(marker.fileno())
            except FileExistsError:
                data = {"state": "unknown", "reason": "同一操作已有跨进程请求，未重复执行"}
                return make_event_result(_event_from_execute(req), status=EventStatus.PASS,
                    executor_used="ide", started_at=now_iso(), elapsed_ms=0,
                    summary=data["reason"], raw_response={"development": data})
        mutation = params.get("action") in {"execute", "restart", "build", "install"}
        admitted = False
        try:
            if mutation:
                with core._lock:
                    if core._active_runs or getattr(core, "_development_io", 0) or getattr(core, "_development_busy", False):
                        raise IDEError("有活动测试或准备操作，请等待结束后发起新的准备操作")
                    core._development_busy = True
                    admitted = True
            data = perform(params)
        except (httpx.TimeoutException, httpx.TransportError):
            data = {"state": "unknown" if mutation else "failed", "reason": "IDE 无回执，执行结果尚未确认；不会自动重试"}
        except Exception as exc:
            known_rejection = isinstance(exc, IDEError) and not exc.effect_unknown
            state = ("awaiting_developer" if known_rejection else "unknown") if admitted else "failed"
            data = {"state": state,
                    "reason": str(exc)[:2000] if isinstance(exc, IDEError) else type(exc).__name__}
        finally:
            if admitted:
                with core._lock:
                    core._development_busy = False
        data = sanitize(data)
        with _lock:
            _save_receipt(path, {"digest": digest, "result": data})
    return make_event_result(_event_from_execute(req), status=EventStatus.PASS, executor_used="ide",
        started_at=now_iso(), elapsed_ms=0, summary=data.get("reason", data["state"]), raw_response={"development": data})
