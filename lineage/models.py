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
    active: bool = True
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
class DraftMapping:
    """One staged transform replacement inside a draft proposal."""

    transform_id: str
    name: str
    version: str
    code_ref: str | None = None
    description: str | None = None
    mappings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftMapping":
        return cls(**data)


@dataclass(slots=True)
class DraftProposal:
    """An isolated what-if proposal staged outside the effective graph."""

    id: str
    name: str
    base_revision: int
    status: str = "open"  # open | published | discarded
    transforms: list[DraftMapping] = field(default_factory=list)
    deleted_fields: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DraftProposal":
        data = dict(data)
        data["transforms"] = [
            DraftMapping.from_dict(item) for item in data.get("transforms", [])
        ]
        return cls(**data)


@dataclass(slots=True)
class MergeConflict:
    """A user-visible conflict discovered while combining two drafts."""

    id: str
    type: str  # same_mapping | transform_metadata | deleted_field
    draft_ids: list[str]
    transform_id: str | None = None
    source_field_id: str | None = None
    target_field_id: str | None = None
    field_id: str | None = None
    options: dict[str, Any] = field(default_factory=_new_metadata)
    impact_chain: dict[str, Any] = field(default_factory=_new_metadata)
    fingerprint: str = ""
    baseline_signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MergeConflict":
        return cls(**data)


@dataclass(slots=True)
class MergeDecision:
    conflict_id: str
    choice: str
    fingerprint: str
    confirmed_revision: int = 0
    baseline_signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MergeDecision":
        return cls(**data)


@dataclass(slots=True)
class MergeSession:
    """Interactive, persistable state for merging two reviewed drafts."""

    id: str
    output_draft_id: str
    draft_ids: list[str]
    base_revision: int
    status: str = "open"  # open | published | abandoned
    decisions: list[MergeDecision] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MergeSession":
        data = dict(data)
        data["decisions"] = [
            MergeDecision.from_dict(item) for item in data.get("decisions", [])
        ]
        return cls(**data)


@dataclass(slots=True)
class HistoryEntry:
    """One committed graph change, kept forever for audit and rollback."""

    id: str
    revision: int
    action: str  # publish | withdraw
    draft_id: str | None = None
    note: str | None = None
    activated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    deactivated_fields: list[str] = field(default_factory=list)
    created_transforms: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEntry":
        return cls(**data)


@dataclass(slots=True)
class FieldSplitDecision:
    """An explicit human decision for one old-source dependency edge."""

    transform_id: str
    source_field_id: str
    target_field_id: str
    status: str  # keep_old | use_new | manual | recheck
    new_field_id: str | None = None
    interpretation: str | None = None
    rationale: str | None = None
    decided_at: str | None = None
    batch_id: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.transform_id, self.source_field_id, self.target_field_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FieldSplitDecision":
        return cls(**data)


@dataclass(slots=True)
class FieldSplitBatch:
    """A downstream-scoped, independently published set of split decisions."""

    id: str
    plan_id: str
    dependency_keys: list[list[str]] = field(default_factory=list)
    status: str = "open"  # open | published | withdrawn
    history_id: str | None = None
    published_revision: int | None = None
    published_at: str | None = None
    withdrawn_at: str | None = None
    adopted: list[dict[str, Any]] = field(default_factory=list)
    source_comparison: dict[str, Any] = field(default_factory=_new_metadata)
    affected_chain: dict[str, Any] = field(default_factory=_new_metadata)
    recheck_required: bool = False
    recheck_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FieldSplitBatch":
        return cls(**data)


@dataclass(slots=True)
class FieldSplitPlan:
    """Independent draft for splitting one old field into explicit meanings."""

    id: str
    old_field_id: str
    new_field_ids: list[str]
    base_revision: int
    status: str = "open"  # open | retired
    decisions: list[FieldSplitDecision] = field(default_factory=list)
    batches: list[FieldSplitBatch] = field(default_factory=list)
    retirement_draft_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=_new_metadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FieldSplitPlan":
        data = dict(data)
        data["decisions"] = [
            FieldSplitDecision.from_dict(item)
            for item in data.get("decisions", [])
        ]
        data["batches"] = [
            FieldSplitBatch.from_dict(item) for item in data.get("batches", [])
        ]
        return cls(**data)
