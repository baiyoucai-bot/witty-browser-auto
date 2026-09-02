"""只启动和管理本项目拥有的 Chrome/Chromium 进程。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path

from witty_browser_auto.config import BrowserConfig
from witty_browser_auto.domain.errors import BrowserLaunchError

logger = logging.getLogger(__name__)
_ENDPOINT_MARKER = ".witty-browser-auto-cdp.json"


@dataclass(frozen=True, slots=True)
class ExistingBrowserEndpoint:
    endpoint: str
    profile_dir: Path
    target_id: str | None = None


@dataclass(slots=True)
class ManagedBrowserProcess:
    process: asyncio.subprocess.Process
    endpoint: str
    profile_dir: Path
    cleanup_profile: bool
    endpoint_marker: Path | None = None

    async def terminate(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.cleanup_profile and self.profile_dir.exists():
            # 只清理由本启动器通过 mkdtemp 创建的目录。
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        elif self.endpoint_marker is not None:
            self.endpoint_marker.unlink(missing_ok=True)


class ChromiumLauncher:
    COMMON_EXECUTABLES = (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    )

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config

    def find_executable(self) -> Path:
        if self.config.executable:
            executable = self.config.executable.resolve()
            if executable.is_file() and os.access(executable, os.X_OK):
                return executable
            raise BrowserLaunchError(
                "配置的浏览器可执行文件不存在或不可执行",
                context={"executable": str(executable)},
            )
        for candidate in self.COMMON_EXECUTABLES:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        raise BrowserLaunchError(
            "未找到 Chrome 或 Chromium，请设置 WITTY_BROWSER_AUTO_BROWSER_EXECUTABLE"
        )

    async def launch(self) -> ManagedBrowserProcess:
        executable = self.find_executable()
        profile_dir, cleanup_profile = self.prepare_profile()
        debugging_port = self.find_available_debugging_port()
        args = self.build_arguments(executable, profile_dir, debugging_port)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            self._cleanup_profile(profile_dir, cleanup_profile)
            raise BrowserLaunchError(
                "启动 Chrome/Chromium 失败",
                context={"executable": str(executable)},
            ) from exc

        endpoint = f"http://127.0.0.1:{debugging_port}"
        deadline = asyncio.get_running_loop().time() + self.config.launch_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                self._cleanup_profile(profile_dir, cleanup_profile)
                raise BrowserLaunchError(
                    "浏览器在远程调试端口就绪前退出",
                    context={"returncode": process.returncode},
                )
            if await self._is_debugging_port_ready(debugging_port):
                endpoint_marker = self.write_endpoint_marker(
                    profile_dir,
                    debugging_port,
                    process.pid,
                )
                logger.info(
                    "受管浏览器已启动",
                    extra={
                        "pid": process.pid,
                        "endpoint": endpoint,
                        "profile_dir": str(profile_dir),
                        "profile_reused": not cleanup_profile,
                        "headless": self.config.headless,
                    },
                )
                return ManagedBrowserProcess(
                    process,
                    endpoint,
                    profile_dir,
                    cleanup_profile,
                    endpoint_marker,
                )
            await asyncio.sleep(0.05)

        process.terminate()
        await process.wait()
        self._cleanup_profile(profile_dir, cleanup_profile)
        raise BrowserLaunchError(
            "等待浏览器远程调试端口超时",
            context={"timeout_seconds": self.config.launch_timeout_seconds},
        )

    async def find_existing_endpoint(self) -> ExistingBrowserEndpoint | None:
        if not self.config.reuse_profile:
            return None
        profile_dir = self.config.profile_root.resolve() / self.config.profile_key
        marker = profile_dir / _ENDPOINT_MARKER
        payload = self._read_endpoint_marker(marker)
        if payload is None:
            return None
        port = payload.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            marker.unlink(missing_ok=True)
            return None
        if not await self._is_debugging_port_ready(port):
            marker.unlink(missing_ok=True)
            return None
        target_id = payload.get("target_id")
        return ExistingBrowserEndpoint(
            endpoint=f"http://127.0.0.1:{port}",
            profile_dir=profile_dir,
            target_id=target_id if isinstance(target_id, str) and target_id else None,
        )

    def write_endpoint_marker(
        self,
        profile_dir: Path,
        port: int,
        pid: int,
        *,
        target_id: str | None = None,
    ) -> Path:
        marker = profile_dir / _ENDPOINT_MARKER
        payload: dict[str, object] = {"port": port, "pid": pid}
        if target_id:
            payload["target_id"] = target_id
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{_ENDPOINT_MARKER}.", dir=profile_dir)
        temp_path = Path(temp_name)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temp_path, marker)
            marker.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temp_path.unlink(missing_ok=True)
        return marker

    def remember_target(self, target_id: str) -> None:
        if not self.config.reuse_profile:
            return
        profile_dir = self.config.profile_root.resolve() / self.config.profile_key
        marker = profile_dir / _ENDPOINT_MARKER
        payload = self._read_endpoint_marker(marker)
        if payload is None:
            return
        port = payload.get("port")
        pid = payload.get("pid")
        if isinstance(port, int) and isinstance(pid, int):
            self.write_endpoint_marker(profile_dir, port, pid, target_id=target_id)

    def clear_endpoint_marker(self) -> None:
        if self.config.reuse_profile:
            marker = self.config.profile_root.resolve() / self.config.profile_key / _ENDPOINT_MARKER
            marker.unlink(missing_ok=True)

    @staticmethod
    def _read_endpoint_marker(marker: Path) -> dict[str, object] | None:
        try:
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 1024:
                return None
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def prepare_profile(self) -> tuple[Path, bool]:
        """准备专用 profile；持久模式绝不由任务结束流程删除。"""

        root = self.config.profile_root.resolve()
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root.chmod(0o700)
            if self.config.reuse_profile:
                profile_dir = root / self.config.profile_key
                profile_dir.mkdir(parents=False, exist_ok=True, mode=0o700)
                profile_dir.chmod(0o700)
                return profile_dir, False
            return Path(tempfile.mkdtemp(prefix="managed-", dir=root)), True
        except OSError as exc:
            raise BrowserLaunchError(
                "准备浏览器 profile 目录失败",
                context={"profile_root": str(root)},
            ) from exc

    def build_arguments(
        self,
        executable: Path,
        profile_dir: Path,
        debugging_port: int,
    ) -> list[str]:
        args = [
            str(executable),
            f"--remote-debugging-port={debugging_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not self.config.reuse_profile:
            args.extend(("--disable-background-networking", "--disable-component-update"))
        if self.config.headless:
            args.append("--headless=new")
        args.append("about:blank")
        return args

    @staticmethod
    def find_available_debugging_port() -> int:
        """选择非零回环端口，避免 Chrome 因端口 0 暴露额外自动化信号。"""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        if not isinstance(port, int) or port <= 0:
            raise BrowserLaunchError("无法分配浏览器远程调试端口")
        return port

    @staticmethod
    async def _is_debugging_port_ready(port: int) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            return False
        writer.close()
        await writer.wait_closed()
        return True

    @staticmethod
    def _cleanup_profile(profile_dir: Path, cleanup_profile: bool) -> None:
        if cleanup_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)
