"""Deterministic delivery of verified task data to a user-selected directory."""

# ruff: noqa: E501

from __future__ import annotations

import csv
import filecmp
import json
import os
import re
import shutil
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from witty_browser_auto.domain.extraction import CollectionExtractionResult
from witty_browser_auto.domain.models import TaskSpec
from witty_browser_auto.domain.network_data import NetworkDataExportResult

_INVALID_XML_CHARACTERS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


@dataclass(frozen=True, slots=True)
class DeliveredOutputs:
    collection_name: str
    paths: tuple[Path, ...]

    def model_summary(self) -> dict[str, object]:
        files = [
            {
                "path": str(path),
                "format": path.suffix.removeprefix(".").lower(),
                "size_bytes": path.stat().st_size,
                "verified": path.is_file() and path.stat().st_size > 0,
            }
            for path in self.paths
        ]
        return {
            "collection_name": self.collection_name,
            "paths": [str(path) for path in self.paths],
            "formats": [str(item["format"]) for item in files],
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(int(item["size_bytes"]) for item in files),
            "verified": bool(files) and all(bool(item["verified"]) for item in files),
        }


def deliver_task_outputs(
    task: TaskSpec,
    structured_result: CollectionExtractionResult | None,
    network_result: NetworkDataExportResult | None,
) -> DeliveredOutputs | None:
    """Copy verified exports to the requested directory and create XLSX when requested."""

    if structured_result is None and network_result is None:
        return None
    collection_name, json_path, csv_path = _verified_sources(
        structured_result,
        network_result,
    )
    formats = task.output_formats or tuple(
        name for name, path in (("json", json_path), ("csv", csv_path)) if path is not None
    )
    if not formats:
        raise RuntimeError("没有可交付的数据格式")

    source_path = json_path or csv_path
    assert source_path is not None
    output_directory = (
        Path(task.output_directory).expanduser()
        if task.output_directory
        else source_path.parent / "deliverables"
    )
    output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not output_directory.is_dir():
        raise RuntimeError(f"输出目录不可用：{output_directory}")

    safe_name = _safe_filename(collection_name)
    task_suffix = _safe_filename(task.task_id.rsplit("-", 1)[-1])
    stem = f"{safe_name}-{task_suffix}"
    delivered: list[Path] = []
    for output_format in formats:
        destination = output_directory / f"{stem}.{output_format}"
        if output_format == "json":
            if json_path is None:
                raise RuntimeError("采集结果没有可交付的 JSON 文件")
            _atomic_copy(json_path, destination)
        elif output_format == "csv":
            if csv_path is None:
                raise RuntimeError("采集结果没有可交付的 CSV 文件")
            _atomic_copy(csv_path, destination)
        elif output_format == "xlsx":
            if csv_path is None:
                raise RuntimeError("采集结果没有可用于生成 Excel 的表格数据")
            _atomic_xlsx(csv_path, destination)
        else:  # TaskSpec validation owns the public error; this protects direct calls.
            raise RuntimeError(f"不支持的输出格式：{output_format}")
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise RuntimeError(f"交付文件校验失败：{destination}")
        _verify_delivered_file(
            output_format,
            destination,
            source=(json_path if output_format == "json" else csv_path),
        )
        delivered.append(destination)
    return DeliveredOutputs(collection_name, tuple(delivered))


def redeliver_verified_outputs(
    delivery: Mapping[str, Any],
    output_directory: Path,
    output_formats: Sequence[str] = (),
) -> DeliveredOutputs:
    """Reopen completed artifacts and atomically deliver them to a new directory."""

    sources = _verified_delivered_sources(delivery)
    requested_formats = tuple(dict.fromkeys(output_formats)) or tuple(sources)
    if unsupported := set(requested_formats) - {"json", "csv", "xlsx"}:
        raise RuntimeError(f"不支持的输出格式：{', '.join(sorted(unsupported))}")
    if not output_directory.is_absolute():
        raise RuntimeError("输出目录必须是绝对路径")
    output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not output_directory.is_dir():
        raise RuntimeError(f"输出目录不可用：{output_directory}")

    delivered: list[Path] = []
    for output_format in requested_formats:
        source = sources.get(output_format)
        if source is not None:
            destination = output_directory / source.name
            _atomic_copy(source, destination)
            _verify_delivered_file(output_format, destination, source=source)
        elif output_format == "xlsx" and (csv_source := sources.get("csv")) is not None:
            destination = output_directory / f"{csv_source.stem}.xlsx"
            _atomic_xlsx(csv_source, destination)
            _verify_delivered_file("xlsx", destination, source=csv_source)
        else:
            raise RuntimeError(f"已完成任务没有可用于交付 {output_format.upper()} 的源文件")
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise RuntimeError(f"交付文件校验失败：{destination}")
        delivered.append(destination)

    collection_name = str(delivery.get("collection_name", "")).strip() or "data"
    return DeliveredOutputs(collection_name, tuple(delivered))


def _verified_delivered_sources(delivery: Mapping[str, Any]) -> dict[str, Path]:
    if delivery.get("verified") is not True:
        raise RuntimeError("当前任务没有已验证的交付文件")
    raw_files = delivery.get("files")
    if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
        raise RuntimeError("当前任务的交付文件记录无效")

    sources: dict[str, Path] = {}
    for item in raw_files:
        if not isinstance(item, Mapping) or item.get("verified") is not True:
            raise RuntimeError("当前任务包含未经验证的交付文件")
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            raise RuntimeError("当前任务的交付文件路径无效")
        path = Path(raw_path).expanduser()
        output_format = str(item.get("format") or path.suffix.removeprefix(".")).casefold()
        if output_format not in {"json", "csv", "xlsx"}:
            continue
        if not path.is_absolute() or not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError(f"已验证交付源文件当前不可用：{path}")
        expected_size = item.get("size_bytes")
        if (
            isinstance(expected_size, int)
            and expected_size > 0
            and path.stat().st_size != expected_size
        ):
            raise RuntimeError(f"交付源文件大小与完成记录不一致：{path}")
        _verify_existing_output(output_format, path)
        sources[output_format] = path
    if not sources:
        raise RuntimeError("当前任务没有可重新交付的文件")
    return sources


def _verify_existing_output(output_format: str, path: Path) -> None:
    if output_format == "json":
        try:
            with path.open("r", encoding="utf-8") as source:
                json.load(source)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"JSON 交付源文件无法重新打开：{path}") from exc
    elif output_format == "csv":
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as source:
                next(csv.reader(source), None)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise RuntimeError(f"CSV 交付源文件无法重新打开：{path}") from exc
    else:
        _verify_delivered_file("xlsx", path, source=None)


def _verified_sources(
    structured_result: CollectionExtractionResult | None,
    network_result: NetworkDataExportResult | None,
) -> tuple[str, Path | None, Path | None]:
    # 模型只会在 DOM 列表结果之后进入网络路径补充更丰富字段；当该路径也通过
    # 完整性门时，应交付后生成的网络聚合结果，不能再退回字段更少的列表 CSV。
    if (
        network_result is not None
        and network_result.has_strong_completion_evidence
        and network_result.json_path.is_file()
        and network_result.csv_path is not None
        and network_result.csv_path.is_file()
    ):
        return network_result.collection_name, network_result.json_path, network_result.csv_path
    if (
        structured_result is not None
        and structured_result.complete
        and structured_result.has_strong_completion_evidence
        and structured_result.json_path is not None
        and structured_result.csv_path is not None
        and structured_result.json_path.is_file()
        and structured_result.csv_path.is_file()
    ):
        return (
            structured_result.collection_name,
            structured_result.json_path,
            structured_result.csv_path,
        )
    raise RuntimeError("任务没有通过完整性校验的可交付数据")


def _safe_filename(value: str) -> str:
    normalized = "".join(
        character
        for character in value.strip()
        if character.isalnum() or character in {"-", "_", "."}
    )
    return normalized[:100] or "data"


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_xlsx(csv_path: Path, destination: Path) -> None:
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        _write_xlsx(csv_path, temporary)
        with temporary.open("rb") as output_file:
            os.fsync(output_file.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_delivered_file(
    output_format: str,
    destination: Path,
    *,
    source: Path | None,
) -> None:
    """Reopen every delivered artifact before publishing terminal success."""

    if output_format in {"json", "csv"}:
        if source is None or not filecmp.cmp(source, destination, shallow=False):
            raise RuntimeError(f"交付文件回读不一致：{destination}")
        return
    if output_format == "xlsx":
        try:
            with zipfile.ZipFile(destination) as archive:
                if archive.testzip() is not None:
                    raise RuntimeError(f"Excel 交付文件内容损坏：{destination}")
                if "xl/worksheets/sheet1.xml" not in archive.namelist():
                    raise RuntimeError(f"Excel 交付文件缺少工作表：{destination}")
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"Excel 交付文件无法重新打开：{destination}") from exc


def _write_xlsx(csv_path: Path, destination: Path) -> None:
    row_count, column_widths = _csv_dimensions(csv_path)
    column_count = len(column_widths)
    last_cell = f"{_column_name(max(1, column_count))}{max(1, row_count)}"
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELATIONSHIPS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELATIONSHIPS)
        archive.writestr("xl/styles.xml", _STYLES)
        with archive.open("xl/worksheets/sheet1.xml", "w") as sheet:
            sheet.write(_sheet_header(last_cell, column_widths).encode("utf-8"))
            with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
                for row_index, row in enumerate(csv.reader(source), 1):
                    sheet.write(_xlsx_row(row_index, row).encode("utf-8"))
            sheet.write(_sheet_footer(last_cell, row_count, column_count).encode("utf-8"))


def _csv_dimensions(csv_path: Path) -> tuple[int, list[int]]:
    widths: list[int] = []
    row_count = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        for current_row_count, row in enumerate(csv.reader(source), 1):
            row_count = current_row_count
            if len(widths) < len(row):
                widths.extend([0] * (len(row) - len(widths)))
            for index, value in enumerate(row):
                widths[index] = min(50, max(widths[index], _display_width(value) + 2))
    return row_count, [max(10, width) for width in widths]


def _display_width(value: str) -> int:
    return sum(2 if ord(character) > 0xFF else 1 for character in value[:100])


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_row(row_index: int, row: list[str]) -> str:
    style = 1 if row_index == 1 else 2
    cells = "".join(
        (
            f'<c r="{_column_name(column_index)}{row_index}" s="{style}" '
            f't="inlineStr"><is><t xml:space="preserve">'
            f"{escape(_INVALID_XML_CHARACTERS.sub('', value))}</t></is></c>"
        )
        for column_index, value in enumerate(row, 1)
    )
    return f'<row r="{row_index}">{cells}</row>'


def _sheet_header(last_cell: str, widths: list[int]) -> str:
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_cell}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{columns}</cols><sheetData>"
    )


def _sheet_footer(last_cell: str, row_count: int, column_count: int) -> str:
    auto_filter = f'<autoFilter ref="A1:{last_cell}"/>' if row_count and column_count else ""
    return f"</sheetData>{auto_filter}</worksheet>"


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELATIONSHIPS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2563EB"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""
