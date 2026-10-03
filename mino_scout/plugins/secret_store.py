"""插件密钥放进操作系统保险库。写不进去就失败，不落明文文件。

macOS 走 Security 框架，密钥不出现在进程参数里。
Windows 走当前用户的凭据管理器。
Linux 走 Secret Service（secret-tool 的标准输入）。没有这项服务就失败。
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from ctypes import wintypes

SERVICE = "MinoScout"

_ERR_NOT_FOUND = -25300
_ERR_DUPLICATE = -25299
_ERR_USER_CANCELED = -128
_ERR_AUTH = -25293


class SecretStoreError(Exception):
    pass


def account_name(node_id: str, kind: str, plugin_id: str, field: str) -> str:
    return f"{node_id}:{kind}:{plugin_id}:{field}"


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
    lib = ctypes.WinDLL("advapi32")
    lib.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    lib.CredWriteW.restype = wintypes.BOOL
    lib.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    lib.CredReadW.restype = wintypes.BOOL
    lib.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    lib.CredDeleteW.restype = wintypes.BOOL
    lib.CredFree.argtypes = [ctypes.c_void_p]
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


def _linux_tool() -> str:
    path = shutil.which("secret-tool")
    if not path:
        raise SecretStoreError("没有 Secret Service（找不到 secret-tool），没有把密钥写成明文")
    return path


def _linux_put(account: str, secret: str) -> None:
    proc = subprocess.run(
        [_linux_tool(), "store", "--label=MinoScout", "service", SERVICE, "account", account],
        input=secret.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise SecretStoreError(err or "Secret Service 写入失败")


def _linux_get(account: str) -> str:
    proc = subprocess.run(
        [_linux_tool(), "lookup", "service", SERVICE, "account", account],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    raw = proc.stdout
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    return raw.decode("utf-8")


def _linux_delete(account: str) -> None:
    subprocess.run(
        [_linux_tool(), "clear", "service", SERVICE, "account", account],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
