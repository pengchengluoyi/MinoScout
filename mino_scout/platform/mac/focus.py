"""Mac 键盘焦点和前台进程。"""
from __future__ import annotations

import ctypes


class MacFocus:

    def __init__(self):
        cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        ax = ctypes.CDLL('/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices')
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFEqual.restype = ctypes.c_bool
        ax.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        ax.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        root = ax.AXUIElementCreateSystemWide()
        app = ctypes.c_void_p()
        focus = ctypes.c_void_p()
        try:
            for owner, attribute, output in ((root, b'AXFocusedApplication', app), (app, b'AXFocusedUIElement', focus)):
                name = cf.CFStringCreateWithCString(None, attribute, 0x08000100)
                try:
                    if ax.AXUIElementCopyAttributeValue(owner, name, ctypes.byref(output)) != 0 or not output.value:
                        raise RuntimeError('Cannot confirm accessibility keyboard focus')
                finally:
                    cf.CFRelease(name)
        except Exception:
            if focus.value:
                cf.CFRelease(focus)
            raise
        finally:
            if app.value:
                cf.CFRelease(app)
            cf.CFRelease(root)
        self.cf, self.element = cf, focus

    def matches(self, other):
        return isinstance(other, MacFocus) and bool(self.element.value and other.element.value and self.cf.CFEqual(self.element, other.element))

    def close(self):
        if self.element.value:
            self.cf.CFRelease(self.element)
            self.element.value = None


def mac_foreground_pid():
    ctypes.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    send_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    send_pid = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    send_void = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    pool = send_object(objc.objc_getClass(b'NSAutoreleasePool'), objc.sel_registerName(b'new'))
    try:
        workspace = send_object(objc.objc_getClass(b'NSWorkspace'), objc.sel_registerName(b'sharedWorkspace'))
        if not workspace:
            raise RuntimeError('Cannot inspect native workspace')
        app = send_object(workspace, objc.sel_registerName(b'frontmostApplication'))
        if not app:
            raise RuntimeError('Cannot confirm native foreground application')
        pid = send_pid(app, objc.sel_registerName(b'processIdentifier'))
        if pid <= 0:
            raise RuntimeError('Cannot confirm native foreground process')
        return pid
    finally:
        if pool:
            send_void(pool, objc.sel_registerName(b'drain'))
