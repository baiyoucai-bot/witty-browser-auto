from __future__ import annotations

from pathlib import Path

import pytest

from witty_browser_auto.config import (
    AppConfig,
    BrowserConfig,
    BrowserSessionMode,
    NetworkCaptureConfig,
    RuntimeConfig,
    SecurityPolicyConfig,
    StorageConfig,
)
from witty_browser_auto.domain.errors import ConfigurationError
from witty_browser_auto.domain.models import ExecutionScope
from witty_browser_auto.toolkit.bootstrap import _scoped_profile_key


def test_config_reads_explicit_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WITTY_BROWSER_AUTO_HEADLESS", "false")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_REUSE_PROFILE", "false")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_BROWSER_SESSION_MODE", "takeover")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_LOG_LEVEL", "debug")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_NETWORK_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_NETWORK_MAX_BODY_BYTES", "2097152")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_NETWORK_MAX_RESPONSES", "25")
    monkeypatch.setenv(
        "WITTY_BROWSER_AUTO_TRUSTED_CHALLENGE_ORIGINS",
        "https://INTRANET.example:443/, http://erp.internal:8080",
    )
    monkeypatch.setenv("WITTY_BROWSER_AUTO_TRUSTED_CHALLENGE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("WITTY_BROWSER_AUTO_READ_ONLY", "true")

    config = AppConfig.from_env()

    assert config.browser.headless is False
    assert config.browser.reuse_profile is False
    assert config.browser.session_mode is BrowserSessionMode.TAKEOVER
    assert config.runtime.log_level == "DEBUG"
    assert config.network.enabled is True
    assert config.network.max_body_bytes == 2_097_152
    assert config.network.max_responses == 25
    assert config.security.trusted_challenge_origins == (
        "https://intranet.example",
        "http://erp.internal:8080",
    )
    assert config.security.trusted_challenge_max_attempts == 2
    assert config.security.read_only is True


def test_config_rejects_invalid_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WITTY_BROWSER_AUTO_HEADLESS", "sometimes")

    with pytest.raises(ConfigurationError, match="不是有效布尔值"):
        AppConfig.from_env()


@pytest.mark.parametrize(
    ("field", "value"),
    (("max_body_bytes", 512), ("max_body_bytes", 20 * 1024 * 1024), ("max_responses", 0)),
)
def test_network_capture_rejects_unbounded_limits(field: str, value: int) -> None:
    with pytest.raises(ConfigurationError, match="网络响应"):
        NetworkCaptureConfig(**{field: value})


def test_runtime_rejects_invalid_log_level() -> None:
    with pytest.raises(ConfigurationError, match="日志级别"):
        RuntimeConfig(log_level="verbose")


def test_browser_defaults_to_visible_persistent_profile() -> None:
    config = BrowserConfig()

    assert config.headless is False
    assert config.reuse_profile is True
    assert config.session_mode is BrowserSessionMode.MANAGED


def test_legacy_model_era_config_still_loads() -> None:
    """收敛为纯工具库前写下的配置文件必须还能加载。

    `load_app_config` 是 `launch_browser_toolkit` 的第一步：把这些历史字段当未知字段
    拒绝，等于整个库在所有既有安装上都起不来。
    """

    legacy = {
        "model": {"base_url": "http://192.0.2.10:8086/v1", "model": "qwq", "api_key": ""},
        "browser": {"headless": True},
        "runtime": {
            "log_level": "DEBUG",
            "max_steps": 20,
            "task_timeout_seconds": 900,
            "code_repair_enabled": True,
            "code_repair_project_root": "/tmp/x",
            "code_repair_validation_timeout_seconds": 180,
            "code_repair_max_restarts": 2,
        },
        "security": {
            "allow_public_model_diagnostics": True,
            "trusted_challenge_max_attempts": 2,
        },
    }

    config = AppConfig.from_mapping(legacy)

    assert config.runtime.log_level == "DEBUG"
    assert config.browser.headless is True
    assert config.security.trusted_challenge_max_attempts == 2


def test_genuinely_unknown_fields_are_still_rejected() -> None:
    """忽略历史字段不能顺带放过拼错的字段。"""

    with pytest.raises(ConfigurationError, match="未知字段"):
        AppConfig.from_mapping({"browsr": {}})
    with pytest.raises(ConfigurationError, match="未知字段"):
        AppConfig.from_mapping({"runtime": {"log_levl": "INFO"}})
    with pytest.raises(ConfigurationError, match="未知字段"):
        AppConfig.from_mapping({"security": {"allow_everything": True}})


def test_browser_rejects_unknown_session_mode() -> None:
    with pytest.raises(ConfigurationError, match="浏览器会话模式"):
        BrowserConfig(session_mode="borrow")  # type: ignore[arg-type]


def test_browser_rejects_unsafe_profile_key() -> None:
    with pytest.raises(ConfigurationError, match="浏览器 profile 标识"):
        BrowserConfig(profile_key="../daily-profile")


@pytest.mark.parametrize(
    "origin",
    (
        "example.com",
        "https://user:secret@example.com",
        "https://example.com/login",
        "https://*.example.com",
        "https://example.com:not-a-port",
    ),
)
def test_security_policy_rejects_non_origin_values(origin: str) -> None:
    with pytest.raises(ConfigurationError, match="受信挑战来源"):
        SecurityPolicyConfig(trusted_challenge_origins=(origin,))


def test_security_policy_keeps_legacy_positive_attempt_hint_without_using_it_as_limit() -> None:
    config = SecurityPolicyConfig(trusted_challenge_max_attempts=4)

    assert config.trusted_challenge_max_attempts == 4


def test_profile_key_is_stable_scoped_and_does_not_expose_account() -> None:
    first_scope = ExecutionScope("project", "tenant", "account-secret")
    same_scope = ExecutionScope("project", "tenant", "account-secret")
    another_account = ExecutionScope("project", "tenant", "another-account")

    first_key = _scoped_profile_key(first_scope, "https://example.com/orders?page=1")

    assert _scoped_profile_key(same_scope, "https://example.com/login") == first_key
    assert _scoped_profile_key(another_account, "https://example.com/orders") != first_key
    assert "account-secret" not in first_key


def test_app_config_mapping_round_trip_and_prepare_directories(tmp_path: Path) -> None:
    config = AppConfig(
        browser=BrowserConfig(
            headless=True,
            reuse_profile=False,
            profile_root=tmp_path / "profiles",
        ),
        storage=StorageConfig(
            memory_database=tmp_path / "memory" / "store.db",
            artifact_root=tmp_path / "artifacts",
        ),
        network=NetworkCaptureConfig(enabled=False, max_body_bytes=4096, max_responses=10),
        runtime=RuntimeConfig(log_level="warning"),
        security=SecurityPolicyConfig(
            trusted_challenge_origins=("https://erp.internal",),
            trusted_challenge_max_attempts=2,
            read_only=True,
        ),
    )

    loaded = AppConfig.from_mapping(config.to_mapping())
    loaded.prepare_directories()

    assert loaded.browser.headless is True
    assert loaded.browser.reuse_profile is False
    assert loaded.browser.profile_root == tmp_path / "profiles"
    assert loaded.storage.memory_database == tmp_path / "memory" / "store.db"
    assert loaded.storage.artifact_root == tmp_path / "artifacts"
    assert loaded.network.enabled is False
    assert loaded.network.max_body_bytes == 4096
    assert loaded.runtime.log_level == "WARNING"
    assert loaded.security.trusted_challenge_origins == ("https://erp.internal",)
    assert loaded.security.read_only is True
    assert (tmp_path / "profiles").is_dir()
    assert (tmp_path / "memory").is_dir()
    assert (tmp_path / "artifacts").is_dir()
