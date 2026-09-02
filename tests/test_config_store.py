from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from witty_browser_auto.config import (
    AppConfig,
    BrowserConfig,
    BrowserSessionMode,
    NetworkCaptureConfig,
    SecurityPolicyConfig,
    StorageConfig,
)
from witty_browser_auto.config_store import LocalConfigStore
from witty_browser_auto.domain.errors import ConfigurationError


def _configured_app(tmp_path: Path) -> AppConfig:
    return AppConfig(
        browser=BrowserConfig(
            headless=True,
            reuse_profile=False,
            session_mode=BrowserSessionMode.TAKEOVER,
            profile_root=tmp_path / "profiles",
        ),
        storage=StorageConfig(
            memory_database=tmp_path / "memory.db",
            artifact_root=tmp_path / "artifacts",
        ),
        network=NetworkCaptureConfig(enabled=True, max_body_bytes=2048, max_responses=12),
        security=SecurityPolicyConfig(
            trusted_challenge_origins=("https://erp.internal",),
            trusted_challenge_max_attempts=2,
        ),
    )


def test_local_config_round_trip_permissions_and_public_state(tmp_path: Path) -> None:
    store = LocalConfigStore(tmp_path / "private" / "config.json")
    source = _configured_app(tmp_path)
    store.save(source)

    loaded = store.load_saved()
    public_state = store.public_state()

    assert loaded.browser.headless is True
    assert loaded.browser.reuse_profile is False
    assert loaded.browser.session_mode is BrowserSessionMode.TAKEOVER
    assert loaded.browser.profile_root == tmp_path / "profiles"
    assert loaded.storage.memory_database == tmp_path / "memory.db"
    assert loaded.storage.artifact_root == tmp_path / "artifacts"
    assert loaded.network.max_body_bytes == 2048
    assert loaded.security.trusted_challenge_origins == ("https://erp.internal",)
    assert public_state["saved"]["browser"]["profile_root"] == str(tmp_path / "profiles")
    assert public_state["saved"]["storage"]["memory_database"] == str(tmp_path / "memory.db")
    assert "api_key" not in public_state["saved"]
    assert "api_key" not in public_state["effective"]
    assert "model" not in public_state["saved"]
    assert "model" not in public_state["effective"]
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_environment_overrides_saved_config_and_reports_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalConfigStore(tmp_path / "config.json")
    store.save(_configured_app(tmp_path))
    monkeypatch.setenv("WITTY_BROWSER_AUTO_HEADLESS", "false")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_ARTIFACT_ROOT", str(tmp_path / "env-artifacts"))

    effective = store.load_effective()
    state = store.public_state()

    assert store.load_saved().browser.headless is True
    assert effective.browser.headless is False
    assert effective.storage.artifact_root == tmp_path / "env-artifacts"
    assert state["environment_overrides"] == {
        "browser.headless": "WITTY_BROWSER_AUTO_HEADLESS",
        "storage.artifact_root": "WITTY_BROWSER_AUTO_ARTIFACT_ROOT",
    }


def test_failed_atomic_replace_preserves_previous_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalConfigStore(tmp_path / "config.json")
    store.save(_configured_app(tmp_path))
    original = store.path.read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ConfigurationError, match="保存本地配置失败"):
        store.save(
            AppConfig(
                browser=BrowserConfig(headless=False, profile_root=tmp_path / "other-profiles"),
            )
        )

    assert store.path.read_bytes() == original


def test_local_config_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "actual.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "config.json"
    link.symlink_to(target)

    with pytest.raises(ConfigurationError, match="符号链接"):
        LocalConfigStore(link).load_saved()


def test_mapping_rejects_unknown_fields() -> None:
    with pytest.raises(ConfigurationError, match="未知字段"):
        AppConfig.from_mapping({"browser": {"headless": True, "unknown_setting": 0.2}})
