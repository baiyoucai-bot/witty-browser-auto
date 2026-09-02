"""把 use-browser-toolkit 技能文档钉在真实工具契约上。

技能包是外部智能体写代码时唯一的入口说明，文档漂移等同于工具不可调用：示例里的方法名、
参数名、工具数量一旦和注册表对不上，外部调用方只会得到 `ToolArgumentError`。这些检查让
新增或改名工具时必须同步改文档，而不是等调用方踩坑。
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from witty_browser_auto.toolkit import BrowserToolkit
from witty_browser_auto.toolkit.catalog import BROWSER_TOOLS

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "use-browser-toolkit"
SKILL_PATH = SKILL_DIR / "SKILL.md"
REFERENCES_DIR = SKILL_DIR / "references"
_FENCE = re.compile(r"^```(\w*)\s*$")
# 文档里描述工具规模的句子，数量必须由注册表反推而不是靠人肉维护。
_EXTERNAL_COUNT = re.compile(r"本项目的 (\d+) 个浏览器工具")
_TOTAL_COUNT = re.compile(r"全部 (\d+) 个工具的可读契约，其中 (\d+) 个仅引擎可用")


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _reference_paths() -> list[Path]:
    return sorted(REFERENCES_DIR.glob("*.md"))


def _all_doc_paths() -> list[Path]:
    return [SKILL_PATH, *_reference_paths()]


def _code_blocks(text: str) -> list[tuple[str, str]]:
    """返回 (语言, 代码) 列表，代码块未闭合时直接失败而不是静默吞掉。"""

    blocks: list[tuple[str, str]] = []
    language: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence is None:
            if language is not None:
                buffer.append(line)
            continue
        if language is None:
            language = fence.group(1)
            buffer = []
            continue
        blocks.append((language, "\n".join(buffer)))
        language = None
    assert language is None, "SKILL.md 存在未闭合的代码块"
    return blocks


def _toolkit_calls(code: str) -> list[tuple[str, int, list[str]]]:
    """抽取 `toolkit.<方法>(...)` 调用，返回方法名、位置参数个数和关键字参数名。"""

    # 文档片段大多是裸 await 语句，包一层协程才能通过语法解析。
    indented = "\n".join(f"    {line}" for line in code.splitlines())
    module = ast.parse(f"async def _doc_snippet():\n{indented}")
    calls: list[tuple[str, int, list[str]]] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "toolkit":
            continue
        keywords = [kw.arg for kw in node.keywords if kw.arg is not None]
        calls.append((func.attr, len(node.args), keywords))
    return calls


def test_every_externally_callable_tool_has_facade_method() -> None:
    """注册表开放的工具必须能在门面上直接调用，否则外部只能自己拼参数字典。"""

    missing = [
        definition.name
        for definition in BROWSER_TOOLS.externally_callable()
        if not callable(getattr(BrowserToolkit, definition.name, None))
    ]
    assert not missing, f"以下工具缺少门面方法：{'、'.join(missing)}"


def test_engine_only_tools_are_not_exposed_on_facade() -> None:
    """终态与等待语义属于智能体循环，暴露成门面方法会让外部误以为可以直接调用。"""

    leaked = [
        definition.name
        for definition in BROWSER_TOOLS
        if not definition.externally_callable and hasattr(BrowserToolkit, definition.name)
    ]
    assert not leaked, f"仅引擎可用的工具不应出现在门面：{'、'.join(leaked)}"


def test_skill_tool_counts_match_registry() -> None:
    text = _skill_text()
    external = BROWSER_TOOLS.externally_callable()
    engine_only = len(BROWSER_TOOLS) - len(external)

    external_match = _EXTERNAL_COUNT.search(text)
    assert external_match is not None, "SKILL.md 缺少对外可调用工具数量的说明"
    assert int(external_match.group(1)) == len(external)

    total_match = _TOTAL_COUNT.search(text)
    assert total_match is not None, "SKILL.md 缺少全部工具数量的说明"
    assert int(total_match.group(1)) == len(BROWSER_TOOLS)
    assert int(total_match.group(2)) == engine_only


def test_skill_documents_every_externally_callable_tool() -> None:
    """新增工具必须写进主文档速查表，否则外部智能体没有任何途径知道它存在。"""

    text = _skill_text()
    undocumented = [
        definition.name
        for definition in BROWSER_TOOLS.externally_callable()
        if definition.name not in text
    ]
    assert not undocumented, f"以下工具未写入技能文档：{'、'.join(undocumented)}"


def test_skill_body_stays_within_progressive_disclosure_budget() -> None:
    """SKILL.md 是触发后整体载入上下文的正文，超过 500 行应拆进 references/。"""

    line_count = len(_skill_text().splitlines())
    assert line_count <= 500, f"SKILL.md 已达 {line_count} 行，把深内容拆进 references/"


def test_skill_reference_pointers_are_bidirectional() -> None:
    """主文档指到的参考文件必须存在；存在的参考文件必须被主文档指到，否则永远不会被读。"""

    text = _skill_text()
    mentioned = set(re.findall(r"references/([\w-]+\.md)", text))
    existing = {path.name for path in _reference_paths()}
    assert mentioned == existing, f"指引与文件不一致：文档提到 {mentioned}，实际存在 {existing}"
    assert existing, "技能包缺少 references/ 参考文件"


def test_skill_code_blocks_are_non_empty_python() -> None:
    for path in _all_doc_paths():
        blocks = _code_blocks(path.read_text(encoding="utf-8"))
        assert blocks, f"{path.name} 没有可执行示例"
        for language, code in blocks:
            assert language == "python", f"{path.name} 只应给出 python 示例，实际出现 {language!r}"
            assert code.strip(), f"{path.name} 存在空代码块"


def test_skill_examples_bind_to_real_facade_signatures() -> None:
    """主文档与全部参考文件逐个示例做签名绑定，防止文档写出并不存在的参数名。"""

    sentinel = object()
    for path in _all_doc_paths():
        for _, code in _code_blocks(path.read_text(encoding="utf-8")):
            for method_name, positional, keywords in _toolkit_calls(code):
                method = getattr(BrowserToolkit, method_name, None)
                assert callable(method), f"{path.name} 引用了不存在的门面方法：{method_name}"
                signature = inspect.signature(method)
                try:
                    signature.bind(
                        sentinel,
                        *[sentinel] * positional,
                        **{name: sentinel for name in keywords},
                    )
                except TypeError as exc:  # pragma: no cover - 仅在文档漂移时触发
                    pytest.fail(f"{path.name} 中 {method_name} 的示例与签名不符：{exc}")


def test_documented_key_names_match_keyboard_whitelist() -> None:
    """按键白名单直接决定 press_key 会不会被拒绝，文档列举的键必须真实存在。"""

    from witty_browser_auto.browser.keyboard import supported_key_names, supported_modifier_names

    text = _skill_text()
    keys = set(supported_key_names())
    for documented in ("enter", "tab", "escape", "space", "page_up", "numpad_enter"):
        assert documented in keys
        assert f"`{documented}`" in text
    for modifier in supported_modifier_names():
        assert f"`{modifier}`" in text
