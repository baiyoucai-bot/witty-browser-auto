"""把观察与工具结果转成可直接喂给大模型的 JSON 安全结构。

外部智能体框架自己持有 LLM：它必须把页面观察和工具结果放进模型上下文。但
`Observation` 与 `ToolExecutionResult` 是带 `datetime` 与枚举的 dataclass，
`json.dumps(dataclasses.asdict(...))` 会直接抛 TypeError；候选上限又是 200 个，
原样塞进上下文足以吃满一次请求。

本模块是这两类对象通往模型的唯一出口，顺带承担 token 预算：候选按置信度排序后截断、
文本按上限截断，并把截断事实显式写进结果，让调用方知道自己看到的不是全部。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from witty_browser_auto.browser.ranking import rank_candidates, viewport_height_of
from witty_browser_auto.domain.models import CandidateTarget, Observation

if TYPE_CHECKING:  # 仅用于类型标注，避免与执行层形成导入环。
    from witty_browser_auto.agent.tools import ToolExecutionResult

# 与引擎时代的候选预算一致：更多候选并不提升选择质量，只会挤占上下文。
DEFAULT_MAX_CANDIDATES = 24
DEFAULT_MAX_SUMMARY_CHARS = 1200
DEFAULT_MAX_TEXT_CHARS = 120

__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_MAX_SUMMARY_CHARS",
    "DEFAULT_MAX_TEXT_CHARS",
    "candidate_to_dict",
    "observation_to_dict",
    "observation_to_prompt",
    "tool_result_to_dict",
]


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def _selected_candidates(
    observation: Observation,
    *,
    max_candidates: int,
    roles: Sequence[str],
) -> tuple[CandidateTarget, ...]:
    pool = list(observation.candidates)
    if roles:
        wanted = {role.casefold() for role in roles}
        pool = [item for item in pool if item.role.casefold() in wanted]
    # 与驱动侧同一套次序：可输入控件 > 其它控件 > 链接，视口内优先，再按置信度。
    # 截断时留下的必须是模型最可能要操作的那些，而不是字母序靠前的那些。
    pool = rank_candidates(pool, viewport_height=viewport_height_of(observation.metadata))
    return tuple(pool[:max_candidates]) if max_candidates > 0 else tuple(pool)


def candidate_to_dict(
    candidate: CandidateTarget,
    *,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    include_box: bool = False,
) -> dict[str, Any]:
    """把单个候选转成 JSON 安全字典；枚举取 value，几何默认不带。"""

    text, clipped = _clip(candidate.text, max_text_chars)
    payload: dict[str, Any] = {
        "target_id": candidate.target_id,
        "role": candidate.role,
        "name": candidate.name,
        "text": text,
        "confidence": round(candidate.confidence, 3),
    }
    if clipped:
        payload["text_truncated"] = True
    if candidate.disabled:
        payload["disabled"] = True
    if candidate.drag_risk is not None:
        payload["drag_risk"] = candidate.drag_risk.value
    if include_box and candidate.box is not None:
        payload["box"] = {
            "x": candidate.box.x,
            "y": candidate.box.y,
            "width": candidate.box.width,
            "height": candidate.box.height,
        }
    return payload


def observation_to_dict(
    observation: Observation,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    roles: Sequence[str] = (),
    include_boxes: bool = False,
) -> dict[str, Any]:
    """把页面观察转成可 `json.dumps` 的紧凑字典。

    候选按置信度排序后截断到 `max_candidates`；实际候选总数与是否截断都会写进结果，
    避免调用方把"看到 24 个"当成"页面只有 24 个"。
    """

    selected = _selected_candidates(
        observation,
        max_candidates=max_candidates,
        roles=roles,
    )
    summary, summary_clipped = _clip(observation.summary, max_summary_chars)
    payload: dict[str, Any] = {
        "url": observation.url,
        "title": observation.title,
        "fingerprint": observation.fingerprint,
        "observation_version": observation.version,
        "captured_at": observation.captured_at.isoformat(),
        "summary": summary,
        "candidate_count": len(observation.candidates),
        "returned_candidate_count": len(selected),
        "candidates": [
            candidate_to_dict(item, max_text_chars=max_text_chars, include_box=include_boxes)
            for item in selected
        ],
    }
    if summary_clipped:
        payload["summary_truncated"] = True
    if len(selected) < len(observation.candidates):
        payload["candidates_truncated"] = True
    return payload


def observation_to_prompt(
    observation: Observation,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    roles: Sequence[str] = (),
) -> str:
    """把页面观察渲染成紧凑文本，可直接作为模型消息内容。

    `target_id` 逐字列出：元素类工具只接受来自当前观察的 target_id，模型看不到这份
    清单就只能猜，而猜出来的 target_id 一定会被执行层拒绝。
    """

    selected = _selected_candidates(
        observation,
        max_candidates=max_candidates,
        roles=roles,
    )
    lines = [
        f"页面标题：{observation.title}",
        f"页面地址：{observation.url}",
    ]
    if selected:
        scope = (
            f"{len(selected)}/{len(observation.candidates)}"
            if len(selected) < len(observation.candidates)
            else str(len(selected))
        )
        lines.append(
            f"可操作候选 {scope} 项(输入框与控件在前、链接在后、视口内优先)，"
            "target_id 必须逐字使用："
        )
        for item in selected:
            text, _ = _clip(item.text, max_text_chars)
            parts = [f"- {item.target_id} [{item.role}]"]
            if item.name:
                parts.append(item.name)
            if text and text != item.name:
                parts.append(f"文本：{text}")
            if item.disabled:
                parts.append("已禁用")
            lines.append(" ".join(parts))
    else:
        lines.append("可操作候选：无；需要用显式定位器 css/xpath/role/text/label/test_id")
    summary, clipped = _clip(observation.summary, max_summary_chars)
    if summary:
        lines.append(f"页面摘要：{summary}{'…' if clipped else ''}")
    return "\n".join(lines)


def tool_result_to_dict(
    result: ToolExecutionResult,
    *,
    for_model: bool = True,
    include_page: bool = True,
    page_max_candidates: int = DEFAULT_MAX_CANDIDATES,
    page_roles: Sequence[str] = (),
) -> dict[str, Any]:
    """把工具结果转成 JSON 安全字典。

    `for_model=True` 时用工具自己声明的有界脱敏视图 `model_data`，未声明才回退到
    `data`；证据只给类型与说明不给本机路径；`for_model=False` 给调用方完整视图，
    含 `data` 与证据文件路径。两种视图都带上 `failure_kind` 与后置条件校验结论——
    这两项决定下一步该重试、换路还是停下，缺了模型只能瞎猜。

    结果若带有动作后的新观察，会以 `page` 字段给出(与 `observation_to_dict` 同一
    形状)，其中的 `target_id` 可直接用于下一步元素工具，不必再调一次 observe。
    """

    payload: dict[str, Any] = {
        "call_id": result.call_id,
        "name": result.name,
        "success": result.success,
        "message": result.message,
        "idempotent": result.idempotent,
        "counts_as_action": result.counts_as_action,
    }
    if result.failure_kind is not None:
        payload["failure_kind"] = result.failure_kind.value
    data: Mapping[str, Any] | None
    if for_model:
        data = result.data if result.model_data is None else result.model_data
    else:
        data = result.data
    payload["data"] = dict(data or {})
    if for_model and result.model_data is None and result.data:
        # 工具没单独声明模型视图，这里给出的就是完整调用方数据，调用方需自行判断是否合适。
        payload["data_is_caller_view"] = True
    if result.verification is not None:
        payload["verification"] = {
            "success": result.verification.success,
            "reason": result.verification.reason,
        }
    if result.evidence is not None:
        evidence: dict[str, Any] = {
            "kind": result.evidence.kind,
            "summary": result.evidence.summary,
        }
        if not for_model:
            evidence["evidence_id"] = result.evidence.evidence_id
            evidence["path"] = result.evidence.path
        payload["evidence"] = evidence
    if not for_model and result.receipt is not None:
        payload["receipt"] = {
            "action_id": result.receipt.action_id,
            "success": result.receipt.success,
            "outcome_known": result.receipt.outcome_known,
            "message": result.receipt.message,
            "duration_ms": result.receipt.duration_ms,
        }
    if include_page and result.observation is not None:
        payload["page"] = observation_to_dict(
            result.observation,
            max_candidates=page_max_candidates,
            roles=page_roles,
        )
    return payload
