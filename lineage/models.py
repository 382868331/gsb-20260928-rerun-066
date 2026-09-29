from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _new_metadata() -> dict[str, Any]:
    return {}


@dataclass(slots=True)
class DataObject:
    id: str
    name: str
    version: str
    branch: str | None = None
    metadata: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataObject":
        return cls(**data)


@dataclass(slots=True)
class Field:
    id: str
    object_id: str
    name: str
    data_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Field":
        return cls(**data)


@dataclass(slots=True)
class TransformVersion:
    id: str
    name: str
    version: str
    code_ref: str | None = None
    description: str | None = None
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransformVersion":
        return cls(**data)


@dataclass(slots=True)
class Contribution:
    """One directed source-field to target-field evidence edge."""

    id: str
    transform_id: str
    source_field_id: str
    target_field_id: str
    expression: str | None = None
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contribution":
        return cls(**data)
