"""本地配置文件的安全加载、原子保存和优先级解析。"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from witty_browser_auto.config import DEFAULT_CONFIG_PATH, AppConfig, environment_override_paths
from witty_browser_auto.domain.errors import ConfigurationError

logger = logging.getLogger(__name__)
_MAX_CONFIG_BYTES = 1024 * 1024


def resolve_config_path(path: Path | None = None) -> Path:
    if path is not None:
        selected = path
    else:
        raw = os.getenv("WITTY_BROWSER_AUTO_CONFIG_FILE", "").strip()
        selected = Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH
    if ".." in selected.parts:
        raise ConfigurationError("配置文件路径不能包含上级目录跳转")
    return selected


class LocalConfigStore:
    """保存部署级配置；任务目标和单次高风险授权不进入这里。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = resolve_config_path(path)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def load_saved(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        self._reject_symlink_target()
        try:
            if self.path.stat().st_size > _MAX_CONFIG_BYTES:
                raise ConfigurationError("配置文件超过 1 MiB 安全上限")
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except ConfigurationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("读取本地配置文件失败，请检查文件格式和权限") from exc
        if not isinstance(data, dict):
            raise ConfigurationError("本地配置文件根节点必须是 JSON 对象")
        return AppConfig.from_mapping(data)

    def load_effective(self) -> AppConfig:
        return AppConfig.from_env(self.load_saved())

    def save(self, config: AppConfig) -> None:
        parent = self.path.parent
        self._prepare_private_parent(parent)
        self._reject_symlink_target()
        payload = (
            json.dumps(
                config.to_mapping(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        temp_path = parent / f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(temp_path, flags, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            self._reject_symlink_target()
            os.replace(temp_path, self.path)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            self._fsync_directory(parent)
        except ConfigurationError:
            raise
        except OSError as exc:
            raise ConfigurationError("保存本地配置失败，请检查目录权限和磁盘空间") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理配置临时文件失败", extra={"config_path": str(self.path)})
        logger.info("本地配置已原子保存", extra={"config_path": str(self.path)})

    def public_state(self) -> dict[str, Any]:
        saved = self.load_saved()
        effective = AppConfig.from_env(saved)
        overrides = environment_override_paths()
        return {
            "saved": saved.to_mapping(),
            "effective": effective.to_mapping(),
            "config_path": str(self.path.absolute()),
            "saved_file_exists": self.exists,
            "environment_overrides": overrides,
            "effect_note": "保存后对下一次任务生效；环境变量覆盖的字段仍以部署环境为准。",
        }

    def _reject_symlink_target(self) -> None:
        if self.path.is_symlink():
            raise ConfigurationError("配置文件不能是符号链接")
        parent = self.path.parent
        if parent.exists() and parent.is_symlink():
            raise ConfigurationError("配置文件目录不能是符号链接")

    @staticmethod
    def _prepare_private_parent(parent: Path) -> None:
        if parent.exists() and parent.is_symlink():
            raise ConfigurationError("配置文件目录不能是符号链接")
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(parent, stat.S_IRWXU)
        except OSError as exc:
            raise ConfigurationError("创建私有配置目录失败") from exc

    @staticmethod
    def _fsync_directory(parent: Path) -> None:
        try:
            descriptor = os.open(parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def load_app_config(path: Path | None = None) -> AppConfig:
    return LocalConfigStore(path).load_effective()
