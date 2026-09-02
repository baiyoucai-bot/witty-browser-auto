"""发现 Chrome 官方远程调试入口，不启动浏览器或依赖扩展。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_DEVTOOLS_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9._-]+")
_REMOTE_DEBUGGING_URL = "chrome://inspect/#remote-debugging"
_DARWIN_BROWSERS = (
    ("Google Chrome", "com.google.Chrome"),
    ("Microsoft Edge", "com.microsoft.edgemac"),
    ("Chromium", "org.chromium.Chromium"),
)
_LINUX_BROWSERS = (
    ("chrome", "google-chrome"),
    ("chromium", "chromium"),
    ("msedge", "microsoft-edge"),
)


def default_live_profile_roots() -> tuple[Path, ...]:
    home = Path.home()
    if sys.platform == "darwin":
        application_support = home / "Library" / "Application Support"
        return (
            application_support / "Google" / "Chrome",
            application_support / "Microsoft Edge",
            application_support / "Chromium",
        )
    if sys.platform == "win32":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return (
            local_app_data / "Google" / "Chrome" / "User Data",
            local_app_data / "Microsoft" / "Edge" / "User Data",
            local_app_data / "Chromium" / "User Data",
        )
    return (
        home / ".config" / "google-chrome",
        home / ".config" / "microsoft-edge",
        home / ".config" / "chromium",
    )


async def discover_live_browser_endpoint(
    profile_roots: tuple[Path, ...] | None = None,
) -> str | None:
    """返回当前浏览器官方调试 WebSocket；忽略缺失、陈旧或异常 profile。"""

    for profile_root in profile_roots or default_live_profile_roots():
        endpoint = _read_active_endpoint(profile_root / "DevToolsActivePort")
        if endpoint is not None and await _endpoint_is_reachable(endpoint):
            return endpoint
    return None


async def open_live_browser_authorization_page() -> bool:
    """只向已运行的浏览器打开授权页，绝不借此启动新的浏览器实例。"""

    if sys.platform == "darwin":
        for process_name, bundle_id in _DARWIN_BROWSERS:
            if await _process_is_running(process_name) and await _run_quiet_command(
                "/usr/bin/open",
                "-b",
                bundle_id,
                _REMOTE_DEBUGGING_URL,
            ):
                logger.info("已在当前浏览器打开原生接管授权页", extra={"browser": process_name})
                return True
        return False
    if sys.platform.startswith("linux"):
        for process_name, executable in _LINUX_BROWSERS:
            if await _process_is_running(process_name) and await _run_quiet_command(
                executable,
                _REMOTE_DEBUGGING_URL,
            ):
                logger.info("已在当前浏览器打开原生接管授权页", extra={"browser": process_name})
                return True
    return False


async def wait_for_live_browser_endpoint(
    *,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 0.25,
    profile_roots: tuple[Path, ...] | None = None,
) -> str | None:
    """等待用户完成 Chrome 原生授权，不阻塞事件循环或无限等待。"""

    deadline = asyncio.get_running_loop().time() + max(0.0, timeout_seconds)
    while True:
        endpoint = await discover_live_browser_endpoint(profile_roots)
        if endpoint is not None:
            return endpoint
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(max(0.01, poll_interval_seconds), remaining))


def _read_active_endpoint(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return None
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0].strip())
    except ValueError:
        return None
    devtools_path = lines[1].strip()
    if not 1 <= port <= 65535 or _DEVTOOLS_PATH.fullmatch(devtools_path) is None:
        return None
    return f"ws://127.0.0.1:{port}{devtools_path}"


async def _endpoint_is_reachable(endpoint: str) -> bool:
    parsed = urlsplit(endpoint)
    if parsed.hostname != "127.0.0.1" or parsed.port is None:
        return False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, parsed.port),
            timeout=0.5,
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


async def _process_is_running(process_name: str) -> bool:
    return await _run_quiet_command("pgrep", "-x", process_name)


async def _run_quiet_command(*args: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return await asyncio.wait_for(process.wait(), timeout=3.0) == 0
    except TimeoutError:
        process.kill()
        await process.wait()
        return False
