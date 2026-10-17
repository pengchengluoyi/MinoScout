"""Fixed release updates with durable receipts and a separate stable helper.

Never execute downloaded installer scripts. Keep the full old bin tree and
configuration; only validated release payloads enter the staged installation.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
import uuid
import zipfile

import psutil

from mino_scout.config import config_dir

FINAL = {"succeeded", "failed", "rolled_back"}
ID = re.compile(r"[A-Za-z0-9_-]{8,100}\Z")
BUILD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


def root():
    path = config_dir() / "updates"
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or path.resolve().parent != config_dir().resolve():
        raise RuntimeError("更新目录边界不符")
    return path


def op_path(opid):
    if not ID.fullmatch(opid):
        raise ValueError("operation_id 格式无效")
    path = root() / opid
    if path.is_symlink():
        raise ValueError("操作目录不能是符号链接")
    return path


def save(path, data):
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with tmp.open("x", encoding="utf-8") as out:
        os.chmod(tmp, 0o600)
        json.dump(data, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def read(opid):
    path = op_path(opid) / "operation.json"
    return json.loads(path.read_text()) if path.is_file() else None


def write(data, **changes):
    data.update(changes)
    data["updated_at"] = time.time()
    save(op_path(data["operation_id"]) / "operation.json", data)
    return data


@contextmanager
def lock(name="install.lock"):
    path = root() / name
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise RuntimeError("安装锁不能是硬链接")
        if os.name == "nt":
            import msvcrt
            if not os.fstat(fd).st_size:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def active():
    for path in root().glob("*/operation.json"):
        data = json.loads(path.read_text())
        if data.get("state") not in FINAL:
            return data
    return None


def loaded_build():
    from mino_scout.app_version import process_build_identity
    return process_build_identity()


def receipt(data):
    return {k: data[k] for k in ("operation_id", "state", "build_id", "reason",
        "running_build_id", "running_version_verified", "previous_build_id", "updated_at") if k in data}


def status(opid, running=None):
    data = read(opid)
    if not data:
        return {"operation_id": opid, "state": "not_found"}
    # Called by the connected Scout, using metadata captured at core construction.
    # A helper/CLI reading the receipt cannot certify a running build.
    if running and data["state"] in {"verifying", "rollback_verifying"}:
        expected = data["build_id"] if data["state"] == "verifying" else data["previous_build_id"]
        if running.get("build_id") == expected:
            with lock():
                data = read(opid)
                if data["state"] in {"verifying", "rollback_verifying"}:
                    write(data, state="succeeded" if data["state"] == "verifying" else "rolled_back",
                        running_build_id=expected, running_version_verified=True)
    result = receipt(data)
    if data["state"] not in FINAL:
        result["recovery_command"] = "update-recover " + opid
    return result


def fixed_url(build_id):
    from mino_scout.self_update import manifest_url_from_config
    if not BUILD.fullmatch(build_id) or build_id.lower() == "latest":
        raise ValueError("必须指定固定 release tag/build_id，不能使用 latest")
    source = urlsplit(manifest_url_from_config())
    match = re.fullmatch(r"/([^/]+)/([^/]+)/releases/(?:latest/download|download/[^/]+)/manifest.json", source.path)
    if source.scheme != "https" or source.netloc != "github.com" or not match or source.query or source.fragment:
        raise ValueError("固定版本更新需要节点已配置的 GitHub Releases 发布源")
    return f"https://github.com/{match[1]}/{match[2]}/releases/download/{quote(build_id, safe='')}/manifest.json"


def submit(opid, build_id, *, release_tag, action="update", rollback_of="", parent_pid=0):
    from mino_scout.config import development
    if development():
        raise ValueError("源码 Scout 不执行安装包更新，请更新源码并重启")
    from mino_scout.service import read_pid, pid_alive
    if action not in {"update", "rollback"}:
        raise ValueError("未知更新动作")
    params = {"build_id": build_id, "release_tag": release_tag, "action": action, "rollback_of": rollback_of}
    with lock():
        old = read(opid)
        if old:
            if old["params"] != params:
                raise ValueError("operation_id 参数冲突")
            return receipt(old)
        other = active()
        if other:
            raise RuntimeError("已有未完成更新：" + other["operation_id"])
        current = config_dir() / "bin"
        exe = current / ("mino-scout.exe" if os.name == "nt" else "mino-scout")
        if not exe.is_file() or current.is_symlink():
            raise RuntimeError("需要完整正式分层安装；源码进程不能自更新")
        if parent_pid and (not getattr(sys, "frozen", False) or Path(sys.executable).resolve() != exe.resolve()):
            raise RuntimeError("当前进程不是该正式安装版，拒绝更新其他实例")
        if not parent_pid and pid_alive(read_pid()):
            raise RuntimeError("节点正在运行，请通过 Nexus 门禁更新，或空闲后先停止再使用 CLI")
        previous = loaded_build().get("build_id", "")
        if not previous or loaded_build().get("dirty"):
            raise RuntimeError("当前安装缺少 BUILD.json；先安装支持 D1 合同的正式基线")
        url = fixed_url(release_tag)
        if action == "rollback":
            source = read(rollback_of)
            if not source or source.get("state") != "succeeded" or source.get("previous_build_id") != build_id:
                raise RuntimeError("只能回退到已成功更新所保留的兼容旧版本")
            if previous != source.get("build_id") or not (op_path(rollback_of) / "previous-bin").is_dir():
                raise RuntimeError("当前版本或保留旧层与回退来源不匹配")
        directory = op_path(opid)
        directory.mkdir(mode=0o700)
        data = {"operation_id": opid, "params": params, "build_id": build_id,
            "previous_build_id": previous, "manifest_url": url, "state": "installing",
            "created_at": time.time(), "parent_pid": parent_pid,
            "parent_created": psutil.Process(parent_pid).create_time() if parent_pid else 0}
        write(data)
        try:
            # The helper runtime/app must outlive replacement of bin. Browser payload
            # is unnecessary for the helper; configuration is never copied.
            helper = directory / "helper-bin"
            shutil.copytree(current, helper, symlinks=True, ignore=shutil.ignore_patterns("ms-playwright"))
            args = [str(helper / exe.name), "update-helper", opid]
            kwargs = {"env": {**os.environ, "MINO_SCOUT_HOME": str(config_dir())},
                      "cwd": str(directory), "stdin": subprocess.DEVNULL,
                      "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(args, **kwargs)
        except Exception as exc:
            write(data, state="failed", reason="独立助手启动失败：" + type(exc).__name__)
            raise
        return receipt(data)


def unpack(archive, destination):
    destination.mkdir()
    with zipfile.ZipFile(archive) as zf:
        seen = set()
        for entry in zf.infolist():
            rel = Path(entry.filename)
            target = destination / rel
            mode = entry.external_attr >> 16
            if (rel.is_absolute() or ".." in rel.parts or "\\" in entry.filename
                    or stat.S_ISLNK(mode) or not target.resolve().is_relative_to(destination.resolve())
                    or entry.filename in seen):
                raise ValueError("发布包包含不安全或重复路径")
            seen.add(entry.filename)
            zf.extract(entry, destination)
            if target.is_file():
                target.chmod((mode & 0o777) or 0o644)
    candidates = list(destination.rglob("layers.txt"))
    roots = [p.parent for p in candidates if any((p.parent / k).is_dir() for k in ("app", "runtime", "browser"))]
    if len(roots) != 1:
        raise ValueError("发布包缺少唯一分层根目录")
    return roots[0]


def prepare(data):
    from mino_scout.http_fetch import fetch_json, download_file
    from mino_scout.self_update import pick_manifest_item, _verify_sha256
    from mino_scout.install_plan import plan_scout_update, parse_layers_txt
    directory = op_path(data["operation_id"])
    current = config_dir() / "bin"
    candidate = directory / "next-bin"
    if data["params"]["action"] == "rollback":
        shutil.copytree(op_path(data["params"]["rollback_of"]) / "previous-bin", candidate, symlinks=True)
        return candidate
    manifest = fetch_json(data["manifest_url"])
    if (manifest.get("manifest_schema_version") != 1 or manifest.get("component") != "scout"
            or manifest.get("build_id") != data["build_id"]
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", "")))):
        raise ValueError("发布清单的 schema/component/build/source_commit 不符合固定版本合同")
    compat = manifest.get("compatibility") or {}
    if (compat.get("scout_update_contract") != 1 or compat.get("automatic_rollback") is not True
            or data["previous_build_id"] not in compat.get("rollback_build_ids", [])):
        raise ValueError("此版本未声明当前旧版的已验证回退兼容性，需人工维护")
    item = pick_manifest_item(manifest)
    if not isinstance(manifest.get("items"), list):
        raise ValueError("固定版本必须声明 os/arch")
    installed = parse_layers_txt((current / "layers.txt").read_text())
    plan = plan_scout_update(item, installed, bin_dir=current)
    if plan["mode"] == "up-to-date" and data["build_id"] != data["previous_build_id"]:
        raise ValueError("不同 build 却没有不同层指纹，发布清单不一致")
    save(directory / "manifest.json", manifest)
    write(data, plan=plan, source_commit=manifest["source_commit"])
    shutil.copytree(current, candidate, symlinks=True)
    layers = dict(installed or {})
    origin = urlsplit(data["manifest_url"])
    repo_prefix = "/".join(origin.path.split("/")[:3]) + "/releases/download/"
    for idx, step in enumerate(plan.get("steps", [])):
        url = urlsplit(step.get("url", ""))
        sha = str(step.get("sha256", ""))
        size = step.get("bytes", 0)
        if (url.scheme != "https" or url.netloc != "github.com" or not url.path.startswith(repo_prefix)
                or url.query or url.fragment or not re.fullmatch(r"[0-9a-fA-F]{64}", sha)
                or type(size) is not int or size <= 0):
            raise ValueError("安装层缺少可信发布地址、sha256 或大小")
        archive = directory / f"layer-{idx}.zip"
        download_file(step["url"], archive)
        if archive.stat().st_size != size:
            raise ValueError("安装包大小不匹配")
        _verify_sha256(archive, sha.lower())
        payload = unpack(archive, directory / f"payload-{idx}")
        incoming = parse_layers_txt((payload / "layers.txt").read_text()) or {}
        requested = [step["layer"]] if step["layer"] else list(incoming)
        if any(name not in {"runtime", "app", "browser"} for name in requested):
            raise ValueError("未知安装层")
        for name in requested:
            expected = (item.get("layers") or {}).get(name, {}).get("key")
            if not expected or incoming.get(name) != expected or not (payload / name).is_dir():
                raise ValueError("包内层指纹与固定 manifest 不匹配")
            if name == "runtime":
                # Remove only candidate runtime entries, preserve app/browser.
                for child in list(candidate.iterdir()):
                    if child.name not in {"app", "ms-playwright", "layers.txt"}:
                        remove_staged(child)
                for child in (payload / name).iterdir():
                    if child.name in {"app", "ms-playwright", "layers.txt"}:
                        raise ValueError("runtime 包越过层边界")
                    copy_path(child, candidate / child.name)
            else:
                target = candidate / ("ms-playwright" if name == "browser" else name)
                remove_staged(target)
                shutil.copytree(payload / name, target)
            layers[name] = incoming[name]
    app_contract = (item.get("layers") or {}).get("app") or {}
    if not app_contract.get("requires_runtime") or layers.get("runtime") != app_contract["requires_runtime"]:
        raise ValueError("app/runtime 指纹不兼容")
    if app_contract.get("requires_browser") and layers.get("browser") != app_contract["requires_browser"]:
        raise ValueError("app/browser 指纹不兼容；需先准备匹配浏览器层")
    (candidate / "layers.txt").write_text("".join(f"{k} {v}\n" for k, v in layers.items()))
    build = json.loads((candidate / "app/mino_scout/BUILD.json").read_text())
    if build.get("build_id") != data["build_id"] or build.get("source_commit") != manifest["source_commit"]:
        raise ValueError("包内 BUILD 与清单不匹配")
    return candidate


def remove_staged(path):
    # Only our new candidate/payload trees call this helper, never user configuration.
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def copy_path(source, target):
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _wait_parent(data):
    pid = data.get("parent_pid", 0)
    if not pid:
        return
    try:
        proc = psutil.Process(pid)
        if proc.create_time() == data["parent_created"]:
            proc.wait(timeout=120)
    except psutil.NoSuchProcess:
        pass


def _start_and_verify(data, rollback=False):
    exe = config_dir() / "bin" / ("mino-scout.exe" if os.name == "nt" else "mino-scout")
    with lock():
        write(data, state="rollback_verifying" if rollback else "verifying")
    kwargs = {"cwd": str(config_dir()), "env": {**os.environ, "MINO_SCOUT_HOME": str(config_dir())},
              "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    child = subprocess.Popen([str(exe), "run"], **kwargs)
    with lock():
        data = read(data["operation_id"])
        write(data, child_pid=child.pid, child_created=psutil.Process(child.pid).create_time())
    end = time.monotonic() + 120
    while time.monotonic() < end:
        current = read(data["operation_id"])
        if current["state"] in {"succeeded", "rolled_back"}:
            return True
        if child.poll() is not None:
            break
        time.sleep(1)
    from mino_scout.process_control import request_local_stop
    if child.poll() is None:
        request_local_stop(child.pid)
        child.wait(timeout=30)  # no force kill; failure retains maintenance state
    return False


def helper(opid, *, recover=False):
    op_path(opid)
    with lock(f"{opid}/helper.lock"):
        return _helper(opid, recover=recover)


def _helper(opid, *, recover=False):
    data = read(opid)
    if not data:
        return 1
    if data["state"] in FINAL:
        return 0
    if data.get("helper_started") and not recover:
        return 1  # explicit recovery only; never replay a partially installed op
    with lock():
        write(data, helper_started=True)
    try:
        _wait_parent(data)
        from mino_scout.service import _RunLockHold
        run_lock = _RunLockHold()
        acquired, _ = run_lock.try_acquire(stale_wait_sec=1)
        if not acquired:
            raise RuntimeError("旧 Scout 未退出或另一个进程正在运行")
        try:
            current = config_dir() / "bin"
            previous = op_path(opid) / "previous-bin"
            if recover:
                # Recovery never downloads/reinstalls. Restore the retained original
                # after an interrupted rename, or restart it if switch never began.
                if previous.is_dir():
                    if current.exists():
                        os.replace(current, op_path(opid) / ("recovery-bin-" + uuid.uuid4().hex))
                    os.replace(previous, current)
                installed = json.loads((current / "app/mino_scout/BUILD.json").read_text())
                if installed.get("build_id") != data["previous_build_id"]:
                    raise RuntimeError("无法确认恢复后的旧版身份")
            else:
                candidate = prepare(data)
                with lock():
                    write(data, state="installing", phase="switching")
                    os.replace(current, previous)
                    os.replace(candidate, current)
                    write(data, state="restarting", phase="switched")
        finally:
            run_lock.release()
        if _start_and_verify(data, rollback=recover):
            return 0
        if recover:
            raise RuntimeError("旧版未能重新连接，保留人工维护状态")
        with lock():
            write(data, state="installing", phase="rolling_back")
        acquired, _ = run_lock.try_acquire(stale_wait_sec=1)
        if not acquired:
            raise RuntimeError("新版未确认退出，不能回退")
        try:
            os.replace(config_dir() / "bin", op_path(opid) / "failed-bin")
            os.replace(op_path(opid) / "previous-bin", config_dir() / "bin")
        finally:
            run_lock.release()
        if _start_and_verify(data, rollback=True):
            return 0
        raise RuntimeError("兼容旧版未能重新连接")
    except Exception as exc:
        with lock():
            write(data, state="needs_operator", reason=type(exc).__name__ + ": " + str(exc)[:500])
        return 1


def startup_allowed():
    data = active()
    return not data or data["state"] in {"verifying", "rollback_verifying"}
