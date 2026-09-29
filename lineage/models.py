from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class FieldRef:
    """A stable reference to one field on one stored data object."""

    object_id: str
    field_name: str

    def as_dict(self) -> dict[str, str]:
        return {"object_id": self.object_id, "field_name": self.field_name}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "FieldRef":
        return cls(object_id=data["object_id"], field_name=data["field_name"])


@dataclass(slots=True)
class DataObject:
    id: str
    name: str
    version: str
    fields: list[str]
    branch: str = "main"
    description: str = ""
    created_at: str = field(default_factory=utc_now)

    def has_field(self, field_name: str) -> bool:
        return field_name in self.fields

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DataObject":
        return cls(**data)


@dataclass(slots=True)
class Transformation:
    id: str
    name: str
    version: str
    branch: str = "main"
    description: str = ""
    code_ref: str = ""
    created_at: str = field(default_factory=utc_now)
    deleted_at: str | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transformation":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class Relation:
    """Evidence that one transformation derives a target field from a source."""

    source: FieldRef
    target: FieldRef
    transformation_id: str
    expression: str = ""
    created_at: str = field(default_factory=utc_now)
    properties: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.as_dict(),
            "target": self.target.as_dict(),
            "transformation_id": self.transformation_id,
            "expression": self.expression,
            "created_at": self.created_at,
            "properties": self.properties,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Relation":
        return cls(
            source=FieldRef.from_dict(data["source"]),
            target=FieldRef.from_dict(data["target"]),
            transformation_id=data["transformation_id"],
            expression=data.get("expression", ""),
            created_at=data["created_at"],
            properties=data.get("properties", {}),
        )
