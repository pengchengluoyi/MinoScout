"""Read Windows session and process identity without changing the desktop."""
from __future__ import annotations

import ctypes
import os


class WindowsApi:
    def __init__(self):
        from ctypes import wintypes

        self.types = wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        self.wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]

    def process_session_id(self):
        session_id = self.types.DWORD()
        if not self.kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
            raise ctypes.WinError(ctypes.get_last_error())
        return session_id.value

    def connection_state(self, session_id):
        value, length = ctypes.c_void_p(), self.types.DWORD()
        if not self.wts.WTSQuerySessionInformationW(None, session_id, 8, ctypes.byref(value), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if length.value < ctypes.sizeof(self.types.DWORD):
                raise OSError("Invalid WTS session state")
            return ctypes.cast(value, ctypes.POINTER(self.types.DWORD)).contents.value
        finally:
            self.wts.WTSFreeMemory(value)

    def logon_identity(self):
        class Luid(ctypes.Structure):
            _fields_ = [("low", self.types.DWORD), ("high", self.types.LONG)]

        class TokenStatistics(ctypes.Structure):
            _fields_ = [("token_id", Luid), ("authentication_id", Luid),
                        ("expiration", ctypes.c_longlong), ("token_type", ctypes.c_int),
                        ("impersonation_level", ctypes.c_int), ("dynamic_charged", self.types.DWORD),
                        ("dynamic_available", self.types.DWORD), ("group_count", self.types.DWORD),
                        ("privilege_count", self.types.DWORD), ("modified_id", Luid)]

        token, length = self.types.HANDLE(), self.types.DWORD()
        if not self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            stats = TokenStatistics()
            if not self.security.GetTokenInformation(token, 10, ctypes.byref(stats), ctypes.sizeof(stats), ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            return f"{stats.authentication_id.high}:{stats.authentication_id.low}"
        finally:
            self.kernel.CloseHandle(token)

    def input_desktop_name(self):
        desktop = self.user.OpenInputDesktop(0, False, 0x0001)
        if not desktop:
            return None
        try:
            length = self.types.DWORD()
            self.user.GetUserObjectInformationW(desktop, 2, None, 0, ctypes.byref(length))
            if not length.value or length.value > 65536:
                return None
            name = ctypes.create_unicode_buffer(length.value // ctypes.sizeof(ctypes.c_wchar) + 1)
            if not self.user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(length)):
                return None
            return name.value
        finally:
            self.user.CloseDesktop(desktop)

    def process_image(self, pid):
        process = self.kernel.OpenProcess(0x1000, False, int(pid))
        if not process:
            return None
        try:
            length = self.types.DWORD(32768)
            path = ctypes.create_unicode_buffer(length.value)
            if not self.kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)):
                return None
            return path.value
        finally:
            self.kernel.CloseHandle(process)


def windows_session(api=None):
    api = api or WindowsApi()
    session_id = api.process_session_id()
    if session_id == 0:
        return {"state": "no_session", "identity": "service:0"}
    identity = f"{session_id}:{api.logon_identity()}"
    if api.connection_state(session_id) != 0:
        return {"state": "no_session", "identity": identity}
    desktop = api.input_desktop_name()
    if not desktop or desktop.casefold() != "default":
        return {"state": "locked", "identity": identity + ":locked"}
    return {"state": "ready", "identity": identity,
            "permissions": {"interactive_session": True, "input_desktop": True}}


def process_image(pid):
    try:
        return WindowsApi().process_image(pid)
    except (OSError, ValueError):
        return None
