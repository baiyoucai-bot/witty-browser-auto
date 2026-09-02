"""元素只读读取：按固定浏览器模板返回白名单语义、状态与内容字段。

调用方和执行模型都不能提供 JavaScript；本模块持有唯一的页面执行模板，只用参数
控制文本上限和是否返回结构片段。密码类控件永远只返回长度，不返回明文。
"""

from __future__ import annotations

from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession

MAX_TEXT_LENGTH = 20000
MAX_HTML_LENGTH = 20000
MAX_OPTIONS = 100

# 只返回定位、语义和表单语义需要的属性，避免整节点属性表把页面数据整体带出。
_ATTRIBUTE_WHITELIST: tuple[str, ...] = (
    "id",
    "name",
    "type",
    "class",
    "role",
    "href",
    "src",
    "target",
    "alt",
    "title",
    "placeholder",
    "value",
    "for",
    "action",
    "method",
    "maxlength",
    "min",
    "max",
    "step",
    "pattern",
    "disabled",
    "readonly",
    "required",
    "checked",
    "selected",
    "multiple",
    "data-testid",
    "aria-label",
    "aria-labelledby",
    "aria-describedby",
    "aria-disabled",
    "aria-expanded",
    "aria-checked",
    "aria-selected",
    "aria-hidden",
    "aria-current",
)

_ELEMENT_STATE_SCRIPT = r"""
function(options) {
  const element = this;
  const normalize = (input) => String(input == null ? '' : input).replace(/\s+/g, ' ').trim();
  const clamp = (input, limit) => {
    const text = String(input == null ? '' : input);
    return text.length > limit ? text.slice(0, limit) : text;
  };
  const maxText = Math.max(0, Math.min(Number(options.maxText) || 0, options.textCeiling));
  const tag = element.tagName ? element.tagName.toLowerCase() : '';
  const type = normalize(element.getAttribute && element.getAttribute('type')).toLowerCase();
  const isPassword = tag === 'input' && type === 'password';

  let role = normalize(element.getAttribute && element.getAttribute('role')).toLowerCase();
  if (!role) {
    if (tag === 'a' && element.hasAttribute('href')) role = 'link';
    else if (tag === 'button' || tag === 'summary') role = 'button';
    else if (tag === 'select') role = 'combobox';
    else if (tag === 'textarea') role = 'textbox';
    else if (tag === 'option') role = 'option';
    else if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) role = 'button';
    else if (tag === 'input' && type === 'checkbox') role = 'checkbox';
    else if (tag === 'input' && type === 'radio') role = 'radio';
    else if (tag === 'input' && type === 'range') role = 'slider';
    else if (tag === 'input' && type !== 'hidden') role = 'textbox';
  }

  const labels = element.labels
    ? Array.from(element.labels).map((item) => item.textContent || '').join(' ')
    : '';
  const rawText = normalize(element.innerText || element.textContent);
  const name = normalize(
    (element.getAttribute && element.getAttribute('aria-label')) || labels ||
    (element.getAttribute && element.getAttribute('placeholder')) ||
    (element.getAttribute && element.getAttribute('title')) ||
    (element.getAttribute && element.getAttribute('alt')) || rawText ||
    (element.getAttribute && element.getAttribute('name')) || element.id
  );

  const attributes = {};
  for (const key of options.attributeWhitelist) {
    if (element.getAttribute && element.hasAttribute(key)) {
      attributes[key] = clamp(element.getAttribute(key), 1024);
    }
  }
  if (isPassword) {
    delete attributes.value;
  }

  const rect = element.getBoundingClientRect ? element.getBoundingClientRect() : null;
  const style = element.ownerDocument && element.ownerDocument.defaultView
    ? element.ownerDocument.defaultView.getComputedStyle(element) : null;
  const visible = !!rect && rect.width > 0 && rect.height > 0 && !!style &&
    style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) !== 0;
  const viewportWidth = (element.ownerDocument && element.ownerDocument.documentElement)
    ? element.ownerDocument.documentElement.clientWidth : 0;
  const viewportHeight = (element.ownerDocument && element.ownerDocument.documentElement)
    ? element.ownerDocument.documentElement.clientHeight : 0;

  const result = {
    tag,
    role,
    name: clamp(name, 500),
    text: clamp(rawText, maxText),
    text_truncated: rawText.length > maxText,
    text_length: rawText.length,
    visible,
    in_viewport: !!rect && visible && rect.bottom > 0 && rect.right > 0 &&
      rect.top < viewportHeight && rect.left < viewportWidth,
    disabled: !!(element.disabled) ||
      (element.getAttribute && element.getAttribute('aria-disabled') === 'true'),
    child_element_count: element.childElementCount || 0,
    attributes
  };

  if (rect) {
    result.box = {
      x: Math.round(rect.x * 100) / 100,
      y: Math.round(rect.y * 100) / 100,
      width: Math.round(rect.width * 100) / 100,
      height: Math.round(rect.height * 100) / 100
    };
  }

  if ('value' in element && typeof element.value === 'string') {
    result.value_length = element.value.length;
    // 密码控件永远只暴露长度，避免读取工具成为凭据旁路。
    result.value = isPassword ? null : clamp(element.value, maxText);
    result.value_masked = isPassword;
  }
  if (typeof element.checked === 'boolean') result.checked = element.checked;
  if (typeof element.selected === 'boolean') result.selected = element.selected;
  if (typeof element.readOnly === 'boolean') result.read_only = element.readOnly;
  if (typeof element.required === 'boolean') result.required = element.required;

  if (tag === 'select' && element.options) {
    const options_list = Array.from(element.options).slice(0, options.maxOptions);
    result.options = options_list.map((item) => ({
      label: clamp(normalize(item.textContent), 200),
      value: clamp(item.value, 200),
      selected: !!item.selected
    }));
    result.option_count = element.options.length;
  }

  if (options.includeHtml) {
    result.outer_html = clamp(element.outerHTML || '', options.htmlCeiling);
    result.outer_html_truncated = String(element.outerHTML || '').length > options.htmlCeiling;
  }
  return result;
}
"""


async def read_element_state(
    session: CdpTargetSession,
    object_id: str,
    *,
    max_text_length: int = 2000,
    include_html: bool = False,
) -> dict[str, Any]:
    """读取单个元素的白名单状态；页面执行模板固定，不接受调用方脚本。"""

    if max_text_length < 0 or max_text_length > MAX_TEXT_LENGTH:
        raise ValueError(f"文本上限必须在 0 到 {MAX_TEXT_LENGTH} 之间")
    result = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _ELEMENT_STATE_SCRIPT,
            "returnByValue": True,
            "arguments": [
                {
                    "value": {
                        "maxText": max_text_length,
                        "textCeiling": MAX_TEXT_LENGTH,
                        "htmlCeiling": MAX_HTML_LENGTH,
                        "maxOptions": MAX_OPTIONS,
                        "includeHtml": include_html,
                        "attributeWhitelist": list(_ATTRIBUTE_WHITELIST),
                    }
                }
            ],
        },
    )
    state = result.get("result", {}).get("value")
    if not isinstance(state, dict):
        raise ValueError("浏览器没有返回可用的元素状态")
    return state
