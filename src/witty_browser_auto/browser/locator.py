"""显式 CSS/XPath/语义定位器的有界解析与可操作性校验。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from witty_browser_auto.browser.frames import FrameHandle
from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.domain.errors import TargetNotFoundError
from witty_browser_auto.domain.models import BoundingBox, CandidateTarget, LocatorRecipe

_MAX_SEARCH_RESULTS = 200

# `this` 是本帧 document；composed 让 shadow DOM 里的节点也能回溯到宿主文档。
_OWN_ELEMENT_SCRIPT = (
    "function(node){ return node.nodeType === 1 && node.getRootNode({composed: true}) === this; }"
)

_SEMANTIC_LOCATOR_SCRIPT = r"""
function(strategy, value, name, exact, index, mode) {
  const root = this;
  const normalize = (input) => String(input || '').replace(/\s+/g, ' ').trim();
  const matches = (actual, expected) => {
    const left = normalize(actual);
    const right = normalize(expected);
    return exact ? left === right : left.includes(right);
  };
  const implicitRole = (element) => {
    const explicit = normalize(element.getAttribute('role')).toLowerCase();
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    const type = normalize(element.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'a' && element.hasAttribute('href')) return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'option') return 'option';
    if (tag !== 'input' || type === 'hidden') return '';
    if (['button', 'submit', 'reset'].includes(type)) return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'range') return 'slider';
    if (type === 'search') return 'searchbox';
    if (type === 'number') return 'spinbutton';
    return 'textbox';
  };
  const accessibleName = (element) => {
    const labelledBy = normalize(element.getAttribute('aria-labelledby'));
    const labelledText = labelledBy
      ? labelledBy.split(/\s+/).map((id) => root.getElementById(id)?.textContent || '')
          .join(' ')
      : '';
    const labels = element.labels
      ? Array.from(element.labels).map((item) => item.textContent || '').join(' ')
      : '';
    return normalize(
      element.getAttribute('aria-label') || labelledText || labels ||
      element.getAttribute('placeholder') || element.getAttribute('title') ||
      element.getAttribute('alt') || element.innerText || element.textContent ||
      element.getAttribute('name') || element.id
    );
  };
  let found = [];
  if (strategy === 'test_id') {
    found = Array.from(root.querySelectorAll('[data-testid]'))
      .filter((element) => matches(element.getAttribute('data-testid'), value));
  } else if (strategy === 'label') {
    const labels = Array.from(root.querySelectorAll('label'))
      .filter((element) => matches(element.innerText || element.textContent, value));
    found = labels.map((label) => label.control ||
      (label.htmlFor ? root.getElementById(label.htmlFor) : null) ||
      label.querySelector('input,textarea,select,button,[contenteditable="true"]'))
      .filter(Boolean);
  } else {
    const elements = Array.from(root.querySelectorAll(
      'a,button,input,textarea,select,option,summary,label,[role],[contenteditable="true"]'
    ));
    if (strategy === 'role') {
      found = elements.filter((element) => implicitRole(element) === value &&
        (!name || matches(accessibleName(element), name)));
    } else if (strategy === 'text') {
      found = elements.filter((element) =>
        matches(element.innerText || element.textContent, value));
    }
  }
  if (mode === 'count') return found.length;
  return found[index] || null;
}
"""

# 同进程 iframe 没有独立会话，DOM.performSearch 无法按帧收敛，只能在帧文档上直接查询。
_FRAME_QUERY_SCRIPT = r"""
function(query, isXPath, index, mode) {
  const root = this;
  let found = [];
  if (isXPath) {
    const snapshot = root.evaluate(query, root, null, 7, null);
    for (let i = 0; i < snapshot.snapshotLength; i += 1) found.push(snapshot.snapshotItem(i));
  } else {
    found = Array.from(root.querySelectorAll(query));
  }
  found = found.filter((node) => node && node.nodeType === 1);
  if (mode === 'count') return found.length;
  return found[index] || null;
}
"""

_TARGET_STATE_SCRIPT = r"""
function() {
  const normalize = (input) => String(input || '').replace(/\s+/g, ' ').trim();
  const tag = this.tagName.toLowerCase();
  const type = normalize(this.getAttribute('type') || 'text').toLowerCase();
  let role = normalize(this.getAttribute('role')).toLowerCase();
  if (!role) {
    if (tag === 'a' && this.hasAttribute('href')) role = 'link';
    else if (tag === 'button' || tag === 'summary') role = 'button';
    else if (tag === 'select') role = 'combobox';
    else if (tag === 'textarea') role = 'textbox';
    else if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) role = 'button';
    else if (tag === 'input' && type === 'checkbox') role = 'checkbox';
    else if (tag === 'input' && type === 'radio') role = 'radio';
    else if (tag === 'input' && type === 'range') role = 'slider';
    else if (tag === 'input') role = 'textbox';
  }
  const labels = this.labels
    ? Array.from(this.labels).map((item) => item.textContent || '').join(' ')
    : '';
  const text = normalize(this.innerText || this.textContent);
  const name = normalize(this.getAttribute('aria-label') || labels ||
    this.getAttribute('placeholder') || this.getAttribute('title') ||
    this.getAttribute('alt') || text || this.getAttribute('name') || this.id);
  const disabled = this.hasAttribute('disabled') || this.getAttribute('aria-disabled') === 'true';
  return {role, name, text, disabled, attrs: {
    href: this.getAttribute('href') || '',
    target: this.getAttribute('target') || '',
    type
  }};
}
"""

_ACTIONABLE_SCRIPT = r"""
function() {
  const element = this;
  const first = element.getBoundingClientRect();
  return new Promise((resolve) => setTimeout(() => {
    const second = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    const stable = Math.abs(first.x - second.x) < 0.5 && Math.abs(first.y - second.y) < 0.5 &&
      Math.abs(first.width - second.width) < 0.5 && Math.abs(first.height - second.height) < 0.5;
    resolve(element.isConnected && stable && second.width > 0 && second.height > 0 &&
      style.visibility !== 'hidden' && style.display !== 'none' && style.pointerEvents !== 'none' &&
      !element.hasAttribute('disabled') && element.getAttribute('aria-disabled') !== 'true');
  }, 50));
}
"""

# 命中判定必须用元素所在帧的局部坐标：iframe 内元素的盒模型是主框架视口坐标，
# 但 elementFromPoint 走的是该帧自己的坐标系，混用会把可点击元素误判为被遮挡。
_HIT_TEST_SCRIPT = (
    "function(){const rect=this.getBoundingClientRect();"
    "const hit=this.ownerDocument.elementFromPoint("
    "rect.left+rect.width/2,rect.top+rect.height/2);"
    "return !!hit&&(hit===this||this.contains(hit));}"
)
_CLIENT_RECT_SCRIPT = (
    "function(){const r=this.getBoundingClientRect();"
    "return {x:r.x,y:r.y,width:r.width,height:r.height};}"
)
_FOCUS_SCRIPT = "function(){this.focus();return document.activeElement===this;}"


async def resolve_explicit_locator(
    frame: FrameHandle,
    recipe: LocatorRecipe,
) -> tuple[CandidateTarget, BoundingBox, str]:
    """解析显式定位器并返回视口坐标下的可操作目标。

    `box` 已换算到主框架视口，可以直接交给 `Input` 域派发；`object_id` 属于
    `frame.session`，后续所有元素级协议调用都必须发到同一个会话。
    """

    session = frame.session
    config = _locator_config(recipe)
    deadline = time.monotonic() + config["timeout_seconds"]
    last_reason = "定位器没有匹配页面元素"
    while time.monotonic() < deadline:
        try:
            object_id = await _resolve_once(frame, recipe.strategy, config)
            box = await _actionable_box(frame, object_id)
            state = await _target_state(session, object_id)
            if state.get("disabled") is True:
                raise TargetNotFoundError("定位器匹配的目标处于禁用状态")
            candidate_recipe = LocatorRecipe(
                strategy=recipe.strategy,
                value=json.dumps({**config, "attrs": state.get("attrs", {})}, ensure_ascii=False),
                role=str(state.get("role", "")),
                name=str(state.get("name", "")),
            )
            candidate = CandidateTarget(
                target_id=f"locator:{recipe.strategy}:{config['index']}",
                role=str(state.get("role", "")),
                name=str(state.get("name", "")) or config["value"],
                text=str(state.get("text", "")),
                confidence=1.0,
                reasons=("显式定位器严格匹配并通过可操作性校验",),
                recipe=candidate_recipe,
                box=box,
            )
            return candidate, box, object_id
        except _RetryLocator as exc:
            last_reason = str(exc)
            await asyncio.sleep(0.1)
    raise TargetNotFoundError(f"显式定位器等待超时：{last_reason}")


async def resolve_locator_object(
    frame: FrameHandle,
    recipe: LocatorRecipe,
) -> str:
    """只读解析显式定位器并返回远程对象 ID。

    读取场景要能看到禁用、被遮挡和滚动区域外的元素，因此这里只做有界等待和唯一性
    校验，不执行可操作性、命中和滚动判断；写动作仍必须走 `resolve_explicit_locator`。
    """

    config = _locator_config(recipe)
    deadline = time.monotonic() + config["timeout_seconds"]
    last_reason = "定位器没有匹配页面元素"
    while time.monotonic() < deadline:
        try:
            return await _resolve_once(frame, recipe.strategy, config)
        except _RetryLocator as exc:
            last_reason = str(exc)
            await asyncio.sleep(0.1)
    raise TargetNotFoundError(f"显式定位器等待超时：{last_reason}")


async def focus_object(session: CdpTargetSession, object_id: str) -> None:
    result = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _FOCUS_SCRIPT,
            "returnByValue": True,
        },
    )
    if result.get("result", {}).get("value") is not True:
        raise TargetNotFoundError("定位器目标无法获得输入焦点")


class _RetryLocator(RuntimeError):
    pass


def _locator_config(recipe: LocatorRecipe) -> dict[str, Any]:
    if not recipe.strategy.startswith("explicit_"):
        raise TargetNotFoundError("显式定位器策略无效")
    try:
        config = json.loads(recipe.value or "{}")
    except json.JSONDecodeError as exc:
        raise TargetNotFoundError("显式定位器配置无效") from exc
    if not isinstance(config, dict):
        raise TargetNotFoundError("显式定位器配置必须是对象")
    return {
        "value": str(config.get("value", "")),
        "name": str(config.get("name", "")),
        "exact": config.get("exact") is True,
        "index": int(config.get("index", 0)),
        "index_explicit": config.get("index_explicit") is True,
        "timeout_seconds": min(max(float(config.get("timeout_seconds", 5)), 0.1), 15.0),
    }


async def _resolve_once(
    frame: FrameHandle,
    strategy_name: str,
    config: dict[str, Any],
) -> str:
    strategy = strategy_name.removeprefix("explicit_")
    if strategy in {"css", "xpath"}:
        if frame.is_main:
            # 主框架保留 DOM.performSearch，它能穿透浏览器内建 shadow DOM。
            return await _resolve_dom_search(frame, config)
        declaration = _FRAME_QUERY_SCRIPT
        arguments: list[dict[str, Any]] = [
            {"value": config["value"]},
            {"value": strategy == "xpath"},
            {"value": config["index"]},
        ]
    else:
        declaration = _SEMANTIC_LOCATOR_SCRIPT
        arguments = [
            {"value": strategy},
            {"value": config["value"]},
            {"value": config["name"]},
            {"value": config["exact"]},
            {"value": config["index"]},
        ]
    return await _resolve_in_frame(frame, declaration, arguments, config)


async def _resolve_in_frame(
    frame: FrameHandle,
    declaration: str,
    arguments: list[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    """在帧文档上跑固定模板：先数匹配数消歧，再取回目标元素句柄。"""

    count_result = await frame.call_on_document(declaration, [*arguments, {"value": "count"}])
    count = count_result.get("result", {}).get("value")
    if not isinstance(count, int) or count <= config["index"]:
        raise _RetryLocator("定位器尚未匹配元素")
    _require_unambiguous(count, config)
    resolved = await frame.call_on_document(
        declaration,
        [*arguments, {"value": "element"}],
        return_by_value=False,
    )
    object_id = resolved.get("result", {}).get("objectId")
    if not isinstance(object_id, str):
        raise _RetryLocator("定位器匹配结果已经失效")
    return object_id


async def _resolve_dom_search(frame: FrameHandle, config: dict[str, Any]) -> str:
    session = frame.session
    # Chrome only guarantees search node IDs can be resolved after the document
    # tree has been requested in this frontend session.
    await session.call("DOM.getDocument", {"depth": 0, "pierce": True})
    search = await session.call(
        "DOM.performSearch",
        {"query": config["value"], "includeUserAgentShadowDOM": True},
    )
    search_id = search.get("searchId")
    count = search.get("resultCount")
    if not isinstance(search_id, str) or not isinstance(count, int):
        raise _RetryLocator("浏览器没有返回定位搜索结果")
    try:
        if count <= 0:
            raise _RetryLocator("定位器尚未匹配元素")
        node_ids = await _search_result_nodes(session, search_id, count)
        matches = await _elements_owned_by_frame(frame, node_ids)
        if len(matches) <= config["index"]:
            await _release_objects(session, matches)
            raise _RetryLocator("定位器尚未匹配元素")
        _require_unambiguous(len(matches), config)
        chosen = matches.pop(config["index"])
        await _release_objects(session, matches)
        return chosen
    finally:
        await session.call("DOM.discardSearchResults", {"searchId": search_id})


async def _search_result_nodes(
    session: CdpTargetSession,
    search_id: str,
    count: int,
) -> list[int]:
    selected = await session.call(
        "DOM.getSearchResults",
        {"searchId": search_id, "fromIndex": 0, "toIndex": min(count, _MAX_SEARCH_RESULTS)},
    )
    node_ids = selected.get("nodeIds")
    if not isinstance(node_ids, list) or not node_ids:
        raise _RetryLocator("定位器匹配结果已经失效")
    return [node_id for node_id in node_ids if isinstance(node_id, int)]


async def _elements_owned_by_frame(frame: FrameHandle, node_ids: list[int]) -> list[str]:
    """把搜索命中收敛成本帧自己的元素句柄。

    `DOM.performSearch` 有两个超出选择器语义的行为：它同时做纯文本匹配，所以样式表里
    的 `#submit` 字面量也会命中；它还横跨页面里的所有帧，所以主框架定位器会摸到 iframe
    内部的元素。前者让唯一的定位器被误判成歧义，后者让作用域不可预测——iframe 内的元素
    必须由带 frame_id 的定位器负责，坐标换算才对得上。
    """

    resolved = await asyncio.gather(
        *(frame.session.call("DOM.resolveNode", {"nodeId": node_id}) for node_id in node_ids),
        return_exceptions=True,
    )
    object_ids = [
        node["object"]["objectId"]
        for node in resolved
        if isinstance(node, dict) and isinstance(node.get("object", {}).get("objectId"), str)
    ]
    if not object_ids:
        raise _RetryLocator("定位器匹配结果已经失效")
    # 跨帧的节点属于另一个 JavaScript world，传进本帧文档会直接报错，正好是判据。
    checked = await asyncio.gather(
        *(
            frame.call_on_document(_OWN_ELEMENT_SCRIPT, [{"objectId": object_id}])
            for object_id in object_ids
        ),
        return_exceptions=True,
    )
    matches: list[str] = []
    rejected: list[str] = []
    for object_id, outcome in zip(object_ids, checked, strict=True):
        owned = not isinstance(outcome, BaseException) and (
            outcome.get("result", {}).get("value") is True
        )
        (matches if owned else rejected).append(object_id)
    await _release_objects(frame.session, rejected)
    return matches


async def _release_objects(session: CdpTargetSession, object_ids: list[str]) -> None:
    """落选的匹配项如果不释放，长任务里反复定位会把远程对象越攒越多。"""

    if not object_ids:
        return
    await asyncio.gather(
        *(
            session.call("Runtime.releaseObject", {"objectId": object_id})
            for object_id in object_ids
        ),
        return_exceptions=True,
    )


def _require_unambiguous(count: int, config: dict[str, Any]) -> None:
    if count > 1 and not config["index_explicit"]:
        raise TargetNotFoundError(f"显式定位器匹配到 {count} 个元素，请提供 index 消除歧义")


async def _actionable_box(frame: FrameHandle, object_id: str) -> BoundingBox:
    session = frame.session
    await session.call("DOM.scrollIntoViewIfNeeded", {"objectId": object_id})
    actionable = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _ACTIONABLE_SCRIPT,
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    if actionable.get("result", {}).get("value") is not True:
        raise _RetryLocator("目标尚未达到稳定、可见和可交互状态")
    # 统一用 getBoundingClientRect：它在任何帧里都是该帧的局部视口坐标，配合帧偏移就能
    # 得到主框架坐标。DOM.getBoxModel 对同进程 iframe 返回绝对坐标、对 OOPIF 返回局部
    # 坐标，两套语义混在一起很容易算错。
    measured = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _CLIENT_RECT_SCRIPT,
            "returnByValue": True,
        },
    )
    rect = measured.get("result", {}).get("value")
    if not isinstance(rect, dict) or not rect.get("width") or not rect.get("height"):
        raise _RetryLocator("目标没有可用的页面尺寸")
    box = BoundingBox(
        float(rect["x"]),
        float(rect["y"]),
        float(rect["width"]),
        float(rect["height"]),
    )
    hit = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _HIT_TEST_SCRIPT,
            "returnByValue": True,
        },
    )
    if hit.get("result", {}).get("value") is not True:
        raise _RetryLocator("目标当前被其他元素遮挡")
    return frame.to_viewport(box)


async def _target_state(session: CdpTargetSession, object_id: str) -> dict[str, Any]:
    result = await session.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": _TARGET_STATE_SCRIPT,
            "returnByValue": True,
        },
    )
    value = result.get("result", {}).get("value")
    if not isinstance(value, dict):
        raise _RetryLocator("无法读取定位器目标状态")
    return value
