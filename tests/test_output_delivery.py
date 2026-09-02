from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from witty_browser_auto.domain.extraction import CollectionExtractionResult
from witty_browser_auto.domain.models import ExecutionScope, TaskSpec
from witty_browser_auto.domain.network_data import NetworkDataExportResult
from witty_browser_auto.output_delivery import deliver_task_outputs, redeliver_verified_outputs

_SHEET_NAMESPACE = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _task(output_directory: Path, *formats: str) -> TaskSpec:
    return TaskSpec(
        "task-output-1234",
        "导出全部订单",
        "https://example.com/orders",
        ExecutionScope("project", allowed_origins=("https://example.com",)),
        output_directory=str(output_directory),
        output_formats=formats,
    )


def _result(tmp_path: Path) -> CollectionExtractionResult:
    source = tmp_path / "source"
    source.mkdir()
    json_path = source / "orders.json"
    csv_path = source / "orders.csv"
    json_path.write_text(
        json.dumps({"items": [{"订单号": "A-1"}, {"订单号": "A-2"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    csv_path.write_text("订单号,状态\nA-1,完成\nA-2,待处理\n", encoding="utf-8-sig")
    return CollectionExtractionResult(
        collection_name="全部订单",
        complete=True,
        unique_count=2,
        exported_count=2,
        duplicate_count=0,
        visited_pages=(1,),
        failed_pages=(),
        declared_total=2,
        declared_pages=1,
        completion_evidence=("声明总数与导出数一致",),
        failure_reasons=(),
        json_path=json_path,
        csv_path=csv_path,
        pagination_mode="none",
    )


def test_delivers_requested_xlsx_as_a_valid_private_workbook(tmp_path: Path) -> None:
    output_directory = tmp_path / "requested"

    delivered = deliver_task_outputs(
        _task(output_directory, "xlsx"),
        _result(tmp_path),
        None,
    )

    assert delivered is not None
    assert len(delivered.paths) == 1
    workbook = delivered.paths[0]
    assert workbook.parent == output_directory
    assert workbook.suffix == ".xlsx"
    assert stat.S_IMODE(workbook.stat().st_mode) == 0o600
    with zipfile.ZipFile(workbook) as archive:
        assert archive.testzip() is None
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
        sheet = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows = sheet.findall(".//s:sheetData/s:row", _SHEET_NAMESPACE)
    texts = [node.text for node in sheet.findall(".//s:t", _SHEET_NAMESPACE)]
    assert len(rows) == 3
    assert texts == ["订单号", "状态", "A-1", "完成", "A-2", "待处理"]
    assert not tuple(output_directory.glob(".*.tmp"))


def test_delivers_exact_json_and_csv_copies(tmp_path: Path) -> None:
    result = _result(tmp_path)

    delivered = deliver_task_outputs(
        _task(tmp_path / "requested", "json", "csv"),
        result,
        None,
    )

    assert delivered is not None
    json_copy, csv_copy = delivered.paths
    assert json_copy.read_bytes() == result.json_path.read_bytes()  # type: ignore[union-attr]
    assert csv_copy.read_bytes() == result.csv_path.read_bytes()  # type: ignore[union-attr]
    summary = delivered.model_summary()
    assert summary["verified"] is True
    assert summary["file_count"] == 2
    assert summary["formats"] == ["json", "csv"]
    assert all(item["size_bytes"] > 0 for item in summary["files"])  # type: ignore[index,union-attr]


def test_data_task_without_requested_directory_uses_private_source_delivery(
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)
    task = TaskSpec(
        "task-default-output",
        "导出全部订单",
        "https://example.com/orders",
        ExecutionScope("project", allowed_origins=("https://example.com",)),
    )

    delivered = deliver_task_outputs(task, result, None)

    assert delivered is not None
    assert {path.suffix for path in delivered.paths} == {".json", ".csv"}
    assert all(path.parent == result.json_path.parent / "deliverables" for path in delivered.paths)  # type: ignore[union-attr]
    assert delivered.model_summary()["verified"] is True


def test_non_data_task_does_not_create_delivery(tmp_path: Path) -> None:
    task = _task(tmp_path / "unused")

    assert deliver_task_outputs(task, None, None) is None
    assert not (tmp_path / "unused").exists()


def test_delivery_rejects_unverified_result(tmp_path: Path) -> None:
    result = _result(tmp_path)
    incomplete = CollectionExtractionResult(
        collection_name=result.collection_name,
        complete=False,
        unique_count=result.unique_count,
        exported_count=result.exported_count,
        duplicate_count=0,
        visited_pages=(1,),
        failed_pages=(2,),
        declared_total=2,
        declared_pages=2,
        completion_evidence=(),
        failure_reasons=("分页未完成",),
        json_path=result.json_path,
        csv_path=result.csv_path,
    )

    with pytest.raises(RuntimeError, match="没有通过完整性校验"):
        deliver_task_outputs(_task(tmp_path / "requested", "xlsx"), incomplete, None)


def test_complete_network_enrichment_is_preferred_over_dom_list(tmp_path: Path) -> None:
    structured = _result(tmp_path)
    network_json = tmp_path / "network.json"
    network_csv = tmp_path / "network.csv"
    network_json.write_text('{"records":[{"id":"A-1","detail":"full"}]}', encoding="utf-8")
    network_csv.write_text("id,detail\nA-1,full\n", encoding="utf-8")
    network = NetworkDataExportResult(
        candidate_id="batch-1",
        collection_name="订单详情",
        endpoint="https://example.com/api/orders",
        byte_count=64,
        body_sha256="a" * 64,
        record_count=1,
        json_path=network_json,
        csv_path=network_csv,
        complete=True,
        captured_response_count=2,
        visited_pages=(1, 2),
        declared_total=1,
        declared_pages=2,
        completion_evidence=("接口声明总数与聚合记录数一致",),
        failure_reasons=(),
    )

    delivered = deliver_task_outputs(
        _task(tmp_path / "requested", "csv"),
        structured,
        network,
    )

    assert delivered is not None
    assert delivered.collection_name == "订单详情"
    assert delivered.paths[0].read_bytes() == network_csv.read_bytes()


def test_task_rejects_relative_output_directory() -> None:
    with pytest.raises(ValueError, match="绝对路径"):
        _task(Path("relative"), "xlsx")


def test_redelivers_existing_verified_files_without_recollecting(tmp_path: Path) -> None:
    result = _result(tmp_path)
    original = deliver_task_outputs(
        _task(tmp_path / "original", "json", "csv"),
        result,
        None,
    )
    assert original is not None

    redelivered = redeliver_verified_outputs(
        original.model_summary(),
        tmp_path / "requested",
    )

    assert [path.name for path in redelivered.paths] == [path.name for path in original.paths]
    assert all(path.parent == tmp_path / "requested" for path in redelivered.paths)
    assert [path.read_bytes() for path in redelivered.paths] == [
        path.read_bytes() for path in original.paths
    ]
    assert redelivered.model_summary()["verified"] is True


def test_redelivery_can_create_xlsx_from_existing_verified_csv(tmp_path: Path) -> None:
    result = _result(tmp_path)
    original = deliver_task_outputs(
        _task(tmp_path / "original", "json", "csv"),
        result,
        None,
    )
    assert original is not None

    redelivered = redeliver_verified_outputs(
        original.model_summary(),
        tmp_path / "requested",
        ("xlsx",),
    )

    assert len(redelivered.paths) == 1
    with zipfile.ZipFile(redelivered.paths[0]) as archive:
        assert archive.testzip() is None
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
