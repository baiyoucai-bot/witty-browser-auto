"""表单字段的批量写入与逐字段回读校验。

真实 Chrome 探测确认了两件事，决定了这里的做法：
- 同一次观察拿到的多个 `target_id` 在连续写入之后仍然有效，所以批量填写可以在一次
  观察里跑完，不必逐字段重新观察。
- 填表单**不改变页面指纹**，`fingerprint_changed` 这类页面级后置条件对 select 与复选框
  必然判失败——探测里两者在页面上明明写成功了，工具却报"页面状态尚未变化"。因此这里
  一律按字段回读真实值校验，和 `input_text` 保持同一条判据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from witty_browser_auto.domain.models import LocatorRecipe

FIELD_KINDS: tuple[str, ...] = ("text", "select", "checkbox")

MAX_FIELDS = 30
MAX_TEXT_LENGTH = 2000


class FormSession(Protocol):
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FormField:
    """一个待写入的表单字段。"""

    index: int
    kind: str
    value: str = ""
    checked: bool = False
    target_id: str | None = None
    locator: LocatorRecipe | None = None
    # 值可能来自任务输入，报告里只说来源不回显内容。
    sensitive: bool = False

    def describe(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"index": self.index, "kind": self.kind}
        if self.target_id:
            payload["target_id"] = self.target_id
        if self.locator is not None:
            payload["locator"] = f"{self.locator.strategy}={self.locator.value}"
        return payload


_CLEAR_SCRIPT = (
    "function(){if('value' in this){this.value='';"
    "this.dispatchEvent(new Event('input',{bubbles:true}));}}"
)
_TEXT_MATCH_SCRIPT = (
    "function(expected){return 'value' in this&&String(this.value)===String(expected);}"
)
# 选项按 value、label 或可见文本三者任一命中；调用方拿到的往往是屏幕上看到的文字。
_SELECT_SCRIPT = """
function(wanted) {
  const options = Array.from(this.options || []);
  if (!options.length) return {ok: false, reason: 'not_a_select'};
  const match = options.find((option) =>
    option.value === wanted ||
    option.label === wanted ||
    (option.text || '').trim() === wanted
  );
  if (!match) {
    return {
      ok: false,
      reason: 'option_missing',
      available: options.slice(0, 20).map((option) => (option.text || '').trim()),
    };
  }
  this.value = match.value;
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: this.value === match.value, value: this.value, text: (match.text || '').trim()};
}
"""
# 用 click() 而不是直接改 checked，否则框架的受控状态不会跟着更新。
_CHECKBOX_SCRIPT = """
function(desired) {
  if (typeof this.checked !== 'boolean') return {ok: false, reason: 'not_checkable'};
  if (this.disabled) return {ok: false, reason: 'disabled'};
  if (this.checked !== desired) this.click();
  return {ok: this.checked === desired, checked: this.checked};
}
"""


async def apply_field(
    session: FormSession,
    page_session: FormSession,
    object_id: str,
    field: FormField,
) -> dict[str, Any]:
    """写入单个字段并回读校验；失败只描述原因，不回显敏感取值。"""

    if field.kind == "text":
        return await _apply_text(session, page_session, object_id, field)
    if field.kind == "select":
        return await _apply_select(session, object_id, field)
    if field.kind == "checkbox":
        return await _apply_checkbox(session, object_id, field)
    raise ValueError(f"不支持的字段类型：{field.kind}")


async def _apply_text(
    session: FormSession,
    page_session: FormSession,
    object_id: str,
    field: FormField,
) -> dict[str, Any]:
    await _focus(session, object_id)
    await session.call(
        "Runtime.callFunctionOn",
        {"objectId": object_id, "functionDeclaration": _CLEAR_SCRIPT, "returnByValue": True},
    )
    # 走真实按键通道而不是直接赋值，受控组件才会更新自己的状态。
    await page_session.call("Input.insertText", {"text": field.value})
    matched = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _TEXT_MATCH_SCRIPT,
            "arguments": [{"value": field.value}],
            "returnByValue": True,
        },
    )
    if matched.get("result", {}).get("value") is True:
        return {
            **field.describe(),
            "filled": True,
            "value_source": "任务输入" if field.sensitive else "字面量",
            "length": len(field.value),
        }
    return {
        **field.describe(),
        "filled": False,
        "reason": "文本已发送，但输入框回读与预期不一致",
    }


async def _apply_select(
    session: FormSession,
    object_id: str,
    field: FormField,
) -> dict[str, Any]:
    result = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _SELECT_SCRIPT,
            "arguments": [{"value": field.value}],
            "returnByValue": True,
        },
    )
    payload = result.get("result", {}).get("value")
    if not isinstance(payload, dict):
        return {**field.describe(), "filled": False, "reason": "下拉框未返回可用结果"}
    if payload.get("ok") is True:
        return {
            **field.describe(),
            "filled": True,
            "selected_value": payload.get("value"),
            "selected_text": payload.get("text"),
        }
    reason = payload.get("reason")
    if reason == "option_missing":
        available = payload.get("available") or []
        return {
            **field.describe(),
            "filled": False,
            "reason": f"下拉框没有匹配 {field.value!r} 的选项",
            "available_options": available,
        }
    if reason == "not_a_select":
        return {**field.describe(), "filled": False, "reason": "目标不是下拉框"}
    return {**field.describe(), "filled": False, "reason": "下拉框赋值后回读不一致"}


async def _apply_checkbox(
    session: FormSession,
    object_id: str,
    field: FormField,
) -> dict[str, Any]:
    result = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _CHECKBOX_SCRIPT,
            "arguments": [{"value": field.checked}],
            "returnByValue": True,
        },
    )
    payload = result.get("result", {}).get("value")
    if not isinstance(payload, dict):
        return {**field.describe(), "filled": False, "reason": "勾选框未返回可用结果"}
    if payload.get("ok") is True:
        return {**field.describe(), "filled": True, "checked": payload.get("checked")}
    reason = payload.get("reason")
    messages = {
        "not_checkable": "目标不是可勾选控件",
        "disabled": "勾选框处于禁用状态",
    }
    return {
        **field.describe(),
        "filled": False,
        "reason": messages.get(str(reason), "勾选后回读状态与预期不一致"),
    }


async def _focus(session: FormSession, object_id: str) -> None:
    await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": "function(){this.focus();return document.activeElement===this;}",
            "returnByValue": True,
        },
    )
