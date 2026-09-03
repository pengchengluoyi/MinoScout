# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""ADB 执行通道：通过 subprocess 调 adb 直接操作真机。

支持的 capability：
  - launch_app / close_app / press_key / wait_ms
  - swipe_direction / install_apk / read_device_data / set_clipboard
  - get_app_version / get_foreground_app
  - tap_element / multi_tap / input_text (需 VLM 在 ctx.screen 上先定位)
"""
from __future__ import annotations

import shlex
import subprocess
import time
from typing import Any, Optional

from mino_scout.log import SLog

from mino_scout.schemas import EventResult, EventStatus, PlanEvent
from mino_scout.executors.base import (
    Executor,
    ExecutorContext,
    _now_iso,
    make_event_result,
)

TAG = "AdbExecutor"

# capability_id → 内部处理方法（有专属 Python 实现的）
_SUPPORTED_CAPS: set[str] = {
    "launch_app",
    "close_app",
    "press_key",
    "wait_ms",
    "swipe_direction",
    "swipe_element_to_element",
    "install_apk",
    "read_device_data",
    "get_app_version",
    "get_foreground_app",
    "set_clipboard",
    "tap_element",
    "multi_tap",
    "long_press_element",
    "input_text",
    "exec_script",
}


def _runnable_low_level(low_level: Optional[dict[str, Any]]) -> bool:
    """Nexus 是否给了一段可执行的 low_level 声明。

    port(scout) 语义改动：上游同名逻辑叫 `_declares_adb_low_level(capability_id)`，
    它通过 `plugin_registry.get_capability()` **回查能力目录**，判断"这条 cap 虽然
    没有 Python 分支，但它的 adb 实现声明了 low_level，所以我支持"。

    Scout 不读能力目录（CLAUDE.md §1 约束 3），所以改成由 Nexus 把 low_level 段
    直接塞进 EXECUTE 载荷、经 `supports(cap, low_level)` 传进来。

    保留这条通路是必须的 —— 它是「加 YAML 就多一个能力」的支撑
    （见 docs/EXECUTORS.md §3）。丢了它就退化成"每个能力都要改 Python"。
    """
    low = low_level or {}
    return bool(low.get("shell") or low.get("command") or low.get("items") or low.get("steps"))


class AdbExecutor:
    id = "adb"

    # 上报给 Nexus 的 abstract cap（协议 §4.1 的 executors[].provides）。
    # 词表真源是 Nexus 的 abstract_caps.yaml —— 本仓只报字符串，不解释含义。
    # 与 MiniOrangeServer 的 plugins/executors/adb.yaml 保持一致。
    provides = (
        "system_shell",
        "system_pkg_install",
        "system_pkg_clear",
        "read_system_data",
        "ui_native_input",
        "ui_input_text",
        "ui_screenshot",
        "app_launch_native",
        "app_force_stop",
        "clipboard_set",
        "key_event",
    )

    def probe(self) -> tuple[bool, str]:
        """adb 二进制在不在。具体设备的连通性在 devices[].channels 里报。"""
        try:
            proc = subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            return False, "adb 不在 PATH"
        except Exception as exc:
            return False, f"adb version 失败: {exc}"
        if proc.returncode != 0:
            return False, f"adb version rc={proc.returncode}"
        return True, ""

    def supports(self, capability_id: str, low_level: Optional[dict[str, Any]] = None) -> bool:
        return capability_id in _SUPPORTED_CAPS or _runnable_low_level(low_level)

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        # 上游是 ctx.run_context.adb["serial"]（RunContext 查库 + 跑 connectivity_probe
        # 得来）。Scout 不查库，serial 由 Nexus 在 EXECUTE.device_hint 里注入。
        serial = str(ctx.device.adb_serial or "")
        try:
            if not serial:
                return self._fail(
                    event, started_at, t0,
                    "adb serial 缺失：EXECUTE.device_hint 未带 adb_serial（Nexus 侧应注入）",
                )

            if cap == "launch_app":
                return self._launch_app(event, ctx, serial, started_at, t0)
            if cap == "close_app":
                return self._close_app(event, ctx, serial, started_at, t0)
            if cap == "press_key":
                return self._press_key(event, ctx, serial, started_at, t0)
            if cap == "wait_ms":
                return self._wait_ms(event, ctx, started_at, t0)
            if cap == "swipe_direction":
                return self._swipe_direction(event, ctx, serial, started_at, t0)
            if cap == "swipe_element_to_element":
                return self._swipe_element_to_element(event, ctx, serial, started_at, t0)
            if cap == "install_apk":
                return self._install_apk(event, ctx, serial, started_at, t0)
            if cap == "read_device_data":
                return self._read_device_data(event, ctx, serial, started_at, t0)
            if cap == "get_app_version":
                return self._get_app_version(event, ctx, serial, started_at, t0)
            if cap == "get_foreground_app":
                return self._get_foreground_app(event, ctx, serial, started_at, t0)
            if cap == "set_clipboard":
                return self._set_clipboard(event, ctx, serial, started_at, t0)
            if cap == "tap_element":
                return self._tap_element(event, ctx, serial, started_at, t0)
            if cap == "multi_tap":
                return self._multi_tap(event, ctx, serial, started_at, t0)
            if cap == "long_press_element":
                return self._long_press_element(event, ctx, serial, started_at, t0)
            if cap == "input_text":
                return self._input_text(event, ctx, serial, started_at, t0)
            if cap == "exec_script":
                return self._exec_script(event, ctx, serial, started_at, t0)
            # 兜底：capability 未写 Python 分支时，按 yaml 里的 low_level 声明执行。
            # 这条分支让"只加 yaml 就多一个能力"成真（见 docs/plan-skill-packs-and-console.md §3.1）。
            return self._run_declared_low_level(event, ctx, serial, started_at, t0)
        except Exception as e:
            SLog.e(TAG, f"execute exception cap={cap} sn={ctx.device.sn}: {e}")
            return self._fail(event, started_at, t0, f"exception: {e}")

    # ---------- 通用 shell helper ----------

    def _adb_shell(self, serial: str, *args: str, timeout_sec: float = 30.0) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["adb", "-s", serial, "shell", *args],
                capture_output=True, text=True, timeout=timeout_sec,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout {timeout_sec}s"
        except FileNotFoundError:
            return -2, "", "adb not in PATH"

    def _adb_sh_script(self, serial: str, script: str, *, timeout_sec: float = 30.0):
        """在设备上跑一段**复合 shell 脚本**（含管道 / 分号 / 括号）。

        为什么不能直接 `_adb_shell(serial, "sh", "-c", script)`：
        `adb shell` 会把 argv 拼成一行发到设备，**设备侧的 shell 再解析一次词**，
        于是 `sh -c` 只拿到脚本的第一个词（如 `(`），报
        `/system/bin/sh: syntax error: unexpected '('`。

        必须再引一层。这是上游 MiniOrangeServer 的一个真 bug ——
        `get_foreground_app` / 任何带管道的 dumpsys 在真机上一直是坏的
        （已在 vivo V2528A 上复现并验证修复）。
        """
        return self._adb_shell(serial, "sh", "-c", shlex.quote(script), timeout_sec=timeout_sec)

    def _adb(self, serial: str, *args: str, timeout_sec: float = 60.0) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["adb", "-s", serial, *args],
                capture_output=True, text=True, timeout=timeout_sec,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout {timeout_sec}s"
        except FileNotFoundError:
            return -2, "", "adb not in PATH"

    # ---------- handlers ----------

    def _launch_app(self, event, ctx, serial, started_at, t0):
        pkg = (event.params or {}).get("package") or ""
        if not pkg:
            return self._fail(event, started_at, t0, "launch_app 缺 params.package")
        rc, out, err = self._adb_shell(serial, "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0 and "Events injected" in out:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"启动 {pkg}",
                raw_response={"stdout": out[:240]},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"启动 {pkg} 失败", error=err or out or f"rc={rc}",
            raw_response={"stdout": out[:240], "stderr": err[:240], "rc": rc},
        )

    def _close_app(self, event, ctx, serial, started_at, t0):
        pkg = (event.params or {}).get("package") or ""
        if not pkg:
            return self._fail(event, started_at, t0, "close_app 缺 params.package")
        rc, out, err = self._adb_shell(serial, "am", "force-stop", pkg)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"强停 {pkg}",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"强停 {pkg} 失败", error=err or out or f"rc={rc}",
        )

    def _press_key(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        keycode = str(params.get("key") or params.get("keycode") or "").upper()
        if not keycode:
            return self._fail(event, started_at, t0, "press_key 缺 params.key")
        key_map = {
            "BACK": "4", "HOME": "3", "MENU": "82", "POWER": "26", "ENTER": "66",
            "VOLUME_UP": "24", "VOLUME_DOWN": "25", "RECENT": "187",
        }
        kev = key_map.get(keycode, keycode)
        rc, out, err = self._adb_shell(serial, "input", "keyevent", kev)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"按键 {keycode}",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"按键 {keycode} 失败", error=err or out,
        )

    def _wait_ms(self, event, ctx, started_at, t0):
        ms = int((event.params or {}).get("duration_ms") or (event.params or {}).get("ms") or 500)
        ms = max(0, min(ms, 60_000))
        time.sleep(ms / 1000.0)
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=f"等待 {ms}ms",
        )

    def _swipe_direction(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        direction = str(params.get("direction") or "up").lower()
        # 通过 wm size 拿屏幕尺寸然后按方向算坐标
        rc, out, _err = self._adb_shell(serial, "wm", "size")
        w, h = 1080, 1920  # 兜底
        if rc == 0 and "Physical size:" in out:
            try:
                size_str = out.rsplit(":", 1)[-1].strip()
                wp, hp = size_str.split("x")
                w, h = int(wp), int(hp)
            except Exception:
                pass
        cx = w // 2
        steps = {
            "up": (cx, int(h * 0.75), cx, int(h * 0.25)),
            "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
            "left": (int(w * 0.85), h // 2, int(w * 0.15), h // 2),
            "right": (int(w * 0.15), h // 2, int(w * 0.85), h // 2),
        }
        if direction not in steps:
            return self._fail(event, started_at, t0, f"unsupported direction={direction}")
        x1, y1, x2, y2 = steps[direction]
        duration = int(params.get("duration_ms") or 300)
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"滑动 {direction}",
                raw_response={"from": (x1, y1), "to": (x2, y2)},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"滑动 {direction} 失败", error=err or out,
        )

    def _swipe_element_to_element(self, event, ctx, serial, started_at, t0):
        # 需要 vlm 子流程：from/to 两次 locate；这里假设上层已塞 coords
        params = event.params or {}
        x1 = params.get("from_x"); y1 = params.get("from_y")
        x2 = params.get("to_x"); y2 = params.get("to_y")
        if None in (x1, y1, x2, y2):
            return self._fail(event, started_at, t0, "swipe_element_to_element 需要 from_x/from_y/to_x/to_y（VLM locate 应已注入）")
        duration = int(params.get("duration_ms") or 400)
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"拖拽 ({x1},{y1})→({x2},{y2})",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="拖拽失败", error=err or out,
        )

    def _install_apk(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        path = params.get("path") or ""
        url = params.get("url") or ""
        tmp_path = ""
        # 对齐 ClawNode：支持 url（内部下载到本地再 install），也兼容本地 path
        if not path and url:
            try:
                from mino_scout.executors.adb_command import _download_apk

                path = _download_apk(url, params.get("file_name") or "")
                tmp_path = path
            except Exception as e:
                return self._fail(event, started_at, t0, f"apk 下载失败: {e}")
        if not path:
            return self._fail(event, started_at, t0, "install_apk 缺 params.path 或 params.url")
        try:
            rc, out, err = self._adb(serial, "install", "-r", "-t", path, timeout_sec=300.0)
            elapsed = int((time.time() - t0) * 1000)
            if rc == 0 and "Success" in out:
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                    elapsed_ms=elapsed, summary=f"安装 {path}",
                )
            return make_event_result(
                event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary="安装失败", error=err or out,
            )
        finally:
            if tmp_path:
                import os as _os

                try:
                    _os.remove(tmp_path)
                except OSError:
                    pass

    def _exec_script(self, event, ctx, serial, started_at, t0):
        """adb 版 EXEC_SCRIPT：委托 adb_script（dsl/shell；js→not_supported），与 ClawNode 协议一致。"""
        from mino_scout.executors.adb_script import run_adb_script

        res = run_adb_script(serial, event.params or {})
        elapsed = int((time.time() - t0) * 1000)
        status_raw = str(res.get("status") or "").lower()
        if status_raw == "success":
            status = EventStatus.PASS
        elif status_raw == "not_supported":
            status = EventStatus.SKIPPED
        else:
            status = EventStatus.FAIL
        return make_event_result(
            event, status=status, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=res.get("message") or "exec_script",
            error=res.get("stderr") or "", raw_response={"stdout": (res.get("stdout") or "")[:2000]},
        )

    def _read_device_data(self, event, ctx, serial, started_at, t0):
        key = (event.params or {}).get("key") or "model"
        # 简单映射几个常用 getprop
        prop_map = {
            "model": "ro.product.model",
            "brand": "ro.product.brand",
            "android_version": "ro.build.version.release",
            "sdk": "ro.build.version.sdk",
            "abi": "ro.product.cpu.abi",
        }
        prop = prop_map.get(key, key)
        rc, out, err = self._adb_shell(serial, "getprop", prop)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            value = out.strip()
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"{key}={value}",
                raw_response={"key": key, "value": value},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"读取 {key} 失败", error=err or out,
        )

    def _get_app_version(self, event, ctx, serial, started_at, t0):
        from mino_scout.probe.app_query import parse_package_version, version_dump_shell

        pkg = str((event.params or {}).get("package") or ctx.device.extra.get("target_package", "") or "").strip()
        if not pkg:
            return self._fail(event, started_at, t0, "get_app_version 缺 params.package")
        rc, out, err = self._adb_sh_script(serial, version_dump_shell(pkg), timeout_sec=20.0)
        elapsed = int((time.time() - t0) * 1000)
        parsed = parse_package_version(out)
        name = str(parsed.get("version_name") or "").strip()
        if rc == 0 and name:
            # 上游这里调 stamp_app_version(ctx, name) 把版本写回 RunContext。
            # RunContext 归 Nexus，Scout 只把版本放进 raw_response，由 Nexus 落账。
            code = parsed.get("version_code")
            extra = f" ({code})" if code is not None else ""
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"{pkg} {name}{extra}",
                raw_response={"package": pkg, **parsed, "rc": rc},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"读不到 {pkg} 的版本",
            error=err or out or f"rc={rc}",
            raw_response={"package": pkg, **parsed, "rc": rc, "stdout": (out or "")[:400]},
        )

    def _get_foreground_app(self, event, ctx, serial, started_at, t0):
        from mino_scout.probe.app_query import FOREGROUND_SHELL, parse_foreground

        rc, out, err = self._adb_sh_script(serial, FOREGROUND_SHELL, timeout_sec=20.0)
        elapsed = int((time.time() - t0) * 1000)
        parsed = parse_foreground(out)
        pkg = str(parsed.get("package") or "").strip()
        expect = str((event.params or {}).get("package") or ctx.device.extra.get("target_package", "") or "").strip()
        if rc == 0 and pkg:
            summary = parsed.get("activity") or pkg
            if expect:
                summary = f"{summary}（目标 {expect}{'命中' if pkg == expect else '未命中'}）"
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=str(summary),
                raw_response={**parsed, "expected_package": expect, "match": (pkg == expect) if expect else None, "rc": rc},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="读不到前台应用",
            error=err or out or f"rc={rc}",
            raw_response={**parsed, "rc": rc, "stdout": (out or "")[:400]},
        )

    def _set_clipboard(self, event, ctx, serial, started_at, t0):
        text = (event.params or {}).get("text") or ""
        if not text:
            return self._fail(event, started_at, t0, "set_clipboard 缺 params.text")
        # Android 没有原生 adb shell 写剪贴板的口子，用 broadcast 或 input text 兜底
        rc, out, err = self._adb_shell(serial, "service", "call", "clipboard", "1")
        elapsed = int((time.time() - t0) * 1000)
        if rc != 0:
            # 退而求其次：写到剪贴板 helper app 是另一回事；这里 stub
            return make_event_result(
                event, status=EventStatus.SKIPPED, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary="set_clipboard adb 通路无原生支持，建议改 remote",
            )
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"剪贴板设置 {len(text)} 字",
        )

    def _tap_element(self, event, ctx, serial, started_at, t0):
        # 坐标来源：语义锚点优先（稳定、可复用），模型给的坐标兜底
        x, y, audit, how = self._point_for(event, serial)
        if x is None or y is None:
            return self._fail(
                event, started_at, t0,
                "tap_element 无可用坐标（锚点未命中且未给 x/y）"
                + (f"：{(audit.get('anchor') or {}).get('reason', '')}" if audit else ""),
            )
        rc, out, err = self._adb_shell(serial, "input", "tap", str(x), str(y))
        elapsed = int((time.time() - t0) * 1000)
        label = self._point_label(how, audit, x, y)
        self._invalidate_hierarchy(serial)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"点击 {label}", raw_response=audit,
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"点击 {label} 失败", error=err or out, raw_response=audit,
        )

    def _multi_tap(self, event, ctx, serial, started_at, t0):
        from mino_scout.executors.multi_tap import parse_multi_tap

        parsed, err = parse_multi_tap(event.params)
        if err:
            return self._fail(event, started_at, t0, err)
        x, y, count, interval = parsed
        for i in range(count):
            rc, out, err_s = self._adb_shell(serial, "input", "tap", str(x), str(y))
            if rc != 0:
                self._invalidate_hierarchy(serial)
                return self._fail(
                    event, started_at, t0,
                    f"连点 ({x},{y}) 第 {i + 1}/{count} 次失败",
                )
            if i + 1 < count:
                time.sleep(interval / 1000.0)
        self._invalidate_hierarchy(serial)
        elapsed = int((time.time() - t0) * 1000)
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"连点 ({x},{y}) ×{count} 间隔{interval}ms",
        )

    def _point_label(self, how: str, audit: dict, x: int, y: int) -> str:
        """人可读的落点说明：锚点命中时说清点的是哪个元素。"""
        if how == "anchor":
            a = audit.get("anchor") or {}
            name = a.get("text") or a.get("content_desc") or a.get("resource_id") or "?"
            return f"「{str(name)[:20]}」({x},{y}) via {a.get('matched_by')}"
        if how == "fallback_xy":
            return f"({x},{y})[锚点未命中，回落坐标]"
        return f"({x},{y})"

    def _invalidate_hierarchy(self, serial: str) -> None:
        """动作后屏幕会变，主动失效层级缓存，避免下一步拿到旧快照。"""
        try:
            from mino_scout import hierarchy as H

            H.invalidate_cache(serial)
        except Exception:  # pragma: no cover
            pass

    def _long_press_element(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        x, y, audit, how = self._point_for(event, serial)
        if x is None or y is None:
            return self._fail(event, started_at, t0, "long_press_element 无可用坐标")
        duration = int(params.get("duration_ms") or 1000)
        # 长按 = swipe 自己到自己
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(x), str(y), str(x), str(y), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        label = self._point_label(how, audit, x, y)
        self._invalidate_hierarchy(serial)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"长按 {label} {duration}ms", raw_response=audit,
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="长按失败", error=err or out, raw_response=audit,
        )

    def _input_text(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        text = params.get("text") or ""
        if not text:
            return self._fail(event, started_at, t0, "input_text 缺 params.text")
        # 若能定出输入框位置（锚点优先），先点它取焦点
        x, y, audit, how = self._point_for(event, serial)
        if x is not None and y is not None:
            self._adb_shell(serial, "input", "tap", str(x), str(y))
        # adb input text 不支持中文 / 空格；空格转 %s
        safe_text = str(text).replace(" ", "%s")
        rc, out, err = self._adb_shell(serial, "input", "text", safe_text)
        elapsed = int((time.time() - t0) * 1000)
        self._invalidate_hierarchy(serial)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"输入 {len(text)} 字", raw_response=audit,
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="输入失败（中文需要 IME 协助，建议改 remote）",
            error=err or out, raw_response=audit,
        )

    # ---------- 内部 ----------

    # ---------- 语义锚点解析（S0b） ----------

    def _resolve_anchor_xy(self, event, serial: str) -> tuple[Optional[int], Optional[int], dict]:
        """params.target 有语义锚点时，用 UI 层级解析成精确坐标。

        返回 (x, y, audit)。解析不到时 x/y 为 None，调用方回落到模型给的坐标。
        audit 会进 raw_response，便于回放时看清"这一下点的到底是哪个元素"。
        """
        from mino_scout import hierarchy as H

        params = event.params or {}
        if not H.has_target(params):
            return None, None, {}
        target = dict(params.get("target") or {})
        dump = H.dump_ui_nodes(serial)
        if not dump.ok:
            return None, None, {"anchor": {"ok": False, "reason": f"层级采集失败: {dump.error[:120]}",
                                           "target": target}}
        match = H.resolve_target(dump.nodes, target)
        if match is None:
            return None, None, {"anchor": {"ok": False, "reason": "锚点未命中任何节点",
                                           "target": target, "nodes": len(dump)}}
        x, y = match.node.center
        audit = {"anchor": {"ok": True, "target": target, "nodes": len(dump),
                            "dump_ms": dump.elapsed_ms, **match.to_brief()}}
        SLog.i(TAG, f"anchor hit by={match.matched_by} label={match.node.label()!r} "
                    f"-> ({x},{y}) candidates={match.candidates}")
        return x, y, audit

    def _point_for(self, event, serial: str) -> tuple[Optional[int], Optional[int], dict, str]:
        """统一取坐标：锚点优先，模型坐标兜底。返回 (x, y, audit, how)。"""
        params = event.params or {}
        ax, ay, audit = self._resolve_anchor_xy(event, serial)
        if ax is not None and ay is not None:
            return ax, ay, audit, "anchor"
        x, y = params.get("x"), params.get("y")
        if x is None or y is None:
            return None, None, audit, "none"
        return int(x), int(y), audit, "fallback_xy" if audit else "xy"

    # ---------- 通用 low_level 兜底 ----------

    def _run_declared_low_level(self, event, ctx, serial, started_at, t0):
        """按 capability yaml 的 low_level 声明执行（无 Python 分支时的通路）。"""
        from mino_scout.executors.low_level import run_low_level

        # 上游只从 ctx.selected_impl 取（router 从能力目录塞进去的）。
        # 拆分后 Nexus 把 low_level 直接放在 EXECUTE 载荷里，优先用它。
        low = dict(event.low_level or {})
        if not low:
            low = dict((ctx.selected_impl or {}).get("low_level") or {})
        cap = event.capability_id
        if not low:
            return self._fail(
                event, started_at, t0,
                f"cap={cap} 既无 Python 分支，其 adb 实现也未声明 low_level",
            )

        def _shell(cmd: str) -> tuple[int, str, str]:
            # 整串交给 adb shell（命令已过白名单，不含重定向/管道到 sh）
            return self._adb_shell(serial, cmd)

        outcome = run_low_level(
            low, dict(event.params or {}), _shell,
            log_prefix=f"[{ctx.run_id}] cap={cap} ",
        )
        elapsed = int((time.time() - t0) * 1000)
        SLog.i(TAG, f"[{ctx.run_id}] low_level cap={cap} kind={low.get('kind')} "
                    f"ok={outcome.ok} cmds={len(outcome.commands)}")
        return make_event_result(
            event,
            status=EventStatus.PASS if outcome.ok else EventStatus.FAIL,
            executor_used=self.id, started_at=started_at, elapsed_ms=elapsed,
            summary=outcome.summary, error=outcome.error,
            raw_response=outcome.as_raw_response(),
        )

    def _fail(self, event, started_at, t0, msg: str) -> EventResult:
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=msg, error=msg,
        )
