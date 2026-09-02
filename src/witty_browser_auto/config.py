"""Witty 浏览器工具库 类型化配置、JSON 映射和环境变量覆盖规则。"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from witty_browser_auto.domain.errors import ConfigurationError
from witty_browser_auto.memory.url import normalize_url

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(".witty-browser-auto/config.json")

# 配置中心使用这些映射显示当前哪些字段被部署环境覆盖，不能包含字段值。
CONFIG_ENV_FIELDS: dict[str, str] = {
    "browser.executable": "WITTY_BROWSER_AUTO_BROWSER_EXECUTABLE",
    "browser.cdp_endpoint": "WITTY_BROWSER_AUTO_CDP_ENDPOINT",
    "browser.session_mode": "WITTY_BROWSER_AUTO_BROWSER_SESSION_MODE",
    "browser.headless": "WITTY_BROWSER_AUTO_HEADLESS",
    "browser.profile_root": "WITTY_BROWSER_AUTO_PROFILE_ROOT",
    "browser.reuse_profile": "WITTY_BROWSER_AUTO_REUSE_PROFILE",
    "browser.command_timeout_seconds": "WITTY_BROWSER_AUTO_CDP_TIMEOUT",
    "browser.launch_timeout_seconds": "WITTY_BROWSER_AUTO_LAUNCH_TIMEOUT",
    "storage.memory_database": "WITTY_BROWSER_AUTO_MEMORY_DB",
    "storage.artifact_root": "WITTY_BROWSER_AUTO_ARTIFACT_ROOT",
    "network.enabled": "WITTY_BROWSER_AUTO_NETWORK_CAPTURE_ENABLED",
    "network.max_body_bytes": "WITTY_BROWSER_AUTO_NETWORK_MAX_BODY_BYTES",
    "network.max_responses": "WITTY_BROWSER_AUTO_NETWORK_MAX_RESPONSES",
    "runtime.log_level": "WITTY_BROWSER_AUTO_LOG_LEVEL",
    "security.trusted_challenge_origins": "WITTY_BROWSER_AUTO_TRUSTED_CHALLENGE_ORIGINS",
    "security.trusted_challenge_max_attempts": (
        "WITTY_BROWSER_AUTO_TRUSTED_CHALLENGE_MAX_ATTEMPTS"
    ),
    "security.read_only": "WITTY_BROWSER_AUTO_READ_ONLY",
}


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"环境变量 {name} 不是有效布尔值")


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 不是有效数字") from exc
    if value <= 0:
        raise ConfigurationError(f"环境变量 {name} 必须大于零")
    return value


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"环境变量 {name} 不是有效整数") from exc


def _read_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _require_positive(value: int | float, label: str) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"{label}必须大于零")


def _normalize_trusted_origin(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or "*" in value
    ):
        raise ConfigurationError("企业受信挑战来源必须是无账号、路径、查询和通配符的 origin")
    try:
        return normalize_url(f"{value.rstrip('/')}/").origin
    except (ConfigurationError, ValueError) as exc:
        raise ConfigurationError("企业受信挑战来源必须是有效的 http 或 https origin") from exc


def _normalize_cdp_endpoint(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    parts = urlsplit(normalized)
    if (
        parts.scheme not in {"http", "https", "ws", "wss"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or "*" in normalized
    ):
        raise ConfigurationError("CDP 地址必须是无账号、查询和通配符的 HTTP 或 WebSocket 地址")
    try:
        _ = parts.port
    except ValueError as exc:
        raise ConfigurationError("CDP 地址包含无效端口") from exc
    return normalized


class BrowserSessionMode(str, Enum):
    MANAGED = "managed"
    TAKEOVER = "takeover"


def _normalize_browser_session_mode(value: BrowserSessionMode | str) -> BrowserSessionMode:
    if isinstance(value, BrowserSessionMode):
        return value
    if not isinstance(value, str):
        raise ConfigurationError("浏览器会话模式必须是 managed 或 takeover")
    normalized = value.strip().lower()
    try:
        return BrowserSessionMode(normalized)
    except ValueError as exc:
        raise ConfigurationError("浏览器会话模式必须是 managed 或 takeover") from exc


def _mapping_section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"配置分组 {name} 必须是 JSON 对象")
    return value


def _reject_unknown_keys(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ConfigurationError(f"{label}包含未知字段：{', '.join(unknown)}")


# 项目收敛为纯工具库时移除的配置段与字段。旧配置文件仍带着它们，直接按未知字段拒绝会让
# 既有安装连配置都加载不了——`load_app_config` 是 `launch_browser_toolkit` 的第一步，
# 因此这些名字被显式忽略；真正拼错的字段仍然会被拒绝。
_LEGACY_SECTIONS: frozenset[str] = frozenset({"model"})
_LEGACY_RUNTIME_KEYS: frozenset[str] = frozenset(
    {
        "max_steps",
        "task_timeout_seconds",
        "max_model_decisions",
        "code_repair_enabled",
        "code_repair_project_root",
        "code_repair_validation_timeout_seconds",
        "code_repair_max_restarts",
    }
)
_LEGACY_SECURITY_KEYS: frozenset[str] = frozenset({"allow_public_model_diagnostics"})


def _drop_legacy_keys(
    data: Mapping[str, Any],
    legacy: frozenset[str],
    label: str,
) -> dict[str, Any]:
    """去掉已移除的历史字段并记录一次，保留其余内容交给严格校验。"""

    dropped = sorted(str(key) for key in data if key in legacy)
    if dropped:
        logger.info(
            "忽略已移除的历史配置项",
            extra={"类型": label, "字段": dropped},
        )
    return {key: value for key, value in data.items() if key not in legacy}


def _as_string(value: Any, label: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label}必须是文本")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ConfigurationError(f"{label}不能为空")
    return normalized


def _as_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    normalized = _as_string(value, label)
    return normalized or None


def _as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{label}必须是布尔值")
    return value


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{label}必须是整数")
    return value


def _as_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{label}必须是数字")
    return float(value)


def _as_resource_types(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """CDP 资源类型区分大小写，这里保持原样只做去重和空值过滤。"""

    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError("流量正文采集范围必须是字符串数组")
    normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    if not normalized:
        raise ConfigurationError("流量正文采集范围不能为空")
    return normalized


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    executable: Path | None = None
    cdp_endpoint: str | None = None
    session_mode: BrowserSessionMode = BrowserSessionMode.MANAGED
    headless: bool = False
    profile_root: Path = Path(".witty-browser-auto/profiles")
    reuse_profile: bool = True
    profile_key: str = "default"
    command_timeout_seconds: float = 15.0
    launch_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", self.profile_key):
            raise ConfigurationError("浏览器 profile 标识只能包含字母、数字、点、下划线和短横线")
        if self.profile_key in {".", ".."}:
            raise ConfigurationError("浏览器 profile 标识不能是相对目录")
        if not str(self.profile_root).strip():
            raise ConfigurationError("浏览器 profile 根目录不能为空")
        _require_positive(self.command_timeout_seconds, "CDP 命令超时")
        _require_positive(self.launch_timeout_seconds, "浏览器启动超时")
        object.__setattr__(self, "cdp_endpoint", _normalize_cdp_endpoint(self.cdp_endpoint))
        object.__setattr__(
            self,
            "session_mode",
            _normalize_browser_session_mode(self.session_mode),
        )


@dataclass(frozen=True, slots=True)
class StorageConfig:
    memory_database: Path = Path(".witty-browser-auto/memory.db")
    artifact_root: Path = Path(".witty-browser-auto/artifacts")

    def __post_init__(self) -> None:
        if not str(self.memory_database).strip() or not str(self.artifact_root).strip():
            raise ConfigurationError("记忆数据库和诊断目录不能为空")


@dataclass(frozen=True, slots=True)
class NetworkCaptureConfig:
    """只读网络响应体捕获配置；任务作用域限制不能通过配置关闭。"""

    enabled: bool = True
    max_body_bytes: int = 1024 * 1024
    max_responses: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("网络响应体捕获开关必须是布尔值")
        if (
            isinstance(self.max_body_bytes, bool)
            or not 1024 <= self.max_body_bytes <= 10 * 1024 * 1024
        ):
            raise ConfigurationError("网络响应体最大字节数必须在 1024 到 10485760 之间")
        if isinstance(self.max_responses, bool) or not 1 <= self.max_responses <= 1000:
            raise ConfigurationError("网络响应最大保留数量必须在 1 到 1000 之间")


@dataclass(frozen=True, slots=True)
class NetworkTrafficConfig:
    """完整流量检查配置；只作用于本项目受管浏览器。"""

    enabled: bool = True
    max_exchanges: int = 2000
    max_body_bytes: int = 2 * 1024 * 1024
    max_total_body_bytes: int = 64 * 1024 * 1024
    body_resource_types: tuple[str, ...] = (
        "Document",
        "EventSource",
        "Fetch",
        "Manifest",
        "Other",
        "Script",
        "Stylesheet",
        "WebSocket",
        "XHR",
    )
    max_websocket_frames: int = 500
    max_websocket_frame_bytes: int = 64 * 1024
    # 超过单体上限的响应改为落盘而不是丢弃；0 表示关闭落盘，只保留长度与原因。
    spill_body_bytes: int = 64 * 1024 * 1024
    max_total_spill_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("流量检查开关必须是布尔值")
        if isinstance(self.max_exchanges, bool) or not 10 <= self.max_exchanges <= 20_000:
            raise ConfigurationError("流量交换最大保留数量必须在 10 到 20000 之间")
        if (
            isinstance(self.max_body_bytes, bool)
            or not 1024 <= self.max_body_bytes <= 32 * 1024 * 1024
        ):
            raise ConfigurationError("流量正文单体上限必须在 1024 到 33554432 之间")
        if (
            isinstance(self.max_total_body_bytes, bool)
            or not self.max_body_bytes <= self.max_total_body_bytes <= 1024 * 1024 * 1024
        ):
            raise ConfigurationError("流量正文全局预算必须不小于单体上限且不超过 1 GiB")
        if not self.body_resource_types:
            raise ConfigurationError("流量正文采集范围不能为空")
        if (
            isinstance(self.max_websocket_frames, bool)
            or not 1 <= self.max_websocket_frames <= 5000
        ):
            raise ConfigurationError("WebSocket 帧最大保留数量必须在 1 到 5000 之间")
        if (
            isinstance(self.max_websocket_frame_bytes, bool)
            or not 256 <= self.max_websocket_frame_bytes <= 4 * 1024 * 1024
        ):
            raise ConfigurationError("WebSocket 单帧上限必须在 256 到 4194304 之间")
        if isinstance(self.spill_body_bytes, bool) or self.spill_body_bytes < 0:
            raise ConfigurationError("落盘正文单体上限必须是非负整数，0 表示关闭落盘")
        if self.spill_body_bytes and not (
            self.max_body_bytes <= self.spill_body_bytes <= 1024 * 1024 * 1024
        ):
            raise ConfigurationError("落盘正文单体上限必须不小于内存单体上限且不超过 1 GiB")
        if isinstance(self.max_total_spill_bytes, bool) or self.max_total_spill_bytes < 0:
            raise ConfigurationError("落盘正文全局预算必须是非负整数")
        if self.spill_body_bytes and self.max_total_spill_bytes < self.spill_body_bytes:
            raise ConfigurationError("落盘正文全局预算必须不小于落盘单体上限")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        normalized_level = self.log_level.strip().upper()
        if normalized_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("日志级别必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
        object.__setattr__(self, "log_level", normalized_level)


@dataclass(frozen=True, slots=True)
class SecurityPolicyConfig:
    """由企业部署管理员配置的站点所有者授权策略。"""

    trusted_challenge_origins: tuple[str, ...] = ()
    trusted_challenge_max_attempts: int = 1
    # 部署级生产保护；任务只能进一步收紧，不能绕过该开关。
    read_only: bool = False

    def __post_init__(self) -> None:
        if self.trusted_challenge_max_attempts < 1:
            raise ConfigurationError("兼容挑战审计提示值必须大于零")
        if not isinstance(self.read_only, bool):
            raise ConfigurationError("只读策略必须是布尔值")
        normalized = tuple(
            dict.fromkeys(
                _normalize_trusted_origin(origin) for origin in self.trusted_challenge_origins
            )
        )
        object.__setattr__(self, "trusted_challenge_origins", normalized)


@dataclass(frozen=True, slots=True)
class AppConfig:
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    network: NetworkCaptureConfig = field(default_factory=NetworkCaptureConfig)
    traffic: NetworkTrafficConfig = field(default_factory=NetworkTrafficConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    security: SecurityPolicyConfig = field(default_factory=SecurityPolicyConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> AppConfig:
        """从本地 JSON 构建配置，严格拒绝拼错或未知的字段。

        收敛为纯工具库时移除的配置段与字段属于例外：既有安装的配置文件里仍留着它们，
        一律拒绝会让整个库在这些机器上连配置都加载不了，因此忽略并记录一条日志。
        """

        data = _drop_legacy_keys(data, _LEGACY_SECTIONS, "配置段")
        _reject_unknown_keys(
            data,
            {"browser", "storage", "network", "traffic", "runtime", "security"},
            "配置",
        )
        defaults = cls()
        browser_data = _mapping_section(data, "browser")
        storage_data = _mapping_section(data, "storage")
        network_data = _mapping_section(data, "network")
        traffic_data = _mapping_section(data, "traffic")
        runtime_data = _drop_legacy_keys(
            _mapping_section(data, "runtime"), _LEGACY_RUNTIME_KEYS, "runtime 字段"
        )
        security_data = _drop_legacy_keys(
            _mapping_section(data, "security"), _LEGACY_SECURITY_KEYS, "security 字段"
        )
        _reject_unknown_keys(
            browser_data,
            {
                "executable",
                "cdp_endpoint",
                "session_mode",
                "headless",
                "profile_root",
                "reuse_profile",
                "command_timeout_seconds",
                "launch_timeout_seconds",
            },
            "浏览器配置",
        )
        _reject_unknown_keys(
            storage_data,
            {"memory_database", "artifact_root"},
            "存储配置",
        )
        _reject_unknown_keys(
            network_data,
            {"enabled", "max_body_bytes", "max_responses"},
            "网络数据配置",
        )
        _reject_unknown_keys(
            traffic_data,
            {
                "enabled",
                "max_exchanges",
                "max_body_bytes",
                "max_total_body_bytes",
                "body_resource_types",
                "max_websocket_frames",
                "max_websocket_frame_bytes",
                "spill_body_bytes",
                "max_total_spill_bytes",
            },
            "流量检查配置",
        )
        _reject_unknown_keys(
            runtime_data,
            {"log_level"},
            "运行时配置",
        )
        _reject_unknown_keys(
            security_data,
            {
                "trusted_challenge_origins",
                "trusted_challenge_max_attempts",
                "read_only",
            },
            "安全策略配置",
        )

        origins_value = security_data.get(
            "trusted_challenge_origins",
            defaults.security.trusted_challenge_origins,
        )
        if not isinstance(origins_value, Sequence) or isinstance(origins_value, (str, bytes)):
            raise ConfigurationError("企业受信挑战来源必须是文本数组")
        origins = tuple(
            _as_string(item, "企业受信挑战来源", allow_empty=False) for item in origins_value
        )
        executable = _as_optional_string(
            browser_data.get("executable", None),
            "浏览器可执行文件",
        )
        memory_database = _as_string(
            storage_data.get("memory_database", str(defaults.storage.memory_database)),
            "记忆数据库路径",
            allow_empty=False,
        )
        artifact_root = _as_string(
            storage_data.get("artifact_root", str(defaults.storage.artifact_root)),
            "诊断目录",
            allow_empty=False,
        )
        return cls(
            browser=BrowserConfig(
                executable=Path(executable).expanduser() if executable else None,
                cdp_endpoint=_as_optional_string(
                    browser_data.get("cdp_endpoint", defaults.browser.cdp_endpoint),
                    "CDP 地址",
                ),
                session_mode=_normalize_browser_session_mode(
                    _as_string(
                        browser_data.get("session_mode", defaults.browser.session_mode.value),
                        "浏览器会话模式",
                        allow_empty=False,
                    )
                ),
                headless=_as_bool(
                    browser_data.get("headless", defaults.browser.headless),
                    "浏览器无头模式",
                ),
                profile_root=Path(
                    _as_string(
                        browser_data.get("profile_root", str(defaults.browser.profile_root)),
                        "浏览器 profile 根目录",
                        allow_empty=False,
                    )
                ).expanduser(),
                reuse_profile=_as_bool(
                    browser_data.get("reuse_profile", defaults.browser.reuse_profile),
                    "浏览器会话复用",
                ),
                command_timeout_seconds=_as_float(
                    browser_data.get(
                        "command_timeout_seconds",
                        defaults.browser.command_timeout_seconds,
                    ),
                    "CDP 命令超时",
                ),
                launch_timeout_seconds=_as_float(
                    browser_data.get(
                        "launch_timeout_seconds",
                        defaults.browser.launch_timeout_seconds,
                    ),
                    "浏览器启动超时",
                ),
            ),
            storage=StorageConfig(
                memory_database=Path(memory_database).expanduser(),
                artifact_root=Path(artifact_root).expanduser(),
            ),
            network=NetworkCaptureConfig(
                enabled=_as_bool(
                    network_data.get("enabled", defaults.network.enabled),
                    "网络响应体捕获",
                ),
                max_body_bytes=_as_int(
                    network_data.get("max_body_bytes", defaults.network.max_body_bytes),
                    "网络响应体最大字节数",
                ),
                max_responses=_as_int(
                    network_data.get("max_responses", defaults.network.max_responses),
                    "网络响应最大保留数量",
                ),
            ),
            traffic=NetworkTrafficConfig(
                enabled=_as_bool(
                    traffic_data.get("enabled", defaults.traffic.enabled),
                    "流量检查",
                ),
                max_exchanges=_as_int(
                    traffic_data.get("max_exchanges", defaults.traffic.max_exchanges),
                    "流量交换最大保留数量",
                ),
                max_body_bytes=_as_int(
                    traffic_data.get("max_body_bytes", defaults.traffic.max_body_bytes),
                    "流量正文单体上限",
                ),
                max_total_body_bytes=_as_int(
                    traffic_data.get("max_total_body_bytes", defaults.traffic.max_total_body_bytes),
                    "流量正文全局预算",
                ),
                body_resource_types=_as_resource_types(
                    traffic_data.get("body_resource_types"),
                    defaults.traffic.body_resource_types,
                ),
                max_websocket_frames=_as_int(
                    traffic_data.get("max_websocket_frames", defaults.traffic.max_websocket_frames),
                    "WebSocket 帧最大保留数量",
                ),
                max_websocket_frame_bytes=_as_int(
                    traffic_data.get(
                        "max_websocket_frame_bytes",
                        defaults.traffic.max_websocket_frame_bytes,
                    ),
                    "WebSocket 单帧上限",
                ),
                spill_body_bytes=_as_int(
                    traffic_data.get("spill_body_bytes", defaults.traffic.spill_body_bytes),
                    "落盘正文单体上限",
                ),
                max_total_spill_bytes=_as_int(
                    traffic_data.get(
                        "max_total_spill_bytes",
                        defaults.traffic.max_total_spill_bytes,
                    ),
                    "落盘正文全局预算",
                ),
            ),
            runtime=RuntimeConfig(
                log_level=_as_string(
                    runtime_data.get("log_level", defaults.runtime.log_level),
                    "日志级别",
                    allow_empty=False,
                ),
            ),
            security=SecurityPolicyConfig(
                trusted_challenge_origins=origins,
                trusted_challenge_max_attempts=_as_int(
                    security_data.get(
                        "trusted_challenge_max_attempts",
                        defaults.security.trusted_challenge_max_attempts,
                    ),
                    "企业受信挑战最大尝试次数",
                ),
                read_only=_as_bool(
                    security_data.get("read_only", defaults.security.read_only),
                    "只读策略",
                ),
            ),
        )

    @classmethod
    def from_env(cls, base: AppConfig | None = None) -> AppConfig:
        """把环境变量覆盖到默认值或已保存的本地配置之上。"""

        source = base or cls()
        executable_raw = os.getenv("WITTY_BROWSER_AUTO_BROWSER_EXECUTABLE")
        executable = source.browser.executable
        if executable_raw is not None:
            executable = (
                Path(executable_raw.strip()).expanduser() if executable_raw.strip() else None
            )
        cdp_raw = os.getenv("WITTY_BROWSER_AUTO_CDP_ENDPOINT")
        cdp_endpoint = source.browser.cdp_endpoint if cdp_raw is None else (cdp_raw.strip() or None)
        session_mode_raw = os.getenv("WITTY_BROWSER_AUTO_BROWSER_SESSION_MODE")
        log_level_raw = os.getenv("WITTY_BROWSER_AUTO_LOG_LEVEL")
        profile_root_raw = os.getenv("WITTY_BROWSER_AUTO_PROFILE_ROOT")
        memory_db_raw = os.getenv("WITTY_BROWSER_AUTO_MEMORY_DB")
        artifact_root_raw = os.getenv("WITTY_BROWSER_AUTO_ARTIFACT_ROOT")

        return cls(
            browser=BrowserConfig(
                executable=executable,
                cdp_endpoint=cdp_endpoint,
                session_mode=_normalize_browser_session_mode(
                    session_mode_raw
                    if session_mode_raw is not None
                    else source.browser.session_mode
                ),
                headless=_read_bool("WITTY_BROWSER_AUTO_HEADLESS", source.browser.headless),
                profile_root=Path(
                    profile_root_raw
                    if profile_root_raw is not None
                    else source.browser.profile_root
                ).expanduser(),
                reuse_profile=_read_bool(
                    "WITTY_BROWSER_AUTO_REUSE_PROFILE",
                    source.browser.reuse_profile,
                ),
                profile_key=source.browser.profile_key,
                command_timeout_seconds=_read_float(
                    "WITTY_BROWSER_AUTO_CDP_TIMEOUT",
                    source.browser.command_timeout_seconds,
                ),
                launch_timeout_seconds=_read_float(
                    "WITTY_BROWSER_AUTO_LAUNCH_TIMEOUT",
                    source.browser.launch_timeout_seconds,
                ),
            ),
            storage=StorageConfig(
                memory_database=Path(
                    memory_db_raw if memory_db_raw is not None else source.storage.memory_database
                ).expanduser(),
                artifact_root=Path(
                    artifact_root_raw
                    if artifact_root_raw is not None
                    else source.storage.artifact_root
                ).expanduser(),
            ),
            network=NetworkCaptureConfig(
                enabled=_read_bool(
                    "WITTY_BROWSER_AUTO_NETWORK_CAPTURE_ENABLED",
                    source.network.enabled,
                ),
                max_body_bytes=_read_int(
                    "WITTY_BROWSER_AUTO_NETWORK_MAX_BODY_BYTES",
                    source.network.max_body_bytes,
                ),
                max_responses=_read_int(
                    "WITTY_BROWSER_AUTO_NETWORK_MAX_RESPONSES",
                    source.network.max_responses,
                ),
            ),
            traffic=NetworkTrafficConfig(
                enabled=_read_bool(
                    "WITTY_BROWSER_AUTO_TRAFFIC_ENABLED",
                    source.traffic.enabled,
                ),
                max_exchanges=_read_int(
                    "WITTY_BROWSER_AUTO_TRAFFIC_MAX_EXCHANGES",
                    source.traffic.max_exchanges,
                ),
                max_body_bytes=_read_int(
                    "WITTY_BROWSER_AUTO_TRAFFIC_MAX_BODY_BYTES",
                    source.traffic.max_body_bytes,
                ),
                max_total_body_bytes=_read_int(
                    "WITTY_BROWSER_AUTO_TRAFFIC_MAX_TOTAL_BODY_BYTES",
                    source.traffic.max_total_body_bytes,
                ),
                body_resource_types=_as_resource_types(
                    _read_list(
                        "WITTY_BROWSER_AUTO_TRAFFIC_BODY_RESOURCE_TYPES",
                        source.traffic.body_resource_types,
                    ),
                    source.traffic.body_resource_types,
                ),
                max_websocket_frames=_read_int(
                    "WITTY_BROWSER_AUTO_TRAFFIC_MAX_WEBSOCKET_FRAMES",
                    source.traffic.max_websocket_frames,
                ),
                max_websocket_frame_bytes=_read_int(
                    "WITTY_BROWSER_AUTO_TRAFFIC_MAX_WEBSOCKET_FRAME_BYTES",
                    source.traffic.max_websocket_frame_bytes,
                ),
                spill_body_bytes=_read_int(
                    "WITTY_BROWSER_AUTO_TRAFFIC_SPILL_BODY_BYTES",
                    source.traffic.spill_body_bytes,
                ),
                max_total_spill_bytes=_read_int(
                    "WITTY_BROWSER_AUTO_TRAFFIC_MAX_TOTAL_SPILL_BYTES",
                    source.traffic.max_total_spill_bytes,
                ),
            ),
            runtime=RuntimeConfig(
                log_level=log_level_raw if log_level_raw is not None else source.runtime.log_level,
            ),
            security=SecurityPolicyConfig(
                trusted_challenge_origins=_read_list(
                    "WITTY_BROWSER_AUTO_TRUSTED_CHALLENGE_ORIGINS",
                    source.security.trusted_challenge_origins,
                ),
                trusted_challenge_max_attempts=_read_int(
                    "WITTY_BROWSER_AUTO_TRUSTED_CHALLENGE_MAX_ATTEMPTS",
                    source.security.trusted_challenge_max_attempts,
                ),
                read_only=_read_bool("WITTY_BROWSER_AUTO_READ_ONLY", source.security.read_only),
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        """转换为稳定 JSON 结构。"""

        return {
            "browser": {
                "executable": str(self.browser.executable) if self.browser.executable else None,
                "cdp_endpoint": self.browser.cdp_endpoint,
                "session_mode": self.browser.session_mode.value,
                "headless": self.browser.headless,
                "profile_root": str(self.browser.profile_root),
                "reuse_profile": self.browser.reuse_profile,
                "command_timeout_seconds": self.browser.command_timeout_seconds,
                "launch_timeout_seconds": self.browser.launch_timeout_seconds,
            },
            "storage": {
                "memory_database": str(self.storage.memory_database),
                "artifact_root": str(self.storage.artifact_root),
            },
            "network": {
                "enabled": self.network.enabled,
                "max_body_bytes": self.network.max_body_bytes,
                "max_responses": self.network.max_responses,
            },
            "traffic": {
                "enabled": self.traffic.enabled,
                "max_exchanges": self.traffic.max_exchanges,
                "max_body_bytes": self.traffic.max_body_bytes,
                "max_total_body_bytes": self.traffic.max_total_body_bytes,
                "body_resource_types": list(self.traffic.body_resource_types),
                "max_websocket_frames": self.traffic.max_websocket_frames,
                "max_websocket_frame_bytes": self.traffic.max_websocket_frame_bytes,
                "spill_body_bytes": self.traffic.spill_body_bytes,
                "max_total_spill_bytes": self.traffic.max_total_spill_bytes,
            },
            "runtime": {
                "log_level": self.runtime.log_level,
            },
            "security": {
                "trusted_challenge_origins": list(self.security.trusted_challenge_origins),
                "trusted_challenge_max_attempts": (self.security.trusted_challenge_max_attempts),
                "read_only": self.security.read_only,
            },
        }

    def prepare_directories(self) -> None:
        for path in (
            self.browser.profile_root,
            self.storage.memory_database.parent,
            self.storage.artifact_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


def environment_override_paths() -> dict[str, str]:
    """返回配置路径到环境变量名的映射，不返回可能含密钥的值。"""

    return {
        field_path: env_name
        for field_path, env_name in CONFIG_ENV_FIELDS.items()
        if env_name in os.environ
    }
