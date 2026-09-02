"""设备、网络与环境模拟。

真实 Chrome 探测钉死了几条规则，实现必须照做：
- `setDeviceMetricsOverride` 请求的宽度不等于页面拿到的 `innerWidth`。页面没有
  `viewport` meta 时，`mobile: true` 会退回 980 CSS 像素的默认布局宽，请求 390 也没用。
  所以应用之后必须回读实测值，否则调用方会以为自己在 390 宽下验证过了。
- `mobile: true` 不会带来触控，`maxTouchPoints` 仍是 0，必须单独开触控模拟。
- `setUserAgentOverride` 不带 `userAgentMetadata` 时，`navigator.userAgentData` 仍报桌面，
  用客户端提示判断设备的站点会照旧发桌面版页面。
- 新建标签页不继承任何覆盖，切页后必须整套重施。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

logger = logging.getLogger(__name__)

COLOR_SCHEMES: tuple[str, ...] = ("light", "dark", "no-preference")

_MAX_DIMENSION = 4000
_MIN_DIMENSION = 100
_MAX_SCALE = 5.0
_MAX_CPU_RATE = 20.0


class EmulationSession(Protocol):
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Viewport:
    width: int
    height: int
    device_scale_factor: float = 1.0
    mobile: bool = False


@dataclass(frozen=True, slots=True)
class NetworkConditions:
    offline: bool = False
    latency_ms: float = 0.0
    download_kbps: float = -1.0
    upload_kbps: float = -1.0


@dataclass(frozen=True, slots=True)
class DevicePreset:
    label: str
    viewport: Viewport
    user_agent: str
    platform: str
    platform_version: str
    model: str
    touch_points: int


@dataclass(frozen=True, slots=True)
class EmulationState:
    """一次会话希望页面处于的模拟环境。"""

    device: str | None = None
    viewport: Viewport | None = None
    user_agent: str = ""
    platform: str = ""
    platform_version: str = ""
    model: str = ""
    touch_points: int = 0
    network: NetworkConditions | None = None
    network_preset: str | None = None
    cpu_throttle_rate: float = 1.0
    locale: str = ""
    timezone: str = ""
    color_scheme: str = ""
    geolocation: tuple[float, float, float] | None = None

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "device": self.device,
            "network_preset": self.network_preset,
            "cpu_throttle_rate": self.cpu_throttle_rate,
            "locale": self.locale,
            "timezone": self.timezone,
            "color_scheme": self.color_scheme,
        }
        if self.viewport is not None:
            payload["requested_viewport"] = {
                "width": self.viewport.width,
                "height": self.viewport.height,
                "device_scale_factor": self.viewport.device_scale_factor,
                "mobile": self.viewport.mobile,
            }
        if self.network is not None:
            payload["network"] = {
                "offline": self.network.offline,
                "latency_ms": self.network.latency_ms,
                "download_kbps": self.network.download_kbps,
                "upload_kbps": self.network.upload_kbps,
            }
        if self.geolocation is not None:
            latitude, longitude, accuracy = self.geolocation
            payload["geolocation"] = {
                "latitude": latitude,
                "longitude": longitude,
                "accuracy": accuracy,
            }
        return {key: value for key, value in payload.items() if value not in (None, "", 1.0)}


DEVICE_PRESETS: dict[str, DevicePreset] = {
    "iphone_15": DevicePreset(
        label="iPhone 15",
        viewport=Viewport(393, 852, 3.0, True),
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        platform="iOS",
        platform_version="17.0",
        model="iPhone",
        touch_points=5,
    ),
    "pixel_8": DevicePreset(
        label="Pixel 8",
        viewport=Viewport(412, 915, 2.625, True),
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
        ),
        platform="Android",
        platform_version="14",
        model="Pixel 8",
        touch_points=5,
    ),
    "ipad_air": DevicePreset(
        label="iPad Air",
        viewport=Viewport(820, 1180, 2.0, True),
        user_agent=(
            "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        platform="iOS",
        platform_version="17.0",
        model="iPad",
        touch_points=5,
    ),
    "desktop_1080p": DevicePreset(
        label="桌面 1920x1080",
        viewport=Viewport(1920, 1080, 1.0, False),
        user_agent="",
        platform="",
        platform_version="",
        model="",
        touch_points=0,
    ),
}

# 吞吐单位是 kbps，换算成 CDP 要的字节每秒在应用时完成。
NETWORK_PRESETS: dict[str, NetworkConditions] = {
    "offline": NetworkConditions(offline=True),
    "slow_3g": NetworkConditions(latency_ms=400, download_kbps=400, upload_kbps=400),
    "fast_3g": NetworkConditions(latency_ms=150, download_kbps=1600, upload_kbps=750),
    "regular_4g": NetworkConditions(latency_ms=70, download_kbps=4000, upload_kbps=3000),
    "no_throttle": NetworkConditions(),
}

_EFFECTIVE_PROBE = """({
  innerWidth: window.innerWidth,
  innerHeight: window.innerHeight,
  devicePixelRatio: window.devicePixelRatio,
  maxTouchPoints: navigator.maxTouchPoints,
  userAgent: navigator.userAgent,
  userAgentDataMobile: navigator.userAgentData ? navigator.userAgentData.mobile : null,
  language: navigator.language,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  prefersDark: matchMedia('(prefers-color-scheme: dark)').matches,
})"""


def build_state(
    *,
    previous: EmulationState | None,
    device: str | None = None,
    viewport: Viewport | None = None,
    network_preset: str | None = None,
    network: NetworkConditions | None = None,
    cpu_throttle_rate: float | None = None,
    locale: str | None = None,
    timezone: str | None = None,
    color_scheme: str | None = None,
    geolocation: tuple[float, float, float] | None = None,
) -> EmulationState:
    """把一次调用叠加到既有模拟状态上；未提及的维度保持不变。"""

    state = previous or EmulationState()
    if device is not None:
        preset = DEVICE_PRESETS.get(device)
        if preset is None:
            raise ValueError(f"未知的设备预设：{device}")
        state = replace(
            state,
            device=device,
            viewport=preset.viewport,
            user_agent=preset.user_agent,
            platform=preset.platform,
            platform_version=preset.platform_version,
            model=preset.model,
            touch_points=preset.touch_points,
        )
    if viewport is not None:
        _validate_viewport(viewport)
        # 显式视口覆盖预设的尺寸，但保留预设带来的 UA 与触控。
        state = replace(state, viewport=viewport)
    if network_preset is not None:
        conditions = NETWORK_PRESETS.get(network_preset)
        if conditions is None:
            raise ValueError(f"未知的网络预设：{network_preset}")
        state = replace(state, network=conditions, network_preset=network_preset)
    if network is not None:
        state = replace(state, network=network, network_preset="custom")
    if cpu_throttle_rate is not None:
        if not 1.0 <= cpu_throttle_rate <= _MAX_CPU_RATE:
            raise ValueError(f"CPU 节流倍率必须在 1 到 {_MAX_CPU_RATE:.0f} 之间")
        state = replace(state, cpu_throttle_rate=float(cpu_throttle_rate))
    if locale is not None:
        state = replace(state, locale=locale)
    if timezone is not None:
        state = replace(state, timezone=timezone)
    if color_scheme is not None:
        if color_scheme not in COLOR_SCHEMES:
            raise ValueError(f"配色方案只能是 {'、'.join(COLOR_SCHEMES)}")
        state = replace(state, color_scheme=color_scheme)
    if geolocation is not None:
        latitude, longitude, accuracy = geolocation
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("地理坐标超出合法范围")
        state = replace(state, geolocation=(latitude, longitude, max(0.0, accuracy)))
    return state


def _validate_viewport(viewport: Viewport) -> None:
    if not _MIN_DIMENSION <= viewport.width <= _MAX_DIMENSION:
        raise ValueError(f"视口宽度必须在 {_MIN_DIMENSION} 到 {_MAX_DIMENSION} 之间")
    if not _MIN_DIMENSION <= viewport.height <= _MAX_DIMENSION:
        raise ValueError(f"视口高度必须在 {_MIN_DIMENSION} 到 {_MAX_DIMENSION} 之间")
    if not 0 < viewport.device_scale_factor <= _MAX_SCALE:
        raise ValueError(f"设备像素比必须大于 0 且不超过 {_MAX_SCALE}")


async def apply_state(session: EmulationSession, state: EmulationState) -> None:
    """把模拟状态整套下发到指定会话。"""

    if state.viewport is not None:
        await session.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": state.viewport.width,
                "height": state.viewport.height,
                "deviceScaleFactor": state.viewport.device_scale_factor,
                "mobile": state.viewport.mobile,
            },
        )
    # mobile=True 不会带来触控，必须显式开。
    await session.call(
        "Emulation.setTouchEmulationEnabled",
        {"enabled": state.touch_points > 0, "maxTouchPoints": max(1, state.touch_points)},
    )
    if state.user_agent or state.locale:
        params: dict[str, Any] = {"userAgent": state.user_agent}
        if state.locale:
            params["acceptLanguage"] = state.locale
        if state.platform:
            params["platform"] = state.platform
        if state.user_agent:
            # 不带 metadata 时客户端提示仍报桌面，移动端站点会照发桌面版。
            params["userAgentMetadata"] = {
                "brands": [{"brand": "Chromium", "version": "140"}],
                "fullVersion": "140.0.0.0",
                "platform": state.platform or "Unknown",
                "platformVersion": state.platform_version,
                "architecture": "",
                "model": state.model,
                "mobile": bool(state.viewport and state.viewport.mobile),
            }
        await session.call("Emulation.setUserAgentOverride", params)
    if state.network is not None:
        await session.call(
            "Network.emulateNetworkConditions",
            {
                "offline": state.network.offline,
                "latency": state.network.latency_ms,
                "downloadThroughput": _kbps_to_bytes(state.network.download_kbps),
                "uploadThroughput": _kbps_to_bytes(state.network.upload_kbps),
            },
        )
    await session.call("Emulation.setCPUThrottlingRate", {"rate": state.cpu_throttle_rate})
    if state.timezone:
        await session.call("Emulation.setTimezoneOverride", {"timezoneId": state.timezone})
    if state.color_scheme:
        await session.call(
            "Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-color-scheme", "value": state.color_scheme}]},
        )
    if state.geolocation is not None:
        latitude, longitude, accuracy = state.geolocation
        await session.call(
            "Emulation.setGeolocationOverride",
            {"latitude": latitude, "longitude": longitude, "accuracy": accuracy},
        )


async def clear_state(session: EmulationSession) -> None:
    """撤销全部模拟覆盖；实测可精确回到基线。"""

    await session.call("Emulation.clearDeviceMetricsOverride")
    await session.call("Emulation.setTouchEmulationEnabled", {"enabled": False})
    await session.call("Emulation.setUserAgentOverride", {"userAgent": ""})
    await session.call("Emulation.setEmulatedMedia", {"features": []})
    await session.call("Emulation.setCPUThrottlingRate", {"rate": 1})
    await session.call(
        "Network.emulateNetworkConditions",
        {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
    )
    await session.call("Emulation.clearGeolocationOverride")
    await session.call("Emulation.setTimezoneOverride", {"timezoneId": ""})


async def read_effective(session: EmulationSession) -> dict[str, Any]:
    """回读页面实际拿到的环境。

    请求值和生效值经常不同：页面缺少 viewport meta 时，请求 390 宽会得到 980。
    """

    result = await session.call(
        "Runtime.evaluate", {"expression": _EFFECTIVE_PROBE, "returnByValue": True}
    )
    value = result.get("result", {}).get("value")
    return value if isinstance(value, dict) else {}


def _kbps_to_bytes(kbps: float) -> float:
    return -1 if kbps < 0 else kbps * 1024 / 8
