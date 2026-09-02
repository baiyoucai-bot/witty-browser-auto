from __future__ import annotations

import asyncio
import stat
from pathlib import Path

from witty_browser_auto.browser.launcher import ChromiumLauncher
from witty_browser_auto.config import BrowserConfig


def test_persistent_profile_is_stable_and_not_marked_for_cleanup(tmp_path: Path) -> None:
    launcher = ChromiumLauncher(
        BrowserConfig(profile_root=tmp_path, profile_key="account-scope", reuse_profile=True)
    )

    first_path, first_cleanup = launcher.prepare_profile()
    second_path, second_cleanup = launcher.prepare_profile()

    assert first_path == tmp_path.resolve() / "account-scope"
    assert second_path == first_path
    assert first_cleanup is False
    assert second_cleanup is False
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o700


def test_ephemeral_profile_is_marked_for_cleanup(tmp_path: Path) -> None:
    launcher = ChromiumLauncher(BrowserConfig(profile_root=tmp_path, reuse_profile=False))

    profile_path, cleanup = launcher.prepare_profile()

    assert profile_path.parent == tmp_path.resolve()
    assert profile_path.name.startswith("managed-")
    assert cleanup is True


def test_browser_arguments_use_nonzero_debugging_port(tmp_path: Path) -> None:
    launcher = ChromiumLauncher(BrowserConfig())

    args = launcher.build_arguments(Path("/Applications/Chrome"), tmp_path, 19321)

    assert "--remote-debugging-port=19321" in args
    assert "--remote-debugging-port=0" not in args
    assert "--headless=new" not in args
    assert "--disable-background-networking" not in args
    assert "--disable-component-update" not in args


def test_headless_argument_is_only_added_when_explicit() -> None:
    launcher = ChromiumLauncher(BrowserConfig(headless=True))

    args = launcher.build_arguments(Path("/Applications/Chrome"), Path("/tmp/profile"), 19321)

    assert "--headless=new" in args


def test_ephemeral_profile_disables_background_browser_services() -> None:
    launcher = ChromiumLauncher(BrowserConfig(reuse_profile=False))

    args = launcher.build_arguments(Path("/Applications/Chrome"), Path("/tmp/profile"), 19321)

    assert "--disable-background-networking" in args
    assert "--disable-component-update" in args


def test_persistent_profile_recovers_live_debugging_endpoint(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    async def scenario() -> None:
        launcher = ChromiumLauncher(
            BrowserConfig(profile_root=tmp_path, profile_key="account-scope", reuse_profile=True)
        )
        profile_dir, _ = launcher.prepare_profile()
        marker = launcher.write_endpoint_marker(
            profile_dir,
            19321,
            4321,
            target_id="order-page",
        )

        async def ready(port: int) -> bool:
            return port == 19321

        monkeypatch.setattr(launcher, "_is_debugging_port_ready", ready)  # type: ignore[attr-defined]
        existing = await launcher.find_existing_endpoint()

        assert existing is not None
        assert existing.endpoint == "http://127.0.0.1:19321"
        assert existing.target_id == "order-page"
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    asyncio.run(scenario())


def test_stale_persistent_profile_endpoint_is_removed(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    async def scenario() -> None:
        launcher = ChromiumLauncher(
            BrowserConfig(profile_root=tmp_path, profile_key="account-scope", reuse_profile=True)
        )
        profile_dir, _ = launcher.prepare_profile()
        marker = launcher.write_endpoint_marker(profile_dir, 19321, 4321)

        async def not_ready(port: int) -> bool:
            assert port == 19321
            return False

        monkeypatch.setattr(launcher, "_is_debugging_port_ready", not_ready)  # type: ignore[attr-defined]

        assert await launcher.find_existing_endpoint() is None
        assert not marker.exists()

    asyncio.run(scenario())
