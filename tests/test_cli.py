from __future__ import annotations

import json
from pathlib import Path

import pytest

from witty_browser_auto import __version__
from witty_browser_auto.cli import build_parser, main
from witty_browser_auto.config import AppConfig, BrowserConfig, StorageConfig


def test_cli_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_cli_parser_only_exposes_version_and_doctor() -> None:
    parser = build_parser()

    assert parser.parse_args(["doctor"]).command == "doctor"
    assert parser.parse_args(["version"]).command == "version"
    with pytest.raises(SystemExit):
        parser.parse_args(["run"])


def test_mcp_parser_exposes_read_only_hard_gate() -> None:
    args = build_parser().parse_args(["mcp", "--read-only"])

    assert args.read_only is True


def test_cli_doctor_checks_browser_and_storage_without_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = AppConfig(
        browser=BrowserConfig(headless=True, profile_root=tmp_path / "profiles"),
        storage=StorageConfig(
            memory_database=tmp_path / "memory.db",
            artifact_root=tmp_path / "artifacts",
        ),
    )

    class FakeLauncher:
        def __init__(self, browser_config: object) -> None:
            self.browser_config = browser_config

        def find_executable(self) -> Path:
            return tmp_path / "fake-chrome"

    chrome = tmp_path / "fake-chrome"
    chrome.write_text("", encoding="utf-8")
    chrome.chmod(0o755)

    monkeypatch.setattr("witty_browser_auto.cli.load_app_config", lambda: config)
    monkeypatch.setattr("witty_browser_auto.cli.ChromiumLauncher", FakeLauncher)

    with pytest.raises(SystemExit) as exited:
        main(["doctor"])

    payload = json.loads(capsys.readouterr().out)
    assert exited.value.code == 0
    assert payload["结果"] == "通过"
    assert "模型" not in payload
    assert "model" not in json.dumps(payload).lower()
    assert payload["浏览器"]["模式"] == "受管启动"
    assert payload["存储"]["profile根目录"]["可写"] is True
    assert payload["存储"]["记忆数据库目录"]["可写"] is True
    assert payload["存储"]["诊断产物目录"]["可写"] is True
