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


@dataclass(slots=True)
class DraftTransformProposal:
    """Trial replacement of one transform's active mapping set inside a draft."""

    transform_id: str
    mappings: list[tuple[str, str, str | None]]
    name: str | None = None
    version: str | None = None
    code_ref: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transform_id": self.transform_id,
            "mappings": [list(mapping) for mapping in self.mappings],
            "name": self.name,
            "version": self.version,
            "code_ref": self.code_ref,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftTransformProposal":
        return cls(
            transform_id=data["transform_id"],
            mappings=[tuple(mapping) for mapping in data.get("mappings", [])],
            name=data.get("name"),
            version=data.get("version"),
            code_ref=data.get("code_ref"),
            description=data.get("description"),
        )


@dataclass(slots=True)
class Draft:
    """An unpublished mapping proposal isolated from the effective graph."""

    id: str
    proposals: list[DraftTransformProposal]
    base_revision: int
    status: str = "open"  # open | published | withdrawn
    created_at: str | None = None
    published_revision: int | None = None
    withdrawn_revision: int | None = None
    metadata: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "proposals": [item.to_dict() for item in self.proposals],
            "base_revision": self.base_revision,
            "status": self.status,
            "created_at": self.created_at,
            "published_revision": self.published_revision,
            "withdrawn_revision": self.withdrawn_revision,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Draft":
        return cls(
            id=data["id"],
            proposals=[
                DraftTransformProposal.from_dict(item)
                for item in data.get("proposals", [])
            ],
            base_revision=data["base_revision"],
            status=data.get("status", "open"),
            created_at=data.get("created_at"),
            published_revision=data.get("published_revision"),
            withdrawn_revision=data.get("withdrawn_revision"),
            metadata=data.get("metadata", {}),
        )


@dataclass(slots=True)
class GraphRevision:
    """An immutable point on the published-graph history timeline."""

    revision: int
    kind: str  # baseline | publish | withdraw
    created_at: str
    draft_id: str | None = None
    withdrawn_from: int | None = None
    graph_before: dict[str, Any] | None = None
    graph_after: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GraphRevision":
        return cls(
            revision=data["revision"],
            kind=data["kind"],
            created_at=data["created_at"],
            draft_id=data.get("draft_id"),
            withdrawn_from=data.get("withdrawn_from"),
            graph_before=data.get("graph_before"),
            graph_after=data.get("graph_after"),
        )
