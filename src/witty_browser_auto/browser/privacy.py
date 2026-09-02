"""在截图前用页面内遮罩隐藏任务输入，遮罩失败时禁止上传原图。"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from witty_browser_auto.domain.models import ActionCommand, ActionKind
from witty_browser_auto.domain.protocols import AutomationDriver

logger = logging.getLogger(__name__)

_INSTALL_MASK_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_INSTALL_PRIVACY_MASK */
(() => {
  const values = __VALUES__;
  const token = __TOKEN__;
  const matches = (value) => values.some((item) => item && String(value || '').includes(item));
  const addMask = (element) => {
    if (!(element instanceof Element)) return;
    const box = element.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return;
    const mask = document.createElement('div');
    mask.dataset.wittyPrivacyMask = token;
    Object.assign(mask.style, {
      position: 'fixed',
      left: `${box.left}px`,
      top: `${box.top}px`,
      width: `${box.width}px`,
      height: `${box.height}px`,
      background: '#000',
      zIndex: '2147483647',
      pointerEvents: 'none',
      margin: '0',
      padding: '0',
      border: '0',
    });
    document.documentElement.appendChild(mask);
  };
  const masked = new Set();
  for (const element of Array.from(document.querySelectorAll('input,textarea,select'))) {
    if (matches(element.value)) masked.add(element);
  }
  for (const element of Array.from(document.body?.querySelectorAll('*') || []).slice(0, 5000)) {
    const directText = Array.from(element.childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent || '')
      .join(' ');
    if (matches(directText)) masked.add(element);
  }
  masked.forEach(addMask);
  return {installed: true, count: masked.size};
})()
"""

_REMOVE_MASK_TEMPLATE = r"""
/* WITTY_BROWSER_AUTO_REMOVE_PRIVACY_MASK */
(() => {
  const token = __TOKEN__;
  const selector = `[data-witty-privacy-mask="${CSS.escape(token)}"]`;
  const masks = Array.from(document.querySelectorAll(selector));
  masks.forEach((element) => element.remove());
  return {removed: masks.length};
})()
"""


def _task_input_values(inputs: Mapping[str, Any]) -> tuple[str, ...]:
    values = {str(item) for item in inputs.values() if str(item)}
    if len(values) > 50:
        raise ValueError("截图脱敏最多支持 50 个任务输入值")
    if any(len(item) > 2048 for item in values):
        raise ValueError("截图脱敏的单个任务输入值不能超过 2048 个字符")
    return tuple(sorted(values, key=len, reverse=True))


@asynccontextmanager
async def mask_task_inputs(
    driver: AutomationDriver,
    inputs: Mapping[str, Any],
) -> AsyncIterator[None]:
    """临时安装黑色覆盖层；无法证明遮罩成功时直接失败，不能上传原图。"""

    values = _task_input_values(inputs)
    if not values:
        yield
        return
    if not driver.capabilities.dom or not driver.capabilities.javascript:
        raise RuntimeError("当前自动化表面无法在截图前遮罩任务输入")
    token = uuid.uuid4().hex
    install_script = _INSTALL_MASK_TEMPLATE.replace(
        "__VALUES__",
        json.dumps(values, ensure_ascii=False),
    ).replace("__TOKEN__", json.dumps(token))
    receipt = await driver.execute(
        ActionCommand(
            action_id=f"privacy-mask-{uuid.uuid4().hex}",
            kind=ActionKind.EVALUATE,
            script=install_script,
            idempotent=True,
        )
    )
    if not receipt.success:
        raise RuntimeError("截图前任务输入遮罩安装失败")
    try:
        yield
    finally:
        cleanup_script = _REMOVE_MASK_TEMPLATE.replace("__TOKEN__", json.dumps(token))
        cleanup = await driver.execute(
            ActionCommand(
                action_id=f"privacy-unmask-{uuid.uuid4().hex}",
                kind=ActionKind.EVALUATE,
                script=cleanup_script,
                idempotent=True,
            )
        )
        if not cleanup.success:
            logger.warning("截图后任务输入遮罩清理失败，后续观察会重新检查页面")


async def capture_masked_evidence(
    driver: AutomationDriver,
    inputs: Mapping[str, Any],
    label: str,
) -> Path:
    async with mask_task_inputs(driver, inputs):
        return await driver.capture_evidence(label)
