"""Windows 键盘焦点。"""
from __future__ import annotations

import ctypes


class WindowsFocus:

    def __init__(self):
        from ctypes import wintypes as W
        class GuiInfo(ctypes.Structure):
            _fields_ = [("size", W.DWORD), ("flags", W.DWORD), ("active", W.HWND), ("focus", W.HWND),
                        ("capture", W.HWND), ("menu", W.HWND), ("move", W.HWND), ("caret", W.HWND), ("rect", W.RECT)]
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.GetForegroundWindow.restype = W.HWND
        api.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        api.GetGUIThreadInfo.argtypes = [W.DWORD, ctypes.POINTER(GuiInfo)]
        hwnd = api.GetForegroundWindow()
        pid = W.DWORD()
        tid = api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        info = GuiInfo()
        info.size = ctypes.sizeof(info)
        if not tid or not api.GetGUIThreadInfo(tid, ctypes.byref(info)) or not info.focus:
            raise RuntimeError("Cannot confirm native keyboard focus")
        self.identity = (pid.value, int(hwnd), int(info.focus))

    def matches(self, other):
        return isinstance(other, WindowsFocus) and self.identity == other.identity

    def close(self):
        pass
