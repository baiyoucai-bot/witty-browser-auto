#!/usr/bin/env python3
"""清理历史轨迹和运行数据库中已落盘的敏感查询值。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from witty_browser_auto.security.migration import sanitize_runtime_artifacts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    arguments = parser.parse_args()
    report = sanitize_runtime_artifacts(arguments.artifact_root, arguments.database)
    print(
        "历史敏感数据清理完成："
        f"发现 {report.discovered_values} 个旧敏感值，"
        f"改写 {report.trace_files_changed} 个轨迹文件/"
        f"{report.trace_lines_changed} 行，"
        f"更新数据库 {report.database_cells_changed} 个字段。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
