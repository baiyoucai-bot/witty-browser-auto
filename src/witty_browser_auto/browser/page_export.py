"""把当前页面导出为 PDF。"""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol

PAPER_SIZES: dict[str, tuple[float, float]] = {
    "a4": (8.27, 11.69),
    "a3": (11.69, 16.54),
    "a5": (5.83, 8.27),
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
    "tabloid": (11.0, 17.0),
}

MAX_PDF_BYTES = 80_000_000
_PAGE_RANGES_PATTERN = re.compile(r"^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$")


class ExportSession(Protocol):
    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...


def build_print_params(
    *,
    paper: str = "a4",
    landscape: bool = False,
    print_background: bool = True,
    scale: float = 1.0,
    margin_inches: float = 0.4,
    page_ranges: str = "",
    prefer_css_page_size: bool = False,
) -> dict[str, Any]:
    if paper not in PAPER_SIZES:
        raise ValueError(f"不支持的纸张：{paper}，可选 {'、'.join(sorted(PAPER_SIZES))}")
    if not 0.1 <= scale <= 2:
        raise ValueError("scale 必须在 0.1 到 2 之间")
    if not 0 <= margin_inches <= 5:
        raise ValueError("margin_inches 必须在 0 到 5 之间")
    if page_ranges and not _PAGE_RANGES_PATTERN.match(page_ranges):
        raise ValueError("page_ranges 格式无效，示例：1-3 或 1,3,5-7")
    width, height = PAPER_SIZES[paper]
    params: dict[str, Any] = {
        "landscape": landscape,
        "printBackground": print_background,
        "scale": scale,
        "paperWidth": width,
        "paperHeight": height,
        "marginTop": margin_inches,
        "marginBottom": margin_inches,
        "marginLeft": margin_inches,
        "marginRight": margin_inches,
        "preferCSSPageSize": prefer_css_page_size,
        # 不走流式返回，正文直接在响应里，省掉一轮 IO.read。
        "transferMode": "ReturnAsBase64",
    }
    if page_ranges:
        params["pageRanges"] = page_ranges
    return params


async def export_pdf(
    session: ExportSession,
    directory: Path,
    *,
    label: str = "page",
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """导出 PDF 并以私有权限独占写入文件。"""

    result = await session.call("Page.printToPDF", params or build_print_params())
    data = result.get("data")
    if not isinstance(data, str) or not data:
        raise RuntimeError("浏览器未返回 PDF 数据")
    payload = base64.b64decode(data)
    if not payload.startswith(b"%PDF"):
        raise RuntimeError("浏览器返回的数据不是 PDF")
    if len(payload) > MAX_PDF_BYTES:
        raise RuntimeError(f"PDF 超过 {MAX_PDF_BYTES} 字节上限")
    path = write_pdf_file(payload, directory, label=label)
    return {"pdf_path": str(path), "bytes": len(payload)}


def write_pdf_file(payload: bytes, directory: Path, *, label: str) -> Path:
    """以私有权限独占写入 PDF；标签里的路径分隔符一律剔除。"""

    safe_label = "".join(char for char in label if char.isalnum() or char in "-_") or "page"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{safe_label}-{time.time_ns()}.pdf"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    return path
