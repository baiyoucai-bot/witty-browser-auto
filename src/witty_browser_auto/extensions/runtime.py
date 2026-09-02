"""把 Skills 和 MCP 统一适配为现有模型 function tools。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from witty_browser_auto.domain.models import Observation, TaskSpec
from witty_browser_auto.extensions.mcp import McpManager, ProjectMcpRegistry
from witty_browser_auto.extensions.skills import ProjectSkillRegistry


@dataclass(frozen=True, slots=True)
class ExtensionExecutionResult:
    success: bool
    message: str
    data: dict[str, Any]
    idempotent: bool
    counts_as_action: bool


class AgentExtensionRuntime:
    def __init__(self, project_root: Path) -> None:
        self.skills = ProjectSkillRegistry(project_root)
        self.mcp = McpManager(ProjectMcpRegistry(project_root))
        self._initialization_task: asyncio.Task[None] | None = None

    def start_background(self) -> None:
        if self._initialization_task is None:
            self._initialization_task = asyncio.create_task(
                self.mcp.initialize(),
                name="witty-mcp-initialize",
            )

    async def initialize(self) -> None:
        self.start_background()
        assert self._initialization_task is not None
        await self._initialization_task

    def prompt_context(self) -> str:
        return self.skills.prompt_context()

    def tool_schemas(
        self,
        task: TaskSpec,
        observation: Observation,
        *,
        external_tools_enabled: bool,
    ) -> list[dict[str, Any]]:
        if not external_tools_enabled:
            return []
        schemas: list[dict[str, Any]] = []
        skill_schema = self.skills.tool_schema()
        if skill_schema is not None:
            schemas.append(skill_schema)
        schemas.extend(
            self.mcp.schemas_for_context(f"{task.goal}\n{observation.title}\n{observation.summary}")
        )
        return schemas

    def handles(self, name: str) -> bool:
        return name == "load_skill" or self.mcp.handles(name)

    async def execute(self, name: str, arguments: dict[str, Any]) -> ExtensionExecutionResult:
        if name == "load_skill":
            data = self.skills.load(arguments.get("skill_id"))
            return ExtensionExecutionResult(
                True,
                f"已加载项目 Skill：{data['name']}",
                data,
                idempotent=True,
                counts_as_action=False,
            )
        success, message, data, read_only = await self.mcp.execute(name, arguments)
        return ExtensionExecutionResult(
            success,
            message,
            data,
            idempotent=read_only,
            counts_as_action=not read_only,
        )

    async def close(self) -> None:
        if self._initialization_task is not None and not self._initialization_task.done():
            self._initialization_task.cancel()
            await asyncio.gather(self._initialization_task, return_exceptions=True)
        await self.mcp.close()
