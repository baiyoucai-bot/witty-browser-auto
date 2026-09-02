from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_local_config_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """测试不得意外读取开发者工作区中的真实模型地址或密钥。"""

    monkeypatch.setenv("WITTY_BROWSER_AUTO_CONFIG_FILE", str(tmp_path / "config.json"))
