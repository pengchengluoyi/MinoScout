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
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    return {k: data[k] for k in ("operation_id", "state", "build_id", "reason", "phase",
        "preparation_error", "startup_error", "recovery_error",
        "running_build_id", "running_version_verified", "previous_build_id", "updated_at") if k in data}


def status(opid, running=None):
    data = read(opid)
    if not data:
        return {"operation_id": opid, "state": "not_found"}
    # Only the helper's actual child can certify its loaded build. CLI reads
    # remain read-only; the helper also observes the registered process sidecar.
    if running:
        with lock():
            data = read(opid)
            if _matches_child(data, os.getpid(), psutil.Process().create_time()):
                _confirm_build(data, running)
    result = receipt(data)
    if data["state"] not in FINAL:
        result["recovery_command"] = "update-recover " + opid
    return result


def _matches_child(data, pid, created):
    return (data.get("child_pid") == pid and data.get("child_created") == created
            and data.get("state") in {"verifying", "rollback_verifying"})


def _confirm_build(data, running):
    rollback = data["state"] == "rollback_verifying"
    expected = data["previous_build_id"] if rollback else data["build_id"]
    commit = data.get("previous_source_commit" if rollback else "source_commit")
    if (running.get("build_id") != expected or running.get("dirty") is not False
            or (commit and running.get("source_commit") != commit)):
        return False
    write(data, state="rolled_back" if rollback else "succeeded",
          running_build_id=expected, running_version_verified=True)
    return True


def registration_matches(side, *, pid, created, now):
    """A receipt must describe this process and a fresh registered connection."""
    if not isinstance(side, dict) or side.get("pid") != pid:
        return False
    connection = side.get("connection") or {}
    if not isinstance(connection, dict):
        return False
    try:
        return (connection.get("registered") is True
                and connection.get("connected") is True
                and created <= float(connection.get("last_registered_at", 0)) <= now
                and max(created, now - 90) <= float(side.get("updated_at", 0)) <= now)
    except (ValueError, TypeError, OverflowError):
        return False


def _confirm_registered_process(opid, pid, created):
    from mino_scout.runtime_sidecar import read_runtime_sidecar
    try:
        proc = psutil.Process(pid)
        if proc.create_time() != created or not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        side = read_runtime_sidecar(expect_pid=pid)
        if not registration_matches(side, pid=pid, created=created, now=time.time()):
            return False
        with lock():
            data = read(opid)
            if (_matches_child(data, pid, created) and proc.is_running()
                    and proc.status() != psutil.STATUS_ZOMBIE):
                return _confirm_build(data, side.get("process_build") or {})
    except (OSError, ValueError, TypeError, psutil.Error):
        return False
    return False


def _confirm_registered_child(opid, child):
    """Confirm only the helper's live child, never a disk build or old receipt."""
    if child.poll() is not None:
        return False
    try:
        return _confirm_registered_process(opid, child.pid, psutil.Process(child.pid).create_time())
    except psutil.Error:
        return False


def _resume_live_verification(data):
    """Observe a surviving child before trying to acquire its run lock.

    None means no matching live child remains. A live but unverified child
    keeps the original verification state; explicit recovery may query again.
    Never replace bin, launch a duplicate child or terminate an unknown process.
    """
    if data.get("state") not in {"verifying", "rollback_verifying"}:
        return None
    pid, created = data.get("child_pid"), data.get("child_created")
    if not pid or not created:
        return None
    try:
        proc = psutil.Process(pid)
        if proc.create_time() != created or not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
    except psutil.NoSuchProcess:
        return None
    if _confirm_registered_process(data["operation_id"], pid, created):
        return 0
    with lock():
        current = read(data["operation_id"])
        if current.get("state") in FINAL:
            return 0
        write(current, reason="原操作子进程仍存活，尚未取得匹配的注册回执；保留核验状态，请查询或恢复同一操作")
    return 1


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
    if not BUILD.fullmatch(build_id) or build_id.lower() == "latest":
        raise ValueError("必须指定有效的固定 build_id")
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
        installed_build = json.loads((current / "app/mino_scout/BUILD.json").read_text())
        if any(installed_build.get(key) != loaded_build().get(key) for key in ("build_id", "source_commit", "dirty")):
            raise RuntimeError("磁盘安装与当前进程身份不一致，请先恢复一致后更新")
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
            "previous_build_id": previous, "previous_source_commit": loaded_build().get("source_commit"),
            "manifest_url": url, "state": "installing",
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
        source = op_path(data["params"]["rollback_of"]) / "previous-bin"
        original = read(data["params"]["rollback_of"])
        build = _checked_install(source, data["build_id"], original.get("previous_source_commit"))
        shutil.copytree(source, candidate, symlinks=True)
        write(data, source_commit=build["source_commit"])
        return candidate
    manifest = fetch_json(data["manifest_url"])
    if (manifest.get("manifest_schema_version") != 1 or manifest.get("component") != "scout"
            or manifest.get("build_id") != data["build_id"]
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("source_commit", "")))):
        raise ValueError("发布清单的 schema/component/build/source_commit 不符合固定版本合同")
    compat = manifest.get("compatibility") or {}
    from mino_scout.protocol import PROTOCOL_VERSION
    if (compat.get("protocol") or {}).get("node") != PROTOCOL_VERSION:
        raise ValueError("节点协议变化须通过维护窗口安装固定兼容版本")
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
    _checked_install(candidate, data["build_id"], manifest["source_commit"])
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


def _checked_install(directory, expected, commit=None):
    # Validate before moving any existing installation, including recovery.
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("安装目录缺失或为符号链接")
    build = json.loads((directory / "app/mino_scout/BUILD.json").read_text())
    if (build.get("build_id") != expected or build.get("dirty") is not False
            or not re.fullmatch(r"[0-9a-f]{40}", str(build.get("source_commit", "")))
            or (commit and build.get("source_commit") != commit)):
        raise RuntimeError("安装 build/source_commit/clean 身份核验失败")
    exe = directory / ("mino-scout.exe" if os.name == "nt" else "mino-scout")
    if not exe.is_file():
        raise RuntimeError("安装缺少 Scout 可执行文件")
    return build


def _restore_previous(data):
    """Caller owns run/install locks; retain replaced files for diagnosis."""
    current = config_dir() / "bin"
    previous = op_path(data["operation_id"]) / "previous-bin"
    source = previous if previous.exists() or previous.is_symlink() else current
    _checked_install(source, data["previous_build_id"], data.get("previous_source_commit"))
    if source == previous:
        if current.exists() or current.is_symlink():
            os.replace(current, op_path(data["operation_id"]) / ("recovery-bin-" + uuid.uuid4().hex))
        os.replace(previous, current)


def _startup_failure(opid, reason, rollback):
    with lock():
        data = read(opid)
        field = "recovery_error" if rollback else "startup_error"
        write(data, **{field: reason[:500]})


def _start_and_verify(data, rollback=False):
    exe = config_dir() / "bin" / ("mino-scout.exe" if os.name == "nt" else "mino-scout")
    with lock():
        data = read(data["operation_id"])
        write(data, state="rollback_verifying" if rollback else "verifying",
              child_pid=None, child_created=None)
    kwargs = {"cwd": str(config_dir()), "env": {**os.environ, "MINO_SCOUT_HOME": str(config_dir())},
              "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen([str(exe), "run"], **kwargs)
    except OSError as exc:
        # No process exists, so the caller can safely restore the retained bin.
        _startup_failure(data["operation_id"], "启动失败：" + type(exc).__name__ + ": " + str(exc), rollback)
        return False
    try:
        created = psutil.Process(child.pid).create_time()
    except psutil.NoSuchProcess:
        child.wait(timeout=5)
        _startup_failure(data["operation_id"], "启动后立即退出：" + str(child.returncode), rollback)
        return False
    with lock():
        data = read(data["operation_id"])
        write(data, child_pid=child.pid, child_created=created)
    end = time.monotonic() + 120
    while time.monotonic() < end:
        if _confirm_registered_child(data["operation_id"], child):
            return True
        current = read(data["operation_id"])
        if current["state"] in {"succeeded", "rolled_back"}:
            return True
        if child.poll() is not None:
            break
        time.sleep(1)
    from mino_scout.process_control import request_local_stop
    if child.poll() is None:
        try:
            request_local_stop(child.pid)
        except psutil.NoSuchProcess:
            pass
        child.wait(timeout=30)  # no force kill; failure retains maintenance state
    _startup_failure(data["operation_id"], "未完成运行版本核验；子进程退出码：" + str(child.returncode), rollback)
    return False


def helper(opid, *, recover=False):
    directory = op_path(opid)
    if getattr(sys, "frozen", False) and Path(sys.executable).resolve().parent != (directory / "helper-bin").resolve():
        raise RuntimeError("请使用原操作 helper-bin 中的独立助手执行恢复")
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
        data = read(opid)
        if data["state"] in FINAL:
            return 0
        write(data, helper_started=True)
    try:
        if recover:
            resumed = _resume_live_verification(data)
            if resumed is not None:
                return resumed
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
                with lock():
                    _restore_previous(data)
            else:
                try:
                    candidate = prepare(data)
                except Exception as exc:
                    # prepare only writes operation-owned staging, never bin.
                    # Re-open the existing installation after releasing run_lock.
                    with lock():
                        write(data, phase="prepare_failed", preparation_error=type(exc).__name__ + ": " + str(exc)[:500],
                              reason=type(exc).__name__ + ": " + str(exc)[:500])
                    _checked_install(current, data["previous_build_id"], data.get("previous_source_commit"))
                    recover = True
                if not recover:
                    with lock():
                        if previous.exists() or previous.is_symlink():
                            raise RuntimeError("旧层保留目录已存在，拒绝覆盖")
                        write(data, state="installing", phase="switching")
                        os.replace(current, previous)
                        try:
                            os.replace(candidate, current)
                        except OSError as exc:
                            write(data, phase="switch_failed", reason=type(exc).__name__ + ": " + str(exc)[:500])
                            _restore_previous(data)
                            recover = True
                        else:
                            write(data, state="restarting", phase="switched")
        finally:
            run_lock.release()
        if _start_and_verify(data, rollback=recover):
            return 0
        if recover:
            raise RuntimeError("旧版未能重新连接，保留人工维护状态")
        with lock():
            data = read(opid)
            write(data, state="installing", phase="rolling_back")
        acquired, _ = run_lock.try_acquire(stale_wait_sec=1)
        if not acquired:
            raise RuntimeError("新版未确认退出，不能回退")
        try:
            with lock():
                _restore_previous(data)
        finally:
            run_lock.release()
        if _start_and_verify(data, rollback=True):
            return 0
        raise RuntimeError("兼容旧版未能重新连接")
    except Exception as exc:
        with lock():
            # A child/verification attempt may have added diagnostics since our
            # original snapshot; never overwrite them with stale helper data.
            data = read(opid)
            write(data, state="needs_operator", reason=type(exc).__name__ + ": " + str(exc)[:500])
        return 1


def startup_allowed():
    data = active()
    return not data or data["state"] in {"verifying", "rollback_verifying"}
