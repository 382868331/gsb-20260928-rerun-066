from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Any, Iterable

from .exceptions import (
    AlreadyExistsError,
    NotFoundError,
    StaleBaselineError,
    ValidationError,
)
from .models import (
    Contribution,
    DataObject,
    DraftMapping,
    DraftProposal,
    FieldSplit,
    Field,
    HistoryEntry,
    MigrationBatch,
    MergeConflict,
    MergeDecision,
    MergeSession,
    TransformVersion,
)
from .store import JsonLineageStore


@dataclass(slots=True)
class LineageNode:
    field: Field
    object: DataObject
    distance: int
    paths: list[list[str]]


@dataclass(slots=True)
class LineageQueryResult:
    start_field_id: str
    direct: dict[str, LineageNode]
    transitive: dict[str, LineageNode]
    edges: dict[str, Contribution]
    cycles: list[list[str]]

    def all_nodes(self) -> dict[str, LineageNode]:
        return {**self.direct, **self.transitive}


class LineageService:
    def __init__(self, store: JsonLineageStore):
        self.store = store

    def create_object(
        self,
        object_id: str,
        name: str,
        version: str,
        *,
        branch: str | None = None,
        metadata: dict[str, Any] | None = None,
        exist_ok: bool = False,
    ) -> DataObject:
        existing = self.store.objects.get(object_id)
        if existing is not None:
            if not exist_ok:
                raise AlreadyExistsError(f"data object '{object_id}' already exists")
            return existing
        obj = DataObject(
            id=object_id,
            name=name,
            version=version,
            branch=branch,
            metadata=metadata or {},
        )
        self.store.objects[object_id] = obj
        self.store.save()
        return obj

    def create_field(
        self,
        field_id: str,
        object_id: str,
        name: str,
        *,
        data_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        exist_ok: bool = False,
    ) -> Field:
        existing = self.store.fields.get(field_id)
        if existing is not None:
            if not exist_ok:
                raise AlreadyExistsError(f"field '{field_id}' already exists")
            return existing
        if object_id not in self.store.objects:
            raise NotFoundError(f"data object '{object_id}' does not exist")
        duplicate = next(
            (
                item
                for item in self.store.fields.values()
                if item.object_id == object_id and item.name == name
            ),
            None,
        )
        if duplicate is not None:
            raise AlreadyExistsError(
                f"field name '{name}' already exists in object '{object_id}'"
            )
        field_item = Field(
            id=field_id,
            object_id=object_id,
            name=name,
            data_type=data_type,
            metadata=metadata or {},
        )
        self.store.fields[field_id] = field_item
        self.store.save()
        return field_item

    def create_transform(
        self,
        transform_id: str,
        name: str,
        version: str,
        *,
        code_ref: str | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
        exist_ok: bool = False,
    ) -> TransformVersion:
        existing = self.store.transforms.get(transform_id)
        if existing is not None:
            if not exist_ok:
                raise AlreadyExistsError(
                    f"transform '{transform_id}' already exists"
                )
            return existing
        transform = TransformVersion(
            id=transform_id,
            name=name,
            version=version,
            code_ref=code_ref,
            description=description,
            metadata=metadata or {},
        )
        self.store.transforms[transform_id] = transform
        self.store.save()
        return transform

    def add_contribution(
        self,
        source_field_id: str,
        target_field_id: str,
        transform_id: str,
        *,
        expression: str | None = None,
        metadata: dict[str, Any] | None = None,
        exist_ok: bool = False,
    ) -> Contribution:
        if source_field_id not in self.store.fields:
            raise NotFoundError(f"source field '{source_field_id}' does not exist")
        if target_field_id not in self.store.fields:
            raise NotFoundError(f"target field '{target_field_id}' does not exist")
        if transform_id not in self.store.transforms:
            raise NotFoundError(f"transform '{transform_id}' does not exist")
        if source_field_id == target_field_id:
            raise ValidationError("a field cannot contribute to itself")
        existing = next(
            (
                item
                for item in self.store.contributions.values()
                if item.source_field_id == source_field_id
                and item.target_field_id == target_field_id
                and item.transform_id == transform_id
            ),
            None,
        )
        if existing is not None:
            if not exist_ok:
                raise AlreadyExistsError("identical contribution already exists")
            return existing
        contribution_id = (
            f"contrib:{transform_id}:{source_field_id}->{target_field_id}"
        )
        if contribution_id in self.store.contributions:
            raise AlreadyExistsError(f"contribution '{contribution_id}' exists")
        contribution = Contribution(
            id=contribution_id,
            transform_id=transform_id,
            source_field_id=source_field_id,
            target_field_id=target_field_id,
            expression=expression,
            metadata=metadata or {},
        )
        self.store.contributions[contribution_id] = contribution
        self.store.save()
        return contribution

    def _check_mapping_fields(self, source_field_id: str, target_field_id: str) -> None:
        source = self.store.fields.get(source_field_id)
        target = self.store.fields.get(target_field_id)
        if source is None:
            raise NotFoundError(f"source field '{source_field_id}' does not exist")
        if target is None:
            raise NotFoundError(f"target field '{target_field_id}' does not exist")
        if not source.active or not target.active:
            raise ValidationError("cannot map to a deleted field")
        if source_field_id == target_field_id:
            raise ValidationError("a field cannot contribute to itself")

    @staticmethod
    def _contribution_id(transform_id: str, source_field_id: str, target_field_id: str) -> str:
        return f"contrib:{transform_id}:{source_field_id}->{target_field_id}"

    def submit_transform(
        self,
        transform_id: str,
        name: str,
        version: str,
        mappings: Iterable[tuple[str, str, str | None]],
        *,
        code_ref: str | None = None,
        description: str | None = None,
        transform_metadata: dict[str, Any] | None = None,
        exist_ok: bool = False,
    ) -> list[Contribution]:
        """Create one transform and all of its source-to-target edges atomically.

        Every mapping is validated before anything is mutated, and the whole
        batch is persisted with a single save. If validation or persistence
        fails, neither memory nor disk keeps any part of the batch.
        """

        mappings = list(mappings)
        if not mappings:
            raise ValidationError("a transform must contribute at least one mapping")
        transform = self.store.transforms.get(transform_id)
        transform_created = transform is None
        if transform_created:
            transform = TransformVersion(
                id=transform_id,
                name=name,
                version=version,
                code_ref=code_ref,
                description=description,
                metadata=transform_metadata or {},
            )
        elif not exist_ok:
            raise AlreadyExistsError(f"transform '{transform_id}' already exists")

        planned: list[Contribution] = []
        reused: list[Contribution] = []
        planned_keys: set[tuple[str, str]] = set()
        for source_id, target_id, expression in mappings:
            self._check_mapping_fields(source_id, target_id)
            key = (source_id, target_id)
            if key in planned_keys:
                if not exist_ok:
                    raise AlreadyExistsError("identical contribution already exists")
                continue
            planned_keys.add(key)
            contribution_id = self._contribution_id(transform_id, source_id, target_id)
            existing = self.store.contributions.get(contribution_id)
            if existing is not None:
                if not exist_ok:
                    raise AlreadyExistsError("identical contribution already exists")
                reused.append(existing)
                continue
            planned.append(
                Contribution(
                    id=contribution_id,
                    transform_id=transform_id,
                    source_field_id=source_id,
                    target_field_id=target_id,
                    expression=expression,
                )
            )

        added_ids = [contribution.id for contribution in planned]
        try:
            if transform_created:
                self.store.transforms[transform_id] = transform
            for contribution in planned:
                self.store.contributions[contribution.id] = contribution
            self.store.save()
        except Exception:
            for contribution_id in added_ids:
                self.store.contributions.pop(contribution_id, None)
            if transform_created:
                self.store.transforms.pop(transform_id, None)
            raise
        return [*reused, *planned]

    def replace_transform(
        self,
        transform_id: str,
        mappings: Iterable[tuple[str, str, str | None]],
    ) -> list[Contribution]:
        """Atomically replace the active contribution set of one transform.

        Old contributions not present in the new set are soft-deleted, matching
        existing ones are reactivated with their new expression, and new edges
        are added, all within a single save. Old and new contributions never
        appear mixed in one persisted version; on failure every change is
        rolled back in memory so memory and disk stay consistent.
        """

        transform = self.store.transforms.get(transform_id)
        if transform is None:
            raise NotFoundError(f"transform '{transform_id}' does not exist")
        mappings = list(mappings)
        if not mappings:
            raise ValidationError("a transform must contribute at least one mapping")

        keep_ids: list[str] = []
        keep_set: set[str] = set()
        to_add: list[Contribution] = []
        to_update: list[tuple[Contribution, str | None]] = []
        planned_keys: set[tuple[str, str]] = set()
        for source_id, target_id, expression in mappings:
            self._check_mapping_fields(source_id, target_id)
            key = (source_id, target_id)
            if key in planned_keys:
                raise AlreadyExistsError("identical contribution already exists")
            planned_keys.add(key)
            contribution_id = self._contribution_id(transform_id, source_id, target_id)
            existing = self.store.contributions.get(contribution_id)
            if existing is not None:
                to_update.append((existing, expression))
            else:
                to_add.append(
                    Contribution(
                        id=contribution_id,
                        transform_id=transform_id,
                        source_field_id=source_id,
                        target_field_id=target_id,
                        expression=expression,
                    )
                )
            keep_ids.append(contribution_id)
            keep_set.add(contribution_id)

        to_deactivate = [
            contribution
            for contribution in self.store.contributions.values()
            if contribution.transform_id == transform_id
            and contribution.active
            and contribution.id not in keep_set
        ]

        saved_states = {
            contribution.id: (contribution.active, contribution.expression)
            for contribution in to_deactivate
        }
        saved_states.update(
            {
                contribution.id: (contribution.active, contribution.expression)
                for contribution, _ in to_update
            }
        )
        transform_was_active = transform.active
        added_ids = [contribution.id for contribution in to_add]
        try:
            for contribution in to_deactivate:
                contribution.active = False
            for contribution, expression in to_update:
                contribution.active = True
                contribution.expression = expression
            for contribution in to_add:
                self.store.contributions[contribution.id] = contribution
            transform.active = True
            self.store.save()
        except Exception:
            for contribution_id in added_ids:
                self.store.contributions.pop(contribution_id, None)
            for contribution_id, (active, expression) in saved_states.items():
                contribution = self.store.contributions.get(contribution_id)
                if contribution is not None:
                    contribution.active = active
                    contribution.expression = expression
            transform.active = transform_was_active
            raise
        return [self.store.contributions[contribution_id] for contribution_id in keep_ids]

    def delete_transform(self, transform_id: str) -> list[str]:
        """Withdraw only edges contributed by one transform version."""

        transform = self.store.transforms.get(transform_id)
        if transform is None:
            raise NotFoundError(f"transform '{transform_id}' does not exist")
        affected = [
            contribution_id
            for contribution_id, item in self.store.contributions.items()
            if item.transform_id == transform_id and item.active
        ]
        for contribution_id in affected:
            self.store.contributions[contribution_id].active = False
        transform.active = False
        try:
            self.store.save()
        except Exception:
            for contribution_id in affected:
                self.store.contributions[contribution_id].active = True
            transform.active = True
            raise
        return affected

    def restore_transform(self, transform_id: str) -> list[str]:
        transform = self.store.transforms.get(transform_id)
        if transform is None:
            raise NotFoundError(f"transform '{transform_id}' does not exist")
        affected = [
            contribution_id
            for contribution_id, item in self.store.contributions.items()
            if item.transform_id == transform_id and not item.active
        ]
        for contribution_id in affected:
            self.store.contributions[contribution_id].active = True
        transform.active = True
        try:
            self.store.save()
        except Exception:
            for contribution_id in affected:
                self.store.contributions[contribution_id].active = False
            transform.active = False
            raise
        return affected

    def field_identity_report(self, field_id: str) -> dict[str, Any]:
        """Return object/version/branch context needed to disambiguate names."""

        field_item = self._require_field(field_id)
        obj = self._require_object(field_item.object_id)
        same_name = [
            {
                "field_id": other.id,
                "object_id": other.object_id,
                "object_name": self._require_object(other.object_id).name,
                "object_version": self._require_object(other.object_id).version,
                "branch": self._require_object(other.object_id).branch,
            }
            for other in self.store.fields.values()
            if other.name == field_item.name and other.id != field_item.id
        ]
        return {
            "field_id": field_item.id,
            "field_name": field_item.name,
            "object_id": obj.id,
            "object_name": obj.name,
            "object_version": obj.version,
            "branch": obj.branch,
            "same_name_other_versions": same_name,
        }

    def upstream(self, field_id: str) -> LineageQueryResult:
        return self._traverse(
            field_id, lambda edge: edge.target_field_id, lambda edge: edge.source_field_id
        )

    def impact(self, field_id: str) -> LineageQueryResult:
        return self._traverse(
            field_id, lambda edge: edge.source_field_id, lambda edge: edge.target_field_id
        )

    def _traverse(
        self,
        start_field_id: str,
        incoming_key,
        next_key,
    ) -> LineageQueryResult:
        self._require_field(start_field_id)
        adjacency: dict[str, list[Contribution]] = defaultdict(list)
        active_edges: dict[str, Contribution] = {}
        for edge in self.store.contributions.values():
            if not edge.active:
                continue
            transform = self.store.transforms.get(edge.transform_id)
            if transform is None or not transform.active:
                continue
            source = self.store.fields.get(edge.source_field_id)
            target = self.store.fields.get(edge.target_field_id)
            if source is None or target is None or not source.active or not target.active:
                continue
            adjacency[incoming_key(edge)].append(edge)
            active_edges[edge.id] = edge

        direct: dict[str, LineageNode] = {}
        transitive: dict[str, LineageNode] = {}
        used_edges: dict[str, Contribution] = {}
        first_distance: dict[str, int] = {}
        first_path: dict[str, list[str]] = {}
        queue = deque(
            (edge, [start_field_id])
            for edge in adjacency[start_field_id]
        )
        while queue:
            edge, path = queue.popleft()
            next_id = next_key(edge)
            used_edges[edge.id] = edge
            if next_id == start_field_id:
                continue
            distance = len(path)
            if next_id not in first_distance or distance < first_distance[next_id]:
                next_path = [*path, next_id]
                first_distance[next_id] = distance
                first_path[next_id] = next_path
                for next_edge in adjacency[next_id]:
                    queue.append((next_edge, next_path))
        for field_id, distance in first_distance.items():
            node = self._node(field_id, distance)
            node.paths.append(first_path[field_id])
            if distance == 1:
                direct[field_id] = node
            else:
                transitive[field_id] = node
        for group in (direct, transitive):
            for node in group.values():
                node.paths = [first_path[node.field.id]]
        cycles = self._find_cycles(start_field_id, adjacency, next_key)
        return LineageQueryResult(
            start_field_id=start_field_id,
            direct=direct,
            transitive=transitive,
            edges=used_edges,
            cycles=cycles,
        )

    def _find_cycles(
        self,
        start_field_id: str,
        adjacency: dict[str, list[Contribution]],
        next_key,
    ) -> list[list[str]]:
        color: dict[str, str] = {}
        stack: list[str] = []
        cycles: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        def visit(node_id: str) -> None:
            color[node_id] = "gray"
            stack.append(node_id)
            for edge in adjacency[node_id]:
                child_id = next_key(edge)
                if color.get(child_id) == "gray":
                    cycle_nodes = stack[stack.index(child_id) :]
                    normalized = tuple(self._normalize_cycle(cycle_nodes))
                    if normalized not in seen:
                        seen.add(normalized)
                        cycles.append([*cycle_nodes, child_id])
                elif color.get(child_id) != "black":
                    visit(child_id)
            stack.pop()
            color[node_id] = "black"

        visit(start_field_id)
        return cycles

    @staticmethod
    def _normalize_cycle(cycle: list[str]) -> list[str]:
        nodes = cycle[:-1]
        rotations = [nodes[index:] + nodes[:index] for index in range(len(nodes))]
        return min(rotations)

    def _node(self, field_id: str, distance: int) -> LineageNode:
        field_item = self._require_field(field_id)
        obj = self._require_object(field_item.object_id)
        return LineageNode(field=field_item, object=obj, distance=distance, paths=[])

    def _require_field(self, field_id: str) -> Field:
        try:
            return self.store.fields[field_id]
        except KeyError:
            raise NotFoundError(f"field '{field_id}' does not exist") from None

    def _require_object(self, object_id: str) -> DataObject:
        try:
            return self.store.objects[object_id]
        except KeyError:
            raise NotFoundError(f"data object '{object_id}' does not exist") from None

    # --- Field splits and phased migration -------------------------------

    def create_field_split(
        self,
        split_id: str,
        old_field_id: str,
        new_field_ids: Iterable[str],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> FieldSplit:
        """Create an explicit split proposal without changing the graph."""

        if split_id in self.store.field_splits:
            raise AlreadyExistsError(f"field split '{split_id}' already exists")
        old_field = self._require_field(old_field_id)
        if not old_field.active:
            raise ValidationError(f"old field '{old_field_id}' is not active")
        new_field_ids = list(new_field_ids)
        if not new_field_ids:
            raise ValidationError("a field split must declare at least one new field")
        if len(set(new_field_ids)) != len(new_field_ids):
            raise ValidationError("new field ids must be unique")
        if old_field_id in new_field_ids:
            raise ValidationError("a split field cannot also be the old field")
        for field_id in new_field_ids:
            field_item = self._require_field(field_id)
            if not field_item.active:
                raise ValidationError(f"new field '{field_id}' is not active")
            if field_item.name == old_field.name:
                raise ValidationError(
                    "a similar field name is not evidence that fields are equivalent"
                )
        split = FieldSplit(
            id=split_id,
            old_field_id=old_field_id,
            new_field_ids=list(new_field_ids),
            base_revision=self.store.revision,
            metadata=metadata or {},
        )
        self.store.field_splits[split_id] = split
        self.store.save()
        return split

    def _require_field_split(self, split_id: str) -> FieldSplit:
        try:
            split = self.store.field_splits[split_id]
        except KeyError:
            raise NotFoundError(f"field split '{split_id}' does not exist") from None
        if split.status != "open":
            raise ValidationError(f"field split '{split_id}' is {split.status}")
        return split

    def _split_edge_id(self, split: FieldSplit, transform_id: str, target_field_id: str) -> str:
        return self._contribution_id(
            transform_id, split.old_field_id, target_field_id
        )

    def record_split_decision(
        self,
        split_id: str,
        transform_id: str,
        target_field_id: str,
        new_field_id: str,
        *,
        new_target_field_id: str | None = None,
        rationale: str | None = None,
    ) -> FieldSplit:
        """Record a human-reviewed exact edge-to-new-field interpretation."""

        split = self._require_field_split(split_id)
        edge_id = self._split_edge_id(split, transform_id, target_field_id)
        edge = self.store.contributions.get(edge_id)
        if edge is None or not edge.active:
            raise NotFoundError(
                f"active dependency {split.old_field_id} -> {target_field_id} "
                f"for transform '{transform_id}' does not exist"
            )
        transform = self.store.transforms.get(transform_id)
        if transform is None or not transform.active:
            raise NotFoundError(f"transform '{transform_id}' does not exist")
        if new_field_id not in split.new_field_ids:
            raise ValidationError(
                f"field '{new_field_id}' is not declared by split '{split_id}'"
            )
        if new_target_field_id is not None:
            new_target = self._require_field(new_target_field_id)
            if not new_target.active:
                raise ValidationError(
                    f"new target field '{new_target_field_id}' is not active"
                )
        split.decisions[edge_id] = {
            "status": "explicit",
            "transform_id": transform_id,
            "old_field_id": split.old_field_id,
            "target_field_id": target_field_id,
            "new_field_id": new_field_id,
            "new_target_field_id": new_target_field_id,
            "rationale": rationale,
        }
        self.store.save()
        return split

    def record_manual_split_dependency(
        self,
        split_id: str,
        transform_id: str,
        target_field_id: str,
        reason: str,
    ) -> FieldSplit:
        """Mark an old-field dependency as requiring human interpretation."""

        if not reason:
            raise ValidationError("a manual review reason is required")
        split = self._require_field_split(split_id)
        edge_id = self._split_edge_id(split, transform_id, target_field_id)
        edge = self.store.contributions.get(edge_id)
        if edge is None or not edge.active:
            raise NotFoundError(
                f"active dependency {split.old_field_id} -> {target_field_id} "
                f"for transform '{transform_id}' does not exist"
            )
        split.decisions[edge_id] = {
            "status": "manual",
            "transform_id": transform_id,
            "old_field_id": split.old_field_id,
            "target_field_id": target_field_id,
            "reason": reason,
        }
        self.store.save()
        return split

    def _active_old_edges(self, split: FieldSplit) -> list[Contribution]:
        return [
            edge
            for edge in self._effective_edges().values()
            if edge.source_field_id == split.old_field_id
        ]

    def split_report(self, split_id: str) -> dict[str, Any]:
        split = self._require_field_split(split_id)
        old_dependencies: list[dict[str, Any]] = []
        adoptable: list[dict[str, Any]] = []
        adopted: list[dict[str, Any]] = []
        manual: list[dict[str, Any]] = []
        decided_old_targets = {
            (item["transform_id"], item["target_field_id"]): item
            for item in split.decisions.values()
        }
        decided_new_targets = {
            (
                item["transform_id"],
                item.get("new_target_field_id") or item["target_field_id"],
            ): item
            for item in split.decisions.values()
        }

        for edge in sorted(self._active_old_edges(split), key=lambda item: item.id):
            decision = split.decisions.get(edge.id)
            payload = {
                "transform_id": edge.transform_id,
                "old_field_id": edge.source_field_id,
                "target_field_id": edge.target_field_id,
                "expression": edge.expression,
                "impact_chain": self._impact_field_chain(
                    edge.target_field_id, downstream=True
                ),
            }
            if decision is None:
                old_dependencies.append(payload)
            elif decision.get("status") == "manual":
                manual.append({**payload, "reason": decision.get("reason")})
            else:
                adoptable.append({**payload, "new_field_id": decision["new_field_id"]})

        for edge in sorted(self._effective_edges().values(), key=lambda item: item.id):
            if edge.source_field_id not in split.new_field_ids:
                continue
            decision = decided_new_targets.get((edge.transform_id, edge.target_field_id))
            expected_target = edge.target_field_id
            if decision is not None and (
                decision.get("new_target_field_id")
                or decision["target_field_id"]
            ) != expected_target:
                decision = None
            if (
                decision is None
                or decision.get("status") != "explicit"
                or decision.get("transform_id") != edge.transform_id
                or decision.get("new_field_id") != edge.source_field_id
            ):
                continue
            adopted.append(
                {
                    "transform_id": edge.transform_id,
                    "old_field_id": split.old_field_id,
                    "new_field_id": edge.source_field_id,
                    "target_field_id": edge.target_field_id,
                    "expression": edge.expression,
                }
            )

        return {
            "split_id": split.id,
            "old_field_id": split.old_field_id,
            "new_field_ids": list(split.new_field_ids),
            "old_dependencies": old_dependencies,
            "adoptable_dependencies": adoptable,
            "adopted_dependencies": adopted,
            "manual_dependencies": manual,
            "remaining_old_dependency_count": (
                len(old_dependencies) + len(manual) + len(adoptable)
            ),
        }

    def plan_split_migration(
        self, split_id: str, downstream_field_ids: Iterable[str]
    ) -> dict[str, Any]:
        """Preview a scope and detect shared-chain/parallel-draft conflicts."""

        split = self._require_field_split(split_id)
        scope = sorted(set(downstream_field_ids))
        if not scope:
            raise ValidationError("migration scope must not be empty")
        for field_id in scope:
            self._require_field(field_id)
        edges = [
            edge
            for edge in self._active_old_edges(split)
            if edge.target_field_id in scope
        ]
        decisions = [split.decisions[edge.id] for edge in edges if edge.id in split.decisions]
        unresolved = [
            {
                "transform_id": edge.transform_id,
                "old_field_id": edge.source_field_id,
                "target_field_id": edge.target_field_id,
                "reason": "no explicit split decision",
            }
            for edge in edges
            if edge.id not in split.decisions
        ]
        unresolved.extend(
            {
                "transform_id": item["transform_id"],
                "old_field_id": item["old_field_id"],
                "target_field_id": item["target_field_id"],
                "reason": item.get("reason", "manual interpretation required"),
            }
            for item in decisions
            if item.get("status") == "manual"
        )

        conflicts: list[dict[str, Any]] = []
        effective = self._effective_edges()
        outgoing: dict[str, list[Contribution]] = defaultdict(list)
        for edge in effective.values():
            outgoing[edge.source_field_id].append(edge)
        selected = {
            edge.id: split.decisions[edge.id]
            for edge in edges
            if split.decisions.get(edge.id, {}).get("status") == "explicit"
        }
        for edge_id, decision in sorted(selected.items()):
            target = decision["target_field_id"]
            if decision.get("new_target_field_id") is not None:
                continue
            affected_consumers: list[str] = []
            queue = deque(item.target_field_id for item in outgoing.get(target, []))
            seen: set[str] = set()
            while queue:
                consumer = queue.popleft()
                if consumer in seen:
                    continue
                seen.add(consumer)
                if consumer not in scope:
                    affected_consumers.append(consumer)
                queue.extend(item.target_field_id for item in outgoing.get(consumer, []))
            affected_consumers.sort()
            if affected_consumers:
                conflicts.append(
                    {
                        "id": f"split-conflict:{split.id}:shared:{target}",
                        "type": "shared_intermediate",
                        "transform_id": decision["transform_id"],
                        "old_field_id": split.old_field_id,
                        "new_field_id": decision["new_field_id"],
                        "intermediate_field_id": target,
                        "affected_chain": [target, *sorted(affected_consumers)],
                        "message": "migrate the shared result itself or provide a new path",
                    }
                )

        scope_set = set(scope)
        for draft in self.store.drafts.values():
            if draft.status != "open" or draft.metadata.get("split_id") != split.id:
                continue
            for staged in draft.transforms:
                for mapping in staged.mappings:
                    target = mapping["target_field_id"]
                    if mapping.get("source_field_id") in split.new_field_ids and target in scope_set:
                        conflicts.append(
                            {
                                "id": f"split-conflict:{split.id}:draft:{draft.id}:{target}",
                                "type": "parallel_draft",
                                "draft_id": draft.id,
                                "new_field_id": mapping["source_field_id"],
                                "target_field_id": target,
                                "affected_chain": self._impact_field_chain(
                                    target, downstream=True
                                ),
                            }
                        )
        return {
            "split_id": split.id,
            "scope": scope,
            "selected_decisions": [decision for _, decision in sorted(selected.items())],
            "unresolved": unresolved,
            "conflicts": conflicts,
        }

    def create_split_migration_batch(
        self,
        batch_id: str,
        split_id: str,
        downstream_field_ids: Iterable[str],
        *,
        additional_paths: Iterable[dict[str, str]] | None = None,
        note: str | None = None,
    ) -> MigrationBatch:
        """Create an independently reviewable draft for one downstream scope."""

        if batch_id in self.store.migration_batches:
            raise AlreadyExistsError(f"migration batch '{batch_id}' already exists")
        draft_id = f"draft.split-migration:{batch_id}"
        split = self._require_field_split(split_id)
        plan = self.plan_split_migration(split_id, downstream_field_ids)
        if plan["unresolved"]:
            raise ValidationError("migration scope contains undecided dependencies")
        if plan["conflicts"]:
            raise ValidationError(
                "migration scope has conflicts; call plan_split_migration for details"
            )

        draft = self.create_draft(
            draft_id,
            f"Split migration {batch_id}",
            metadata={
                "kind": "split_migration",
                "split_id": split.id,
                "migration_batch_id": batch_id,
                "note": note,
            },
        )
        effective = self._effective_edges()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in effective.values():
            grouped[edge.transform_id].append(
                {
                    "source_field_id": edge.source_field_id,
                    "target_field_id": edge.target_field_id,
                    "expression": edge.expression,
                }
            )

        decisions = plan["selected_decisions"]
        selected_edge_ids = {
            self._contribution_id(
                item["transform_id"],
                item["old_field_id"],
                item["target_field_id"],
            )
            for item in decisions
            if item.get("new_target_field_id") is None
        }
        affected_transforms = {item["transform_id"] for item in decisions}
        new_by_key: set[tuple[str, str, str]] = set()
        for item in decisions:
            new_target = item.get("new_target_field_id") or item["target_field_id"]
            new_by_key.add((item["transform_id"], item["new_field_id"], new_target))

        for path in additional_paths or []:
            transform_id = path["transform_id"]
            source_id = path["source_field_id"]
            target_id = path["target_field_id"]
            self._check_mapping_fields(source_id, target_id)
            affected_transforms.add(transform_id)
            new_by_key.add((transform_id, source_id, target_id))
        replacement_targets: dict[str, set[str]] = defaultdict(set)
        for path in additional_paths or []:
            replacement_targets[path["transform_id"]].add(path["target_field_id"])

        for transform_id in sorted(affected_transforms):
            transform = self.store.transforms[transform_id]
            retained = [
                mapping
                for mapping in grouped.get(transform_id, [])
                if self._contribution_id(
                    transform_id,
                    mapping["source_field_id"],
                    mapping["target_field_id"],
                )
                not in selected_edge_ids
                and mapping["target_field_id"] not in replacement_targets.get(transform_id, set())
            ]
            for staged_transform, source_id, target_id in sorted(new_by_key):
                if staged_transform != transform_id:
                    continue
                expression = None
                source_decision = next(
                    (
                        item
                        for item in decisions
                        if item["transform_id"] == transform_id
                        and item["new_field_id"] == source_id
                        and (
                            item.get("new_target_field_id") or item["target_field_id"]
                        )
                        == target_id
                    ),
                    None,
                )
                source_edge = None
                if source_decision is not None:
                    source_edge = effective.get(
                        self._contribution_id(
                            transform_id,
                            split.old_field_id,
                            source_decision["target_field_id"],
                        )
                    )
                explicit_path = next(
                    (
                        item
                        for item in (additional_paths or [])
                        if item["transform_id"] == transform_id
                        and item["source_field_id"] == source_id
                        and item["target_field_id"] == target_id
                    ),
                    None,
                )
                if explicit_path is not None:
                    expression = explicit_path.get("expression")
                elif source_edge is not None:
                    expression = source_edge.expression
                retained.append(
                    {
                        "source_field_id": source_id,
                        "target_field_id": target_id,
                        "expression": expression,
                    }
                )
            draft.transforms.append(
                DraftMapping(
                    transform_id=transform_id,
                    name=transform.name,
                    version=transform.version,
                    code_ref=transform.code_ref,
                    description=transform.description,
                    mappings=retained,
                )
            )

        before_sources = self._target_sources(effective.values())
        simulated, _ = self._simulate_changes(effective, set(), draft.transforms)
        after_sources = self._target_sources(simulated.values())
        current_cycles = {tuple(c) for c in self._graph_cycles(effective.values())}
        new_cycles = [
            cycle
            for cycle in self._graph_cycles(simulated.values())
            if tuple(cycle) not in current_cycles
        ]
        if new_cycles:
            draft.status = "discarded"
            self.store.save()
            raise ValidationError("migration would introduce a new graph cycle")
        draft.migration_changes = [dict(item) for item in decisions]
        comparison = {
            "before": {
                target: sorted(before_sources.get(target, set()))
                for target in plan["scope"]
            },
            "after": {
                target: sorted(after_sources.get(target, set()))
                for target in plan["scope"]
            },
        }
        batch = MigrationBatch(
            id=batch_id,
            split_id=split.id,
            draft_id=draft_id,
            scope=plan["scope"],
            decisions=[dict(item) for item in decisions],
            source_comparison=comparison,
            metadata={"note": note},
        )
        draft.metadata["migration_batch_id"] = batch_id
        self.store.migration_batches[batch_id] = batch
        self.store.save()
        return batch

    @staticmethod
    def _target_sources(edges: Iterable[Contribution]) -> dict[str, set[str]]:
        sources: dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            sources[edge.target_field_id].add(edge.source_field_id)
        return dict(sources)

    def _migration_dependency_resources(self, batch: MigrationBatch) -> set[str]:
        resources = set(batch.scope)
        for decision in batch.decisions:
            resources.add(decision["target_field_id"])
            resources.add(decision.get("new_target_field_id") or decision["target_field_id"])
        return {item for item in resources if item}

    def migration_batch(self, batch_id: str) -> MigrationBatch:
        try:
            return self.store.migration_batches[batch_id]
        except KeyError:
            raise NotFoundError(f"migration batch '{batch_id}' does not exist") from None

    def create_split_retirement_draft(
        self, split_id: str, draft_id: str | None = None
    ) -> DraftProposal:
        """Stage old-field retirement only after every real dependency is handled."""

        split = self._require_field_split(split_id)
        report = self.split_report(split_id)
        if report["remaining_old_dependency_count"]:
            raise ValidationError(
                "old field still has undecided or manual active dependencies"
            )
        draft_id = draft_id or f"draft.split-retire:{split_id}"
        draft = self.create_draft(
            draft_id,
            f"Retire {split.old_field_id}",
            metadata={"kind": "split_retirement", "split_id": split.id},
        )
        self.stage_field_deletion(draft.id, split.old_field_id)
        return draft

    # --- Draft proposals -------------------------------------------------

    def create_draft(
        self,
        draft_id: str,
        name: str,
        *,
        metadata: dict[str, Any] | None = None,
        exist_ok: bool = False,
    ) -> DraftProposal:
        """Open an isolated what-if proposal pinned to the current revision."""

        existing = self.store.drafts.get(draft_id)
        if existing is not None:
            if not exist_ok:
                raise AlreadyExistsError(f"draft '{draft_id}' already exists")
            return existing
        draft = DraftProposal(
            id=draft_id,
            name=name,
            base_revision=self.store.revision,
            metadata=metadata or {},
        )
        self.store.drafts[draft_id] = draft
        self.store.save()
        return draft

    def stage_transform(
        self,
        draft_id: str,
        transform_id: str,
        name: str,
        version: str,
        mappings: Iterable[tuple[str, str, str | None]],
        *,
        code_ref: str | None = None,
        description: str | None = None,
    ) -> DraftProposal:
        """Stage a transform replacement inside a draft.

        Staged edges live only on the draft; effective-graph queries never see
        them until the draft is published. All mappings are validated before
        the draft record is mutated, so a bad batch leaves the draft unchanged.
        """

        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}")
        mappings = list(mappings)
        if not mappings:
            raise ValidationError("a transform must contribute at least one mapping")
        staged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for source_id, target_id, expression in mappings:
            self._check_mapping_fields(source_id, target_id)
            key = (source_id, target_id)
            if key in seen:
                raise AlreadyExistsError("identical contribution already exists")
            seen.add(key)
            staged.append(
                {
                    "source_field_id": source_id,
                    "target_field_id": target_id,
                    "expression": expression,
                }
            )
        draft.transforms = [
            item for item in draft.transforms if item.transform_id != transform_id
        ]
        draft.transforms.append(
            DraftMapping(
                transform_id=transform_id,
                name=name,
                version=version,
                code_ref=code_ref,
                description=description,
                mappings=staged,
            )
        )
        self.store.save()
        return draft

    def stage_field_deletion(self, draft_id: str, field_id: str) -> DraftProposal:
        """Stage a soft field deletion without touching the effective graph."""

        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}")
        self._require_field(field_id)
        if field_id not in draft.deleted_fields:
            draft.deleted_fields.append(field_id)
        self.store.save()
        return draft

    def discard_draft(self, draft_id: str) -> DraftProposal:
        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}")
        draft.status = "discarded"
        self.store.save()
        return draft

    def begin_merge(
        self, session_id: str, output_draft_id: str, left_draft_id: str, right_draft_id: str
    ) -> MergePreview:
        if session_id in self.store.merge_sessions:
            raise AlreadyExistsError(f"merge session '{session_id}' already exists")
        if output_draft_id in self.store.drafts:
            raise AlreadyExistsError(f"draft '{output_draft_id}' already exists")
        if left_draft_id == right_draft_id:
            raise ValidationError("cannot merge a draft with itself")
        left = self._require_open_reviewed_draft(left_draft_id)
        right = self._require_open_reviewed_draft(right_draft_id)
        output = DraftProposal(
            id=output_draft_id,
            name=f"Merge {left_draft_id} + {right_draft_id}",
            base_revision=self.store.revision,
            metadata={
                "merge_session_id": session_id,
                "source_draft_ids": [left_draft_id, right_draft_id],
                "pending": True,
            },
        )
        session = MergeSession(
            id=session_id,
            output_draft_id=output_draft_id,
            draft_ids=[left_draft_id, right_draft_id],
            base_revision=self.store.revision,
        )
        self.store.drafts[output.id] = output
        self.store.merge_sessions[session.id] = session
        self.store.save()
        return self.preview_merge(session_id)

    def _require_open_reviewed_draft(self, draft_id: str) -> DraftProposal:
        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}")
        return draft

    def _require_merge_session(self, session_id: str) -> MergeSession:
        try:
            session = self.store.merge_sessions[session_id]
        except KeyError:
            raise NotFoundError(
                f"merge session '{session_id}' does not exist"
            ) from None
        if session.status != "open":
            raise ValidationError(f"merge session '{session_id}' is {session.status}")
        return session

    def resolve_merge_conflict(
        self, session_id: str, conflict_id: str, choice: str
    ) -> MergePreview:
        session = self._require_merge_session(session_id)
        preview = self._build_merge_preview(session)
        conflict_data = next(
            (item for item in preview.conflicts if item["id"] == conflict_id), None
        )
        conflict = MergeConflict(**conflict_data) if conflict_data else None
        if conflict is None:
            raise NotFoundError(
                f"merge conflict '{conflict_id}' does not require resolution"
            )
        if choice not in conflict.options:
            raise ValidationError(
                f"invalid choice '{choice}' for conflict '{conflict_id}'"
            )
        session.decisions = [
            item for item in session.decisions if item.conflict_id != conflict_id
        ]
        session.decisions.append(
            MergeDecision(
                conflict_id=conflict_id,
                choice=choice,
                fingerprint=conflict.fingerprint,
                confirmed_revision=self.store.revision,
                baseline_signature=conflict.baseline_signature,
            )
        )
        self.store.save()
        return self.preview_merge(session_id)

    def preview_merge(self, session_id: str) -> "MergePreview":
        session = self._require_merge_session(session_id)
        preview = self._build_merge_preview(session)
        conflicts = [MergeConflict(**item) for item in preview.conflicts]
        output = self._require_draft(session.output_draft_id)
        combined, deleted = self._combined_merge_plan(
            session,
            conflicts,
            unresolved_only=True,
            active_conflicts=set(preview.unresolved_conflicts),
        )
        output.transforms = list(combined)
        output.deleted_fields = sorted(deleted)
        output.metadata["pending"] = bool(preview.unresolved_conflicts)
        self.store.save()
        return preview

    def abandon_merge(self, session_id: str) -> MergeSession:
        session = self._require_merge_session(session_id)
        session.status = "abandoned"
        output = self.store.drafts.get(session.output_draft_id)
        if output is not None and output.metadata.get("merge_session_id") == session_id:
            output.status = "discarded"
        self.store.save()
        return session

    def publish_merge(
        self,
        session_id: str,
        *,
        confirm_stale: bool = False,
        note: str | None = None,
    ) -> HistoryEntry:
        preview = self.preview_merge(session_id)
        if preview.unresolved_conflicts:
            raise ValidationError("merge has unresolved conflicts")
        if preview.stale and not confirm_stale:
            raise StaleBaselineError(
                session_id, preview.base_revision, preview.current_revision
            )
        session = self.store.merge_sessions[session_id]
        entry = self.publish_draft(
            session.output_draft_id,
            confirm_stale=confirm_stale,
            note=note,
        )
        session.status = "published"
        self.store.save()
        return entry

    def _build_merge_preview(self, session: MergeSession) -> "MergePreview":
        left = self._require_open_reviewed_draft(session.draft_ids[0])
        right = self._require_open_reviewed_draft(session.draft_ids[1])
        current_edges = self._effective_edges()
        current_cycles = {tuple(c) for c in self._graph_cycles(current_edges.values())}
        current_conflicts = self._merge_conflicts(
            session, left, right, current_edges
        )
        conflicts = current_conflicts
        current_signatures = {
            conflict.id: conflict.baseline_signature for conflict in conflicts
        }
        decision_by_id = {
            decision.conflict_id: decision for decision in session.decisions
        }
        unresolved: list[MergeConflict] = []
        reconfirm: list[str] = []
        for conflict in conflicts:
            decision = decision_by_id.get(conflict.id)
            if decision is None or decision.fingerprint != conflict.fingerprint:
                unresolved.append(conflict)
                if decision is not None:
                    reconfirm.append(conflict.id)
                continue
            if self.store.revision != decision.confirmed_revision:
                affected = (
                    current_signatures.get(conflict.id)
                    != decision.baseline_signature
                )
                if affected:
                    reconfirm.append(conflict.id)
                    unresolved.append(conflict)

        active_conflicts = set(item.id for item in unresolved)
        combined, deleted = self._combined_merge_plan(
            session, conflicts, unresolved_only=True, active_conflicts=active_conflicts
        )
        simulated, _ = self._simulate_changes(current_edges, deleted, combined)

        added_keys, removed_keys = self._edge_set_difference(current_edges, simulated)
        lost = self._lost_sources(current_edges, simulated, deleted)
        new_cycles = [
            cycle
            for cycle in self._graph_cycles(simulated.values())
            if tuple(cycle) not in current_cycles
        ]
        return MergePreview(
            session_id=session.id,
            output_draft_id=session.output_draft_id,
            source_draft_ids=list(session.draft_ids),
            base_revision=session.base_revision,
            current_revision=self.store.revision,
            stale=session.base_revision != self.store.revision,
            conflicts=[item.to_dict() for item in conflicts],
            unresolved_conflicts=[item.id for item in unresolved],
            reconfirm_conflicts=reconfirm,
            added_dependencies=[
                {"transform_id": t, "source_field_id": s, "target_field_id": g}
                for t, s, g in added_keys
            ],
            removed_dependencies=[
                {"transform_id": t, "source_field_id": s, "target_field_id": g}
                for t, s, g in removed_keys
            ],
            lost_sources=lost,
            new_cycles=new_cycles,
            explanations=self._merge_explanations(
                left,
                right,
                conflicts,
                session,
                new_cycles,
                lost,
                added_keys,
                removed_keys,
            ),
        )

    def _merge_conflicts(
        self,
        session: MergeSession,
        left: DraftProposal,
        right: DraftProposal,
        relevant_edges: dict[str, Contribution] | None = None,
    ) -> list[MergeConflict]:
        relevant_edges = relevant_edges or self._effective_edges()
        conflicts: list[MergeConflict] = []
        left_transforms = {item.transform_id: item for item in left.transforms}
        right_transforms = {item.transform_id: item for item in right.transforms}
        for transform_id in sorted(set(left_transforms) & set(right_transforms)):
            left_item = left_transforms[transform_id]
            right_item = right_transforms[transform_id]
            left_maps = self._mapping_map(left_item)
            right_maps = self._mapping_map(right_item)
            for key in sorted(set(left_maps) & set(right_maps)):
                left_mapping = left_maps[key]
                right_mapping = right_maps[key]
                if left_mapping.get("expression") == right_mapping.get("expression"):
                    continue
                conflicts.append(
                    self._conflict(
                        session,
                        "same_mapping",
                        f"mapping:{transform_id}:{key[0]}->{key[1]}",
                        left=left,
                        right=right,
                        transform_id=transform_id,
                        source_field_id=key[0],
                        target_field_id=key[1],
                        options={
                            "left": left_mapping,
                            "right": right_mapping,
                        },
                        relevant_edges=relevant_edges,
                    )
                )
            if any(
                getattr(left_item, name) != getattr(right_item, name)
                for name in ("name", "version", "code_ref", "description")
            ):
                conflicts.append(
                    self._conflict(
                        session,
                        "transform_metadata",
                        f"metadata:{transform_id}",
                        left=left,
                        right=right,
                        transform_id=transform_id,
                        options={
                            "left": self._draft_transform_identity(left_item),
                            "right": self._draft_transform_identity(right_item),
                        },
                        relevant_edges=relevant_edges,
                    )
                )

        left_deletes = set(left.deleted_fields)
        right_deletes = set(right.deleted_fields)
        references = self._draft_field_references([left, right])
        for field_id in sorted((left_deletes | right_deletes) & set(references)):
            deleting_side = "left" if field_id in left_deletes else "right"
            referencing_side = "right" if deleting_side == "left" else "left"
            opposite_draft_id = session.draft_ids[
                {"left": 1, "right": 0}[deleting_side]
            ]
            opposite_refs = [
                item
                for item in references[field_id]
                if item["draft_id"] == opposite_draft_id
            ]
            if not opposite_refs:
                continue
            options = {
                "delete_field": {
                    "deleted_by": session.draft_ids[{"left": 0, "right": 1}[deleting_side]],
                    "removed_references": opposite_refs,
                },
                "keep_field": {
                    "referenced_by": opposite_draft_id,
                    "references": opposite_refs,
                },
            }
            conflicts.append(
                self._conflict(
                    session,
                    "deleted_field",
                    f"deleted-field:{field_id}",
                    left=left,
                    right=right,
                    field_id=field_id,
                options=options,
                relevant_edges=relevant_edges,
            )
            )
        return conflicts

    def _conflict(
        self,
        session: MergeSession,
        conflict_type: str,
        stable_id: str,
        *,
        left: DraftProposal,
        right: DraftProposal,
        transform_id: str | None = None,
        source_field_id: str | None = None,
        target_field_id: str | None = None,
        field_id: str | None = None,
        options: dict[str, Any],
        relevant_edges: dict[str, Contribution] | None = None,
    ) -> MergeConflict:
        conflict_id = f"merge:{session.id}:{stable_id}"
        material = {
            "type": conflict_type,
            "transform_id": transform_id,
            "source_field_id": source_field_id,
            "target_field_id": target_field_id,
            "field_id": field_id,
            "options": options,
        }
        fingerprint = hashlib.sha256(
            repr(material).encode("utf-8")
        ).hexdigest()
        relevant_edges = relevant_edges or {}
        baseline_material = sorted(
            (
                edge.transform_id,
                edge.source_field_id,
                edge.target_field_id,
                edge.expression,
            )
            for edge in relevant_edges.values()
            if edge.transform_id == transform_id
            or edge.source_field_id in {source_field_id, field_id}
            or edge.target_field_id in {target_field_id, field_id}
        )
        baseline_signature = hashlib.sha256(
            repr(baseline_material).encode("utf-8")
        ).hexdigest()
        impact = self._conflict_impact(
            conflict_type,
            source_field_id=source_field_id,
            target_field_id=target_field_id,
            field_id=field_id,
        )
        return MergeConflict(
            id=conflict_id,
            type=conflict_type,
            draft_ids=[left.id, right.id],
            transform_id=transform_id,
            source_field_id=source_field_id,
            target_field_id=target_field_id,
            field_id=field_id,
            options=options,
            impact_chain=impact,
            fingerprint=fingerprint,
            baseline_signature=baseline_signature,
        )

    def _conflict_impact(
        self,
        conflict_type: str,
        *,
        source_field_id: str | None,
        target_field_id: str | None,
        field_id: str | None,
    ) -> dict[str, Any]:
        if conflict_type == "deleted_field":
            field = field_id
            return {
                "upstream": self._impact_field_chain(field, downstream=False),
                "impact": self._impact_field_chain(field, downstream=True),
            }
        return {
            "left_upstream": self._impact_field_chain(target_field_id, downstream=False),
            "right_impact": self._impact_field_chain(source_field_id, downstream=True),
        }

    def _impact_field_chain(self, field_id: str | None, *, downstream: bool) -> list[str]:
        if field_id is None:
            return []
        result = self.impact(field_id) if downstream else self.upstream(field_id)
        chain = [field_id]
        nodes = sorted(result.direct)
        if nodes:
            chain.extend(nodes)
        transitive = sorted(result.transitive)
        if transitive:
            chain.extend(transitive)
        return chain

    @staticmethod
    def _mapping_map(staged: DraftMapping) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (item["source_field_id"], item["target_field_id"]): item
            for item in staged.mappings
        }

    @staticmethod
    def _draft_transform_identity(staged: DraftMapping) -> dict[str, Any]:
        return {
            "transform_id": staged.transform_id,
            "name": staged.name,
            "version": staged.version,
            "code_ref": staged.code_ref,
            "description": staged.description,
        }

    @staticmethod
    def _draft_field_references(drafts: Iterable[DraftMapping]) -> dict[str, list[dict[str, str]]]:
        references: dict[str, list[dict[str, str]]] = defaultdict(list)
        for draft in drafts:
            for staged in draft.transforms:
                for mapping in staged.mappings:
                    for field_role in ("source_field_id", "target_field_id"):
                        references[mapping[field_role]].append(
                            {
                                "draft_id": draft.id,
                                "transform_id": staged.transform_id,
                                "source_field_id": mapping["source_field_id"],
                                "target_field_id": mapping["target_field_id"],
                            }
                        )
        return dict(references)

    def _combined_merge_plan(
        self,
        session: MergeSession,
        conflicts: Iterable[MergeConflict],
        *,
        unresolved_only: bool = False,
        active_conflicts: set[str] | None = None,
    ) -> tuple[list[DraftMapping], set[str]]:
        active_conflicts = active_conflicts or set()
        decisions = {
            decision.conflict_id: decision for decision in session.decisions
        }
        conflict_by_id = {conflict.id: conflict for conflict in conflicts}
        left = self._require_open_reviewed_draft(session.draft_ids[0])
        right = self._require_open_reviewed_draft(session.draft_ids[1])
        deleted: set[str] = set(left.deleted_fields) | set(right.deleted_fields)
        grouped: dict[str, list[tuple[DraftProposal, DraftMapping]]] = defaultdict(list)
        for draft in (left, right):
            for staged in draft.transforms:
                grouped[staged.transform_id].append((draft, staged))

        combined: list[DraftMapping] = []
        for transform_id in sorted(grouped):
            entries = grouped[transform_id]
            metadata_choice = None
            metadata_conflict_id = f"merge:{session.id}:metadata:{transform_id}"
            metadata_conflict = conflict_by_id.get(metadata_conflict_id)
            if metadata_conflict is not None:
                decision = decisions.get(metadata_conflict_id)
                if (
                    unresolved_only
                    and metadata_conflict_id in active_conflicts
                ) or decision is None:
                    continue
                metadata_choice = decision.choice

            if len(entries) == 1:
                draft, staged = entries[0]
            else:
                has_metadata_conflict = metadata_conflict is not None
                chosen_draft_id = session.draft_ids[
                    0 if metadata_choice in (None, "left") else 1
                ]
                chosen_draft, chosen_staged = next(
                    (draft_item, staged_item)
                    for draft_item, staged_item in entries
                    if draft_item.id == chosen_draft_id
                )
                if not has_metadata_conflict:
                    staged = self._merge_transform_mapping(entries[0][1], entries[1][1])
                else:
                    staged = self._draft_mapping_with_identity(chosen_staged)
                draft = chosen_draft

            if len(entries) == 2:
                left_staged = entries[0][1]
                right_staged = entries[1][1]
                left_maps = self._mapping_map(left_staged)
                right_maps = self._mapping_map(right_staged)
                merged_mappings: list[dict[str, Any]] = []
                seen: set[tuple[str, str]] = set()
                all_keys = sorted(set(left_maps) | set(right_maps))
                for key in all_keys:
                    conflict_id = (
                        f"merge:{session.id}:mapping:{transform_id}:{key[0]}->{key[1]}"
                    )
                    if key in left_maps and key in right_maps:
                        if left_maps[key].get("expression") == right_maps[key].get("expression"):
                            chosen = left_maps[key]
                        else:
                            decision = decisions.get(conflict_id)
                            if (
                                unresolved_only and conflict_id in active_conflicts
                            ) or decision is None:
                                continue
                            chosen = (
                                left_maps[key]
                                if decision.choice == "left"
                                else right_maps[key]
                            )
                    elif key in left_maps:
                        chosen = left_maps[key]
                    else:
                        chosen = right_maps[key]
                    if (
                        chosen["source_field_id"] in deleted
                        or chosen["target_field_id"] in deleted
                    ):
                        continue
                    if key not in seen:
                        seen.add(key)
                        merged_mappings.append(chosen)
                staged = DraftMapping(
                    transform_id=staged.transform_id,
                    name=staged.name,
                    version=staged.version,
                    code_ref=staged.code_ref,
                    description=staged.description,
                    mappings=merged_mappings,
                )

            filtered_mappings = []
            for mapping in staged.mappings:
                blocked_by_delete = False
                for field_id in (mapping["source_field_id"], mapping["target_field_id"]):
                    conflict_id = f"merge:{session.id}:deleted-field:{field_id}"
                    if field_id in deleted:
                        decision = decisions.get(conflict_id)
                        if (
                            unresolved_only and conflict_id in active_conflicts
                        ) or decision is None:
                            blocked_by_delete = True
                            break
                        if decision.choice == "delete_field":
                            blocked_by_delete = True
                            break
                if not blocked_by_delete:
                    filtered_mappings.append(mapping)
            if filtered_mappings:
                combined.append(
                    DraftMapping(
                        transform_id=staged.transform_id,
                        name=staged.name,
                        version=staged.version,
                        code_ref=staged.code_ref,
                        description=staged.description,
                        mappings=filtered_mappings,
                    )
                )

        for field_id in list(deleted):
            conflict_id = f"merge:{session.id}:deleted-field:{field_id}"
            decision = decisions.get(conflict_id)
            if conflict_by_id.get(conflict_id) is not None:
                if (unresolved_only and conflict_id in active_conflicts) or decision is None:
                    deleted.discard(field_id)
                elif decision.choice == "keep_field":
                    deleted.discard(field_id)
        return combined, deleted

    @staticmethod
    def _merge_transform_mapping(
        left: DraftMapping, right: DraftMapping
    ) -> DraftMapping:
        mappings = list(left.mappings)
        seen = {
            (item["source_field_id"], item["target_field_id"]) for item in mappings
        }
        for item in right.mappings:
            key = (item["source_field_id"], item["target_field_id"])
            if key not in seen:
                mappings.append(item)
                seen.add(key)
        return DraftMapping(
            transform_id=left.transform_id,
            name=left.name,
            version=left.version,
            code_ref=left.code_ref,
            description=left.description,
            mappings=mappings,
        )

    @staticmethod
    def _draft_mapping_with_identity(staged: DraftMapping) -> DraftMapping:
        return DraftMapping(
            transform_id=staged.transform_id,
            name=staged.name,
            version=staged.version,
            code_ref=staged.code_ref,
            description=staged.description,
            mappings=list(staged.mappings),
        )

    @staticmethod
    def _edge_set_difference(
        current_edges: dict[str, Contribution], simulated: dict[str, Contribution]
    ) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
        current_keys = {
            (edge.transform_id, edge.source_field_id, edge.target_field_id)
            for edge in current_edges.values()
        }
        simulated_keys = {
            (edge.transform_id, edge.source_field_id, edge.target_field_id)
            for edge in simulated.values()
        }
        return (
            sorted(simulated_keys - current_keys),
            sorted(current_keys - simulated_keys),
        )

    @staticmethod
    def _lost_sources(
        current_edges: dict[str, Contribution],
        simulated: dict[str, Contribution],
        deleted_fields: set[str],
    ) -> dict[str, list[str]]:
        current_sources: dict[str, set[str]] = defaultdict(set)
        simulated_sources: dict[str, set[str]] = defaultdict(set)
        for edge in current_edges.values():
            current_sources[edge.target_field_id].add(edge.source_field_id)
        for edge in simulated.values():
            simulated_sources[edge.target_field_id].add(edge.source_field_id)
        return {
            target: sorted(current_sources[target] - simulated_sources.get(target, set()))
            for target in current_sources
            if target not in deleted_fields
            and current_sources[target] - simulated_sources.get(target, set())
        }

    def _merge_explanations(
        self,
        left: DraftProposal,
        right: DraftProposal,
        conflicts: list[MergeConflict],
        session: MergeSession,
        new_cycles: list[list[str]],
        lost_sources: dict[str, list[str]],
        added: list[tuple[str, str, str]],
        removed: list[tuple[str, str, str]],
    ) -> list[dict[str, Any]]:
        decisions = {item.conflict_id: item.choice for item in session.decisions}
        explanations = [
            {
                "kind": "source_change",
                "draft_id": left.id,
                "transforms": [item.transform_id for item in left.transforms],
                "deleted_fields": list(left.deleted_fields),
            },
            {
                "kind": "source_change",
                "draft_id": right.id,
                "transforms": [item.transform_id for item in right.transforms],
                "deleted_fields": list(right.deleted_fields),
            },
        ]
        for cycle in new_cycles:
            explanations.append(
                {
                    "kind": "new_cycle",
                    "cycle": cycle,
                    "source_draft_ids": self._evidence_draft_ids(
                        cycle, left, right
                    ),
                }
            )
        for target, sources in sorted(lost_sources.items()):
            explanations.append(
                {
                    "kind": "lost_sources",
                    "target_field_id": target,
                    "lost_source_field_ids": sources,
                    "source_draft_ids": self._evidence_draft_ids(
                        [*sources, target], left, right
                    ),
                    "conflict_ids": [
                        conflict.id
                        for conflict in conflicts
                        if conflict.target_field_id == target
                        or conflict.field_id in sources
                        or conflict.field_id == target
                    ],
                }
            )
        return explanations

    @staticmethod
    def _evidence_draft_ids(
        field_ids: list[str],
        left: DraftProposal,
        right: DraftProposal,
    ) -> list[str]:
        field_set = set(field_ids)
        result = []
        for draft in (left, right):
            staged_touch = any(
                source in field_set or target in field_set
                for staged in draft.transforms
                for mapping in staged.mappings
                for source, target in [
                    (mapping["source_field_id"], mapping["target_field_id"])
                ]
            )
            deleted_touch = any(field_id in field_set for field_id in draft.deleted_fields)
            if staged_touch or deleted_touch:
                result.append(draft.id)
        return result

    def draft_diff(self, draft_id: str) -> DraftDiff:
        """Compare a draft against the current effective graph.

        Reports dependencies the draft would add, active dependencies it would
        remove, target fields that lose sources, and cycles that would appear.
        """

        draft = self._require_draft(draft_id)
        current_edges = self._effective_edges()
        simulated, deleted = self._simulate_changes(
            current_edges, set(), draft.transforms, draft.deleted_fields
        )

        current_keys = {
            (edge.transform_id, edge.source_field_id, edge.target_field_id)
            for edge in current_edges.values()
        }
        simulated_keys = {
            (edge.transform_id, edge.source_field_id, edge.target_field_id)
            for edge in simulated.values()
        }
        added = sorted(simulated_keys - current_keys)
        removed = sorted(current_keys - simulated_keys)

        current_sources: dict[str, set[str]] = defaultdict(set)
        for edge in current_edges.values():
            current_sources[edge.target_field_id].add(edge.source_field_id)
        simulated_sources: dict[str, set[str]] = defaultdict(set)
        for edge in simulated.values():
            simulated_sources[edge.target_field_id].add(edge.source_field_id)
        for field_id in deleted:
            current_sources.pop(field_id, None)
            simulated_sources.pop(field_id, None)
        lost = {
            target: sorted(current_sources[target] - simulated_sources.get(target, set()))
            for target in current_sources
            if current_sources[target] - simulated_sources.get(target, set())
        }

        current_cycles = {tuple(c) for c in self._graph_cycles(current_edges.values())}
        new_cycles = [
            cycle
            for cycle in self._graph_cycles(simulated.values())
            if tuple(cycle) not in current_cycles
        ]
        return DraftDiff(
            draft_id=draft.id,
            base_revision=draft.base_revision,
            current_revision=self.store.revision,
            stale=draft.base_revision != self.store.revision,
            added_dependencies=[
                {"transform_id": t, "source_field_id": s, "target_field_id": g}
                for t, s, g in added
            ],
            removed_dependencies=[
                {"transform_id": t, "source_field_id": s, "target_field_id": g}
                for t, s, g in removed
            ],
            lost_sources=lost,
            new_cycles=new_cycles,
        )

    def publish_draft(
        self,
        draft_id: str,
        *,
        confirm_stale: bool = False,
        note: str | None = None,
    ) -> HistoryEntry:
        """Atomically apply a draft to the effective graph and record history.

        If another proposal was published after this draft's baseline, the
        publish is rejected with StaleBaselineError unless confirm_stale=True
        is passed after the user re-confirmed against the new baseline. On any
        validation or persistence failure the in-memory and on-disk graphs are
        left exactly as before, and the full pre-publish graph is snapshotted
        into the history entry.
        """

        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}")
        if not draft.transforms:
            raise ValidationError("draft has no staged transforms")
        if draft.metadata.get("pending"):
            raise ValidationError(
                "draft is an unresolved merge output; resolve all conflicts first"
            )
        if draft.base_revision != self.store.revision and not confirm_stale:
            raise StaleBaselineError(
                draft_id, draft.base_revision, self.store.revision
            )
        for staged in draft.transforms:
            for mapping in staged.mappings:
                if (
                    mapping["source_field_id"] in draft.deleted_fields
                    or mapping["target_field_id"] in draft.deleted_fields
                ):
                    raise ValidationError("draft both deletes and maps a field")
                self._check_mapping_fields(
                    mapping["source_field_id"], mapping["target_field_id"]
                )
        migration_batch: MigrationBatch | None = None
        if draft.metadata.get("kind") == "split_migration":
            batch_id = draft.metadata.get("migration_batch_id")
            migration_batch = self.migration_batch(batch_id)
            if migration_batch.draft_id != draft_id:
                raise ValidationError("migration draft does not match its batch")
            if migration_batch.status != "planned":
                raise ValidationError(f"migration batch '{batch_id}' is {migration_batch.status}")
            diff = self.draft_diff(draft_id)
            if diff.new_cycles:
                raise ValidationError("migration would introduce a new graph cycle")

        backup_transforms = dict(self.store.transforms)
        backup_contributions = {
            key: Contribution(**item.to_dict())
            for key, item in self.store.contributions.items()
        }
        backup_fields = {
            key: Field(**item.to_dict()) for key, item in self.store.fields.items()
        }
        backup_batches = {
            key: MigrationBatch(**item.to_dict())
            for key, item in self.store.migration_batches.items()
        }
        backup_splits = {
            key: FieldSplit(**item.to_dict())
            for key, item in self.store.field_splits.items()
        }
        backup_revision = self.store.revision
        activated: list[str] = []
        deactivated: list[str] = []
        deactivated_fields: list[str] = []
        created_transforms: list[str] = []
        try:
            for staged in draft.transforms:
                transform = self.store.transforms.get(staged.transform_id)
                if transform is None:
                    transform = TransformVersion(
                        id=staged.transform_id,
                        name=staged.name,
                        version=staged.version,
                        code_ref=staged.code_ref,
                        description=staged.description,
                    )
                    self.store.transforms[transform.id] = transform
                    created_transforms.append(transform.id)
                else:
                    transform.active = True
                    transform.name = staged.name
                    transform.version = staged.version
                    if staged.code_ref is not None:
                        transform.code_ref = staged.code_ref
                keep: set[str] = set()
                for mapping in staged.mappings:
                    edge_id = self._contribution_id(
                        staged.transform_id,
                        mapping["source_field_id"],
                        mapping["target_field_id"],
                    )
                    keep.add(edge_id)
                    edge = self.store.contributions.get(edge_id)
                    if edge is None:
                        self.store.contributions[edge_id] = Contribution(
                            id=edge_id,
                            transform_id=staged.transform_id,
                            source_field_id=mapping["source_field_id"],
                            target_field_id=mapping["target_field_id"],
                            expression=mapping.get("expression"),
                        )
                    else:
                        edge.active = True
                        edge.expression = mapping.get("expression")
                    activated.append(edge_id)
                for edge in self.store.contributions.values():
                    if (
                        edge.transform_id == staged.transform_id
                        and edge.active
                        and edge.id not in keep
                    ):
                        edge.active = False
                        deactivated.append(edge.id)
            for field_id in draft.deleted_fields:
                field_item = self.store.fields.get(field_id)
                if field_item is not None:
                    field_item.active = False
                    deactivated_fields.append(field_id)
            self.store.revision += 1
            draft.status = "published"
            published_at = datetime.now(timezone.utc).isoformat()
            migration_batch_id = None
            if migration_batch is not None:
                migration_batch.status = "published"
                migration_batch.history_id = (
                    f"history:r{self.store.revision}:publish:{draft_id}"
                )
                migration_batch.published_at = published_at
                migration_batch.adopted_decisions = [
                    dict(item) for item in migration_batch.decisions
                ]
                migration_batch_id = migration_batch.id
            retirement_split_id = None
            if draft.metadata.get("kind") == "split_retirement":
                retirement_split_id = draft.metadata.get("split_id")
                self._require_field_split(retirement_split_id).status = "retired"
            entry = HistoryEntry(
                id=f"history:r{self.store.revision}:publish:{draft_id}",
                revision=self.store.revision,
                action="publish",
                draft_id=draft_id,
                note=note,
                activated=activated,
                deactivated=deactivated,
                deactivated_fields=deactivated_fields,
                created_transforms=created_transforms,
                migration_batch_id=migration_batch_id,
                created_at=published_at,
                snapshot=self._graph_snapshot(),
            )
            self.store.history.append(entry)
            self.store.save()
        except Exception:
            self.store.transforms = backup_transforms
            self.store.contributions = backup_contributions
            self.store.fields = backup_fields
            self.store.migration_batches = backup_batches
            self.store.field_splits = backup_splits
            self.store.revision = backup_revision
            draft.status = "open"
            self.store.history = [
                item for item in self.store.history if item.draft_id != draft_id or item.action != "publish"
            ]
            raise
        return entry

    def publish_split_migration_batch(
        self,
        batch_id: str,
        *,
        confirm_stale: bool = False,
        note: str | None = None,
    ) -> HistoryEntry:
        batch = self.migration_batch(batch_id)
        return self.publish_draft(
            batch.draft_id, confirm_stale=confirm_stale, note=note
        )

    def withdraw_publish(
        self, history_id: str, *, note: str | None = None
    ) -> HistoryEntry:
        """Reverse a recorded publish, keeping the full history intact.

        Edges the publish activated are soft-deleted again and edges it
        deactivated are restored, inside one atomic save. Both the original
        publish entry and this withdraw entry (with its own graph snapshot)
        stay in the history log forever.
        """

        original = next(
            (item for item in self.store.history if item.id == history_id), None
        )
        if original is None:
            raise NotFoundError(f"history entry '{history_id}' does not exist")
        if original.action != "publish":
            raise ValidationError(f"history entry '{history_id}' is not a publish")
        already = next(
            (
                item
                for item in self.store.history
                if item.action == "withdraw"
                and item.draft_id == original.draft_id
                and (
                    item.note == f"withdraw:{original.id}"
                    or (
                        item.activated == list(original.deactivated)
                        and item.deactivated == list(original.activated)
                    )
                )
            ),
            None,
        )
        if already is not None:
            raise ValidationError(f"publish '{history_id}' was already withdrawn")

        withdrawn_notes = {
            f"withdraw:{item.id}"
            for item in self.store.history
            if item.action == "withdraw"
        }

        def edge_active_after_withdrawal(edge_id: str, active_in_original: bool) -> bool:
            for later in self.store.history:
                if later.action != "publish" or later.id == original.id:
                    continue
                if f"withdraw:{later.id}" in withdrawn_notes:
                    continue
                if edge_id in later.activated:
                    return True
                if edge_id in later.deactivated:
                    return False
            return not active_in_original

        backup_transforms = dict(self.store.transforms)
        backup_contributions = {
            key: Contribution(**item.to_dict())
            for key, item in self.store.contributions.items()
        }
        backup_revision = self.store.revision
        backup_batches = {
            key: MigrationBatch(**item.to_dict())
            for key, item in self.store.migration_batches.items()
        }
        backup_splits = {
            key: FieldSplit(**item.to_dict())
            for key, item in self.store.field_splits.items()
        }
        try:
            for edge_id in original.activated:
                edge = self.store.contributions.get(edge_id)
                if edge is not None:
                    edge.active = edge_active_after_withdrawal(edge_id, True)
            for edge_id in original.deactivated:
                edge = self.store.contributions.get(edge_id)
                if edge is not None:
                    edge.active = edge_active_after_withdrawal(edge_id, False)
            for transform_id in original.created_transforms:
                transform = self.store.transforms.get(transform_id)
                if transform is not None:
                    transform.active = False
            for field_id in getattr(original, "deactivated_fields", []):
                field_item = self.store.fields.get(field_id)
                if field_item is not None:
                    field_item.active = True
            withdrawn_batch = (
                self.store.migration_batches.get(original.migration_batch_id)
                if original.migration_batch_id
                else None
            )
            self.store.revision += 1
            withdrawn_at = datetime.now(timezone.utc).isoformat()
            entry = HistoryEntry(
                id=f"history:r{self.store.revision}:withdraw:{original.draft_id}",
                revision=self.store.revision,
                action="withdraw",
                draft_id=original.draft_id,
                note=f"withdraw:{original.id}" if note is None else note,
                activated=list(original.deactivated),
                deactivated=list(original.activated),
                migration_batch_id=original.migration_batch_id,
                created_at=withdrawn_at,
                snapshot=self._graph_snapshot(),
            )
            self.store.history.append(entry)
            if withdrawn_batch is not None:
                withdrawn_batch.status = "withdrawn"
                withdrawn_batch.requires_recheck = False
                withdrawn_batch.recheck_reasons = []
                withdrawn_resources = self._migration_dependency_resources(
                    withdrawn_batch
                )
                for later in self.store.migration_batches.values():
                    if (
                        later.id == withdrawn_batch.id
                        or later.split_id != withdrawn_batch.split_id
                    ):
                        continue
                    later_resources = self._migration_dependency_resources(later)
                    later_sources = {
                        source
                        for sources in later.source_comparison.get("after", {}).values()
                        for source in sources
                    }
                    if withdrawn_resources & later_resources or withdrawn_resources & later_sources:
                        later.requires_recheck = True
                        reason = f"upstream batch '{withdrawn_batch.id}' was withdrawn"
                        if reason not in later.recheck_reasons:
                            later.recheck_reasons.append(reason)
            for split in self.store.field_splits.values():
                draft = self.store.drafts.get(original.draft_id or "")
                if (
                    split.id == draft.metadata.get("split_id")
                    and draft.metadata.get("kind") == "split_retirement"
                    and split.status == "retired"
                ):
                    split.status = "open"
            self.store.save()
        except Exception:
            self.store.transforms = backup_transforms
            self.store.contributions = backup_contributions
            self.store.migration_batches = backup_batches
            self.store.field_splits = backup_splits
            self.store.revision = backup_revision
            raise
        return entry

    def history(self) -> list[HistoryEntry]:
        return list(self.store.history)

    def _effective_edges(self) -> dict[str, Contribution]:
        edges: dict[str, Contribution] = {}
        for edge in self.store.contributions.values():
            if not edge.active:
                continue
            transform = self.store.transforms.get(edge.transform_id)
            if transform is None or not transform.active:
                continue
            if (
                edge.source_field_id not in self.store.fields
                or edge.target_field_id not in self.store.fields
            ):
                continue
            edges[edge.id] = edge
        return edges

    @staticmethod
    def _simulate_changes(
        current_edges: dict[str, Contribution],
        deleted_fields: set[str],
        staged_transforms: Iterable[DraftMapping],
        draft_deleted_fields: Iterable[str] = (),
    ) -> tuple[dict[str, Contribution], set[str]]:
        simulated = dict(current_edges)
        deleted = set(deleted_fields)
        deleted.update(draft_deleted_fields)
        for staged in staged_transforms:
            for edge_id, edge in list(simulated.items()):
                if edge.transform_id == staged.transform_id:
                    del simulated[edge_id]
            for mapping in staged.mappings:
                source_id = mapping["source_field_id"]
                target_id = mapping["target_field_id"]
                if source_id in deleted or target_id in deleted:
                    continue
                edge_id = LineageService._contribution_id(
                    staged.transform_id, source_id, target_id
                )
                simulated[edge_id] = Contribution(
                    id=edge_id,
                    transform_id=staged.transform_id,
                    source_field_id=source_id,
                    target_field_id=target_id,
                    expression=mapping.get("expression"),
                )
        for field_id in deleted:
            simulated = {
                edge_id: edge
                for edge_id, edge in simulated.items()
                if field_id not in (edge.source_field_id, edge.target_field_id)
            }
        return simulated, deleted

    def _graph_snapshot(self) -> dict[str, Any]:
        return {
            "revision": self.store.revision,
            "objects": [item.to_dict() for item in self.store.objects.values()],
            "fields": [item.to_dict() for item in self.store.fields.values()],
            "transforms": [
                item.to_dict() for item in self.store.transforms.values()
            ],
            "contributions": [
                item.to_dict() for item in self.store.contributions.values()
            ],
        }

    def _graph_cycles(self, edges: Iterable[Contribution]) -> list[list[str]]:
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            adjacency[edge.source_field_id].append(edge.target_field_id)
        color: dict[str, str] = {}
        stack: list[str] = []
        cycles: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()

        def visit(node_id: str) -> None:
            color[node_id] = "gray"
            stack.append(node_id)
            for child_id in adjacency[node_id]:
                if color.get(child_id) == "gray":
                    cycle_nodes = stack[stack.index(child_id):]
                    normalized = tuple(self._normalize_cycle(cycle_nodes))
                    if normalized not in seen:
                        seen.add(normalized)
                        cycles.append([*cycle_nodes, child_id])
                elif color.get(child_id) != "black":
                    visit(child_id)
            stack.pop()
            color[node_id] = "black"

        for node_id in list(adjacency):
            if color.get(node_id) is None:
                visit(node_id)
        return cycles

    def _require_draft(self, draft_id: str) -> DraftProposal:
        try:
            return self.store.drafts[draft_id]
        except KeyError:
            raise NotFoundError(f"draft '{draft_id}' does not exist") from None


@dataclass(slots=True)
class DraftDiff:
    """Impact difference between the effective graph and a draft proposal."""

    draft_id: str
    base_revision: int
    current_revision: int
    stale: bool
    added_dependencies: list[dict[str, Any]]
    removed_dependencies: list[dict[str, Any]]
    lost_sources: dict[str, list[str]]
    new_cycles: list[list[str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
            "stale": self.stale,
            "added_dependencies": self.added_dependencies,
            "removed_dependencies": self.removed_dependencies,
            "lost_sources": self.lost_sources,
            "new_cycles": self.new_cycles,
        }


@dataclass(slots=True)
class MergePreview:
    session_id: str
    output_draft_id: str
    source_draft_ids: list[str]
    base_revision: int
    current_revision: int
    stale: bool
    conflicts: list[dict[str, Any]]
    unresolved_conflicts: list[str]
    reconfirm_conflicts: list[str]
    added_dependencies: list[dict[str, Any]]
    removed_dependencies: list[dict[str, Any]]
    lost_sources: dict[str, list[str]]
    new_cycles: list[list[str]]
    explanations: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "output_draft_id": self.output_draft_id,
            "source_draft_ids": self.source_draft_ids,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
            "stale": self.stale,
            "conflicts": self.conflicts,
            "unresolved_conflicts": self.unresolved_conflicts,
            "reconfirm_conflicts": self.reconfirm_conflicts,
            "added_dependencies": self.added_dependencies,
            "removed_dependencies": self.removed_dependencies,
            "lost_sources": self.lost_sources,
            "new_cycles": self.new_cycles,
            "explanations": self.explanations,
        }
