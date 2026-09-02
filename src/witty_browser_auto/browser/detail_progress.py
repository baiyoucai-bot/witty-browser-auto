"""详情批量采集的私有断点存储。"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from witty_browser_auto.domain.extraction import CollectionExtractionSpec

_PROGRESS_VERSION = 1
_MAX_PROGRESS_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DetailProgress:
    route_prefix: str
    route_suffix: str
    source_origin: str
    details_by_key: dict[str, dict[str, str]]


class DetailProgressStore:
    """按采集规格隔离断点，并用唯一键集合摘要拒绝复用过期数据。"""

    def __init__(
        self,
        artifact_root: Path,
        spec: CollectionExtractionSpec,
        unique_keys: Iterable[str],
    ) -> None:
        identity = json.dumps(
            {
                "collection": spec.collection_name,
                "row_selector": spec.row_selector,
                "unique_key": spec.unique_key,
                "detail_trigger_selector": spec.detail_trigger_selector,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.output_root = artifact_root / "structured-data"
        self.path = self.output_root / f".detail-progress-{sha256(identity).hexdigest()[:16]}.json"
        self.unique_keys = tuple(sorted(set(unique_keys)))
        self.unique_keys_hash = _keys_hash(self.unique_keys)

    def load(self) -> DetailProgress | None:
        if not self.path.is_file() or self.path.is_symlink():
            return None
        if self.path.stat().st_size > _MAX_PROGRESS_BYTES:
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping):
            return None
        if value.get("version") != _PROGRESS_VERSION:
            return None
        if value.get("unique_keys_hash") != self.unique_keys_hash:
            return None
        route_prefix = value.get("route_prefix")
        route_suffix = value.get("route_suffix")
        source_origin = value.get("source_origin")
        if not all(isinstance(item, str) for item in (route_prefix, route_suffix, source_origin)):
            return None
        if not route_prefix or len(route_prefix) > 4096 or len(route_suffix) > 4096:
            return None
        raw_details = value.get("details_by_key")
        if not isinstance(raw_details, Mapping):
            return None
        expected_keys = set(self.unique_keys)
        details_by_key: dict[str, dict[str, str]] = {}
        for raw_key, raw_fields in raw_details.items():
            if not isinstance(raw_key, str) or raw_key not in expected_keys:
                return None
            fields = _validated_fields(raw_fields)
            if fields is None:
                return None
            details_by_key[raw_key] = fields
        return DetailProgress(
            route_prefix=route_prefix,
            route_suffix=route_suffix,
            source_origin=source_origin,
            details_by_key=details_by_key,
        )

    def save(self, progress: DetailProgress) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.output_root, 0o700)
        content = json.dumps(
            {
                "version": _PROGRESS_VERSION,
                "unique_keys_hash": self.unique_keys_hash,
                "route_prefix": progress.route_prefix,
                "route_suffix": progress.route_suffix,
                "source_origin": progress.source_origin,
                "details_by_key": progress.details_by_key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(content) > _MAX_PROGRESS_BYTES:
            raise ValueError("详情采集断点超过私有文件大小限制")
        temporary = self.output_root / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def _keys_hash(unique_keys: Iterable[str]) -> str:
    serialized = json.dumps(
        tuple(unique_keys),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _validated_fields(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or not value:
        return None
    fields: dict[str, str] = {}
    for raw_label, raw_value in value.items():
        if not isinstance(raw_label, str) or not isinstance(raw_value, str):
            return None
        label = raw_label.strip()
        field_value = raw_value.strip()
        if not label or len(label) > 64 or not field_value or len(field_value) > 4000:
            return None
        fields[label] = field_value
    return fields
