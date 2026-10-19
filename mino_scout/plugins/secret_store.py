"""插件密钥优先使用系统保险库；Linux 不可用时使用私有权限文件。

macOS 走 Security 框架，密钥不出现在进程参数里。
Windows 走当前用户的凭据管理器。
Linux 优先走 Secret Service；不可用时使用 0700 目录内的 0600 明文文件。
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from ctypes import wintypes

from mino_scout.config import development

# Never read or migrate production vault entries from the source runtime.
SERVICE = "MinoScout.Development" if development() else "MinoScout"

_ERR_NOT_FOUND = -25300
_ERR_DUPLICATE = -25299
_ERR_USER_CANCELED = -128
_ERR_AUTH = -25293


class SecretStoreError(Exception):
    pass


def account_name(node_id: str, kind: str, plugin_id: str, field: str) -> str:
    from mino_scout.config import load_config
    owner = str(load_config().get("plugin_owner_user_id") or "")
    if not owner:
        raise SecretStoreError("插件账号归属尚未确认")
    from mino_scout.plugins.paths import profile_id
    profile = profile_id()
    if profile:
        return f"v2:{profile}:{node_id}:{kind}:{plugin_id}:{field}"
    return f"{owner}:{node_id}:{kind}:{plugin_id}:{field}"


def put_secret(account: str, secret: str) -> None:
    text = str(secret or "")
    if not account:
        raise SecretStoreError("密钥条目名为空")
    if sys.platform == "darwin":
        _mac_put(account, text)
        return
    if sys.platform == "win32":
        _win_put(account, text)
        return
    _linux_put(account, text)


def get_secret(account: str) -> str:
    if not account:
        return ""
    if sys.platform == "darwin":
        return _mac_get(account)
    if sys.platform == "win32":
        return _win_get(account)
    return _linux_get(account)


def delete_secret(account: str) -> None:
    if not account:
        return
    if sys.platform == "darwin":
        _mac_delete(account)
        return
    if sys.platform == "win32":
        _win_delete(account)
        return
    _linux_delete(account)


def _mac_status(status: int, action: str) -> None:
    if status == 0:
        return
    if status == _ERR_USER_CANCELED:
        raise SecretStoreError("钥匙串没有允许 Scout 保存密钥")
    if status == _ERR_AUTH:
        raise SecretStoreError("钥匙串拒绝了 Scout 的访问")
    raise SecretStoreError(f"钥匙串{action}失败 ({status})")


def _security():
    lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    lib.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_void_p,
    ]
    lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    lib.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    lib.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.SecKeychainItemFreeContent.restype = ctypes.c_int32
    lib.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
    lib.SecKeychainItemDelete.restype = ctypes.c_int32
    return lib


def _cf_release(ref: ctypes.c_void_p) -> None:
    if not ref:
        return
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease(ref)


def _mac_put(account: str, secret: str) -> None:
    lib = _security()
    service = SERVICE.encode()
    acct = account.encode()
    blob = secret.encode("utf-8")
    status = lib.SecKeychainAddGenericPassword(
        None, len(service), service, len(acct), acct, len(blob), blob, None,
    )
    if status == _ERR_DUPLICATE:
        _mac_delete(account)
        status = lib.SecKeychainAddGenericPassword(
            None, len(service), service, len(acct), acct, len(blob), blob, None,
        )
    _mac_status(status, "写入")


def _mac_get(account: str) -> str:
    lib = _security()
    service = SERVICE.encode()
    acct = account.encode()
    length = ctypes.c_uint32(0)
    data = ctypes.c_void_p()
    status = lib.SecKeychainFindGenericPassword(
        None, len(service), service, len(acct), acct,
        ctypes.byref(length), ctypes.byref(data), None,
    )
    if status == _ERR_NOT_FOUND:
        return ""
    _mac_status(status, "读取")
    try:
        raw = ctypes.string_at(data, int(length.value))
        return raw.decode("utf-8")
    finally:
        lib.SecKeychainItemFreeContent(None, data)


def _mac_delete(account: str) -> None:
    lib = _security()
    service = SERVICE.encode()
    acct = account.encode()
    item = ctypes.c_void_p()
    status = lib.SecKeychainFindGenericPassword(
        None, len(service), service, len(acct), acct,
        None, None, ctypes.byref(item),
    )
    if status == _ERR_NOT_FOUND:
        return
    _mac_status(status, "查找")
    try:
        deleted = lib.SecKeychainItemDelete(item)
        _mac_status(deleted, "删除")
    finally:
        _cf_release(item)


def _win_target(account: str) -> str:
    return f"{SERVICE}:{account}"


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLow", wintypes.DWORD), ("dwHigh", wintypes.DWORD)]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    lib.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    lib.CredWriteW.restype = wintypes.BOOL
    lib.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.CredReadW.restype = wintypes.BOOL
    lib.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    lib.CredDeleteW.restype = wintypes.BOOL
    lib.CredFree.argtypes = [ctypes.c_void_p]
    lib.CredFree.restype = None
    return lib


def _win_put(account: str, secret: str) -> None:
    blob = secret.encode("utf-8")
    buf = ctypes.create_string_buffer(blob)
    cred = _Credential()
    cred.Type = 1
    cred.TargetName = _win_target(account)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.c_void_p)
    cred.Persist = 2
    cred.UserName = SERVICE
    if not _advapi32().CredWriteW(ctypes.byref(cred), 0):
        raise SecretStoreError(f"凭据管理器写入失败 ({ctypes.get_last_error()})")


def _win_get(account: str) -> str:
    lib = _advapi32()
    out = ctypes.c_void_p()
    if not lib.CredReadW(_win_target(account), 1, 0, ctypes.byref(out)):
        err = ctypes.get_last_error()
        if err == 1168:
            return ""
        raise SecretStoreError(f"凭据管理器读取失败 ({err})")
    try:
        cred = ctypes.cast(out, ctypes.POINTER(_Credential)).contents
        if not cred.CredentialBlob or not cred.CredentialBlobSize:
            return ""
        raw = ctypes.string_at(cred.CredentialBlob, int(cred.CredentialBlobSize))
        return raw.decode("utf-8")
    finally:
        lib.CredFree(out)


def _win_delete(account: str) -> None:
    lib = _advapi32()
    if lib.CredDeleteW(_win_target(account), 1, 0):
        return
    if ctypes.get_last_error() == 1168:
        return
    raise SecretStoreError(f"凭据管理器删除失败 ({ctypes.get_last_error()})")


def _linux_call(action: str, account: str, secret: str | None = None):
    tool = shutil.which("secret-tool")
    if not tool:
        return None
    args = [tool, action]
    if action == "store":
        args.append("--label=MinoScout")
    try:
        return subprocess.run(
            [*args, "service", SERVICE, "account", account],
            input=secret.encode("utf-8") if secret is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _linux_put(account: str, secret: str) -> None:
    from mino_scout.plugins.file_secrets import locked_account
    try:
        with locked_account(account) as entry:
            present, _ = entry.read()
            # Once a file is authoritative, do not silently revert to an old keyring value.
            if not present:
                result = _linux_call("store", account, secret)
                if result is not None and result.returncode == 0:
                    return
            entry.write(secret)
    except (OSError, ValueError) as exc:
        raise SecretStoreError("Linux 凭据文件保存失败，请检查目录权限或文件格式") from exc


def _linux_get(account: str) -> str:
    from mino_scout.plugins.file_secrets import locked_account
    try:
        with locked_account(account) as entry:
            present, value = entry.read()
            if present:
                return value or ""
            result = _linux_call("lookup", account)
            if result is None or result.returncode != 0:
                return ""
            raw = result.stdout
            return (raw[:-1] if raw.endswith(b"\n") else raw).decode("utf-8")
    except (OSError, ValueError) as exc:
        raise SecretStoreError("Linux 凭据文件读取失败，请检查目录权限或文件格式") from exc


def _linux_delete(account: str) -> None:
    from mino_scout.plugins.file_secrets import locked_account
    try:
        with locked_account(account) as entry:
            # Record deletion before contacting an unavailable/locked keyring. This
            # prevents a stale secret from reappearing when Secret Service returns.
            entry.write(None)
            _linux_call("clear", account)
    except (OSError, ValueError) as exc:
        raise SecretStoreError("Linux 凭据删除失败，请检查目录权限或文件格式") from exc
