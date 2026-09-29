from __future__ import annotations

from dataclasses import dataclass

from .models import DataObject, FieldRef, Relation
from .store import LineageStore


@dataclass(frozen=True, slots=True)
class BranchSource:
    source: FieldRef
    source_object: DataObject
    evidence: Relation


@dataclass(frozen=True, slots=True)
class BranchConvergence:
    target: FieldRef
    branches: dict[str, list[BranchSource]]

    @property
    def is_converged(self) -> bool:
        return len(self.branches) > 1


@dataclass(frozen=True, slots=True)
class VersionField:
    ref: FieldRef
    object: DataObject


def branch_convergence(store: LineageStore, target: FieldRef) -> BranchConvergence:
    """Group direct incoming sources by their object branch."""

    branches: dict[str, list[BranchSource]] = {}
    for relation in store.relations_to(target):
        source_object = store.get_object(relation.source.object_id)
        branches.setdefault(source_object.branch, []).append(
            BranchSource(
                source=relation.source,
                source_object=source_object,
                evidence=relation,
            )
        )
    return BranchConvergence(target=target, branches=branches)


def compare_version_fields(
    store: LineageStore,
    object_name: str,
    field_name: str,
    coordinates: list[tuple[str, str]],
) -> list[VersionField]:
    """Resolve same-named fields from explicit (version, branch) coordinates.

    Returning concrete object IDs prevents tools from treating fields that only
    share a name and object name as the same lineage node.
    """

    result: list[VersionField] = []
    seen: set[str] = set()
    for version, branch in coordinates:
        coordinate = f"{branch}/{object_name}@{version}"
        if coordinate in seen:
            raise ValueError(f"Duplicate coordinate {coordinate!r}")
        seen.add(coordinate)
        obj = store.find_object(name=object_name, version=version, branch=branch)
        ref = store.field(obj.id, field_name)
        result.append(VersionField(ref=ref, object=obj))
    return result
