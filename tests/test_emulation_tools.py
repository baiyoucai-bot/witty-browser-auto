"""环境模拟的单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from witty_browser_auto.agent.emulation_tools import execute_emulation_tool
from witty_browser_auto.browser.emulation import (
    DEVICE_PRESETS,
    EmulationState,
    NetworkConditions,
    Viewport,
    apply_state,
    build_state,
    clear_state,
)
from witty_browser_auto.domain.models import DriverCapabilities


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None, *, timeout_seconds=None):
        self.calls.append((method, dict(params or {})))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"innerWidth": 980, "innerHeight": 2121}}}
        return {}

    def params_for(self, method: str) -> dict:
        for name, params in self.calls:
            if name == method:
                return params
        raise AssertionError(f"未调用 {method}：{[name for name, _ in self.calls]}")

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


def _run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 状态叠加
# ----------------------------------------------------------------------


def test_device_preset_fills_viewport_and_client_hints() -> None:
    state = build_state(previous=None, device="iphone_15")
    preset = DEVICE_PRESETS["iphone_15"]
    assert state.viewport == preset.viewport
    assert state.platform == "iOS"
    # 触控必须由预设显式带上，mobile=True 本身不会开启。
    assert state.touch_points == 5


def test_explicit_viewport_overrides_preset_size_but_keeps_identity() -> None:
    state = build_state(previous=None, device="iphone_15")
    state = build_state(previous=state, viewport=Viewport(360, 640))
    assert state.viewport == Viewport(360, 640)
    assert state.device == "iphone_15"
    assert state.user_agent == DEVICE_PRESETS["iphone_15"].user_agent


def test_untouched_dimensions_survive_later_calls() -> None:
    state = build_state(previous=None, device="pixel_8", timezone="Asia/Tokyo")
    state = build_state(previous=state, color_scheme="dark")
    assert state.timezone == "Asia/Tokyo"
    assert state.device == "pixel_8"
    assert state.color_scheme == "dark"


def test_unknown_presets_and_out_of_range_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="未知的设备预设"):
        build_state(previous=None, device="nokia_3310")
    with pytest.raises(ValueError, match="未知的网络预设"):
        build_state(previous=None, network_preset="dialup")
    with pytest.raises(ValueError, match="视口宽度"):
        build_state(previous=None, viewport=Viewport(10, 800))
    with pytest.raises(ValueError, match="CPU 节流倍率"):
        build_state(previous=None, cpu_throttle_rate=99)
    with pytest.raises(ValueError, match="配色方案"):
        build_state(previous=None, color_scheme="sepia")
    with pytest.raises(ValueError, match="地理坐标"):
        build_state(previous=None, geolocation=(200.0, 0.0, 1.0))


# ----------------------------------------------------------------------
# 下发
# ----------------------------------------------------------------------


def test_touch_emulation_is_sent_separately_from_mobile_flag() -> None:
    session = _RecordingSession()
    _run(apply_state(session, build_state(previous=None, device="iphone_15")))
    metrics = session.params_for("Emulation.setDeviceMetricsOverride")
    assert metrics["mobile"] is True
    touch = session.params_for("Emulation.setTouchEmulationEnabled")
    assert touch["enabled"] is True and touch["maxTouchPoints"] == 5


def test_user_agent_override_always_carries_client_hint_metadata() -> None:
    session = _RecordingSession()
    _run(apply_state(session, build_state(previous=None, device="pixel_8")))
    params = session.params_for("Emulation.setUserAgentOverride")
    metadata = params["userAgentMetadata"]
    # 缺少 metadata 时 navigator.userAgentData 仍报桌面，移动端站点会照发桌面版。
    assert metadata["mobile"] is True
    assert metadata["platform"] == "Android"
    assert metadata["model"] == "Pixel 8"


def test_throughput_is_converted_from_kbps_to_bytes_per_second() -> None:
    session = _RecordingSession()
    _run(apply_state(session, build_state(previous=None, network_preset="slow_3g")))
    params = session.params_for("Network.emulateNetworkConditions")
    assert params["latency"] == 400
    assert params["downloadThroughput"] == pytest.approx(400 * 1024 / 8)


def test_unlimited_throughput_stays_negative() -> None:
    session = _RecordingSession()
    _run(
        apply_state(
            session,
            EmulationState(network=NetworkConditions(offline=True), network_preset="offline"),
        )
    )
    params = session.params_for("Network.emulateNetworkConditions")
    assert params["offline"] is True
    assert params["downloadThroughput"] == -1


def test_clear_state_undoes_every_override() -> None:
    session = _RecordingSession()
    _run(clear_state(session))
    assert "Emulation.clearDeviceMetricsOverride" in session.methods()
    assert "Emulation.clearGeolocationOverride" in session.methods()
    assert session.params_for("Emulation.setCPUThrottlingRate")["rate"] == 1
    assert session.params_for("Network.emulateNetworkConditions")["offline"] is False


# ----------------------------------------------------------------------
# 工具层
# ----------------------------------------------------------------------


class _FakeDriver:
    def __init__(self) -> None:
        self.capabilities = DriverCapabilities(emulation=True)
        self.emulation_state: EmulationState | None = None
        self.cleared = False

    async def apply_emulation(self, state):
        self.emulation_state = state
        if state is None:
            self.cleared = True
            return {"innerWidth": 756, "innerHeight": 469}
        # 真实 Chrome 上请求 393 宽会被 980 默认布局宽顶掉。
        return {"innerWidth": 980, "innerHeight": 2121}


def test_tool_reports_that_requested_width_did_not_take_effect() -> None:
    driver = _FakeDriver()
    outcome = _run(execute_emulation_tool({"device": "iphone_15"}, driver=driver))
    assert outcome.data["effective"]["innerWidth"] == 980
    assert outcome.data["requested"]["requested_viewport"]["width"] == 393
    assert "未生效" in outcome.message


def test_tool_reset_clears_state() -> None:
    driver = _FakeDriver()
    _run(execute_emulation_tool({"device": "iphone_15"}, driver=driver))
    outcome = _run(execute_emulation_tool({"reset": True}, driver=driver))
    assert driver.cleared is True
    assert driver.emulation_state is None
    assert outcome.data["reset"] is True


def test_tool_rejects_bad_arguments() -> None:
    driver = _FakeDriver()
    with pytest.raises(ValueError, match="未知参数"):
        _run(execute_emulation_tool({"phone": "iphone"}, driver=driver))
    with pytest.raises(ValueError, match="至少要指定一个"):
        _run(execute_emulation_tool({}, driver=driver))
    with pytest.raises(ValueError, match="reset 不能与其他参数"):
        _run(execute_emulation_tool({"reset": True, "device": "iphone_15"}, driver=driver))
    with pytest.raises(ValueError, match="viewport 包含未知参数"):
        _run(execute_emulation_tool({"viewport": {"w": 1, "h": 2}}, driver=driver))
    with pytest.raises(ValueError, match="整数 width 与 height"):
        _run(execute_emulation_tool({"viewport": {"width": 1.5, "height": 2}}, driver=driver))
    with pytest.raises(ValueError, match="network 包含未知参数"):
        _run(execute_emulation_tool({"network": {"speed": "fast"}}, driver=driver))


def test_tool_requires_driver_capability() -> None:
    class _NoEmulation:
        capabilities = DriverCapabilities()

    with pytest.raises(ValueError, match="不支持环境模拟"):
        _run(execute_emulation_tool({"device": "iphone_15"}, driver=_NoEmulation()))
