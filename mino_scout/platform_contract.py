"""V5 target-platform contract. Pure validation; never accepts runtime aliases.

Legacy conversion belongs to the offline migration planner, not this module.
Installing these helpers does not advertise or activate the new wire contract.
"""
from __future__ import annotations

TARGET_PLATFORMS = ("mac", "win", "android", "web")
NATIVE_PLATFORMS = frozenset({"mac", "win"})
HOST_SYSTEMS = frozenset({"mac", "win", "linux"})
PLATFORM_CONTRACT_VERSION = 1


class PlatformContractError(ValueError):
    def __init__(self, code: str, field: str = "platform"):
        self.code, self.field = code, field
        super().__init__(f"{code}:{field}")


def target_platform(value, *, field="platform") -> str:
    if type(value) is not str or value not in TARGET_PLATFORMS:
        raise PlatformContractError("target_platform_invalid", field)
    return value


def platform_set(values, *, field="platforms") -> list[str]:
    if type(values) is not list:
        raise PlatformContractError("platform_set_required", field)
    for value in values:
        target_platform(value, field=field)
    if len(values) != len(set(values)):
        raise PlatformContractError("platform_set_duplicate", field)
    return [value for value in TARGET_PLATFORMS if value in values]


def coverage(document: dict) -> dict:
    """Missing, explicitly empty and all are three distinct states."""
    scope = document.get("platform_scope")
    if scope not in ("all", "explicit"):
        raise PlatformContractError("platform_scope_required", "platform_scope")
    values = platform_set(document.get("platforms"))
    if scope == "all" and values:
        raise PlatformContractError("platform_scope_conflict", "platforms")
    if "platform" in document:
        single = target_platform(document["platform"])
        if scope != "explicit" or values != [single]:
            raise PlatformContractError("platform_scope_conflict", "platform")
    return {"platform_scope": scope, "platforms": values}


def host_os_from_system(value) -> str:
    """Only for the OS API boundary, never for a device/API platform input."""
    result = {"Darwin": "mac", "darwin": "mac", "Windows": "win",
              "win32": "win", "Linux": "linux", "linux": "linux"}.get(value)
    if result is None:
        raise PlatformContractError("host_os_unknown", "host.os")
    return result


def host_os(value) -> str:
    if type(value) is not str or value not in HOST_SYSTEMS:
        raise PlatformContractError("host_os_invalid", "host.os")
    return value


def native_sn(platform, node_id) -> str:
    target_platform(platform)
    if platform not in NATIVE_PLATFORMS:
        raise PlatformContractError("native_platform_required")
    if not isinstance(node_id, str) or not node_id or ":" in node_id or any(c.isspace() for c in node_id):
        raise PlatformContractError("node_identity_invalid", "node_id")
    return f"{platform}:{node_id}:console"


def device_identity(device: dict, *, node_id: str, host: dict) -> str:
    platform = target_platform(device.get("platform"))
    system = host_os(host.get("os"))
    sn = device.get("sn")
    if not isinstance(sn, str) or not sn:
        raise PlatformContractError("device_identity_missing", "sn")
    if any(key in device for key in ("os", "native_os", "device_platform", "target_platform")):
        raise PlatformContractError("duplicate_platform_field")
    if platform in NATIVE_PLATFORMS:
        if platform != system or sn != native_sn(platform, node_id) or device.get("session_key") != "console":
            raise PlatformContractError("native_identity_conflict", "sn")
    elif sn.startswith(("desktop:", "mac:", "win:")):
        raise PlatformContractError("native_identity_conflict", "sn")
    elif platform == "web" and sn != f"web{node_id}":
        raise PlatformContractError("web_identity_conflict", "sn")
    return platform


def execute_platform(request: dict) -> str:
    platform = target_platform(request.get("platform"))
    hint = request.get("device_hint", {})
    if not isinstance(hint, dict):
        raise PlatformContractError("device_hint_invalid", "device_hint")
    if "platform" in hint and target_platform(hint["platform"], field="device_hint.platform") != platform:
        raise PlatformContractError("platform_hint_conflict", "device_hint.platform")
    for obj in (request, hint):
        if any(key in obj for key in ("os", "native_os", "device_platform", "target_platform")):
            raise PlatformContractError("duplicate_platform_field")
    return platform


def environment_channel(channel: dict) -> dict:
    kind = channel.get("kind")
    if kind == "server":
        if "platform" in channel:
            raise PlatformContractError("server_platform_forbidden")
        return {"kind": kind}
    platform = target_platform(channel.get("platform"))
    if kind != ("web" if platform == "web" else "app"):
        raise PlatformContractError("channel_kind_conflict", "kind")
    return {"kind": kind, "platform": platform}


def batch_platforms(platforms_by_sn) -> list[str]:
    if not isinstance(platforms_by_sn, dict) or not platforms_by_sn:
        raise PlatformContractError("batch_targets_required", "platforms_by_sn")
    for sn, value in platforms_by_sn.items():
        if not isinstance(sn, str) or not sn:
            raise PlatformContractError("device_identity_missing", "platforms_by_sn")
        target_platform(value, field="platforms_by_sn")
    return [p for p in TARGET_PLATFORMS if p in platforms_by_sn.values()]


def registration_shadow(host, devices, runtime_capabilities) -> dict:
    """Bounded, value-free diagnostics. Never changes registration/routing."""
    from collections import Counter
    counts = Counter()
    try:
        host_os((host or {}).get("os"))
    except PlatformContractError as exc:
        counts[exc.code] += 1
    for device in devices:
        try:
            target_platform(device.get("platform"))
        except PlatformContractError as exc:
            counts[exc.code] += 1
        if any(device.get(k) for k in ("os", "native_os", "device_platform", "target_platform")):
            counts["duplicate_platform_field"] += 1
    if runtime_capabilities is None:
        counts["runtime_capabilities_unreported"] += 1
    else:
        for capability in runtime_capabilities:
            try:
                if not isinstance(capability, dict):
                    raise PlatformContractError("runtime_capability_invalid")
                if "platform" in capability:
                    target_platform(capability["platform"])
                elif "platforms" in capability:
                    platform_set(capability["platforms"])
                else:
                    raise PlatformContractError("runtime_capability_platform_missing")
            except PlatformContractError as exc:
                counts[exc.code] += 1
    return {"mode": "shadow", "target_version": PLATFORM_CONTRACT_VERSION,
            "activated": False, "observed_device_count": len(devices),
            "issues": dict(sorted(counts.items())), "migration_required": bool(counts)}
