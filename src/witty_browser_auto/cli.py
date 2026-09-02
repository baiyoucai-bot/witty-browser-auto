"""Witty 浏览器工具库命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from witty_browser_auto.browser.launcher import ChromiumLauncher
from witty_browser_auto.cdp.discovery import ensure_loopback_endpoint
from witty_browser_auto.config_store import load_app_config
from witty_browser_auto.domain.errors import ConfigurationError, RpaError
from witty_browser_auto.mcp_server.tools import PROFILES, profile_definitions
from witty_browser_auto.observability.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="witty-browser-auto",
        description="Witty 浏览器工具库：供外部智能体调用的本机 CDP 浏览器能力",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="检查浏览器可执行文件、存储目录与可选 CDP 端点")
    subparsers.add_parser("version", help="显示版本")

    mcp_parser = subparsers.add_parser(
        "mcp",
        help="以 MCP 服务端方式运行 stdio 传输，供不能执行代码的智能体框架调用",
    )
    mcp_parser.add_argument(
        "--profile",
        choices=list(PROFILES),
        default="core",
        help="暴露的工具档位：core 为主线任务子集，all 为全部开放工具，默认 core",
    )
    mcp_parser.add_argument(
        "--category",
        action="append",
        default=[],
        metavar="NAME",
        help="只暴露指定分类的工具，可重复",
    )
    mcp_parser.add_argument(
        "--tool",
        action="append",
        default=[],
        metavar="NAME",
        help="在档位之外额外暴露指定工具，可重复",
    )
    mcp_parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="授权导航与重放的 origin，可重复；省略时按入口地址自身的 origin 收敛",
    )
    mcp_parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="敏感任务输入；工具参数只引用键名，值不会出现在任何返回结构里",
    )
    mcp_parser.add_argument(
        "--input-file",
        type=Path,
        help="从 JSON 文件读取任务输入，避免密钥出现在进程命令行里",
    )
    mcp_parser.add_argument("--project-id", default="mcp", help="profile 隔离用的项目 ID")
    mcp_parser.add_argument(
        "--respect-robots",
        action="store_true",
        help="遵守目标站点 robots.txt：被禁止的地址拒绝导航，并按站点声明的 Crawl-delay 限速",
    )
    mcp_parser.add_argument(
        "--min-interval-ms",
        type=float,
        default=0.0,
        metavar="MS",
        help="同一主机两次导航之间的最小间隔毫秒数，默认 0 不限速",
    )
    mcp_parser.add_argument(
        "--allow-visual-actions",
        action="store_true",
        help="授权按截图比例坐标的视觉动作；默认关闭",
    )
    mcp_parser.add_argument(
        "--read-only",
        action="store_true",
        help="启用生产只读硬门控；副作用浏览器、存储和接口重放工具直接拒绝",
    )
    return parser


def _parse_inputs(pairs: list[str], input_file: Path | None) -> dict[str, str]:
    inputs: dict[str, str] = {}
    if input_file is not None:
        try:
            payload = json.loads(input_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"读取任务输入文件失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("任务输入文件必须是 JSON 对象")
        for key, value in payload.items():
            inputs[str(key)] = str(value)
    for item in pairs:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ConfigurationError(f"任务输入必须写成 KEY=VALUE：{item}")
        inputs[key.strip()] = value
    return inputs


def _mcp(args: argparse.Namespace) -> int:
    """在 stdio 上运行 MCP 服务端；日志走 stderr，stdout 只允许协议消息。"""

    from witty_browser_auto.mcp_server import McpServer, ToolkitSession, run_stdio_server

    config = load_app_config()
    # stdout 是协议通道，日志必须走 stderr，否则会污染 JSON-RPC 分帧。
    configure_logging(config.runtime.log_level, stream=sys.stderr)
    session = ToolkitSession(
        config=config,
        inputs=_parse_inputs(args.input, args.input_file),
        allowed_origins=tuple(args.allow_origin),
        project_id=args.project_id,
        allow_visual_actions=args.allow_visual_actions,
        respect_robots=args.respect_robots,
        min_request_interval_ms=args.min_interval_ms,
        read_only=args.read_only,
    )
    try:
        definitions = profile_definitions(
            args.profile,
            categories=tuple(args.category),
            extra_tools=tuple(args.tool),
        )
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    server = McpServer(session=session, definitions=definitions)
    asyncio.run(run_stdio_server(server))
    return 0


def _check_writable_dir(path: Path, label: str) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".witty-browser-auto-doctor-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"路径": str(path), "可写": True}
    except OSError as exc:
        return {"路径": str(path), "可写": False, "错误": str(exc), "标签": label}


def _doctor() -> int:
    """检查浏览器路径、存储可写性，以及可选的本机 CDP 端点格式。"""

    report: dict[str, Any] = {
        "Python要求": ">=3.11",
        "浏览器内核": "自主异步 CDP",
        "禁止运行时": ["Playwright", "Selenium", "DrissionPage"],
    }
    exit_code = 0
    try:
        config = load_app_config()
        configure_logging(config.runtime.log_level)

        if config.browser.cdp_endpoint:
            ensure_loopback_endpoint(config.browser.cdp_endpoint)
            report["浏览器"] = {
                "模式": "显式接管",
                "地址": config.browser.cdp_endpoint,
                "回环检查": "通过",
            }
        else:
            executable = ChromiumLauncher(config.browser).find_executable()
            report["浏览器"] = {
                "模式": "受管启动",
                "可执行文件": str(executable),
                "存在且可执行": True,
            }

        storage_checks = {
            "profile根目录": _check_writable_dir(config.browser.profile_root, "profile"),
            "记忆数据库目录": _check_writable_dir(config.storage.memory_database.parent, "memory"),
            "诊断产物目录": _check_writable_dir(config.storage.artifact_root, "artifacts"),
        }
        report["存储"] = storage_checks
        if any(not item.get("可写") for item in storage_checks.values()):
            exit_code = 1
            report["结果"] = "失败"
            report["错误"] = "部分存储目录不可写"
        else:
            config.prepare_directories()
            report["结果"] = "通过"
    except (ConfigurationError, RpaError, OSError) as exc:
        report["结果"] = "失败"
        report["错误"] = str(exc)
        exit_code = 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


def main(argv: list[str] | None = None) -> None:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "version":
            from witty_browser_auto import __version__

            print(__version__)
            raise SystemExit(0)
        if args.command == "doctor":
            raise SystemExit(_doctor())
        if args.command == "mcp":
            raise SystemExit(_mcp(args))
        raise ConfigurationError(f"未知命令：{args.command}")
    except KeyboardInterrupt:
        print("\nWitty 浏览器工具库已停止", file=sys.stderr)
        raise SystemExit(130) from None
    except ConfigurationError as exc:
        print(json.dumps({"错误": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
