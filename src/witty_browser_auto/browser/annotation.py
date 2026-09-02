"""在截图上叠加编号标注，把观察候选的 target_id 与像素位置对上。

多模态调用方的困境是两套坐标系对不上：模型在图上看见一个按钮，却不知道它对应哪个
`target_id`；而元素类工具只接受 target_id。这里在截图前往页面上画一层带编号的临时
覆盖层，并把编号与 target_id 的对应关系作为图例一并返回，模型就能"看图选号、按号操作"。

覆盖层是唯一一个会临时改动 DOM 的只读工具：它只往 `documentElement` 追加一个
`pointer-events:none` 的容器，截图后立即移除，不触碰任何业务节点，也不滚动页面。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from witty_browser_auto.browser.ranking import rank_candidates

CONTAINER_ID = "__witty_browser_auto_annotation_layer__"
DEFAULT_MAX_LABELS = 24
MAX_LABELS = 50
_OUTLINE_COLOR = "#ff3b30"

__all__ = [
    "ANNOTATION_CLEANUP_SCRIPT",
    "ANNOTATION_SCRIPT",
    "CONTAINER_ID",
    "DEFAULT_MAX_LABELS",
    "MAX_LABELS",
    "AnnotationLabel",
    "build_annotation_labels",
    "overlay_payload",
]


@dataclass(frozen=True, slots=True)
class AnnotationLabel:
    """一个候选与它在图上的编号。"""

    label: int
    target_id: str
    role: str
    name: str
    x: float
    y: float
    width: float
    height: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "target_id": self.target_id,
            "role": self.role,
            "name": self.name,
            "box": {
                "x": round(self.x, 2),
                "y": round(self.y, 2),
                "width": round(self.width, 2),
                "height": round(self.height, 2),
            },
        }


def build_annotation_labels(
    candidates: Sequence[Any],
    *,
    max_labels: int = DEFAULT_MAX_LABELS,
    roles: Sequence[str] = (),
) -> tuple[AnnotationLabel, ...]:
    """给候选编号；只保留有可见矩形的候选，按与观察相同的次序取前 `max_labels` 个。

    编号从 1 开始且与返回顺序一致，图例与图上的数字因此永远对得上；次序与 `observe`
    的候选清单一致(控件在前、链接在后、再按置信度)，模型在文字清单里排第几、在图上
    看到的就是几号。
    """

    if not 1 <= max_labels <= MAX_LABELS:
        raise ValueError(f"标注数量必须在 1 到 {MAX_LABELS} 之间")
    wanted = {role.casefold() for role in roles}
    usable: list[Any] = []
    for candidate in candidates:
        box = getattr(candidate, "box", None)
        if box is None or box.width <= 0 or box.height <= 0:
            continue
        if wanted and candidate.role.casefold() not in wanted:
            continue
        usable.append(candidate)
    ranked: list[Any] = rank_candidates(usable)
    return tuple(
        AnnotationLabel(
            label=index,
            target_id=candidate.target_id,
            role=candidate.role,
            name=candidate.name,
            x=float(candidate.box.x),
            y=float(candidate.box.y),
            width=float(candidate.box.width),
            height=float(candidate.box.height),
        )
        for index, candidate in enumerate(ranked[:max_labels], start=1)
    )


def overlay_payload(labels: Sequence[AnnotationLabel]) -> dict[str, Any]:
    """构造绘制脚本的参数；页面脚本只接受结构化数据，不接受任何表达式。"""

    return {
        "containerId": CONTAINER_ID,
        "color": _OUTLINE_COLOR,
        "labels": [
            {
                "label": item.label,
                "x": item.x,
                "y": item.y,
                "width": item.width,
                "height": item.height,
            }
            for item in labels
        ],
    }


def drawn_labels(result: Any) -> tuple[int, ...]:
    """从绘制脚本的返回值里取出真正画上去的编号。"""

    if isinstance(result, Mapping):
        drawn = result.get("drawn")
        if isinstance(drawn, list):
            return tuple(int(item) for item in drawn if isinstance(item, (int, float)))
    return ()


# 固定模板：只按传入的矩形画框和编号，完全在视口外的候选直接跳过并从 drawn 中排除，
# 这样图例里不会出现图上根本看不到的编号。
ANNOTATION_SCRIPT = r"""
function(payload) {
  const existing = document.getElementById(payload.containerId);
  if (existing) { existing.remove(); }
  const width = window.innerWidth;
  const height = window.innerHeight;
  const container = document.createElement('div');
  container.id = payload.containerId;
  container.setAttribute('aria-hidden', 'true');
  container.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;margin:0;'
    + 'padding:0;border:0;z-index:2147483647;pointer-events:none;';
  const drawn = [];
  for (const item of payload.labels) {
    const x = Number(item.x);
    const y = Number(item.y);
    const w = Number(item.width);
    const h = Number(item.height);
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) { continue; }
    if (x + w <= 0 || y + h <= 0 || x >= width || y >= height) { continue; }
    const outline = document.createElement('div');
    outline.style.cssText = 'position:fixed;pointer-events:none;box-sizing:border-box;'
      + 'left:' + x + 'px;top:' + y + 'px;width:' + w + 'px;height:' + h + 'px;'
      + 'border:2px solid ' + payload.color + ';';
    const badge = document.createElement('div');
    badge.textContent = String(item.label);
    const badgeTop = y - 18 < 0 ? y : y - 18;
    badge.style.cssText = 'position:fixed;pointer-events:none;'
      + 'left:' + (x < 0 ? 0 : x) + 'px;top:' + badgeTop + 'px;'
      + 'background:' + payload.color + ';color:#ffffff;'
      + 'font:bold 12px/16px ui-monospace,Menlo,Consolas,monospace;'
      + 'padding:0 4px;border-radius:2px;';
    container.appendChild(outline);
    container.appendChild(badge);
    drawn.push(item.label);
  }
  document.documentElement.appendChild(container);
  return JSON.stringify({ drawn: drawn, viewport: { width: width, height: height } });
}
"""

ANNOTATION_CLEANUP_SCRIPT = r"""
function(containerId) {
  const existing = document.getElementById(containerId);
  if (existing) { existing.remove(); }
  return true;
}
"""
