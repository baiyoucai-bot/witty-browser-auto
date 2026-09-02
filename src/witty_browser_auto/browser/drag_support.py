"""原生业务滑块校准与视觉拖拽的脱敏诊断。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.domain.models import ActionCommand, BoundingBox, CandidateTarget

logger = logging.getLogger(__name__)

_NATIVE_RANGE_SET_SCRIPT = r"""
function(positionRatio) {
  if (!(this instanceof HTMLInputElement) || this.type !== 'range' || this.disabled) {
    return {ok: false, reason: '目标不是可用的原生范围控件'};
  }
  const minimum = Number(this.min || 0);
  const maximum = Number(this.max || 100);
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum) || maximum <= minimum) {
    return {ok: false, reason: '原生范围控件的数值边界无效'};
  }
  const rect = this.getBoundingClientRect();
  const horizontal = rect.width >= rect.height;
  const direction = getComputedStyle(this).direction;
  let normalizedRatio = Math.min(1, Math.max(0, Number(positionRatio)));
  if (horizontal && direction === 'rtl') normalizedRatio = 1 - normalizedRatio;

  const stepText = this.getAttribute('step');
  const parsedStep = stepText && stepText !== 'any' ? Number(stepText) : 1;
  const step = stepText === 'any'
    ? null
    : (Number.isFinite(parsedStep) && parsedStep > 0 ? parsedStep : 1);
  let target = minimum + normalizedRatio * (maximum - minimum);
  if (step !== null) target = minimum + Math.round((target - minimum) / step) * step;
  target = Math.min(maximum, Math.max(minimum, target));

  const previousValue = this.value;
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  if (!descriptor || typeof descriptor.set !== 'function') {
    return {ok: false, reason: '浏览器缺少原生 value setter'};
  }
  descriptor.set.call(this, String(target));
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  const actualValue = this.value;
  const actualNumber = Number(actualValue);
  const tolerance = step === null ? 1e-9 : Math.max(1e-9, step / 1000);
  return {
    ok: Number.isFinite(actualNumber) && Math.abs(actualNumber - target) <= tolerance,
    previousValue,
    actualValue,
    minimum,
    maximum,
    step: step === null ? 'any' : step,
    targetRatio: normalizedRatio,
  };
}
"""


def is_native_range(candidate: CandidateTarget) -> bool:
    if not candidate.recipe.value:
        return False
    try:
        locator = json.loads(candidate.recipe.value)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(locator, dict):
        return False
    attributes = locator.get("attrs")
    return (
        locator.get("tag") == "input"
        and isinstance(attributes, dict)
        and attributes.get("type") == "range"
    )


async def set_native_range_from_drag(
    session: CdpTargetSession,
    box: BoundingBox,
    object_id: str,
    command: ActionCommand,
) -> dict[str, Any]:
    endpoint = command.trajectory[-1]
    if box.width >= box.height:
        target_ratio = 0.5 + endpoint.dx / box.width
    else:
        # 浏览器坐标向下递增，竖向范围控件通常从底部最小值走向顶部最大值。
        target_ratio = 1 - (0.5 + endpoint.dy / box.height)
    target_ratio = min(1.0, max(0.0, target_ratio))
    result = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _NATIVE_RANGE_SET_SCRIPT,
            "arguments": [{"value": target_ratio}],
            "returnByValue": True,
        },
    )
    value = result.get("result", {}).get("value")
    if not isinstance(value, dict):
        raise RuntimeError("浏览器未返回原生范围控件的回读结果")
    if value.get("ok") is not True:
        reason = value.get("reason")
        detail = reason if isinstance(reason, str) and reason else "设置后回读值不匹配"
        raise RuntimeError(f"原生范围控件设置失败：{detail}")
    return {
        "执行方式": "native_range",
        "原值": str(value.get("previousValue", "")),
        "回读值": str(value.get("actualValue", "")),
        "最小值": value.get("minimum"),
        "最大值": value.get("maximum"),
        "步长": value.get("step"),
        "目标比例": value.get("targetRatio"),
    }


async def visual_point_diagnostic(
    session: CdpTargetSession,
    x: float,
    y: float,
) -> dict[str, Any]:
    expression = f"""
    (() => {{
      const element = document.elementFromPoint({json.dumps(x)}, {json.dumps(y)});
      if (!element) return {{hit: false}};
      const style = getComputedStyle(element);
      return {{
        hit: true,
        tag: (element.tagName || '').toLowerCase(),
        role: element.getAttribute('role') || '',
        cursor: style.cursor || '',
        pointerEvents: style.pointerEvents || '',
        childFrame: (element.tagName || '').toLowerCase() === 'iframe',
      }};
    }})()
    """
    try:
        result = await session.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
    except Exception:
        logger.info("视觉拖拽命中诊断不可用", exc_info=True)
        return {"诊断可用": False, "命中": False}
    result_value = result.get("result", {})
    if not isinstance(result_value, dict) or "value" not in result_value:
        return {"诊断可用": False, "命中": False}
    value = result_value.get("value")
    if not isinstance(value, dict) or value.get("hit") is not True:
        return {"诊断可用": True, "命中": False}
    return {
        "诊断可用": True,
        "命中": True,
        "标签": str(value.get("tag", ""))[:32],
        "角色": str(value.get("role", ""))[:64],
        "光标": str(value.get("cursor", ""))[:32],
        "指针事件": str(value.get("pointerEvents", ""))[:32],
        "子框架": value.get("childFrame") is True,
    }


async def visual_pixels_changed(
    session: CdpTargetSession,
    before_fingerprint: str,
) -> bool | None:
    try:
        result = await session.call(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
        )
        encoded = result.get("data")
        if not isinstance(encoded, str):
            raise RuntimeError("浏览器未返回截图数据")
        after = base64.b64decode(encoded)
    except Exception:
        logger.info("视觉拖拽已完成，但拖后像素诊断不可用", exc_info=True)
        return None
    return hashlib.sha256(after).hexdigest() != before_fingerprint
