#!/usr/bin/env python3
"""检查Witty 浏览器工具库 的需求、状态与变更记录是否保持同步。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED_FILES = (
    Path("docs/requirements/WITTY_BROWSER_AUTO_REQUIREMENTS.md"),
    Path("docs/PROJECT_STATUS.md"),
    Path("docs/change_maintenance/CHANGELOG.md"),
    Path("skills/build-witty-browser-auto/SKILL.md"),
)


def validate(root: Path) -> list[str]:
    """返回所有可操作的维护错误。"""
    errors: list[str] = []

    for relative_path in REQUIRED_FILES:
        path = root / relative_path
        if not path.is_file():
            errors.append(f"缺少必要文件：{relative_path}")

    status_path = root / "docs/PROJECT_STATUS.md"
    if status_path.is_file():
        status = status_path.read_text(encoding="utf-8")
        for marker in ("已实现", "部分实现", "未实现", "阻塞"):
            if marker not in status:
                errors.append(f"项目状态缺少状态定义：{marker}")

    changelog_path = root / "docs/change_maintenance/CHANGELOG.md"
    if changelog_path.is_file():
        changelog = changelog_path.read_text(encoding="utf-8")
        for field in ("- Problem:", "- Changes:", "- Rationale:", "- Verification:", "- Risk:"):
            if field not in changelog:
                errors.append(f"变更记录缺少字段：{field}")

    skill_path = root / "skills/build-witty-browser-auto/SKILL.md"
    if skill_path.is_file() and "TODO" in skill_path.read_text(encoding="utf-8"):
        errors.append("项目专属 Skill 仍包含 TODO 占位符")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="项目根目录")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    errors = validate(root)
    if errors:
        print("项目维护状态检查失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"项目维护状态检查通过：{root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
