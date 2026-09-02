"""结构化数据采集的供应商无关契约。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIELD_NAME = re.compile(r"^[\w\u4e00-\u9fff-]{1,64}$", re.UNICODE)
_UNIVERSAL_DATA_ZH = re.compile(r"(全部|所有|无遗漏|完整).{0,12}(订单|数据|记录|列表|明细|结果)")
_DATA_UNIVERSAL_ZH = re.compile(r"(订单|数据|记录|列表|明细|结果).{0,12}(全部|所有|无遗漏|完整)")
_UNIVERSAL_DATA_EN = re.compile(
    r"\b(all|every|complete)\b.{0,32}\b(data|orders?|records?|items?|results?)\b",
    re.IGNORECASE,
)
_RECORD_DETAILS_ZH = re.compile(
    r"(?:每(?:笔|条|个)|所有|全部).{0,32}(?:订单|记录|数据).{0,32}(?:详情|明细)"
)
_RECORD_DETAILS_EN = re.compile(
    r"\b(?:every|all)\s*(?:order|record|item)s?\b.{0,32}\b(?:detail|details|line items?)\b",
    re.IGNORECASE,
)
_SPEC_KEYS = {
    "collection_name",
    "selector_language",
    "row_selector",
    "fields",
    "unique_key",
    "pagination_mode",
    "next_page_selector",
    "load_more_selector",
    "page_number_selector",
    "current_page_selector",
    "scroll_container_selector",
    "scroll_stable_rounds",
    "total_count_selector",
    "total_pages_selector",
    "filters",
    "max_pages",
    "max_items",
    "page_wait_timeout_seconds",
    "detail_trigger_selector",
}
_FIELD_KEYS = {"name", "selector", "source"}
_FILTER_KEYS = {"field", "operator", "value"}
_FIELD_SOURCES = {"text", "value", "href", "src", "title"}
_FILTER_OPERATORS = {"equals", "not_equals", "contains", "starts_with", "ends_with"}
_PAGINATION_MODES = {"none", "next", "load_more", "page_number", "infinite_scroll"}
_INSPECTION_HINT_KEYS = {"selector", "label", "role", "source_options"}
_DETAIL_HINT_KEYS = {"selector", "label", "role"}
_COMPACT_SPEC_KEYS = {
    "collection_name",
    "candidate_id",
    "unique_field_id",
    "filters",
    "max_pages",
    "max_items",
    "detail_field_id",
}
_PAGINATION_HINT_KEYS = {
    "mode",
    "next_page_selector",
    "load_more_selector",
    "page_number_selector",
    "current_page_selector",
    "scroll_container_selector",
    "total_pages_selector",
}
_PAGINATION_HINT_MODES = {"none", "next", "load_more", "page_number", "infinite_scroll"}
_DETAIL_REFERENCE_LABEL = re.compile(r"(详情|明细|查看|detail|view)", re.IGNORECASE)


def _required_text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{name} 不能超过 {maximum} 个字符")
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{name} 不能包含控制字符")
    return result


def _normalize_compact_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """修复工具流中被截短但仍可唯一识别的参数名。"""

    normalized: dict[str, Any] = {}
    unknown: list[str] = []
    for raw_key, value in arguments.items():
        key = str(raw_key)
        if key in _COMPACT_SPEC_KEYS:
            target = key
        else:
            matches = [
                candidate
                for candidate in _COMPACT_SPEC_KEYS
                if len(key) >= 8 and candidate.startswith(key)
            ]
            if len(matches) != 1:
                unknown.append(key)
                continue
            target = matches[0]
        if target in normalized:
            raise ValueError(f"紧凑提取参数重复：{target}")
        normalized[target] = value
    if unknown:
        raise ValueError(f"紧凑提取参数包含未知项：{', '.join(sorted(unknown))}")
    return normalized


def _compact_reference(value: Any, name: str, prefix: str) -> str:
    """从模型说明文本或结构片段中提取执行层签发的短引用。"""

    raw_value = _required_text(value, name)
    match = re.search(rf"{re.escape(prefix)}_\d+", raw_value)
    return match.group(0) if match else _required_text(raw_value, name, maximum=32)


def _required_detail_reference(candidate: Mapping[str, Any]) -> str:
    raw_hints = candidate.get("detail_hints")
    hints = [item for item in raw_hints or [] if isinstance(item, Mapping)]
    semantic_matches = [
        item for item in hints if _DETAIL_REFERENCE_LABEL.search(str(item.get("label", "")))
    ]
    selected = (
        semantic_matches[0] if len(semantic_matches) == 1 else hints[0] if len(hints) == 1 else None
    )
    if selected is None:
        raise ValueError("任务要求逐条详情，但所选集合没有唯一可确认的详情入口")
    return _compact_reference(selected.get("detail_id"), "详情入口 ID", "detail")


def _optional_selector(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _validate_selector(_required_text(value, name), name)


def _validate_selector(selector: str, name: str) -> str:
    lowered = selector.lower()
    forbidden = ("javascript:", "<script", "expression(", "document.", "window.")
    if any(token in lowered for token in forbidden):
        raise ValueError(f"{name} 只能是 CSS 选择器，不能包含脚本或对象访问")
    return selector


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _bounded_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return result


def sanitize_collection_inspection(
    value: Mapping[str, Any],
    *,
    max_candidates: int = 30,
) -> dict[str, Any]:
    """只保留结构提示，拒绝第三方提取器把样例记录值带入模型。"""

    raw_candidates = value.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("集合结构观察缺少候选数组")
    pagination_hint = _sanitize_pagination_hint(value.get("pagination_hint"))
    candidates: list[dict[str, Any]] = []
    for raw_candidate in raw_candidates[:max_candidates]:
        if not isinstance(raw_candidate, Mapping):
            continue
        try:
            row_selector = _validate_selector(
                _required_text(raw_candidate.get("row_selector"), "候选行选择器"),
                "候选行选择器",
            )
            row_count = _bounded_integer(
                raw_candidate.get("row_count"),
                "候选行数量",
                2,
                100000,
            )
        except ValueError:
            continue
        raw_hints = raw_candidate.get("child_hints", [])
        hints: list[dict[str, Any]] = []
        if isinstance(raw_hints, list):
            for hint_index, raw_hint in enumerate(raw_hints[:50], start=1):
                if not isinstance(raw_hint, Mapping):
                    continue
                hint = {key: raw_hint[key] for key in _INSPECTION_HINT_KEYS if key in raw_hint}
                try:
                    selector = _validate_selector(
                        _required_text(hint.get("selector"), "字段提示选择器"),
                        "字段提示选择器",
                    )
                except ValueError:
                    continue
                hint["selector"] = selector
                hint["field_id"] = f"field_{hint_index}"
                label = hint.get("label")
                if not isinstance(label, str) or not label.strip():
                    label = hint["field_id"]
                label = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", label.strip(), flags=re.UNICODE)
                hint["label"] = label[:64] or hint["field_id"]
                if isinstance(hint.get("source_options"), list):
                    hint["source_options"] = [
                        source
                        for source in hint["source_options"]
                        if isinstance(source, str) and source in _FIELD_SOURCES
                    ]
                hints.append(hint)
        raw_detail_hints = raw_candidate.get("detail_hints", [])
        detail_hints: list[dict[str, Any]] = []
        if isinstance(raw_detail_hints, list):
            for detail_index, raw_hint in enumerate(raw_detail_hints[:20], start=1):
                if not isinstance(raw_hint, Mapping):
                    continue
                hint = {key: raw_hint[key] for key in _DETAIL_HINT_KEYS if key in raw_hint}
                try:
                    hint["selector"] = _validate_selector(
                        _required_text(hint.get("selector"), "详情入口选择器"),
                        "详情入口选择器",
                    )
                except ValueError:
                    continue
                label = hint.get("label")
                if not isinstance(label, str) or not label.strip():
                    label = f"detail_{detail_index}"
                hint["label"] = (
                    re.sub(r"[^\w\u4e00-\u9fff-]+", "_", label.strip(), flags=re.UNICODE)[:64]
                    or f"detail_{detail_index}"
                )
                hint["detail_id"] = f"detail_{detail_index}"
                detail_hints.append(hint)
        candidate_pagination = _sanitize_pagination_hint(
            raw_candidate.get("pagination_hint"),
            fallback=pagination_hint,
        )
        candidates.append(
            {
                "candidate_id": f"collection_{len(candidates) + 1}",
                "row_selector": row_selector,
                "row_count": row_count,
                "child_hints": hints,
                "detail_hints": detail_hints,
                "pagination_hint": candidate_pagination,
            }
        )
    return {"candidates": candidates}


def _sanitize_pagination_hint(
    raw: Any,
    *,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return dict(fallback or {"mode": "none"})
    mode = raw.get("mode", "none")
    if mode not in _PAGINATION_HINT_MODES:
        return dict(fallback or {"mode": "none"})
    result: dict[str, Any] = {"mode": mode}
    for key in _PAGINATION_HINT_KEYS - {"mode"}:
        selector = raw.get(key)
        if selector is None:
            continue
        try:
            result[key] = _validate_selector(
                _required_text(selector, f"分页提示 {key}"),
                f"分页提示 {key}",
            )
        except ValueError:
            return dict(fallback or {"mode": "none"})
    if mode == "load_more" and "load_more_selector" not in result:
        return dict(fallback or {"mode": "none"})
    if mode == "page_number" and not {
        "page_number_selector",
        "current_page_selector",
        "total_pages_selector",
    }.issubset(result):
        return dict(fallback or {"mode": "none"})
    if mode == "next" and "next_page_selector" not in result:
        return dict(fallback or {"mode": "none"})
    if mode == "infinite_scroll":
        result["scroll_kind"] = (
            "virtualized" if raw.get("scroll_kind") == "virtualized" else "incremental"
        )
    return result


def collection_spec_from_inspection(
    arguments: Mapping[str, Any],
    inspection: Mapping[str, Any],
    *,
    require_details: bool = False,
) -> CollectionExtractionSpec:
    """把模型选择的短候选引用编译为完整、受控的 DOM 采集规格。"""

    arguments = _normalize_compact_arguments(arguments)
    raw_candidate_id = _required_text(arguments.get("candidate_id"), "集合候选 ID")
    candidates = inspection.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("集合结构观察缓存已失效，请重新观察")
    compact_id = next(
        (match.group(0) for match in [re.search(r"collection_\d+", raw_candidate_id)] if match),
        raw_candidate_id,
    )
    candidate = next(
        (
            item
            for item in candidates
            if isinstance(item, Mapping)
            and (
                item.get("candidate_id") == compact_id
                or item.get("row_selector") == raw_candidate_id
            )
        ),
        None,
    )
    if candidate is None:
        raise ValueError("集合候选不存在或已经过期，请重新观察")
    raw_hints = candidate.get("child_hints")
    if not isinstance(raw_hints, list) or not raw_hints:
        raise ValueError("集合候选没有可采集字段")

    fields: list[dict[str, str]] = []
    field_ids: dict[str, str] = {}
    used_names: set[str] = set()
    for index, hint in enumerate(raw_hints, start=1):
        if not isinstance(hint, Mapping):
            continue
        base_name = str(hint.get("label") or f"field_{index}")[:64]
        name = base_name
        suffix = 2
        while name in used_names:
            suffix_text = f"_{suffix}"
            name = f"{base_name[: 64 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        used_names.add(name)
        source_options = hint.get("source_options")
        source = (
            "text"
            if isinstance(source_options, list) and "text" in source_options
            else str(source_options[0])
            if isinstance(source_options, list) and source_options
            else "text"
        )
        fields.append(
            {
                "name": name,
                "selector": str(hint.get("selector", "")),
                "source": source,
            }
        )
        field_ids[str(hint.get("field_id", f"field_{index}"))] = name
    if not fields:
        raise ValueError("集合候选没有有效采集字段")

    requested_unique = arguments.get("unique_field_id")
    if requested_unique is not None:
        unique_key = field_ids.get(_compact_reference(requested_unique, "唯一字段 ID", "field"))
        if unique_key is None:
            raise ValueError("唯一字段 ID 不属于所选集合候选")
    else:
        unique_key = next(
            (
                field["name"]
                for field in fields
                if re.search(r"(id|编号|编码|单号|账号|名称|name|url|链接)", field["name"], re.I)
            ),
            fields[0]["name"],
        )

    compact: dict[str, Any] = {
        "collection_name": arguments.get("collection_name"),
        "selector_language": "css",
        "row_selector": candidate.get("row_selector"),
        "fields": fields,
        "unique_key": unique_key,
        "max_pages": arguments.get("max_pages", 100),
        "max_items": arguments.get("max_items", 10000),
    }
    requested_detail = arguments.get("detail_field_id")
    if requested_detail is None and require_details:
        requested_detail = _required_detail_reference(candidate)
    if requested_detail is not None:
        detail_hints = candidate.get("detail_hints")
        if not isinstance(detail_hints, list):
            raise ValueError("所选集合候选没有详情入口")
        detail_id = _compact_reference(requested_detail, "详情入口 ID", "detail")
        detail_hint = next(
            (
                item
                for item in detail_hints
                if isinstance(item, Mapping) and item.get("detail_id") == detail_id
            ),
            None,
        )
        if detail_hint is None:
            raise ValueError("详情入口 ID 不属于所选集合候选")
        compact["detail_trigger_selector"] = detail_hint.get("selector")
    raw_filters = arguments.get("filters", [])
    if not isinstance(raw_filters, list) or len(raw_filters) > 20:
        raise ValueError("紧凑过滤条件必须是最多 20 项的数组")
    compiled_filters: list[dict[str, Any]] = []
    for raw_filter in raw_filters:
        if not isinstance(raw_filter, Mapping):
            raise ValueError("每个紧凑过滤条件必须是对象")
        unknown_filter = set(raw_filter) - {"field_id", "operator", "value"}
        if unknown_filter:
            raise ValueError(f"紧凑过滤条件包含未知项：{', '.join(sorted(unknown_filter))}")
        field_name = field_ids.get(
            _required_text(raw_filter.get("field_id"), "过滤字段 ID", maximum=32)
        )
        if field_name is None:
            raise ValueError("过滤字段 ID 不属于所选集合候选")
        compiled_filters.append(
            {
                "field": field_name,
                "operator": raw_filter.get("operator"),
                "value": raw_filter.get("value"),
            }
        )
    compact["filters"] = compiled_filters
    pagination = candidate.get("pagination_hint")
    if isinstance(pagination, Mapping):
        compact["pagination_mode"] = pagination.get("mode", "none")
        for key in _PAGINATION_HINT_KEYS - {"mode"}:
            if pagination.get(key) is not None:
                compact[key] = pagination[key]
    return CollectionExtractionSpec.from_mapping(compact)


@dataclass(frozen=True, slots=True)
class ExtractionField:
    name: str
    selector: str
    source: str = "text"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExtractionField:
        unknown = set(value) - _FIELD_KEYS
        if unknown:
            raise ValueError(f"字段包含不支持的参数：{', '.join(sorted(unknown))}")
        name = _required_text(value.get("name"), "字段名", maximum=64)
        if not _FIELD_NAME.fullmatch(name):
            raise ValueError("字段名只能包含中英文、数字、下划线或连字符")
        selector = _validate_selector(
            _required_text(value.get("selector"), f"字段 {name} 的选择器"),
            f"字段 {name} 的选择器",
        )
        source = _required_text(value.get("source", "text"), f"字段 {name} 的来源", maximum=16)
        if source not in _FIELD_SOURCES:
            raise ValueError(f"字段 {name} 的来源不受支持：{source}")
        return cls(name=name, selector=selector, source=source)


@dataclass(frozen=True, slots=True)
class ExtractionFilter:
    field: str
    operator: str
    value: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExtractionFilter:
        unknown = set(value) - _FILTER_KEYS
        if unknown:
            raise ValueError(f"过滤条件包含不支持的参数：{', '.join(sorted(unknown))}")
        field = _required_text(value.get("field"), "过滤字段", maximum=64)
        operator = _required_text(value.get("operator"), "过滤操作", maximum=20)
        if operator not in _FILTER_OPERATORS:
            raise ValueError(f"过滤操作不受支持：{operator}")
        raw_value = value.get("value")
        if not isinstance(raw_value, (str, int, float, bool)):
            raise ValueError("过滤值只能是短文本、数字或布尔值")
        filter_value = str(raw_value)
        if len(filter_value) > 512:
            raise ValueError("过滤值不能超过 512 个字符")
        return cls(field=field, operator=operator, value=filter_value)

    def matches(self, item: Mapping[str, str]) -> bool:
        actual = str(item.get(self.field, ""))
        if self.operator == "equals":
            return actual == self.value
        if self.operator == "not_equals":
            return actual != self.value
        if self.operator == "contains":
            return self.value in actual
        if self.operator == "starts_with":
            return actual.startswith(self.value)
        return actual.endswith(self.value)


@dataclass(frozen=True, slots=True)
class CollectionExtractionSpec:
    collection_name: str
    row_selector: str
    fields: tuple[ExtractionField, ...]
    unique_key: str
    selector_language: str = "css"
    pagination_mode: str = "none"
    next_page_selector: str | None = None
    load_more_selector: str | None = None
    page_number_selector: str | None = None
    current_page_selector: str | None = None
    scroll_container_selector: str | None = None
    scroll_stable_rounds: int = 2
    total_count_selector: str | None = None
    total_pages_selector: str | None = None
    filters: tuple[ExtractionFilter, ...] = ()
    max_pages: int = 100
    max_items: int = 10000
    page_wait_timeout_seconds: float = 10.0
    detail_trigger_selector: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CollectionExtractionSpec:
        unknown = set(value) - _SPEC_KEYS
        if unknown:
            raise ValueError(f"不支持的提取参数：{', '.join(sorted(unknown))}")
        selector_language = _required_text(
            value.get("selector_language", "css"),
            "选择器语言",
            maximum=16,
        ).lower()
        if selector_language != "css":
            raise ValueError("当前结构化采集只支持 css 选择器")
        raw_fields = value.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            raise ValueError("结构化采集至少需要一个字段")
        if len(raw_fields) > 50:
            raise ValueError("结构化采集字段不能超过 50 个")
        if not all(isinstance(item, Mapping) for item in raw_fields):
            raise ValueError("每个采集字段必须是对象")
        fields = tuple(ExtractionField.from_mapping(item) for item in raw_fields)
        names = [field.name for field in fields]
        if len(set(names)) != len(names):
            raise ValueError("结构化采集字段名不能重复")
        unique_key = _required_text(value.get("unique_key"), "唯一键", maximum=64)
        if unique_key not in names:
            raise ValueError("唯一键必须引用已声明的采集字段")
        raw_filters = value.get("filters", [])
        if not isinstance(raw_filters, list) or len(raw_filters) > 20:
            raise ValueError("过滤条件必须是最多 20 项的数组")
        if not all(isinstance(item, Mapping) for item in raw_filters):
            raise ValueError("每个过滤条件必须是对象")
        filters = tuple(ExtractionFilter.from_mapping(item) for item in raw_filters)
        if any(item.field not in names for item in filters):
            raise ValueError("过滤字段必须引用已声明的采集字段")
        next_page_selector = _optional_selector(
            value.get("next_page_selector"),
            "下一页选择器",
        )
        load_more_selector = _optional_selector(
            value.get("load_more_selector"),
            "加载更多选择器",
        )
        page_number_selector = _optional_selector(
            value.get("page_number_selector"),
            "页码选择器",
        )
        current_page_selector = _optional_selector(
            value.get("current_page_selector"),
            "当前页选择器",
        )
        scroll_container_selector = _optional_selector(
            value.get("scroll_container_selector"),
            "滚动容器选择器",
        )
        total_pages_selector = _optional_selector(
            value.get("total_pages_selector"),
            "声明页数选择器",
        )
        raw_pagination_mode = value.get("pagination_mode")
        if raw_pagination_mode is None:
            pagination_mode = "next" if next_page_selector else "none"
        else:
            pagination_mode = _required_text(
                raw_pagination_mode,
                "分页模式",
                maximum=32,
            ).lower()
        if pagination_mode not in _PAGINATION_MODES:
            raise ValueError(f"分页模式不受支持：{pagination_mode}")
        action_selectors = {
            "next": next_page_selector,
            "load_more": load_more_selector,
            "page_number": page_number_selector,
            "infinite_scroll": scroll_container_selector,
        }
        required_selectors = {
            "next": ("下一页选择器", next_page_selector),
            "load_more": ("加载更多选择器", load_more_selector),
            "page_number": ("页码选择器", page_number_selector),
        }
        required = required_selectors.get(pagination_mode)
        if required is not None and required[1] is None:
            raise ValueError(f"分页模式 {pagination_mode} 缺少{required[0]}")
        configured_modes = {name for name, selector in action_selectors.items() if selector}
        allowed_modes = {pagination_mode}
        if pagination_mode == "infinite_scroll":
            allowed_modes.add("infinite_scroll")
        unexpected_modes = configured_modes - allowed_modes
        if unexpected_modes:
            raise ValueError(
                f"分页模式 {pagination_mode} 不能同时配置其他动作选择器："
                f"{', '.join(sorted(unexpected_modes))}"
            )
        if pagination_mode == "page_number" and (
            current_page_selector is None or total_pages_selector is None
        ):
            raise ValueError("页码分页必须同时配置当前页选择器和声明页数选择器")
        return cls(
            collection_name=_required_text(
                value.get("collection_name"),
                "集合名称",
                maximum=100,
            ),
            selector_language=selector_language,
            row_selector=_validate_selector(
                _required_text(value.get("row_selector"), "行选择器"),
                "行选择器",
            ),
            fields=fields,
            unique_key=unique_key,
            pagination_mode=pagination_mode,
            next_page_selector=next_page_selector,
            load_more_selector=load_more_selector,
            page_number_selector=page_number_selector,
            current_page_selector=current_page_selector,
            scroll_container_selector=scroll_container_selector,
            scroll_stable_rounds=_bounded_integer(
                value.get("scroll_stable_rounds", 2),
                "滚动终点稳定次数",
                2,
                5,
            ),
            total_count_selector=_optional_selector(
                value.get("total_count_selector"),
                "声明总数选择器",
            ),
            total_pages_selector=total_pages_selector,
            filters=filters,
            max_pages=_bounded_integer(value.get("max_pages", 100), "最大页数", 1, 500),
            max_items=_bounded_integer(
                value.get("max_items", 10000),
                "最大条数",
                1,
                100000,
            ),
            page_wait_timeout_seconds=_bounded_number(
                value.get("page_wait_timeout_seconds", 10),
                "翻页等待秒数",
                0.05,
                30,
            ),
            detail_trigger_selector=_optional_selector(
                value.get("detail_trigger_selector"),
                "详情入口选择器",
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        """导出可经 from_mapping 无损重建的受控规格，供已验证采集程序持久化。"""

        payload: dict[str, Any] = {
            "collection_name": self.collection_name,
            "selector_language": self.selector_language,
            "row_selector": self.row_selector,
            "fields": [
                {"name": item.name, "selector": item.selector, "source": item.source}
                for item in self.fields
            ],
            "unique_key": self.unique_key,
            "pagination_mode": self.pagination_mode,
            "scroll_stable_rounds": self.scroll_stable_rounds,
            "filters": [
                {"field": item.field, "operator": item.operator, "value": item.value}
                for item in self.filters
            ],
            "max_pages": self.max_pages,
            "max_items": self.max_items,
            "page_wait_timeout_seconds": self.page_wait_timeout_seconds,
        }
        optional_selectors = {
            "next_page_selector": self.next_page_selector,
            "load_more_selector": self.load_more_selector,
            "page_number_selector": self.page_number_selector,
            "current_page_selector": self.current_page_selector,
            "scroll_container_selector": self.scroll_container_selector,
            "total_count_selector": self.total_count_selector,
            "total_pages_selector": self.total_pages_selector,
            "detail_trigger_selector": self.detail_trigger_selector,
        }
        payload.update(
            {key: value for key, value in optional_selectors.items() if value is not None}
        )
        return payload


@dataclass(frozen=True, slots=True)
class CollectionExtractionResult:
    collection_name: str
    complete: bool
    unique_count: int
    exported_count: int
    duplicate_count: int
    visited_pages: tuple[int, ...]
    failed_pages: tuple[int, ...]
    declared_total: int | None
    declared_pages: int | None
    completion_evidence: tuple[str, ...]
    failure_reasons: tuple[str, ...]
    json_path: Path | None = None
    csv_path: Path | None = None
    pagination_mode: str = "none"
    detail_requested: bool = False
    detail_count: int = 0
    detail_failed_keys: tuple[str, ...] = ()
    detail_fields: tuple[str, ...] = ()
    interrupted_by_security_challenge: bool = False

    @property
    def has_strong_completion_evidence(self) -> bool:
        return bool(self.completion_evidence) and self.complete

    def model_summary(self) -> dict[str, Any]:
        return {
            "集合": self.collection_name,
            "完整": self.complete,
            "去重后总数": self.unique_count,
            "过滤后导出数": self.exported_count,
            "重复数": self.duplicate_count,
            "分页模式": self.pagination_mode,
            "已访问页": list(self.visited_pages),
            "失败页": list(self.failed_pages),
            "页面声明总数": self.declared_total,
            "页面声明页数": self.declared_pages,
            "完整性证据": list(self.completion_evidence),
            "失败原因": list(self.failure_reasons),
            "JSON产物": str(self.json_path) if self.json_path else None,
            "CSV产物": str(self.csv_path) if self.csv_path else None,
            "详情采集": self.detail_requested,
            "详情覆盖数": self.detail_count,
            "详情失败唯一键": list(self.detail_failed_keys),
            "详情字段": list(self.detail_fields),
            "安全挑战中断": self.interrupted_by_security_challenge,
        }


def collection_structure_fingerprint(spec: CollectionExtractionSpec) -> str:
    """采集规格的结构指纹：只含结构期望，不含条数上限、超时或业务过滤值。

    同一列表页在字段和分页结构不变时指纹稳定；行选择器、字段选择器或分页形态
    任一变化都会产生新指纹，旧程序按新指纹替换而不是原地漂移。
    """

    payload = {
        "row_selector": spec.row_selector,
        "fields": sorted((item.name, item.selector, item.source) for item in spec.fields),
        "unique_key": spec.unique_key,
        "pagination_mode": spec.pagination_mode,
        "pagination_selectors": [
            spec.next_page_selector,
            spec.load_more_selector,
            spec.page_number_selector,
            spec.current_page_selector,
            spec.scroll_container_selector,
            spec.total_count_selector,
            spec.total_pages_selector,
        ],
        "detail_trigger_selector": spec.detail_trigger_selector,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_entry_probe(
    spec: CollectionExtractionSpec,
    probe: Mapping[str, Any],
) -> str | None:
    """判定入口页首屏事实是否仍满足规格的结构期望，返回拒绝原因，None 表示通过。

    该判定同时用于存储前验证门和热重放前置检查：只看结构事实，即行数、唯一键
    覆盖、字段取值率、分页控件可读性，不比较业务数值，业务数据随时间变化
    不应导致程序失配。
    """

    row_count = int(probe.get("row_count", 0) or 0)
    if row_count < 1:
        return "行选择器在入口页没有匹配到可见记录"
    unique_filled = int(probe.get("unique_key_filled_count", 0) or 0)
    if unique_filled != row_count:
        return f"唯一键 {spec.unique_key} 在首屏 {row_count} 行中只覆盖 {unique_filled} 行"
    raw_field_counts = probe.get("field_non_empty")
    field_counts: Mapping[str, Any] = (
        raw_field_counts if isinstance(raw_field_counts, Mapping) else {}
    )
    filled_fields = sum(1 for item in spec.fields if int(field_counts.get(item.name, 0) or 0) > 0)
    minimum_fields = max(1, (len(spec.fields) + 1) // 2)
    if filled_fields < minimum_fields:
        return f"首屏仅 {filled_fields}/{len(spec.fields)} 个字段取到值，低于结构一致性下限"
    if spec.pagination_mode == "page_number" and probe.get("current_page") is None:
        return "页码分页无法读取当前页控件"
    if spec.total_count_selector and probe.get("declared_total") is None:
        return "声明总数选择器没有读到数值"
    if spec.total_pages_selector and probe.get("declared_pages") is None:
        return "声明页数选择器没有读到数值"
    return None


def requires_complete_data_extraction(goal: str) -> bool:
    """识别明确要求获取全部业务数据的任务，避免把普通完成步骤误判为采集。"""

    return bool(
        _UNIVERSAL_DATA_ZH.search(goal)
        or _DATA_UNIVERSAL_ZH.search(goal)
        or _UNIVERSAL_DATA_EN.search(goal)
    )


def requires_record_details(goal: str) -> bool:
    """识别用户明确要求逐条详情，列表完整不能替代详情覆盖。"""

    return bool(_RECORD_DETAILS_ZH.search(goal) or _RECORD_DETAILS_EN.search(goal))
