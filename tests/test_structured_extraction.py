from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest

from witty_browser_auto.agent.tools import TOOL_SCHEMAS
from witty_browser_auto.browser.extraction import CdpDomCollectionExtractor
from witty_browser_auto.domain.extraction import (
    CollectionExtractionSpec,
    collection_spec_from_inspection,
    sanitize_collection_inspection,
)
from witty_browser_auto.domain.models import ActionCommand, ActionReceipt


class FakeExtractionDriver:
    def __init__(
        self,
        pages: list[dict[str, object]],
        *,
        advance_on_click: bool = True,
        initial_page_index: int = 0,
    ) -> None:
        self.pages = pages
        self.advance_on_click = advance_on_click
        self.page_index = initial_page_index
        self.commands: list[ActionCommand] = []

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.commands.append(command)
        script = command.script or ""
        if "WITTY_BROWSER_AUTO_EXTRACT_PAGE" in script:
            value = self.pages[self.page_index]
        elif "WITTY_BROWSER_AUTO_CLICK_PAGE_NUMBER" in script:
            target_page = int(script.split("const targetPage = ", 1)[1].split(";", 1)[0])
            if self.advance_on_click and 1 <= target_page <= len(self.pages):
                self.page_index = target_page - 1
            value = {"clicked": True, "target_page": target_page}
        elif (
            "WITTY_BROWSER_AUTO_CLICK_NEXT" in script
            or "WITTY_BROWSER_AUTO_CLICK_LOAD_MORE" in script
        ):
            if self.advance_on_click and self.page_index < len(self.pages) - 1:
                self.page_index += 1
            value = {"clicked": True}
        elif "WITTY_BROWSER_AUTO_SCROLL_MORE" in script:
            if self.advance_on_click and self.page_index < len(self.pages) - 1:
                self.page_index += 1
            value = {
                "scrolled": True,
                "before": self.page_index * 800,
                "after": self.page_index * 800,
                "scroll_height": 1600,
                "client_height": 800,
                "at_bottom": self.page_index >= len(self.pages) - 1,
            }
        elif "WITTY_BROWSER_AUTO_INSPECT_COLLECTION" in script:
            value = {
                "candidates": [],
                "pagination_hint": {
                    "mode": "page_number",
                    "page_number_selector": ".page",
                    "current_page_selector": ".page.active",
                    "total_pages_selector": ".page:last-of-type",
                },
            }
        else:
            raise AssertionError("结构化采集器只能执行内部固定模板")
        return ActionReceipt(
            command.action_id,
            True,
            True,
            "固定模板执行成功",
            1.0,
            data={"value": value},
        )


class FakeDetailExtractionDriver(FakeExtractionDriver):
    def __init__(
        self,
        pages: list[dict[str, object]],
        details: dict[str, dict[str, str]],
        *,
        challenge_on_first_detail: bool = False,
        transient_failures: dict[str, int] | None = None,
        stale_detail_reads: int = 0,
        premature_signature_reads: int = 0,
    ) -> None:
        super().__init__(pages)
        self.details = details
        self.current_detail_key = ""
        self.challenge_on_first_detail = challenge_on_first_detail
        self.transient_failures = transient_failures or {}
        self.navigation_counts: dict[str, int] = {}
        self.current_transient_error = False
        self.detail_click_count = 0
        self.stale_detail_reads = stale_detail_reads
        self.premature_signature_reads = premature_signature_reads

    async def execute(self, command: ActionCommand) -> ActionReceipt:
        self.commands.append(command)
        if command.kind.value == "navigate":
            self.current_detail_key = (command.url or "").rsplit("/", 1)[-1]
            count = self.navigation_counts.get(self.current_detail_key, 0) + 1
            self.navigation_counts[self.current_detail_key] = count
            self.current_transient_error = count <= self.transient_failures.get(
                self.current_detail_key,
                0,
            )
            return ActionReceipt(
                command.action_id,
                True,
                True,
                "详情导航成功",
                1.0,
            )
        script = command.script or ""
        if "WITTY_BROWSER_AUTO_CLICK_RECORD_DETAIL" in script:
            self.detail_click_count += 1
            self.current_detail_key = next(iter(self.details))
            self.current_transient_error = False
            value: dict[str, object] = {
                "clicked": True,
                "unique_key": self.current_detail_key,
                "before_url": "https://example.com/orders",
                "before_signature": "list-before",
            }
        elif "WITTY_BROWSER_AUTO_EXTRACT_RECORD_DETAIL" in script:
            if self.stale_detail_reads > 0:
                self.stale_detail_reads -= 1
                value = {
                    "url": "https://example.com/orders",
                    "content_signature": "list-before",
                    "challenge": False,
                    "transient_error": False,
                    "contains_expected": True,
                    "details": {"订单号": self.current_detail_key},
                    "ready": True,
                }
            elif self.premature_signature_reads > 0:
                self.premature_signature_reads -= 1
                value = {
                    "url": "https://example.com/orders",
                    "content_signature": "navigation-started",
                    "challenge": False,
                    "transient_error": False,
                    "contains_expected": True,
                    "details": {"订单号": self.current_detail_key},
                    "ready": True,
                }
            else:
                value = {
                    "url": f"https://example.com/order/info/{self.current_detail_key}",
                    "content_signature": f"detail-{self.current_detail_key}",
                    "challenge": self.challenge_on_first_detail,
                    "transient_error": self.current_transient_error,
                    "contains_expected": True,
                    "details": (
                        {}
                        if self.current_transient_error
                        else self.details.get(self.current_detail_key, {})
                    ),
                    "ready": not self.challenge_on_first_detail
                    and not self.current_transient_error,
                }
        else:
            self.commands.pop()
            return await super().execute(command)
        return ActionReceipt(
            command.action_id,
            True,
            True,
            "固定详情模板执行成功",
            1.0,
            data={"value": value},
        )


def _spec(**overrides: object) -> CollectionExtractionSpec:
    arguments: dict[str, object] = {
        "collection_name": "订单",
        "selector_language": "css",
        "row_selector": ".order-row",
        "fields": [
            {"name": "订单号", "selector": ".id", "source": "text"},
            {"name": "状态", "selector": ".status", "source": "text"},
        ],
        "unique_key": "订单号",
        "next_page_selector": ".next",
        "total_count_selector": ".total",
        "total_pages_selector": ".pages",
        "max_pages": 10,
        "max_items": 100,
        "page_wait_timeout_seconds": 0.1,
    }
    arguments.update(overrides)
    return CollectionExtractionSpec.from_mapping(arguments)


def test_extraction_spec_rejects_non_css_and_unknown_properties() -> None:
    with pytest.raises(ValueError, match="只支持 css"):
        _spec(selector_language="xpath")

    with pytest.raises(ValueError, match="不支持的提取参数"):
        _spec(javascript="return document.cookie")


def test_extraction_spec_validates_pagination_mode_selectors() -> None:
    assert _spec().pagination_mode == "next"

    with pytest.raises(ValueError, match="缺少加载更多选择器"):
        _spec(pagination_mode="load_more", next_page_selector=None)
    with pytest.raises(ValueError, match="不能同时配置其他动作选择器"):
        _spec(
            pagination_mode="load_more",
            load_more_selector=".more",
        )
    with pytest.raises(ValueError, match="当前页选择器和声明页数选择器"):
        _spec(
            pagination_mode="page_number",
            next_page_selector=None,
            page_number_selector=".page",
        )
    with pytest.raises(ValueError, match="滚动终点稳定次数"):
        _spec(
            pagination_mode="infinite_scroll",
            next_page_selector=None,
            scroll_stable_rounds=1,
        )


def test_structured_tool_schema_uses_compact_inspection_reference() -> None:
    inspect_schema = next(
        item["function"]["parameters"]
        for item in TOOL_SCHEMAS
        if item["function"]["name"] == "inspect_collection_structure"
    )
    assert inspect_schema["properties"] == {}

    schema = next(
        item["function"]["parameters"]
        for item in TOOL_SCHEMAS
        if item["function"]["name"] == "run_structured_extraction"
    )
    assert schema["required"] == ["collection_name", "candidate_id"]
    assert set(schema["properties"]) == {
        "collection_name",
        "candidate_id",
        "unique_field_id",
        "detail_field_id",
        "filters",
        "max_pages",
        "max_items",
    }
    assert "row_selector" not in schema["properties"]
    assert "fields" not in schema["properties"]


def test_inspection_assigns_compact_ids_and_compiles_page_number_spec() -> None:
    inspection = sanitize_collection_inspection(
        {
            "pagination_hint": {
                "mode": "page_number",
                "page_number_selector": ".el-pager > li.number",
                "current_page_selector": ".el-pager > li.number.active",
                "total_pages_selector": ".el-pager > li.number:last-of-type",
            },
            "candidates": [
                {
                    "row_selector": ".order-table tbody > tr",
                    "row_count": 10,
                    "child_hints": [
                        {
                            "selector": ":scope > td:nth-of-type(1)",
                            "label": "商品名",
                            "source_options": ["text"],
                        },
                        {
                            "selector": ":scope > td:nth-of-type(2)",
                            "label": "订单号",
                            "source_options": ["text"],
                        },
                    ],
                    "detail_hints": [
                        {
                            "selector": ":scope > td:nth-of-type(3) button",
                            "label": "订单详情",
                            "role": "button",
                        }
                    ],
                }
            ],
        }
    )

    assert inspection["candidates"][0]["candidate_id"] == "collection_1"
    assert inspection["candidates"][0]["child_hints"][1]["field_id"] == "field_2"
    spec = collection_spec_from_inspection(
        {
            "collection_name": "全部订单",
            "candidate_id": "collection_1",
            "detail_field_id": "detail_1",
        },
        inspection,
    )

    assert spec.row_selector == ".order-table tbody > tr"
    assert [field.name for field in spec.fields] == ["商品名", "订单号"]
    assert spec.unique_key == "订单号"
    assert spec.pagination_mode == "page_number"
    assert spec.page_number_selector == ".el-pager > li.number"
    assert spec.current_page_selector == ".el-pager > li.number.active"
    assert spec.total_pages_selector == ".el-pager > li.number:last-of-type"
    assert spec.detail_trigger_selector == ":scope > td:nth-of-type(3) button"


def test_inspection_compiles_candidate_specific_virtual_scroll_spec() -> None:
    inspection = sanitize_collection_inspection(
        {
            "pagination_hint": {"mode": "none"},
            "candidates": [
                {
                    "row_selector": ".virtual-list > .row",
                    "row_count": 12,
                    "child_hints": [
                        {"selector": ".id", "label": "订单号", "source_options": ["text"]}
                    ],
                    "pagination_hint": {
                        "mode": "infinite_scroll",
                        "scroll_container_selector": ".virtual-list",
                        "scroll_kind": "virtualized",
                    },
                }
            ],
        }
    )

    candidate = inspection["candidates"][0]
    assert candidate["pagination_hint"] == {
        "mode": "infinite_scroll",
        "scroll_container_selector": ".virtual-list",
        "scroll_kind": "virtualized",
    }
    spec = collection_spec_from_inspection(
        {"collection_name": "全部订单", "candidate_id": "collection_1"},
        inspection,
    )
    assert spec.pagination_mode == "infinite_scroll"
    assert spec.scroll_container_selector == ".virtual-list"


def test_compact_spec_repairs_wrapped_ids_and_truncated_unambiguous_key() -> None:
    inspection = sanitize_collection_inspection(
        {
            "candidates": [
                {
                    "row_selector": ".order-table tbody > tr",
                    "row_count": 10,
                    "child_hints": [
                        {"selector": ".name", "label": "商品名", "source_options": ["text"]},
                        {"selector": ".id", "label": "订单号", "source_options": ["text"]},
                    ],
                    "detail_hints": [
                        {"selector": ".detail", "label": "订单详情", "role": "button"}
                    ],
                }
            ],
        }
    )

    spec = collection_spec_from_inspection(
        {
            "collection_name": "订单详情",
            "candidate_id": "请选择 candidate_id=collection_1",
            "unique_field_id": '{"field_id":"field_2"}',
            "detail_fi": "详情入口为 detail_1，请执行",
        },
        inspection,
    )

    assert spec.unique_key == "订单号"
    assert spec.detail_trigger_selector == ".detail"


def test_compact_spec_auto_selects_unique_semantic_detail_for_required_task() -> None:
    inspection = sanitize_collection_inspection(
        {
            "candidates": [
                {
                    "row_selector": ".order-row",
                    "row_count": 10,
                    "child_hints": [
                        {"selector": ".id", "label": "订单号", "source_options": ["text"]}
                    ],
                    "detail_hints": [
                        {"selector": ".complaint", "label": "投诉", "role": "button"},
                        {"selector": ".detail", "label": "查看详情", "role": "button"},
                    ],
                }
            ],
        }
    )

    spec = collection_spec_from_inspection(
        {"collection_name": "全部订单", "candidate_id": "collection_1"},
        inspection,
        require_details=True,
    )

    assert spec.detail_trigger_selector == ".detail"


def test_compact_spec_rejects_stale_candidate_and_unknown_fields() -> None:
    inspection = {"candidates": []}
    with pytest.raises(ValueError, match="不存在或已经过期"):
        collection_spec_from_inspection(
            {"collection_name": "订单", "candidate_id": "collection_1"},
            inspection,
        )


def test_compact_spec_accepts_legacy_row_selector_reference() -> None:
    inspection = sanitize_collection_inspection(
        {
            "candidates": [
                {
                    "row_selector": ".orders tbody > tr",
                    "row_count": 10,
                    "child_hints": [
                        {"selector": ":scope > td", "label": "订单号", "source_options": ["text"]}
                    ],
                }
            ]
        }
    )

    spec = collection_spec_from_inspection(
        {"collection_name": "订单", "candidate_id": ".orders tbody > tr"},
        inspection,
    )

    assert spec.row_selector == ".orders tbody > tr"
    with pytest.raises(ValueError, match="未知项"):
        collection_spec_from_inspection(
            {
                "collection_name": "订单",
                "candidate_id": "collection_1",
                "row_selector": ".model-generated",
            },
            inspection,
        )


def test_collection_inspection_template_never_returns_record_samples(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver([])
        result = await CdpDomCollectionExtractor(driver, tmp_path).inspect()

        assert result == {
            "candidates": [],
            "pagination_hint": {
                "mode": "page_number",
                "page_number_selector": ".page",
                "current_page_selector": ".page.active",
                "total_pages_selector": ".page:last-of-type",
            },
        }
        script = driver.commands[0].script or ""
        assert "sample:" not in script
        assert "source_options" in script
        assert "explicitPaginationHint" in script
        assert "scrollHint" in script
        assert "scroll_kind" in script
        assert "thead th" in script
        assert "compactPath" in script
        assert ".arco-pagination li.arco-pagination-item" in script

    asyncio.run(scenario())


def test_code_extracts_all_pages_deduplicates_and_exports_private_files(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver(
            [
                {
                    "rows": [
                        {"订单号": "A-1", "状态": "已完成"},
                        {"订单号": "A-2", "状态": "处理中"},
                    ],
                    "fingerprint": "page-1",
                    "declared_total": 3,
                    "declared_pages": 2,
                    "next_exists": True,
                    "next_disabled": False,
                },
                {
                    "rows": [
                        {"订单号": "A-2", "状态": "处理中"},
                        {"订单号": "A-3", "状态": "已完成"},
                    ],
                    "fingerprint": "page-2",
                    "declared_total": 3,
                    "declared_pages": 2,
                    "next_exists": True,
                    "next_disabled": True,
                },
            ]
        )
        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(_spec())

        assert result.complete is True
        assert result.visited_pages == (1, 2)
        assert result.unique_count == 3
        assert result.duplicate_count == 1
        assert result.declared_total == 3
        assert result.declared_pages == 2
        assert result.json_path is not None
        assert result.csv_path is not None
        assert stat.S_IMODE(result.json_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.csv_path.stat().st_mode) == 0o600
        payload = json.loads(result.json_path.read_text(encoding="utf-8"))
        assert payload["completeness"] == {
            "source": "dom",
            "complete": True,
            "pagination_mode": "next",
            "visited_pages": [1, 2],
            "failed_pages": [],
            "declared_total": 3,
            "declared_pages": 2,
            "completion_evidence": [
                "页面声明总数与代码去重计数一致",
                "页面声明页数与代码已访问页一致",
                "已验证的下一页控件在终页禁用或消失",
            ],
        }
        assert [item["订单号"] for item in payload["items"]] == ["A-1", "A-2", "A-3"]
        assert all("WITTY_BROWSER_AUTO" in (command.script or "") for command in driver.commands)

    asyncio.run(scenario())


def test_code_merges_every_record_detail_and_audits_coverage(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeDetailExtractionDriver(
            [
                {
                    "rows": [
                        {"订单号": "A-1", "状态": "已完成"},
                        {"订单号": "A-2", "状态": "处理中"},
                    ],
                    "fingerprint": "page-1",
                    "declared_total": 2,
                    "declared_pages": 1,
                    "next_exists": True,
                    "next_disabled": True,
                }
            ],
            {
                "A-1": {"订单号": "A-1", "渠道流水号": "C-1", "购买数量": "1"},
                "A-2": {"订单号": "A-2", "渠道流水号": "C-2", "购买数量": "2"},
            },
        )
        spec = _spec(detail_trigger_selector=".order-detail")

        result = await CdpDomCollectionExtractor(
            driver,
            tmp_path,
            detail_success_delay_seconds=0,
        ).extract(spec)

        assert result.complete is True
        assert result.detail_requested is True
        assert result.detail_count == 2
        assert result.detail_failed_keys == ()
        assert "渠道流水号" in result.detail_fields
        assert "已按唯一键验证并合并 2/2 条记录详情" in result.completion_evidence
        assert result.json_path is not None
        payload = json.loads(result.json_path.read_text(encoding="utf-8"))
        assert payload["completeness"]["detail_count"] == 2
        assert [item["渠道流水号"] for item in payload["items"]] == ["C-1", "C-2"]

    asyncio.run(scenario())


def test_detail_click_waits_until_list_surface_really_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        page = {
            "rows": [{"订单号": "A-1", "状态": "已完成"}],
            "fingerprint": "page-1",
            "declared_total": 1,
            "declared_pages": 1,
            "next_exists": True,
            "next_disabled": True,
        }
        driver = FakeDetailExtractionDriver(
            [page],
            {"A-1": {"订单号": "A-1", "渠道流水号": "TX-1"}},
            stale_detail_reads=1,
        )

        result = await CdpDomCollectionExtractor(
            driver,
            tmp_path,
            detail_success_delay_seconds=0,
        ).extract(_spec(detail_trigger_selector=".order-detail"))

        assert result.complete is True
        assert result.detail_count == 1
        assert result.detail_failed_keys == ()
        assert "渠道流水号" in result.detail_fields

    asyncio.run(scenario())


def test_detail_click_waits_for_keyed_route_after_premature_signature_change(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        page = {
            "rows": [{"订单号": "A-1", "状态": "已完成"}],
            "fingerprint": "page-1",
            "declared_total": 1,
            "declared_pages": 1,
            "next_exists": True,
            "next_disabled": True,
        }
        driver = FakeDetailExtractionDriver(
            [page],
            {"A-1": {"订单号": "A-1", "渠道流水号": "TX-1"}},
            premature_signature_reads=1,
        )

        result = await CdpDomCollectionExtractor(
            driver,
            tmp_path,
            detail_success_delay_seconds=0,
        ).extract(_spec(detail_trigger_selector=".order-detail"))

        assert result.complete is True
        assert result.detail_count == 1
        assert result.detail_failed_keys == ()
        assert "渠道流水号" in result.detail_fields

    asyncio.run(scenario())


def test_detail_route_wait_preserves_security_challenge_interruption(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        page = {
            "rows": [{"订单号": "A-1", "状态": "已完成"}],
            "fingerprint": "page-1",
            "declared_total": 1,
            "declared_pages": 1,
            "next_exists": True,
            "next_disabled": True,
        }
        driver = FakeDetailExtractionDriver(
            [page],
            {"A-1": {"订单号": "A-1", "渠道流水号": "TX-1"}},
            challenge_on_first_detail=True,
            premature_signature_reads=1,
        )

        result = await CdpDomCollectionExtractor(
            driver,
            tmp_path,
            detail_success_delay_seconds=0,
        ).extract(_spec(detail_trigger_selector=".order-detail"))

        assert result.complete is False
        assert result.detail_count == 0
        assert result.detail_failed_keys == ("A-1",)
        assert result.interrupted_by_security_challenge is True

    asyncio.run(scenario())


def test_detail_progress_resumes_only_missing_keys_after_520_circuit_breaker(
    tmp_path: Path,
) -> None:
    page = {
        "rows": [
            {"订单号": "A-1", "状态": "已完成"},
            {"订单号": "A-2", "状态": "已完成"},
            {"订单号": "A-3", "状态": "已完成"},
        ],
        "fingerprint": "page-1",
        "declared_total": 3,
        "declared_pages": 1,
        "next_exists": True,
        "next_disabled": True,
    }
    details = {
        key: {"订单号": key, "渠道流水号": f"C-{index}"}
        for index, key in enumerate(("A-1", "A-2", "A-3"), start=1)
    }

    async def scenario() -> None:
        progress_root = tmp_path / "shared-progress"
        first_driver = FakeDetailExtractionDriver(
            [page],
            details,
            transient_failures={"A-2": 2},
        )
        first_result = await CdpDomCollectionExtractor(
            first_driver,
            tmp_path / "run-1",
            detail_progress_root=progress_root,
            detail_retry_delays_seconds=(0,),
            detail_success_delay_seconds=0,
        ).extract(_spec(detail_trigger_selector=".order-detail"))

        assert first_result.complete is False
        assert first_result.detail_count == 1
        assert first_result.detail_failed_keys == ("A-2", "A-3")
        assert first_driver.navigation_counts == {"A-2": 2}
        assert any("熔断" in reason for reason in first_result.failure_reasons)
        progress_paths = list((progress_root / "structured-data").glob(".detail-progress-*.json"))
        assert len(progress_paths) == 1
        assert stat.S_IMODE(progress_paths[0].stat().st_mode) == 0o600

        second_driver = FakeDetailExtractionDriver([page], details)
        second_result = await CdpDomCollectionExtractor(
            second_driver,
            tmp_path / "run-2",
            detail_progress_root=progress_root,
            detail_retry_delays_seconds=(0,),
            detail_success_delay_seconds=0,
        ).extract(_spec(detail_trigger_selector=".order-detail"))

        assert second_result.complete is True
        assert second_result.detail_count == 3
        assert second_driver.detail_click_count == 0
        assert second_driver.navigation_counts == {"A-2": 1, "A-3": 1}
        assert not progress_paths[0].exists()
        assert second_result.json_path is not None
        payload = json.loads(second_result.json_path.read_text(encoding="utf-8"))
        assert [item["渠道流水号"] for item in payload["items"]] == ["C-1", "C-2", "C-3"]

    asyncio.run(scenario())


def test_detail_520_retries_current_key_before_continuing(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeDetailExtractionDriver(
            [
                {
                    "rows": [
                        {"订单号": "A-1", "状态": "已完成"},
                        {"订单号": "A-2", "状态": "处理中"},
                    ],
                    "fingerprint": "page-1",
                    "declared_total": 2,
                    "declared_pages": 1,
                    "next_exists": True,
                    "next_disabled": True,
                }
            ],
            {
                "A-1": {"订单号": "A-1", "渠道流水号": "C-1"},
                "A-2": {"订单号": "A-2", "渠道流水号": "C-2"},
            },
            transient_failures={"A-2": 1},
        )

        result = await CdpDomCollectionExtractor(
            driver,
            tmp_path,
            detail_retry_delays_seconds=(0,),
            detail_success_delay_seconds=0,
        ).extract(_spec(detail_trigger_selector=".order-detail"))

        assert result.complete is True
        assert driver.navigation_counts == {"A-2": 2}

    asyncio.run(scenario())


def test_detail_field_validation_drops_navigation_sized_labels() -> None:
    fields = CdpDomCollectionExtractor._validated_detail_fields(
        {
            "contains_expected": True,
            "details": {
                "订单号": "A-1",
                "首页 登录 注册 客服中心 " * 8: "导航文本",
            },
        },
        "A-1",
    )

    assert fields == {"订单号": "A-1"}


def test_detail_field_validation_rejects_identity_only_partial_render() -> None:
    with pytest.raises(ValueError, match="列表之外"):
        CdpDomCollectionExtractor._validated_detail_fields(
            {
                "contains_expected": True,
                "details": {"订单号": "A-1"},
            },
            "A-1",
            baseline_item={"订单号": "A-1", "状态": "已完成"},
        )


def test_detail_field_validation_rejects_navigation_only_partial_render() -> None:
    with pytest.raises(ValueError, match="列表之外"):
        CdpDomCollectionExtractor._validated_detail_fields(
            {
                "contains_expected": True,
                "details": {"订单号": "A-1", "订单查询/投诉": "禁售目录"},
            },
            "A-1",
            baseline_item={"订单号": "A-1"},
        )


def test_detail_field_validation_rejects_dangling_label_values() -> None:
    with pytest.raises(ValueError, match="尚未完整渲染"):
        CdpDomCollectionExtractor._validated_detail_fields(
            {
                "contains_expected": True,
                "details": {
                    "订单号": "A-1 渠道流水号",
                    "购买数量": "1件",
                    "下单时间": "2026-08-09 15:06:52 支付成功时间",
                },
            },
            "A-1",
            baseline_item={"订单号": "A-1"},
        )


def test_detail_merge_uses_stable_columns_and_skips_unchanged_list_fields() -> None:
    items = {
        "A-1": {"订单号": "A-1", "状态": "已完成"},
        "A-2": {"订单号": "A-2", "状态": "处理中"},
    }

    fields = CdpDomCollectionExtractor._merge_detail_records(
        items,
        {
            "A-1": {"订单号": "A-1", "状态": "已完成", "渠道流水号": "C-1"},
            "A-2": {"订单号": "A-2", "状态": "已完成", "渠道流水号": "C-2"},
        },
    )

    assert fields == ("详情_状态", "渠道流水号")
    assert "详情_订单号" not in items["A-1"]
    assert "详情_状态" not in items["A-1"]
    assert items["A-2"]["详情_状态"] == "已完成"
    assert [items[key]["渠道流水号"] for key in items] == ["C-1", "C-2"]


def test_detail_challenge_stops_without_export_and_preserves_interruption(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = FakeDetailExtractionDriver(
            [
                {
                    "rows": [{"订单号": "A-1", "状态": "已完成"}],
                    "fingerprint": "page-1",
                    "declared_total": 1,
                    "declared_pages": 1,
                    "next_exists": True,
                    "next_disabled": True,
                }
            ],
            {"A-1": {"订单号": "A-1"}},
            challenge_on_first_detail=True,
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(
            _spec(detail_trigger_selector=".order-detail")
        )

        assert result.complete is False
        assert result.interrupted_by_security_challenge is True
        assert result.detail_count == 0
        assert result.detail_failed_keys == ("A-1",)
        assert result.json_path is None
        assert result.csv_path is None
        assert any("安全挑战" in reason for reason in result.failure_reasons)

    asyncio.run(scenario())


def test_page_number_mode_returns_to_first_page_and_visits_every_declared_page(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        pages = [
            {
                "rows": [{"订单号": f"A-{index}", "状态": "已完成"}],
                "fingerprint": f"page-{index}",
                "declared_total": 3,
                "declared_pages": 3,
                "current_page": index,
            }
            for index in range(1, 4)
        ]
        driver = FakeExtractionDriver(pages, initial_page_index=1)
        spec = _spec(
            pagination_mode="page_number",
            next_page_selector=None,
            page_number_selector=".page-number",
            current_page_selector=".page-number.active",
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(spec)

        assert result.complete is True
        assert result.visited_pages == (1, 2, 3)
        assert result.unique_count == 3
        assert "页码遍历已覆盖页面声明总页数" in result.completion_evidence
        page_clicks = [
            command.script or ""
            for command in driver.commands
            if "WITTY_BROWSER_AUTO_CLICK_PAGE_NUMBER" in (command.script or "")
        ]
        assert [
            int(script.split("const targetPage = ", 1)[1].split(";", 1)[0])
            for script in page_clicks
        ] == [1, 2, 3]

    asyncio.run(scenario())


def test_page_number_mode_stops_when_current_page_cannot_be_read(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver(
            [
                {
                    "rows": [{"订单号": "A-1", "状态": "已完成"}],
                    "fingerprint": "page-unknown",
                    "declared_total": 1,
                    "declared_pages": 1,
                    "current_page": None,
                }
            ]
        )
        spec = _spec(
            pagination_mode="page_number",
            next_page_selector=None,
            page_number_selector=".page-number",
            current_page_selector=".page-number.active",
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(spec)

        assert result.complete is False
        assert result.visited_pages == ()
        assert result.failed_pages == (1,)
        assert any("无法读取当前页" in reason for reason in result.failure_reasons)
        assert result.json_path is None
        assert result.csv_path is None

    asyncio.run(scenario())


def test_load_more_mode_ignores_expected_snapshot_overlap(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver(
            [
                {
                    "rows": [{"订单号": "A-1", "状态": "已完成"}],
                    "fingerprint": "batch-1",
                    "declared_total": 3,
                    "declared_pages": None,
                    "pagination_exists": True,
                    "pagination_disabled": False,
                },
                {
                    "rows": [
                        {"订单号": "A-1", "状态": "已完成"},
                        {"订单号": "A-2", "状态": "已完成"},
                    ],
                    "fingerprint": "batch-2",
                    "declared_total": 3,
                    "declared_pages": None,
                    "pagination_exists": True,
                    "pagination_disabled": False,
                },
                {
                    "rows": [
                        {"订单号": "A-1", "状态": "已完成"},
                        {"订单号": "A-2", "状态": "已完成"},
                        {"订单号": "A-3", "状态": "已完成"},
                    ],
                    "fingerprint": "batch-3",
                    "declared_total": 3,
                    "declared_pages": None,
                    "pagination_exists": False,
                    "pagination_disabled": True,
                },
            ]
        )
        spec = _spec(
            pagination_mode="load_more",
            next_page_selector=None,
            load_more_selector=".load-more",
            total_pages_selector=None,
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(spec)

        assert result.complete is True
        assert result.visited_pages == (1, 2, 3)
        assert result.unique_count == 3
        assert result.duplicate_count == 0
        assert "已验证的加载更多控件在终点禁用或消失" in result.completion_evidence

    asyncio.run(scenario())


def test_infinite_scroll_requires_repeated_stable_bottom_checks(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver(
            [
                {
                    "rows": [{"订单号": "A-1", "状态": "已完成"}],
                    "fingerprint": "scroll-1",
                    "declared_total": None,
                    "declared_pages": None,
                },
                {
                    "rows": [
                        {"订单号": "A-1", "状态": "已完成"},
                        {"订单号": "A-2", "状态": "已完成"},
                    ],
                    "fingerprint": "scroll-2",
                    "declared_total": None,
                    "declared_pages": None,
                },
            ]
        )
        spec = _spec(
            pagination_mode="infinite_scroll",
            next_page_selector=None,
            total_count_selector=None,
            total_pages_selector=None,
            scroll_container_selector=".scroll-list",
            scroll_stable_rounds=2,
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(spec)

        assert result.complete is True
        assert result.visited_pages == (1, 2)
        assert result.unique_count == 2
        assert result.duplicate_count == 0
        assert any(
            "连续 2 次到达底部后列表指纹与滚动高度稳定" in item
            for item in result.completion_evidence
        )
        scroll_commands = [
            command
            for command in driver.commands
            if "WITTY_BROWSER_AUTO_SCROLL_MORE" in (command.script or "")
        ]
        assert len(scroll_commands) == 3

    asyncio.run(scenario())


def test_infinite_scroll_stops_at_max_pages_without_false_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        pages = [
            {
                "rows": [
                    {"订单号": f"A-{item}", "状态": "已完成"} for item in range(1, page_number + 1)
                ],
                "fingerprint": f"scroll-{page_number}",
                "declared_total": None,
                "declared_pages": None,
            }
            for page_number in range(1, 4)
        ]
        driver = FakeExtractionDriver(pages)
        spec = _spec(
            pagination_mode="infinite_scroll",
            next_page_selector=None,
            total_count_selector=None,
            total_pages_selector=None,
            max_pages=2,
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(spec)

        assert result.complete is False
        assert result.visited_pages == (1, 2)
        assert any("达到最大页数 2" in reason for reason in result.failure_reasons)
        assert result.json_path is None
        assert result.csv_path is None

    asyncio.run(scenario())


def test_single_page_without_completion_evidence_is_not_complete(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver(
            [
                {
                    "rows": [{"订单号": "A-1", "状态": "已完成"}],
                    "fingerprint": "page-1",
                    "declared_total": None,
                    "declared_pages": None,
                    "next_exists": False,
                    "next_disabled": True,
                }
            ]
        )
        spec = _spec(
            next_page_selector=None,
            total_count_selector=None,
            total_pages_selector=None,
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(spec)

        assert result.complete is False
        assert result.visited_pages == (1,)
        assert result.completion_evidence == ()
        assert any("完整性证据" in reason for reason in result.failure_reasons)
        assert result.json_path is None
        assert result.csv_path is None

    asyncio.run(scenario())


def test_missing_unverified_next_selector_is_not_completion_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver(
            [
                {
                    "rows": [{"订单号": "A-1", "状态": "已完成"}],
                    "fingerprint": "page-1",
                    "declared_total": None,
                    "declared_pages": None,
                    "next_exists": False,
                    "next_disabled": False,
                }
            ]
        )

        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(_spec())

        assert result.complete is False
        assert result.completion_evidence == ()
        assert any("完整性证据" in reason for reason in result.failure_reasons)
        assert result.json_path is None
        assert result.csv_path is None

    asyncio.run(scenario())


def test_extraction_waits_for_changed_fingerprint_and_stops_without_progress(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        page = {
            "rows": [{"订单号": "A-1", "状态": "处理中"}],
            "fingerprint": "same-page",
            "declared_total": 2,
            "declared_pages": 2,
            "next_exists": True,
            "next_disabled": False,
        }
        driver = FakeExtractionDriver([page, page], advance_on_click=False)
        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(_spec())

        assert result.complete is False
        assert result.visited_pages == (1,)
        assert any("指纹" in reason for reason in result.failure_reasons)
        assert result.json_path is None
        assert result.csv_path is None

    asyncio.run(scenario())


def test_extraction_rejects_count_mismatch_and_missing_unique_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        count_mismatch = FakeExtractionDriver(
            [
                {
                    "rows": [{"订单号": "A-1", "状态": "已完成"}],
                    "fingerprint": "page-1",
                    "declared_total": 2,
                    "declared_pages": 1,
                    "next_exists": True,
                    "next_disabled": True,
                }
            ]
        )
        mismatch_result = await CdpDomCollectionExtractor(
            count_mismatch,
            tmp_path / "mismatch",
        ).extract(_spec())
        assert mismatch_result.complete is False
        assert any("声明总数" in reason for reason in mismatch_result.failure_reasons)

        missing_key = FakeExtractionDriver(
            [
                {
                    "rows": [{"订单号": "", "状态": "已完成"}],
                    "fingerprint": "page-1",
                    "declared_total": 1,
                    "declared_pages": 1,
                    "next_exists": True,
                    "next_disabled": True,
                }
            ]
        )
        missing_key_result = await CdpDomCollectionExtractor(
            missing_key,
            tmp_path / "missing-key",
        ).extract(_spec())
        assert missing_key_result.complete is False
        assert any("唯一键" in reason for reason in missing_key_result.failure_reasons)

    asyncio.run(scenario())


def test_extraction_applies_prompt_defined_filter_after_full_count_validation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = FakeExtractionDriver(
            [
                {
                    "rows": [
                        {"订单号": "A-1", "状态": "已完成"},
                        {"订单号": "A-2", "状态": "处理中"},
                    ],
                    "fingerprint": "page-1",
                    "declared_total": 2,
                    "declared_pages": 1,
                    "next_exists": True,
                    "next_disabled": True,
                }
            ]
        )
        spec = _spec(filters=[{"field": "状态", "operator": "equals", "value": "已完成"}])
        result = await CdpDomCollectionExtractor(driver, tmp_path).extract(spec)

        assert result.complete is True
        assert result.unique_count == 2
        assert result.exported_count == 1
        assert result.json_path is not None
        payload = json.loads(result.json_path.read_text(encoding="utf-8"))
        assert payload["items"] == [{"订单号": "A-1", "状态": "已完成"}]

    asyncio.run(scenario())
