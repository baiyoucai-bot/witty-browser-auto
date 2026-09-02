"""受控键盘输入：把白名单按键名编译为原生 CDP 键事件序列。

调用方只能提交白名单键名和修饰键，不能提供原始键码、字符串脚本或任意文本。
键名到 `key`/`code`/虚拟键码的映射固定在本模块内，是浏览器键事件的唯一来源。
普通文本仍由 `input_text` 走 `Input.insertText`，本模块只负责功能键与组合键。
"""

from __future__ import annotations

from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession

# 键名 -> (key, code, 虚拟键码, 可提交文本)。文本为空表示该键不产生字符输入。
_NAMED_KEYS: dict[str, tuple[str, str, int, str]] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "numpad_enter": ("Enter", "NumpadEnter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, "\t"),
    "escape": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""),
    "space": (" ", "Space", 32, " "),
    "arrow_up": ("ArrowUp", "ArrowUp", 38, ""),
    "arrow_down": ("ArrowDown", "ArrowDown", 40, ""),
    "arrow_left": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrow_right": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""),
    "page_up": ("PageUp", "PageUp", 33, ""),
    "page_down": ("PageDown", "PageDown", 34, ""),
    "insert": ("Insert", "Insert", 45, ""),
}

# 修饰键位掩码按 Chromium `Input.dispatchKeyEvent` 的约定固定。
_MODIFIER_BITS: dict[str, int] = {"alt": 1, "control": 2, "meta": 4, "shift": 8}

_LETTERS = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"


def supported_key_names() -> tuple[str, ...]:
    """返回全部可用键名，供工具契约与错误提示保持同一份事实。"""

    letters = tuple(_LETTERS)
    digits = tuple(_DIGITS)
    functions = tuple(f"f{index}" for index in range(1, 13))
    return tuple(sorted((*_NAMED_KEYS, *letters, *digits, *functions)))


def supported_modifier_names() -> tuple[str, ...]:
    return tuple(sorted(_MODIFIER_BITS))


def resolve_key(key_name: str, modifiers: tuple[str, ...]) -> dict[str, Any]:
    """把键名与修饰键编译为确定性键事件参数；非法组合在派发前拒绝。"""

    normalized = key_name.strip().lower()
    if not normalized:
        raise ValueError("按键名不能为空")
    unknown = [item for item in modifiers if item not in _MODIFIER_BITS]
    if unknown:
        allowed = "、".join(supported_modifier_names())
        raise ValueError(f"不支持的修饰键：{'、'.join(unknown)}；可用修饰键为 {allowed}")
    if len(set(modifiers)) != len(modifiers):
        raise ValueError("修饰键不能重复")
    mask = 0
    for item in modifiers:
        mask |= _MODIFIER_BITS[item]

    if normalized in _NAMED_KEYS:
        key, code, virtual_key_code, text = _NAMED_KEYS[normalized]
    elif normalized in _LETTERS:
        key, code, virtual_key_code = normalized, f"Key{normalized.upper()}", ord(normalized) - 32
        text = normalized
    elif normalized in _DIGITS:
        key, code, virtual_key_code, text = normalized, f"Digit{normalized}", ord(normalized), ""
    elif normalized.startswith("f") and normalized[1:].isdigit() and 1 <= int(normalized[1:]) <= 12:
        index = int(normalized[1:])
        key, code, virtual_key_code, text = f"F{index}", f"F{index}", 111 + index, ""
    else:
        raise ValueError(f"不支持的按键名：{key_name}；请使用工具契约中列出的键名")

    # 字母与数字单独按下等同普通文本输入，应使用 input_text 以便回读校验。
    if not mask and (normalized in _LETTERS or normalized in _DIGITS):
        raise ValueError("字母和数字键必须配合修饰键使用；普通文本请使用 input_text 系列工具")
    # 控制类修饰键会改变按键语义，此时不再提交字符文本。
    if mask & (_MODIFIER_BITS["control"] | _MODIFIER_BITS["meta"] | _MODIFIER_BITS["alt"]):
        text = ""
    return {
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": virtual_key_code,
        "nativeVirtualKeyCode": virtual_key_code,
        "modifiers": mask,
        "text": text,
    }


async def dispatch_key_press(
    session: CdpTargetSession,
    resolved: dict[str, Any],
    *,
    repeat: int = 1,
) -> dict[str, Any]:
    """按已编译参数派发成对的按下与抬起事件，返回不含业务内容的动作审计。"""

    if repeat < 1 or repeat > 20:
        raise ValueError("按键重复次数必须在 1 到 20 之间")
    text = str(resolved.get("text", ""))
    down_type = "keyDown" if text else "rawKeyDown"
    base = {
        "key": resolved["key"],
        "code": resolved["code"],
        "windowsVirtualKeyCode": resolved["windowsVirtualKeyCode"],
        "nativeVirtualKeyCode": resolved["nativeVirtualKeyCode"],
        "modifiers": resolved["modifiers"],
    }
    for _ in range(repeat):
        down = dict(base, type=down_type)
        if text:
            down["text"] = text
            down["unmodifiedText"] = text
        await session.call("Input.dispatchKeyEvent", down)
        await session.call("Input.dispatchKeyEvent", dict(base, type="keyUp"))
    return {
        "key": resolved["key"],
        "code": resolved["code"],
        "modifier_mask": resolved["modifiers"],
        "repeat": repeat,
        "submits_text": bool(text),
    }
