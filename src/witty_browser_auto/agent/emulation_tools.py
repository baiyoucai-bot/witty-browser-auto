"""环境模拟的执行层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from witty_browser_auto.browser.emulation import (
    COLOR_SCHEMES,
    DEVICE_PRESETS,
    NETWORK_PRESETS,
    NetworkConditions,
    Viewport,
    build_state,
)
from witty_browser_auto.domain.protocols import AutomationDriver

EMULATION_TOOL_NAMES = frozenset({"emulate_environment"})

_KNOWN_ARGUMENTS = frozenset(
    {
        "device",
        "viewport",
        "network_preset",
        "network",
        "cpu_throttle_rate",
        "locale",
        "timezone",
        "color_scheme",
        "geolocation",
        "reset",
    }
)


@dataclass(frozen=True, slots=True)
class EmulationToolOutcome:
    message: str
    data: dict[str, Any]


def emulation_available(driver: AutomationDriver) -> bool:
    capabilities = getattr(driver, "capabilities", None)
    return bool(getattr(capabilities, "emulation", False)) and hasattr(driver, "apply_emulation")


async def execute_emulation_tool(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
) -> EmulationToolOutcome:
    if not emulation_available(driver):
        raise ValueError("当前驱动不支持环境模拟")
    unknown = set(arguments) - _KNOWN_ARGUMENTS
    if unknown:
        raise ValueError(f"emulate_environment 包含未知参数：{', '.join(sorted(unknown))}")

    if arguments.get("reset"):
        if len(arguments) > 1:
            raise ValueError("reset 不能与其他参数同时使用")
        effective = await driver.apply_emulation(None)
        return EmulationToolOutcome(
            message="已清除全部环境模拟覆盖",
            data={"reset": True, "requested": {}, "effective": effective},
        )
    if not arguments:
        raise ValueError("至少要指定一个模拟维度，或使用 reset 清除")

    state = build_state(
        previous=getattr(driver, "emulation_state", None),
        device=_optional_str(arguments, "device"),
        viewport=_viewport(arguments.get("viewport")),
        network_preset=_optional_str(arguments, "network_preset"),
        network=_network(arguments.get("network")),
        cpu_throttle_rate=_optional_number(arguments, "cpu_throttle_rate"),
        locale=_optional_str(arguments, "locale"),
        timezone=_optional_str(arguments, "timezone"),
        color_scheme=_optional_str(arguments, "color_scheme"),
        geolocation=_geolocation(arguments.get("geolocation")),
    )
    effective = await driver.apply_emulation(state)
    message = _describe(state.public_dict(), effective)
    return EmulationToolOutcome(
        message=message,
        data={"reset": False, "requested": state.public_dict(), "effective": effective},
    )


def _describe(requested: Mapping[str, Any], effective: Mapping[str, Any]) -> str:
    parts: list[str] = []
    if requested.get("device"):
        parts.append(f"设备 {requested['device']}")
    width = effective.get("innerWidth")
    if width is not None:
        parts.append(f"实测视口 {width}x{effective.get('innerHeight')}")
        wanted = requested.get("requested_viewport", {})
        if wanted and wanted.get("width") != width:
            # 页面没有 viewport meta 时，请求宽会被 980 默认布局宽顶掉。
            parts.append(f"注意请求宽度 {wanted['width']} 未生效，页面按 {width} 布局")
    if requested.get("network_preset"):
        parts.append(f"网络 {requested['network_preset']}")
    if requested.get("cpu_throttle_rate"):
        parts.append(f"CPU 降速 {requested['cpu_throttle_rate']}x")
    return "环境模拟已应用：" + "，".join(parts) if parts else "环境模拟已应用"


def _optional_str(arguments: Mapping[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value.strip()


def _optional_number(arguments: Mapping[str, Any], key: str) -> float | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} 必须是数字")
    return float(value)


def _viewport(raw: Any) -> Viewport | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("viewport 必须是对象")
    unknown = set(raw) - {"width", "height", "device_scale_factor", "mobile"}
    if unknown:
        raise ValueError(f"viewport 包含未知参数：{', '.join(sorted(unknown))}")
    width, height = raw.get("width"), raw.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("viewport 必须同时给出整数 width 与 height")
    scale = raw.get("device_scale_factor", 1.0)
    if isinstance(scale, bool) or not isinstance(scale, int | float):
        raise ValueError("device_scale_factor 必须是数字")
    mobile = raw.get("mobile", False)
    if not isinstance(mobile, bool):
        raise ValueError("viewport.mobile 必须是布尔值")
    return Viewport(width, height, float(scale), mobile)


def _network(raw: Any) -> NetworkConditions | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("network 必须是对象")
    unknown = set(raw) - {"offline", "latency_ms", "download_kbps", "upload_kbps"}
    if unknown:
        raise ValueError(f"network 包含未知参数：{', '.join(sorted(unknown))}")
    offline = raw.get("offline", False)
    if not isinstance(offline, bool):
        raise ValueError("network.offline 必须是布尔值")
    return NetworkConditions(
        offline=offline,
        latency_ms=_number(raw, "latency_ms", 0.0, minimum=0),
        download_kbps=_number(raw, "download_kbps", -1.0),
        upload_kbps=_number(raw, "upload_kbps", -1.0),
    )


def _number(raw: Mapping[str, Any], key: str, default: float, *, minimum: float | None = None):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"network.{key} 必须是数字")
    if minimum is not None and value < minimum:
        raise ValueError(f"network.{key} 不能小于 {minimum}")
    return float(value)


def _geolocation(raw: Any) -> tuple[float, float, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("geolocation 必须是对象")
    unknown = set(raw) - {"latitude", "longitude", "accuracy"}
    if unknown:
        raise ValueError(f"geolocation 包含未知参数：{', '.join(sorted(unknown))}")
    latitude, longitude = raw.get("latitude"), raw.get("longitude")
    for name, value in (("latitude", latitude), ("longitude", longitude)):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"geolocation.{name} 必须是数字")
    accuracy = raw.get("accuracy", 10.0)
    if isinstance(accuracy, bool) or not isinstance(accuracy, int | float):
        raise ValueError("geolocation.accuracy 必须是数字")
    return float(latitude), float(longitude), float(accuracy)


def device_names() -> tuple[str, ...]:
    return tuple(DEVICE_PRESETS)


def network_names() -> tuple[str, ...]:
    return tuple(NETWORK_PRESETS)


def color_scheme_names() -> tuple[str, ...]:
    return COLOR_SCHEMES
