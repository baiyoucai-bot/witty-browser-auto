from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

from witty_browser_auto.browser.driver import CdpAutomationDriver
from witty_browser_auto.config import (
    AppConfig,
    BrowserConfig,
    NetworkCaptureConfig,
    SecurityPolicyConfig,
    StorageConfig,
)
from witty_browser_auto.network.capture import CdpNetworkCapture
from witty_browser_auto.toolkit import bootstrap
from witty_browser_auto.toolkit.bootstrap import (
    _scoped_profile_key,
    build_browser_toolkit,
    launch_browser_toolkit,
    toolkit_usage_reference,
)


def _config(tmp_path: Path, *, network_enabled: bool = True) -> AppConfig:
    return AppConfig(
        browser=BrowserConfig(profile_root=tmp_path / "profiles"),
        storage=StorageConfig(
            memory_database=tmp_path / "memory.db",
            artifact_root=tmp_path / "artifacts",
        ),
        network=NetworkCaptureConfig(enabled=network_enabled),
    )


def test_build_browser_toolkit_assembles_driver_and_extractors(tmp_path: Path) -> None:
    toolkit, driver = build_browser_toolkit(
        "https://example.com/list?page=1",
        goal="采集订单",
        config=_config(tmp_path),
        inputs={"account": "account-value"},
    )

    assert isinstance(driver, CdpAutomationDriver)
    assert toolkit.driver is driver
    assert toolkit.task.goal == "采集订单"
    assert toolkit.task.task_id.startswith("toolkit-")
    assert toolkit.task.inputs == {"account": "account-value"}
    # 网络捕获默认开启且限定在入口 origin。
    capture = toolkit._executor.network_data_extractor
    assert isinstance(capture, CdpNetworkCapture)
    assert toolkit._executor.structured_extractor is not None


def test_build_browser_toolkit_without_network_capture(tmp_path: Path) -> None:
    toolkit, _ = build_browser_toolkit(
        "https://example.com/list",
        config=_config(tmp_path, network_enabled=False),
    )

    assert toolkit._executor.network_data_extractor is None


def test_deployment_read_only_policy_cannot_be_relaxed_by_task(tmp_path: Path) -> None:
    config = AppConfig(
        browser=BrowserConfig(profile_root=tmp_path / "profiles"),
        storage=StorageConfig(
            memory_database=tmp_path / "memory.db",
            artifact_root=tmp_path / "artifacts",
        ),
        security=SecurityPolicyConfig(read_only=True),
    )

    toolkit, _ = build_browser_toolkit(
        "https://example.com/list",
        config=config,
        read_only=False,
    )

    assert toolkit.task.read_only is True


def test_profile_key_matches_agent_task_isolation(tmp_path: Path) -> None:
    """同作用域同站点的工具会话必须与智能体任务复用同一持久 profile。"""

    toolkit, driver = build_browser_toolkit(
        "https://example.com/list",
        config=_config(tmp_path),
        project_id="proj-a",
        tenant_id="tenant-b",
        account_id="account-c",
    )

    assert driver.browser_config.profile_key == _scoped_profile_key(
        toolkit.task.scope,
        toolkit.task.start_url,
    )


def test_allowed_origins_flow_into_task_scope(tmp_path: Path) -> None:
    toolkit, _ = build_browser_toolkit(
        "https://example.com/list",
        config=_config(tmp_path),
        allowed_origins=("https://example.com", "https://api.example.com"),
    )

    assert toolkit.task.scope.allowed_origins == (
        "https://example.com",
        "https://api.example.com",
    )


class FakeLaunchDriver:
    """记录生命周期调用的假驱动，避免测试真正启动 Chrome。"""

    instances: ClassVar[list[FakeLaunchDriver]] = []

    def __init__(
        self,
        config: Any,
        artifact_root: Path,
        *,
        network_capture: Any = None,
        network_traffic: Any = None,
    ) -> None:
        self.config = config
        self.artifact_root = artifact_root
        self.network_capture = network_capture
        self.network_traffic = network_traffic
        self.session = None
        self.network_recorder = None
        self.last_known_url = ""
        self.opened_urls: list[str] = []
        self.closed = False
        FakeLaunchDriver.instances.append(self)

    async def open(self, url: str) -> str:
        self.opened_urls.append(url)
        return "surface"

    async def close(self) -> None:
        self.closed = True


def test_launch_browser_toolkit_opens_and_closes_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeLaunchDriver.instances.clear()
    monkeypatch.setattr(bootstrap, "CdpAutomationDriver", FakeLaunchDriver)

    async def scenario() -> None:
        async with launch_browser_toolkit(
            "https://example.com/list",
            config=_config(tmp_path),
        ) as toolkit:
            assert toolkit.driver.opened_urls == ["https://example.com/list"]
            assert toolkit.driver.closed is False
        assert FakeLaunchDriver.instances[-1].closed is True

    asyncio.run(scenario())


def test_launch_browser_toolkit_closes_driver_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeLaunchDriver.instances.clear()
    monkeypatch.setattr(bootstrap, "CdpAutomationDriver", FakeLaunchDriver)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="调用方异常"):
            async with launch_browser_toolkit(
                "https://example.com/list",
                config=_config(tmp_path),
            ):
                raise RuntimeError("调用方异常")
        assert FakeLaunchDriver.instances[-1].closed is True

    asyncio.run(scenario())


def test_toolkit_usage_reference_groups_by_category() -> None:
    reference = toolkit_usage_reference()

    assert reference["entrypoint"] == "witty_browser_auto.toolkit.launch_browser_toolkit"
    categories = reference["categories"]
    assert {"list_tabs", "open_tab", "switch_tab", "close_tab"} == {
        item["name"] for item in categories["tab"]
    }
    all_names = {item["name"] for items in categories.values() for item in items}
    assert all_names.isdisjoint({"finish", "ask_user", "block", "wait_until"})


def test_toolkit_usage_reference_supports_category_filter() -> None:
    reference = toolkit_usage_reference(category="network")

    assert set(reference["categories"]) == {"network"}
