from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import witty_browser_auto.browser.live_browser as live_browser_module
from witty_browser_auto.browser.live_browser import (
    _read_active_endpoint,
    discover_live_browser_endpoint,
)


def test_reads_valid_devtools_active_port(tmp_path: Path) -> None:
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("53124\n/devtools/browser/browser-id\n", encoding="ascii")

    assert _read_active_endpoint(active_port) == "ws://127.0.0.1:53124/devtools/browser/browser-id"


@pytest.mark.parametrize(
    "contents",
    [
        "not-a-port\n/devtools/browser/browser-id\n",
        "70000\n/devtools/browser/browser-id\n",
        "53124\n/devtools/page/page-id\n",
        "53124\n/devtools/browser/id?query=bad\n",
    ],
)
def test_rejects_invalid_devtools_active_port(tmp_path: Path, contents: str) -> None:
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text(contents, encoding="ascii")

    assert _read_active_endpoint(active_port) is None


def test_rejects_symlinked_devtools_active_port(tmp_path: Path) -> None:
    source = tmp_path / "actual"
    source.write_text("53124\n/devtools/browser/browser-id\n", encoding="ascii")
    active_port = tmp_path / "DevToolsActivePort"
    active_port.symlink_to(source)

    assert _read_active_endpoint(active_port) is None


def test_discovers_first_reachable_live_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_root = tmp_path / "stale"
    live_root = tmp_path / "live"
    stale_root.mkdir()
    live_root.mkdir()
    (stale_root / "DevToolsActivePort").write_text(
        "53124\n/devtools/browser/stale\n",
        encoding="ascii",
    )
    (live_root / "DevToolsActivePort").write_text(
        "53125\n/devtools/browser/live\n",
        encoding="ascii",
    )

    async def endpoint_is_reachable(endpoint: str) -> bool:
        return endpoint.endswith("/live")

    monkeypatch.setattr(
        live_browser_module,
        "_endpoint_is_reachable",
        endpoint_is_reachable,
    )

    endpoint = asyncio.run(discover_live_browser_endpoint((stale_root, live_root)))

    assert endpoint == "ws://127.0.0.1:53125/devtools/browser/live"


def test_opens_authorization_page_only_in_running_macos_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_commands: list[tuple[str, ...]] = []

    async def process_is_running(process_name: str) -> bool:
        return process_name == "Google Chrome"

    async def run_quiet_command(*args: str) -> bool:
        opened_commands.append(args)
        return True

    monkeypatch.setattr(live_browser_module.sys, "platform", "darwin")
    monkeypatch.setattr(live_browser_module, "_process_is_running", process_is_running)
    monkeypatch.setattr(live_browser_module, "_run_quiet_command", run_quiet_command)

    opened = asyncio.run(live_browser_module.open_live_browser_authorization_page())

    assert opened is True
    assert opened_commands == [
        (
            "/usr/bin/open",
            "-b",
            "com.google.Chrome",
            "chrome://inspect/#remote-debugging",
        )
    ]


def test_waits_for_live_browser_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    discoveries = iter((None, "ws://127.0.0.1:53125/devtools/browser/live"))

    async def discover(profile_roots: tuple[Path, ...] | None = None) -> str | None:
        return next(discoveries)

    monkeypatch.setattr(live_browser_module, "discover_live_browser_endpoint", discover)

    endpoint = asyncio.run(
        live_browser_module.wait_for_live_browser_endpoint(
            timeout_seconds=0.1,
            poll_interval_seconds=0.01,
        )
    )

    assert endpoint == "ws://127.0.0.1:53125/devtools/browser/live"
