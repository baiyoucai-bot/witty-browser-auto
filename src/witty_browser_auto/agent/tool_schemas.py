"""大模型可见的核心工具 Schema。

工具声明本身位于 `witty_browser_auto.toolkit.catalog`，本模块只负责派生模型下发格式，
保证执行层、模型 schema 和外部调用方共用同一份契约。
"""

from __future__ import annotations

from typing import Any

from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS

CURRENT_TARGET_REFERENCE = "$current_target"
SUPPORTED_EXPECTED_KINDS = frozenset(
    {"fingerprint_changed", "url_contains", "title_contains", "text_contains", "target_exists"}
)

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = BROWSER_TOOLS.schemas()
