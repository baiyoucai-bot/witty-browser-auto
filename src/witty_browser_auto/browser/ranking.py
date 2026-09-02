"""观察候选的展示排序。

候选清单要在两处截断：驱动侧最多保留 200 个，喂给模型时默认只给 24 个。截断时留下谁
决定了模型能不能看见它真正要点的那个控件。按"置信度、角色字母序、名字字母序"排会让
两百个导航链接把搜索框挤出视野——`link` 排在 `textbox` 前面只是因为字母 l 小于 t。

这里的次序反映智能体的实际需要：能填能选的控件最稀缺也最关键，排最前；按钮与其它
控件其次；链接数量最多、且总能用 `list_page_links` 另行取全，排最后。同一组内视口里
的先于视口外的（模型看截图时看到的就是视口），再按置信度，最后保持文档顺序。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from witty_browser_auto.domain.models import CandidateTarget

_INPUT_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
_CONTROL_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "tab",
        "menuitem",
        "option",
        "listbox",
    }
)


def role_group(role: str) -> int:
    """0 = 可输入控件，1 = 其它控件，2 = 链接，3 = 未知角色。"""

    lowered = role.casefold()
    if lowered in _INPUT_ROLES:
        return 0
    if lowered in _CONTROL_ROLES:
        return 1
    if lowered == "link":
        return 2
    return 3


def in_viewport(candidate: CandidateTarget, viewport_height: float | None) -> bool:
    """包围盒与视口纵向有交集即算在视口内；没有包围盒或不知道视口高度时不惩罚。"""

    box = candidate.box
    if box is None or viewport_height is None or viewport_height <= 0:
        return True
    return box.y < viewport_height and box.y + box.height > 0


def candidate_rank_key(
    candidate: CandidateTarget,
    *,
    viewport_height: float | None = None,
) -> tuple[bool, int, bool, float]:
    return (
        candidate.disabled,
        role_group(candidate.role),
        not in_viewport(candidate, viewport_height),
        -candidate.confidence,
    )


def rank_candidates(
    candidates: Iterable[CandidateTarget],
    *,
    viewport_height: float | None = None,
) -> list[CandidateTarget]:
    """稳定排序：同键的候选保持传入顺序，也就是文档顺序。"""

    return sorted(
        candidates,
        key=lambda item: candidate_rank_key(item, viewport_height=viewport_height),
    )


def viewport_height_of(metadata: dict[str, object] | None) -> float | None:
    """从观察元数据里取 CSS 视口高度；驱动把它写在 `CSS视口` 键下。"""

    if not metadata:
        return None
    viewport = metadata.get("CSS视口")
    if not isinstance(viewport, dict):
        return None
    height = viewport.get("height")
    if isinstance(height, bool) or not isinstance(height, int | float):
        return None
    return float(height) if height > 0 else None


__all__: Sequence[str] = (
    "candidate_rank_key",
    "in_viewport",
    "rank_candidates",
    "role_group",
    "viewport_height_of",
)
