"""Install an explicitly selected local APK on the assigned local Android device."""
from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess
import tempfile

from mino_scout.config import config_path
from mino_scout.ide_client import IDEError


def install(binding):
    source = Path(binding["source_dir"]).resolve(strict=True)
    apk = Path(binding["apk_path"]).resolve(strict=True)
    if not apk.is_relative_to(source) or apk.suffix.lower() != ".apk" or not apk.is_file():
        raise IDEError("APK 必须位于本应用已授权源码目录内")
    serial = binding.get("android_serial", "")
    package = binding.get("apk_package", "")
    version = binding.get("apk_version_code", "")
    if not serial or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package) or not version.isdigit():
        raise IDEError("请明确同节点设备、APK 包名与 versionCode")
    inspector = shutil.which("aapt2") or shutil.which("aapt")
    if not inspector:
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        candidates = sorted((Path(sdk) / "build-tools").glob("*/aapt2*")) if sdk else []
        inspector = str(next((p for p in reversed(candidates) if p.is_file() and os.access(p, os.X_OK)), ""))
    if not inspector:
        raise IDEError("缺少 Android SDK aapt2，无法在安装前核验包名与版本；未安装 APK")
    from mino_scout.executors.adb_command import _adb
    staging = config_path().parent / "ide_artifacts"
    staging.mkdir(parents=True, exist_ok=True)
    # Copy and inspect the exact bytes to install, never a changing build output.
    with tempfile.TemporaryDirectory(prefix="hi3d-apk-", dir=staging) as directory:
        fixed = Path(directory) / "hi3d-install.apk"
        before = apk.stat()
        shutil.copyfile(apk, fixed)
        after = apk.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise IDEError("构建产物仍在变化，未安装")
        result = subprocess.run([inspector, "dump", "badging", str(fixed)], capture_output=True, text=True, timeout=15)
        match = re.search(r"package: name='([^']+)' versionCode='([^']+)'", result.stdout)
        if result.returncode or not match or match.groups() != (package, version):
            raise IDEError("APK 实际包名/versionCode 与配置不匹配，未安装")
        rc, installed, _ = _adb(serial, "shell", "dumpsys", "package", package, timeout=10)
        previous = re.search(r"versionCode=(\d+)", installed)
        if rc != 0:
            raise IDEError("无法读取设备当前包状态，未安装")
        if previous and int(previous.group(1)) > int(version):
            raise IDEError("需要降级，已阻止；不会卸载或清数据")
        with fixed.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        rc, output, error = _adb(serial, "install", "-r", "-t", str(fixed), timeout=60)
        if rc != 0 or "Success" not in output:
            return {"state": "failed", "reason": "APK 安装失败；签名冲突/降级需人工处理，未卸载或清数据", "apk_sha256": digest}
        rc, installed, _ = _adb(serial, "shell", "dumpsys", "package", package, timeout=10)
        actual = re.search(r"versionCode=(\d+)", installed)
        if rc or not actual or actual.group(1) != version:
            return {"state": "unknown", "reason": "安装返回成功但实际版本未确认，请核对设备", "apk_sha256": digest}
        return {"state": "ready", "reason": "APK 已安装并核对包名与版本；尚未运行测试", "apk_sha256": digest,
                "package": package, "version_code": version, "device": serial, "build_verified": True}
