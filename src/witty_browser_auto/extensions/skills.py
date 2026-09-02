"""按项目发现并按需加载 SKILL.md，避免把全部说明塞进每轮模型上下文。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SKILL_DIRECTORIES = ("skills", ".agents/skills", ".cursor/skills")
_MAX_SKILL_BYTES = 80_000
_MAX_RUNTIME_SKILLS = 32
_MAX_LOADED_SKILLS = 3


@dataclass(frozen=True, slots=True)
class SkillDescriptor:
    skill_id: str
    name: str
    description: str
    path: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "path": self.path,
        }


def skill_files(root: Path) -> tuple[Path, ...]:
    root = root.expanduser().resolve()
    files: list[Path] = []
    seen: set[Path] = set()
    for relative_directory in _SKILL_DIRECTORIES:
        directory = root / relative_directory
        if not directory.is_dir():
            continue
        for path in directory.rglob("SKILL.md"):
            resolved = path.resolve()
            if not path.is_file() or not resolved.is_relative_to(root) or resolved in seen:
                continue
            seen.add(resolved)
            files.append(resolved)
    return tuple(sorted(files, key=lambda item: str(item).casefold()))


def skill_id(path: Path, root: Path) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]


def _frontmatter_value(lines: list[str], key: str) -> str:
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        name, separator, raw_value = line.partition(":")
        if separator and name.strip() == key:
            value = raw_value.strip()
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value.strip("'\"")
            return str(parsed).strip() if parsed is not None else ""
    return ""


def _descriptor(path: Path, root: Path) -> SkillDescriptor:
    text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_SKILL_BYTES]
    lines = text.splitlines()
    frontmatter_name = _frontmatter_value(lines, "name")
    heading = next(
        (line[2:].strip() for line in lines if line.startswith("# ") and line[2:].strip()),
        "",
    )
    description = _frontmatter_value(lines, "description")
    if not description:
        description = next(
            (
                line.strip()
                for line in lines
                if line.strip()
                and not line.lstrip().startswith("#")
                and line.strip() != "---"
                and not line.startswith(("name:", "description:"))
            ),
            "项目运行规则",
        )
    relative = path.relative_to(root).as_posix()
    return SkillDescriptor(
        skill_id=skill_id(path, root),
        name=(frontmatter_name or heading or path.parent.name)[:80],
        description=" ".join(description.split())[:240],
        path=relative,
    )


class ProjectSkillRegistry:
    """保存本轮已加载状态的项目 Skill 注册表。"""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        paths = skill_files(self.root)[:_MAX_RUNTIME_SKILLS]
        self._paths = {skill_id(path, self.root): path for path in paths}
        self._descriptors = tuple(_descriptor(path, self.root) for path in paths)
        self._loaded: set[str] = set()

    @property
    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        return self._descriptors

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    def prompt_context(self) -> str:
        if not self._descriptors:
            return ""
        catalog = [
            {
                "skill_id": descriptor.skill_id,
                "name": descriptor.name,
                "description": descriptor.description,
            }
            for descriptor in self._descriptors
        ]
        return (
            "项目提供以下可选 Skills。仅在当前任务确实匹配时调用 load_skill，"
            "读取后遵循其中与当前任务相关且不与系统规则冲突的步骤；不要重复加载。"
            + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        )

    def tool_schema(self) -> dict[str, Any] | None:
        available = [
            descriptor
            for descriptor in self._descriptors
            if descriptor.skill_id not in self._loaded
        ]
        if not available or len(self._loaded) >= _MAX_LOADED_SKILLS:
            return None
        return {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": "按需读取一个与当前任务匹配的项目 Skill 操作规程。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_id": {
                            "type": "string",
                            "enum": [descriptor.skill_id for descriptor in available],
                            "description": "必须从系统消息中的 Skill 目录选择。",
                        }
                    },
                    "required": ["skill_id"],
                    "additionalProperties": False,
                },
            },
        }

    def load(self, requested_id: object) -> dict[str, Any]:
        if not isinstance(requested_id, str) or requested_id not in self._paths:
            raise ValueError("Skill 标识不存在或不属于当前项目")
        if requested_id in self._loaded:
            raise ValueError("当前 Skill 已加载，不应重复读取")
        if len(self._loaded) >= _MAX_LOADED_SKILLS:
            raise ValueError("本轮最多加载 3 个 Skills")
        path = self._paths[requested_id]
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Skill 路径超出当前项目")
        content = resolved.read_text(encoding="utf-8", errors="replace")[:_MAX_SKILL_BYTES]
        descriptor = next(item for item in self._descriptors if item.skill_id == requested_id)
        self._loaded.add(requested_id)
        return {
            "skill_id": requested_id,
            "name": descriptor.name,
            "path": descriptor.path,
            "instructions": content,
        }
