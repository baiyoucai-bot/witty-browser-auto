"""浏览器工具契约注册表：工具名称、参数约束与调用方式的单一事实源。

执行层、模型 schema 和外部调用方都从同一份声明派生，避免三处各写一份后漂移。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

TOOL_CATEGORIES: tuple[str, ...] = (
    "navigation",
    "element",
    "visual",
    "page",
    "collection",
    "network",
    "tab",
    "file",
    "storage",
    "form",
    "performance",
    "emulation",
    "dialog",
    "script",
    "diagnostics",
    "lifecycle",
    "capability",
)

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_TYPE_LABELS: dict[str, str] = {
    "string": "字符串",
    "number": "数字",
    "integer": "整数",
    "boolean": "布尔值",
    "object": "对象",
    "array": "数组",
    "null": "空值",
}


class ToolContractError(ValueError):
    """工具声明本身不满足注册表契约。"""


class ToolArgumentError(ValueError):
    """调用方传入的工具参数不满足声明的约束。"""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """单个工具的完整对外契约。

    `externally_callable` 为假表示该工具由智能体循环本身处理，外部调用方不能直接执行；
    此时必须在 `unavailable_reason` 写明原因和替代路径。
    """

    name: str
    description: str
    category: str
    properties: Mapping[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    returns: str = ""
    requires_observation: bool = False
    externally_callable: bool = True
    unavailable_reason: str = ""
    idempotent: bool = True
    counts_as_action: bool = True
    requires_write_permission: bool = False

    def __post_init__(self) -> None:
        if not _TOOL_NAME.match(self.name):
            raise ToolContractError(f"工具名称不合法：{self.name}")
        if not self.description.strip():
            raise ToolContractError(f"工具 {self.name} 缺少中文说明")
        if self.category not in TOOL_CATEGORIES:
            raise ToolContractError(f"工具 {self.name} 的分类不在允许范围：{self.category}")
        for key, schema in self.properties.items():
            if not isinstance(schema, Mapping):
                raise ToolContractError(f"工具 {self.name} 的参数 {key} 必须声明 JSON Schema")
        missing = [name for name in self.required if name not in self.properties]
        if missing:
            raise ToolContractError(
                f"工具 {self.name} 的必填参数未在属性中声明：{'、'.join(missing)}"
            )
        if len(set(self.required)) != len(self.required):
            raise ToolContractError(f"工具 {self.name} 的必填参数存在重复")
        if not self.externally_callable and not self.unavailable_reason.strip():
            raise ToolContractError(f"工具 {self.name} 不开放外部调用时必须说明原因")

    def json_schema(self) -> dict[str, Any]:
        """生成 OpenAI 兼容 function schema；每次返回独立副本，调用方可安全改写。"""

        parameters: dict[str, Any] = {
            "type": "object",
            "properties": _copy_schema(self.properties),
        }
        if self.required:
            parameters["required"] = list(self.required)
        parameters["additionalProperties"] = False
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def validate_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """按声明校验参数并返回规范化副本；执行前调用可把错误留在本地。"""

        if not isinstance(arguments, Mapping):
            raise ToolArgumentError(f"工具 {self.name} 的参数必须是键值映射")
        unknown = sorted(set(arguments) - set(self.properties))
        if unknown:
            raise ToolArgumentError(f"工具 {self.name} 收到未知参数：{'、'.join(unknown)}")
        absent = [name for name in self.required if name not in arguments]
        if absent:
            raise ToolArgumentError(f"工具 {self.name} 缺少必填参数：{'、'.join(absent)}")
        validated: dict[str, Any] = {}
        for key, value in arguments.items():
            validated[key] = _validate_value(value, self.properties[key], f"{self.name}.{key}")
        return validated

    def describe(self) -> dict[str, Any]:
        """输出面向外部智能体的可读契约，用于生成调用代码或 Skill 文档。"""

        parameters = [
            {
                "name": key,
                "type": _describe_type(schema),
                "required": key in self.required,
                "description": str(schema.get("description", "")),
                "constraints": {
                    name: _copy_schema(value)
                    for name, value in schema.items()
                    if name not in {"type", "description"}
                },
            }
            for key, schema in self.properties.items()
        ]
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "parameters": parameters,
            "returns": self.returns,
            "requires_observation": self.requires_observation,
            "externally_callable": self.externally_callable,
            "unavailable_reason": self.unavailable_reason,
            "idempotent": self.idempotent,
            "counts_as_action": self.counts_as_action,
            "requires_write_permission": self.requires_write_permission,
        }


class ToolRegistry:
    """按注册顺序保存工具声明，并派生 schema、校验和说明。"""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> ToolDefinition:
        if definition.name in self._definitions:
            raise ToolContractError(f"工具 {definition.name} 重复注册，名称必须唯一")
        self._definitions[definition.name] = definition
        return definition

    def extend(self, definitions: Iterable[ToolDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._definitions

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self._definitions.values())

    def __len__(self) -> int:
        return len(self._definitions)

    def get(self, name: str) -> ToolDefinition:
        definition = self._definitions.get(name)
        if definition is None:
            raise ToolArgumentError(f"未注册的工具：{name}")
        return definition

    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(definition.json_schema() for definition in self._definitions.values())

    def schemas_for(self, names: Iterable[str]) -> tuple[dict[str, Any], ...]:
        selected = tuple(names)
        return tuple(self.get(name).json_schema() for name in selected)

    def in_category(self, category: str) -> tuple[ToolDefinition, ...]:
        if category not in TOOL_CATEGORIES:
            raise ToolContractError(f"未知的工具分类：{category}")
        return tuple(
            definition
            for definition in self._definitions.values()
            if definition.category == category
        )

    def externally_callable(self) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition in self._definitions.values()
            if definition.externally_callable
        )

    def validate_arguments(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.get(name).validate_arguments(arguments)

    def describe(self) -> tuple[dict[str, Any], ...]:
        return tuple(definition.describe() for definition in self._definitions.values())


def _copy_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_schema(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_schema(item) for item in value]
    return value


def _describe_type(schema: Mapping[str, Any]) -> str:
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared
    if isinstance(declared, list):
        return "|".join(str(item) for item in declared)
    return "any"


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "null":
        return value is None
    raise ToolContractError(f"注册表不支持的 JSON Schema 类型：{expected}")


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> Any:
    declared = schema.get("type")
    if isinstance(declared, str):
        if not _matches_type(value, declared):
            raise ToolArgumentError(f"{path} 必须是{_TYPE_LABELS[declared]}")
    elif isinstance(declared, list) and declared:
        if not any(_matches_type(value, str(item)) for item in declared):
            labels = "、".join(_TYPE_LABELS.get(str(item), str(item)) for item in declared)
            raise ToolArgumentError(f"{path} 必须是以下类型之一：{labels}")
    choices = schema.get("enum")
    if isinstance(choices, list) and value not in choices:
        raise ToolArgumentError(f"{path} 只能取声明的枚举值之一")
    if isinstance(value, str):
        _validate_string(value, schema, path)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        _validate_number(value, schema, path)
    if isinstance(value, (list, tuple)):
        return _validate_array(value, schema, path)
    if isinstance(value, Mapping):
        return _validate_object(value, schema, path)
    return value


def _validate_string(value: str, schema: Mapping[str, Any], path: str) -> None:
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and len(value) < minimum:
        raise ToolArgumentError(f"{path} 至少需要 {minimum} 个字符")
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and len(value) > maximum:
        raise ToolArgumentError(f"{path} 最多允许 {maximum} 个字符")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.match(pattern, value) is None:
        raise ToolArgumentError(f"{path} 不符合声明的格式")


def _validate_number(value: float, schema: Mapping[str, Any], path: str) -> None:
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
        raise ToolArgumentError(f"{path} 不能小于 {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
        raise ToolArgumentError(f"{path} 不能大于 {maximum}")


def _validate_array(value: Iterable[Any], schema: Mapping[str, Any], path: str) -> list[Any]:
    items = list(value)
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and len(items) < minimum:
        raise ToolArgumentError(f"{path} 至少需要 {minimum} 项")
    maximum = schema.get("maxItems")
    if isinstance(maximum, int) and len(items) > maximum:
        raise ToolArgumentError(f"{path} 最多允许 {maximum} 项")
    if schema.get("uniqueItems") is True:
        identities = [
            json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) for item in items
        ]
        if len(set(identities)) != len(identities):
            raise ToolArgumentError(f"{path} 不允许出现重复项")
    item_schema = schema.get("items")
    if not isinstance(item_schema, Mapping):
        return items
    return [
        _validate_value(item, item_schema, f"{path}[{index}]") for index, item in enumerate(items)
    ]


def _validate_object(
    value: Mapping[str, Any],
    schema: Mapping[str, Any],
    path: str,
) -> dict[str, Any]:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        absent = [str(name) for name in required if str(name) not in value]
        if absent:
            raise ToolArgumentError(f"{path} 缺少必填字段：{'、'.join(absent)}")
    extra_schema = schema.get("additionalProperties")
    validated: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        child = properties.get(name)
        if isinstance(child, Mapping):
            validated[name] = _validate_value(item, child, f"{path}.{name}")
            continue
        if extra_schema is False:
            raise ToolArgumentError(f"{path} 不接受未知字段：{name}")
        if isinstance(extra_schema, Mapping):
            validated[name] = _validate_value(item, extra_schema, f"{path}.{name}")
            continue
        validated[name] = item
    return validated
