from __future__ import annotations

import json
import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
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
    Field,
    HistoryEntry,
    MergeConflict,
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
        if not source.active:
            raise ValidationError(f"source field '{source_field_id}' is deleted")
        if not target.active:
            raise ValidationError(f"target field '{target_field_id}' is deleted")
        if source_field_id == target_field_id:
            raise ValidationError("a field cannot contribute to itself")

    def delete_field(self, field_id: str) -> Field:
        """Soft-delete a field; its edges and audit evidence stay in the store."""

        field_item = self._require_field(field_id)
        if not field_item.active:
            raise AlreadyExistsError(f"field '{field_id}' is already deleted")
        field_item.active = False
        try:
            self.store.save()
        except Exception:
            field_item.active = True
            raise
        return field_item

    def restore_field(self, field_id: str) -> Field:
        field_item = self._require_field(field_id)
        if field_item.active:
            raise AlreadyExistsError(f"field '{field_id}' is already active")
        field_item.active = True
        try:
            self.store.save()
        except Exception:
            field_item.active = False
            raise
        return field_item

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
            if source is None or target is None:
                continue
            if not source.active or not target.active:
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

    def discard_draft(self, draft_id: str) -> DraftProposal:
        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}")
        draft.status = "discarded"
        self.store.save()
        return draft

    def remove_field_from_draft(self, draft_id: str, field_id: str) -> DraftProposal:
        """Mark a field for deletion when the draft is published.

        The field stays active on the effective graph and on other drafts
        until this draft is published; the deletion is review evidence on the
        draft itself.
        """

        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}")
        field_item = self._require_field(field_id)
        if not field_item.active:
            raise ValidationError(f"field '{field_id}' is already deleted")
        if field_id not in draft.removed_fields:
            draft.removed_fields.append(field_id)
        self.store.save()
        return draft

    def draft_diff(self, draft_id: str) -> DraftDiff:
        """Compare a draft against the current effective graph.

        Reports dependencies the draft would add, active dependencies it would
        remove, target fields that lose sources, and cycles that would appear.
        """

        draft = self._require_draft(draft_id)
        current_edges = self._effective_edges()
        simulated = dict(current_edges)
        removed = set(draft.removed_fields)
        for staged in draft.transforms:
            for edge_id, edge in list(simulated.items()):
                if edge.transform_id == staged.transform_id:
                    del simulated[edge_id]
            for mapping in staged.mappings:
                edge_id = self._contribution_id(
                    staged.transform_id,
                    mapping["source_field_id"],
                    mapping["target_field_id"],
                )
                simulated[edge_id] = Contribution(
                    id=edge_id,
                    transform_id=staged.transform_id,
                    source_field_id=mapping["source_field_id"],
                    target_field_id=mapping["target_field_id"],
                    expression=mapping.get("expression"),
                )

        current_keys = {
            (edge.transform_id, edge.source_field_id, edge.target_field_id)
            for edge in current_edges.values()
        }
        simulated_keys = {
            (edge.transform_id, edge.source_field_id, edge.target_field_id)
            for edge in simulated.values()
        }
        if removed:
            simulated = {
                edge_id: edge
                for edge_id, edge in simulated.items()
                if edge.source_field_id not in removed
                and edge.target_field_id not in removed
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
        if not draft.transforms and not draft.removed_fields:
            raise ValidationError("draft has no staged transforms")
        if draft.base_revision != self.store.revision and not confirm_stale:
            raise StaleBaselineError(
                draft_id, draft.base_revision, self.store.revision
            )
        for staged in draft.transforms:
            for mapping in staged.mappings:
                self._check_mapping_fields(
                    mapping["source_field_id"], mapping["target_field_id"]
                )
        for field_id in draft.removed_fields:
            field_item = self.store.fields.get(field_id)
            if field_item is None:
                raise NotFoundError(f"field '{field_id}' does not exist")
            if not field_item.active:
                raise ValidationError(f"field '{field_id}' is already deleted")
        for staged in draft.transforms:
            for mapping in staged.mappings:
                for endpoint in (
                    mapping["source_field_id"],
                    mapping["target_field_id"],
                ):
                    if (
                        endpoint in draft.removed_fields
                        and not draft.sources
                    ):
                        raise ValidationError(
                            f"draft removes field '{endpoint}' but also references it"
                        )

        backup_transforms = dict(self.store.transforms)
        backup_contributions = {
            key: Contribution(**item.to_dict())
            for key, item in self.store.contributions.items()
        }
        backup_field_active = {
            key: item.active for key, item in self.store.fields.items()
        }
        backup_revision = self.store.revision
        activated: list[str] = []
        deactivated: list[str] = []
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
            self.store.revision += 1
            removed_fields: list[str] = []
            for field_id in draft.removed_fields:
                field_item = self.store.fields.get(field_id)
                if field_item is not None and field_item.active:
                    field_item.active = False
                    removed_fields.append(field_id)
            draft.status = "published"
            entry = HistoryEntry(
                id=f"history:r{self.store.revision}:publish:{draft_id}",
                revision=self.store.revision,
                action="publish",
                draft_id=draft_id,
                note=note,
                activated=activated,
                deactivated=deactivated,
                created_transforms=created_transforms,
                removed_fields=removed_fields,
                snapshot=self._graph_snapshot(),
            )
            self.store.history.append(entry)
            self.store.save()
        except Exception:
            self.store.transforms = backup_transforms
            self.store.contributions = backup_contributions
            for key, active in backup_field_active.items():
                field_item = self.store.fields.get(key)
                if field_item is not None:
                    field_item.active = active
            self.store.revision = backup_revision
            draft.status = "open"
            self.store.history = [
                item for item in self.store.history if item.draft_id != draft_id or item.action != "publish"
            ]
            raise
        return entry

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
                if item.action == "withdraw" and item.draft_id == original.draft_id
                and item.note == f"withdraw:{original.id}"
            ),
            None,
        )
        if already is not None:
            raise ValidationError(f"publish '{history_id}' was already withdrawn")

        backup_transforms = dict(self.store.transforms)
        backup_contributions = {
            key: Contribution(**item.to_dict())
            for key, item in self.store.contributions.items()
        }
        backup_field_active = {
            key: item.active for key, item in self.store.fields.items()
        }
        backup_revision = self.store.revision
        try:
            for edge_id in original.activated:
                edge = self.store.contributions.get(edge_id)
                if edge is not None:
                    edge.active = False
            for edge_id in original.deactivated:
                edge = self.store.contributions.get(edge_id)
                if edge is not None:
                    edge.active = True
            for transform_id in original.created_transforms:
                transform = self.store.transforms.get(transform_id)
                if transform is not None:
                    transform.active = False
            for field_id in original.removed_fields:
                field_item = self.store.fields.get(field_id)
                if field_item is not None:
                    field_item.active = True
            self.store.revision += 1
            entry = HistoryEntry(
                id=f"history:r{self.store.revision}:withdraw:{original.draft_id}",
                revision=self.store.revision,
                action="withdraw",
                draft_id=original.draft_id,
                note=f"withdraw:{original.id}" if note is None else note,
                activated=list(original.deactivated),
                deactivated=list(original.activated),
                snapshot=self._graph_snapshot(),
            )
            self.store.history.append(entry)
            self.store.save()
        except Exception:
            self.store.transforms = backup_transforms
            self.store.contributions = backup_contributions
            for key, active in backup_field_active.items():
                field_item = self.store.fields.get(key)
                if field_item is not None:
                    field_item.active = active
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
            source = self.store.fields.get(edge.source_field_id)
            target = self.store.fields.get(edge.target_field_id)
            if source is None or target is None:
                continue
            if not source.active or not target.active:
                continue
            edges[edge.id] = edge
        return edges

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

    # --- Draft merges ----------------------------------------------------

    LEFT = "left"
    RIGHT = "right"
    KEEP_BOTH = "keep_both"
    DROP = "drop"

    def _draft_plan(
        self, draft: DraftProposal
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
        """Return a draft's per-transform edge plans and removed fields."""

        plan: dict[str, list[dict[str, Any]]] = {}
        for staged in draft.transforms:
            plan[staged.transform_id] = [
                {
                    "source_field_id": mapping["source_field_id"],
                    "target_field_id": mapping["target_field_id"],
                    "expression": mapping.get("expression"),
                }
                for mapping in staged.mappings
            ]
        return plan, set(draft.removed_fields)

    @staticmethod
    def _mapping_sig(mapping: dict[str, Any]) -> tuple[str, str, str | None]:
        return (
            mapping["source_field_id"],
            mapping["target_field_id"],
            mapping.get("expression"),
        )

    @staticmethod
    def _edge_desc(
        transform_id: str | None,
        mapping: dict[str, Any] | None,
        *,
        present: bool,
    ) -> dict[str, Any]:
        if mapping is None:
            return {"present": present}
        return {
            "present": present,
            "transform_id": transform_id,
            "source_field_id": mapping["source_field_id"],
            "target_field_id": mapping["target_field_id"],
            "expression": mapping.get("expression"),
        }

    def start_merge(
        self,
        merge_id: str,
        left_draft_id: str,
        right_draft_id: str,
        *,
        name: str | None = None,
        exist_ok: bool = False,
    ) -> MergeSession:
        """Open an interactive merge of two independently reviewed drafts."""

        existing = self.store.merges.get(merge_id)
        if existing is not None:
            if not exist_ok:
                raise AlreadyExistsError(f"merge session '{merge_id}' already exists")
            return existing
        left = self._require_reviewed_draft(left_draft_id)
        right = self._require_reviewed_draft(right_draft_id)
        if left.id == right.id:
            raise ValidationError("cannot merge a draft with itself")
        session = MergeSession(
            id=merge_id,
            name=name or f"merge {left.name} + {right.name}",
            left_draft_id=left.id,
            right_draft_id=right.id,
            created_revision=self.store.revision,
        )
        self.store.merges[merge_id] = session
        self._refresh_conflicts(session, left, right)
        self.store.save()
        return session

    def _require_reviewed_draft(self, draft_id: str) -> DraftProposal:
        draft = self._require_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(
                f"draft '{draft_id}' is {draft.status}; only open reviewed "
                "drafts can be merged"
            )
        if not draft.transforms and not draft.removed_fields:
            raise ValidationError(f"draft '{draft_id}' has no reviewed changes")
        return draft

    def _require_merge(self, merge_id: str) -> MergeSession:
        try:
            return self.store.merges[merge_id]
        except KeyError:
            raise NotFoundError(f"merge session '{merge_id}' does not exist") from None

    def _detect_conflicts(
        self,
        left: DraftProposal,
        right: DraftProposal,
    ) -> list[MergeConflict]:
        left_plan, left_removed = self._draft_plan(left)
        right_plan, right_removed = self._draft_plan(right)
        current_edges = self._effective_edges()
        conflicts: list[MergeConflict] = []

        def baseline_map(transform_id: str) -> dict[tuple[str, str], Contribution]:
            return {
                (edge.source_field_id, edge.target_field_id): edge
                for edge in current_edges.values()
                if edge.transform_id == transform_id
            }

        staged_both = sorted(set(left_plan) & set(right_plan))
        for transform_id in staged_both:
            left_by_key = {(m["source_field_id"], m["target_field_id"]): m for m in left_plan[transform_id]}
            right_by_key = {(m["source_field_id"], m["target_field_id"]): m for m in right_plan[transform_id]}
            base = baseline_map(transform_id)
            all_keys = set(left_by_key) | set(right_by_key) | set(base)
            for key in sorted(all_keys):
                source_id, target_id = key
                left_mapping = left_by_key.get(key)
                right_mapping = right_by_key.get(key)
                left_present = left_mapping is not None
                right_present = right_mapping is not None
                if left_present and right_present:
                    if left_mapping.get("expression") != right_mapping.get("expression"):
                        conflicts.append(
                            self._make_conflict(
                                f"replace:{transform_id}:{source_id}->{target_id}",
                                "mapping_replaced",
                                left, right,
                                transform_id=transform_id,
                                source_field_id=source_id,
                                target_field_id=target_id,
                                left_desc=self._edge_desc(transform_id, left_mapping, present=True),
                                right_desc=self._edge_desc(transform_id, right_mapping, present=True),
                                options=[self.LEFT, self.RIGHT],
                            )
                        )
                else:
                    # Only flag when one side drops an edge the baseline had
                    # while the other side keeps it. A brand-new edge staged
                    # by only one side is a compatible addition.
                    if base.get(key) is None:
                        continue
                    kept_mapping = left_mapping or right_mapping
                    kept_side = self.LEFT if left_present else self.RIGHT
                    base_edge = base.get(key)
                    dropped_side = right if left_present else left
                    options = [kept_side, self.DROP]
                    if base_edge is not None:
                        options.append("keep_baseline")
                    conflicts.append(
                        self._make_conflict(
                            f"drop:{transform_id}:{source_id}->{target_id}",
                            "mapping_dropped",
                            left, right,
                            transform_id=transform_id,
                            source_field_id=source_id,
                            target_field_id=target_id,
                            left_desc=self._edge_desc(transform_id, left_mapping, present=left_present),
                            right_desc=self._edge_desc(transform_id, right_mapping, present=right_present),
                            options=options,
                            facts={
                                "dropped_by": dropped_side.id,
                                "kept_by": (left if left_present else right).id,
                                "baseline_present": base_edge is not None,
                                "kept_side": kept_side,
                            },
                        )
                    )

        for field_id in sorted(left_removed | right_removed):
            left_drops = field_id in left_removed
            right_drops = field_id in right_removed
            referenced: list[dict[str, Any]] = []
            for plan, draft_id in ((left_plan, left.id), (right_plan, right.id)):
                for transform_id, mappings in plan.items():
                    for mapping in mappings:
                        if field_id in (
                            mapping["source_field_id"],
                            mapping["target_field_id"],
                        ):
                            referenced.append(
                                {
                                    "draft_id": draft_id,
                                    "transform_id": transform_id,
                                    "source_field_id": mapping["source_field_id"],
                                    "target_field_id": mapping["target_field_id"],
                                }
                            )
            # Deleting the same field in both is a compatible shared change.
            if left_drops and right_drops:
                continue
            if not referenced and not (field_id in left_removed) ^ (field_id in right_removed):
                continue
            # If one side deletes and the other side references it, conflict.
            if referenced:
                field_live = (
                    self.store.fields.get(field_id) is not None
                    and self.store.fields[field_id].active
                )
                conflicts.append(
                    self._make_conflict(
                        f"field:{field_id}",
                        "field_removed",
                        left, right,
                        field_id=field_id,
                        left_desc={"present": not left_drops, "action": "remove" if left_drops else "keep"},
                        right_desc={"present": not right_drops, "action": "remove" if right_drops else "keep"},
                        options=[self.LEFT, self.RIGHT],
                        facts={
                            "removed_by": (left if left_drops else right).id,
                            "referenced_by": referenced,
                            "field_live_on_baseline": field_live,
                        },
                    )
                )
        return conflicts

    def _make_conflict(
        self,
        conflict_id: str,
        kind: str,
        left: DraftProposal,
        right: DraftProposal,
        *,
        transform_id: str | None = None,
        source_field_id: str | None = None,
        target_field_id: str | None = None,
        field_id: str | None = None,
        left_desc: dict[str, Any],
        right_desc: dict[str, Any],
        options: list[str],
        facts: dict[str, Any] | None = None,
    ) -> MergeConflict:
        left_desc = {"draft_id": left.id, "draft_name": left.name, **left_desc}
        right_desc = {"draft_id": right.id, "draft_name": right.name, **right_desc}
        fingerprint = self._conflict_fingerprint(
            kind, left_desc, right_desc, facts or {}
        )
        conflict = MergeConflict(
            id=conflict_id,
            kind=kind,
            transform_id=transform_id,
            source_field_id=source_field_id,
            target_field_id=target_field_id,
            field_id=field_id,
            left=left_desc,
            right=right_desc,
            options=options,
            fingerprint=fingerprint,
        )
        if facts:
            conflict.impact_chain["facts"] = facts
        if kind == "mapping_dropped" and facts.get("baseline_present"):
            conflict.baseline_dependent = True
        if kind == "field_removed" and facts.get("field_live_on_baseline"):
            conflict.baseline_dependent = True
        return conflict

    @staticmethod
    def _conflict_fingerprint(
        kind: str,
        left_desc: dict[str, Any],
        right_desc: dict[str, Any],
        facts: dict[str, Any],
    ) -> str:
        import hashlib

        payload = json.dumps(
            {
                "kind": kind,
                "left": left_desc,
                "right": right_desc,
                "facts": facts,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _refresh_conflicts(
        self,
        session: MergeSession,
        left: DraftProposal | None = None,
        right: DraftProposal | None = None,
    ) -> list[MergeConflict]:
        """Re-detect conflicts while preserving user resolutions.

        Decisions are keyed by stable conflict id and kept only while the
        underlying facts (fingerprint) are unchanged. If the baseline moved and
        a fact changed, the conflict returns to unresolved and is flagged with
        needs_reconfirm so the user must decide again. New conflicts appear
        unresolved; conflicts that vanished disappear entirely.
        """

        left = left or self._require_draft(session.left_draft_id)
        right = right or self._require_draft(session.right_draft_id)
        previous = {conflict.id: conflict for conflict in session.conflicts}
        fresh = self._detect_conflicts(left, right)
        baseline_moved = (
            left.base_revision != self.store.revision
            or right.base_revision != self.store.revision
            or session.created_revision != self.store.revision
        )
        refreshed: list[MergeConflict] = []
        for conflict in fresh:
            old = previous.get(conflict.id)
            facts_changed = old is not None and old.fingerprint != conflict.fingerprint
            decided_at = old.decision_revision if old else None
            moved_after_decision = (
                baseline_moved
                and old is not None
                and old.decision
                and decided_at is not None
                and decided_at < self.store.revision
                and conflict.baseline_dependent
            )
            if moved_after_decision:
                conflict.decision = old.decision
                conflict.needs_reconfirm = True
                conflict.decision_revision = decided_at
            elif old is not None and old.fingerprint == conflict.fingerprint and old.decision:
                conflict.decision = old.decision
                conflict.decision_revision = decided_at
            elif facts_changed and old is not None and old.decision:
                conflict.needs_reconfirm = True
            elif old is not None and old.needs_reconfirm and old.decision:
                # Baseline shifted again before the user re-confirmed.
                conflict.decision = None
                conflict.needs_reconfirm = True
            conflict.impact_chain = {
                **conflict.impact_chain,
                **self._conflict_impact_chain(session, conflict, fresh),
            }
            refreshed.append(conflict)
        session.conflicts = refreshed
        return refreshed

    def merge_conflicts(self, merge_id: str, *, refresh: bool = True) -> list[MergeConflict]:
        session = self._require_merge(merge_id)
        if session.status != "open":
            raise ValidationError(f"merge session '{merge_id}' is {session.status}")
        if refresh:
            self._refresh_conflicts(session)
            self.store.save()
        return list(session.conflicts)

    def resolve_merge_conflict(
        self, merge_id: str, conflict_id: str, decision: str
    ) -> MergeConflict:
        """Record one user decision; repeated previews never clear it."""

        session = self._require_merge(merge_id)
        if session.status != "open":
            raise ValidationError(f"merge session '{merge_id}' is {session.status}")
        self._refresh_conflicts(session)
        conflict = next(
            (item for item in session.conflicts if item.id == conflict_id), None
        )
        if conflict is None:
            raise NotFoundError(
                f"conflict '{conflict_id}' does not exist in merge '{merge_id}'"
            )
        allowed = set(conflict.options)
        if decision not in allowed:
            raise ValidationError(
                f"decision '{decision}' is not valid for conflict '{conflict_id}'; "
                f"choose one of {sorted(allowed)}"
            )
        conflict.decision = decision
        conflict.needs_reconfirm = False
        conflict.decision_revision = self.store.revision
        self.store.save()
        return conflict

    def _apply_merge_decisions(
        self,
        session: MergeSession,
        left: DraftProposal,
        right: DraftProposal,
    ) -> tuple[dict[str, list[dict[str, Any]]], set[str], list[MergeConflict]]:
        """Build the resolved edge plan.

        Returns per-transform merged mappings, the set of fields to remove, and
        the still-unresolved conflicts. Unresolved conflict edges are left out
        of the plan entirely so they never enter the formal graph.
        """

        left_plan, left_removed = self._draft_plan(left)
        right_plan, right_removed = self._draft_plan(right)
        unresolved = [c for c in session.conflicts if not c.decision or c.needs_reconfirm]
        # Union removals; one-sided removals only survive when the user
        # resolves the corresponding field_removed conflict towards removal.
        both_removed = left_removed & right_removed
        left_only_removed = left_removed - right_removed
        right_only_removed = right_removed - left_removed
        resolved_removals: set[str] = set()

        # Start with the union of both sides' edge sets.
        merged: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)

        def add(transform_id, mapping, side):
            key = (mapping["source_field_id"], mapping["target_field_id"])
            merged[transform_id][key] = dict(mapping)

        for transform_id, mappings in left_plan.items():
            for mapping in mappings:
                add(transform_id, mapping, left.id)
        for transform_id, mappings in right_plan.items():
            for mapping in mappings:
                add(transform_id, mapping, right.id)

        for conflict in session.conflicts:
            decision = None if conflict.needs_reconfirm else conflict.decision
            if conflict.kind == "mapping_replaced":
                key3 = (
                    conflict.transform_id,
                    conflict.source_field_id,
                    conflict.target_field_id,
                )
                edge_key = (conflict.source_field_id, conflict.target_field_id)
                if decision == self.LEFT:
                    merged[conflict.transform_id][edge_key] = {
                        "source_field_id": conflict.left["source_field_id"],
                        "target_field_id": conflict.left["target_field_id"],
                        "expression": conflict.left.get("expression"),
                    }
                elif decision == self.RIGHT:
                    merged[conflict.transform_id][edge_key] = {
                        "source_field_id": conflict.right["source_field_id"],
                        "target_field_id": conflict.right["target_field_id"],
                        "expression": conflict.right.get("expression"),
                    }
                else:
                    merged[conflict.transform_id].pop(edge_key, None)
            elif conflict.kind == "mapping_dropped":
                edge_key = (conflict.source_field_id, conflict.target_field_id)
                kept_side = conflict.impact_chain.get("facts", {}).get("kept_side")
                if decision == self.DROP:
                    merged[conflict.transform_id].pop(edge_key, None)
                elif decision == "keep_baseline":
                    base_edge = next(
                        (
                            e
                            for e in self._effective_edges().values()
                            if e.transform_id == conflict.transform_id
                            and e.source_field_id == conflict.source_field_id
                            and e.target_field_id == conflict.target_field_id
                        ),
                        None,
                    )
                    if base_edge is None:
                        merged[conflict.transform_id].pop(edge_key, None)
                    else:
                        merged[conflict.transform_id][edge_key] = {
                            "source_field_id": base_edge.source_field_id,
                            "target_field_id": base_edge.target_field_id,
                            "expression": base_edge.expression,
                        }
                elif decision == kept_side:
                    kept_mapping = conflict.left if kept_side == self.LEFT else conflict.right
                    merged[conflict.transform_id][edge_key] = {
                        "source_field_id": kept_mapping["source_field_id"],
                        "target_field_id": kept_mapping["target_field_id"],
                        "expression": kept_mapping.get("expression"),
                    }
                else:
                    # Unresolved: keep the still-referenced edge only if it
                    # comes from a transform the other side did not replace.
                    # Here both sides stage the transform, so omit it.
                    merged[conflict.transform_id].pop(edge_key, None)
            elif conflict.kind == "field_removed":
                field_id = conflict.field_id
                removed_by = conflict.impact_chain.get("facts", {}).get("removed_by")
                if decision == (self.LEFT if removed_by == left.id else self.RIGHT):
                    resolved_removals.add(field_id)
                    for transform_id in list(merged):
                        merged[transform_id] = {
                            key: mapping
                            for key, mapping in merged[transform_id].items()
                            if field_id not in key
                        }
                else:
                    left_only_removed.discard(field_id)
                    right_only_removed.discard(field_id)

        merged_mappings = {
            transform_id: list(mappings.values())
            for transform_id, mappings in merged.items()
            if mappings
        }
        # Unresolved one-side deletions are not applied.
        removed_fields = set(both_removed) | set(resolved_removals)
        removed_fields |= {
            field_id
            for field_id in left_only_removed | right_only_removed
            if not any(
                c.kind == "field_removed"
                and c.field_id == field_id
                and (not c.decision or c.needs_reconfirm)
                for c in session.conflicts
            )
        }
        return merged_mappings, removed_fields, unresolved

    def _simulate_plan(
        self,
        merged_mappings: dict[str, list[dict[str, Any]]],
        removed_fields: set[str],
        replaced_transforms: set[str] | None = None,
    ) -> dict[str, Contribution]:
        """Apply a merged plan over the effective graph without persisting it.

        Drafts may stage a transform that also exists on the baseline, or a
        brand-new transform. Either way the staged mapping set replaces that
        transform's baseline edges inside the simulation.
        """

        replaced_transforms = replaced_transforms or set(merged_mappings)
        simulated = {
            edge_id: edge
            for edge_id, edge in self._effective_edges().items()
            if edge.source_field_id not in removed_fields
            and edge.target_field_id not in removed_fields
            and edge.transform_id not in replaced_transforms
        }
        for transform_id, mappings in merged_mappings.items():
            for mapping in mappings:
                if (
                    mapping["source_field_id"] in removed_fields
                    or mapping["target_field_id"] in removed_fields
                ):
                    continue
                edge_id = self._contribution_id(
                    transform_id,
                    mapping["source_field_id"],
                    mapping["target_field_id"],
                )
                simulated[edge_id] = Contribution(
                    id=edge_id,
                    transform_id=transform_id,
                    source_field_id=mapping["source_field_id"],
                    target_field_id=mapping["target_field_id"],
                    expression=mapping.get("expression"),
                )
        return simulated

    def _impact_paths(
        self,
        edges: dict[str, Contribution],
        start_field_id: str,
        *,
        downstream: bool,
        max_paths: int = 5,
    ) -> dict[str, Any]:
        adjacency: dict[str, list[Contribution]] = defaultdict(list)
        for edge in edges.values():
            if downstream:
                adjacency[edge.source_field_id].append(edge)
            else:
                adjacency[edge.target_field_id].append(edge)
        direct: list[str] = []
        paths: list[list[str]] = []
        visited: set[str] = set()
        queue = deque(
            (edge, [start_field_id]) for edge in adjacency[start_field_id]
        )
        while queue and len(paths) < max_paths:
            edge, path = queue.popleft()
            next_id = (
                edge.target_field_id if downstream else edge.source_field_id
            )
            new_path = [*path, next_id]
            if len(new_path) == 2 and next_id not in direct:
                direct.append(next_id)
            if next_id not in visited or len(paths) < max_paths:
                paths.append(new_path)
            visited.add(next_id)
            if next_id != start_field_id:
                for next_edge in adjacency[next_id]:
                    following = (
                        next_edge.target_field_id
                        if downstream
                        else next_edge.source_field_id
                    )
                    if following not in new_path:
                        queue.append((next_edge, new_path))
        return {
            "start_field_id": start_field_id,
            "direct": sorted(set(direct)),
            "example_paths": paths[:max_paths],
        }

    def _conflict_impact_chain(
        self,
        session: MergeSession,
        conflict: MergeConflict,
        all_conflicts: list[MergeConflict],
    ) -> dict[str, Any]:
        """Explain which fields/results each unresolved decision affects."""

        left = self._require_draft(session.left_draft_id)
        right = self._require_draft(session.right_draft_id)
        merged_mappings, removed_fields, _ = self._apply_merge_decisions(
            session, left, right
        )
        replaced = set(self._draft_plan(left)[0]) | set(self._draft_plan(right)[0])
        edges = self._simulate_plan(merged_mappings, removed_fields, replaced)
        chain: dict[str, Any] = {}
        if conflict.kind == "field_removed":
            field_id = conflict.field_id
            # The chain explains what deletion would break on the graph the
            # two reviewed drafts were built on, regardless of tentative picks.
            edges = self._effective_edges()
            chain["field_id"] = field_id
            chain["downstream"] = self._impact_paths(edges, field_id, downstream=True)
            chain["upstream"] = self._impact_paths(edges, field_id, downstream=False)
            chain["referenced_by"] = conflict.impact_chain.get("facts", {}).get(
                "referenced_by", []
            )
        else:
            source_id = conflict.source_field_id
            target_id = conflict.target_field_id
            chain["source_field_id"] = source_id
            chain["target_field_id"] = target_id
            chain["source_impact"] = self._impact_paths(edges, source_id, downstream=True)
            chain["target_upstream"] = self._impact_paths(edges, target_id, downstream=False)
        return chain

    def merge_preview(self, merge_id: str) -> "MergePreview":
        """Preview the merged plan; unresolved pieces stay out of the graph."""

        session = self._require_merge(merge_id)
        if session.status != "open":
            raise ValidationError(f"merge session '{merge_id}' is {session.status}")
        left = self._require_draft(session.left_draft_id)
        right = self._require_draft(session.right_draft_id)
        self._refresh_conflicts(session, left, right)
        merged_mappings, removed_fields, unresolved = self._apply_merge_decisions(
            session, left, right
        )
        replaced_transforms = set(self._draft_plan(left)[0]) | set(
            self._draft_plan(right)[0]
        )
        simulated = self._simulate_plan(
            merged_mappings, removed_fields, replaced_transforms
        )
        current_edges = self._effective_edges()
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
        lost_sources = {
            target: sorted(current_sources[target] - simulated_sources.get(target, set()))
            for target in current_sources
            if current_sources[target] - simulated_sources.get(target, set())
        }
        source_less = sorted(
            target
            for target in set(current_sources) | set(simulated_sources)
            if target not in removed_fields
            and not simulated_sources.get(target)
            and current_sources.get(target)
        )
        current_cycles = {tuple(c) for c in self._graph_cycles(current_edges.values())}
        new_cycles = [
            cycle
            for cycle in self._graph_cycles(simulated.values())
            if tuple(cycle) not in current_cycles
        ]
        baseline_moved = (
            left.base_revision != self.store.revision
            or right.base_revision != self.store.revision
            or session.created_revision != self.store.revision
        )
        preview = MergePreview(
            merge_id=session.id,
            current_revision=self.store.revision,
            left_base_revision=left.base_revision,
            right_base_revision=right.base_revision,
            baseline_moved=baseline_moved,
            left_draft_id=left.id,
            right_draft_id=right.id,
            added_dependencies=[
                {
                    "transform_id": t,
                    "source_field_id": s,
                    "target_field_id": g,
                    "origin": self._edge_origin(t, s, g, left, right),
                }
                for t, s, g in added
            ],
            removed_dependencies=[
                {"transform_id": t, "source_field_id": s, "target_field_id": g}
                for t, s, g in removed
            ],
            removed_fields=sorted(removed_fields),
            lost_sources=lost_sources,
            source_less_results=source_less,
            new_cycles=new_cycles,
            unresolved_conflicts=[
                {
                    "id": conflict.id,
                    "kind": conflict.kind,
                    "needs_reconfirm": conflict.needs_reconfirm,
                }
                for conflict in unresolved
            ],
            resolved_conflict_ids=[
                conflict.id
                for conflict in session.conflicts
                if conflict.decision and not conflict.needs_reconfirm
            ],
            changes_from_left=[
                staged.transform_id for staged in left.transforms
            ],
            changes_from_right=[
                staged.transform_id for staged in right.transforms
            ],
            left_removed_fields=list(left.removed_fields),
            right_removed_fields=list(right.removed_fields),
        )
        self.store.save()
        return preview

    @staticmethod
    def _edge_origin(
        transform_id: str,
        source_id: str,
        target_id: str,
        left: DraftProposal,
        right: DraftProposal,
    ) -> str:
        def contains(draft):
            staged = next(
                (s for s in draft.transforms if s.transform_id == transform_id), None
            )
            if staged is None:
                return False
            return any(
                m["source_field_id"] == source_id
                and m["target_field_id"] == target_id
                for m in staged.mappings
            )

        in_left = contains(left)
        in_right = contains(right)
        if in_left and in_right:
            return "both"
        if in_left:
            return left.id
        return right.id

    def complete_merge(
        self,
        merge_id: str,
        result_draft_id: str,
        *,
        name: str | None = None,
        confirm_stale: bool = False,
    ) -> DraftProposal:
        """Create one pending-publish draft from the resolved merge.

        All conflicts must be resolved and, when the baseline moved since the
        merge started, re-confirmed. Unresolved parts are absent from the
        resulting draft rather than silently overwritten.
        """

        session = self._require_merge(merge_id)
        if session.status != "open":
            raise ValidationError(f"merge session '{merge_id}' is {session.status}")
        left = self._require_draft(session.left_draft_id)
        right = self._require_draft(session.right_draft_id)
        self._refresh_conflicts(session, left, right)
        merged_mappings, removed_fields, unresolved = self._apply_merge_decisions(
            session, left, right
        )
        baseline_moved = (
            left.base_revision != self.store.revision
            or right.base_revision != self.store.revision
            or session.created_revision != self.store.revision
        )
        if baseline_moved and not confirm_stale:
            raise StaleBaselineError(
                merge_id, session.created_revision, self.store.revision
            )
        if unresolved:
            raise ValidationError(
                f"merge '{merge_id}' has {len(unresolved)} unresolved conflict(s): "
                + ", ".join(conflict.id for conflict in unresolved)
            )
        if result_draft_id in self.store.drafts:
            raise AlreadyExistsError(f"draft '{result_draft_id}' already exists")
        result = DraftProposal(
            id=result_draft_id,
            name=name or session.name,
            base_revision=self.store.revision,
            sources=[left.id, right.id],
            removed_fields=sorted(removed_fields),
            metadata={"merged_from": session.id},
        )
        staged_meta = {
            staged.transform_id: staged
            for draft in (left, right)
            for staged in draft.transforms
        }
        for transform_id in sorted(merged_mappings):
            meta = staged_meta.get(transform_id)
            if meta is None:
                base_transform = self.store.transforms.get(transform_id)
                meta_name = base_transform.name if base_transform else transform_id
                meta_version = (
                    base_transform.version if base_transform else "merged"
                )
                meta_code = base_transform.code_ref if base_transform else None
            else:
                meta_name = meta.name
                meta_version = meta.version
                meta_code = meta.code_ref
            result.transforms.append(
                DraftMapping(
                    transform_id=transform_id,
                    name=meta_name,
                    version=meta_version,
                    code_ref=meta_code,
                    description=meta.description if meta else None,
                    mappings=[
                        {
                            "source_field_id": mapping["source_field_id"],
                            "target_field_id": mapping["target_field_id"],
                            "expression": mapping.get("expression"),
                            "origin": self._edge_origin(
                                transform_id,
                                mapping["source_field_id"],
                                mapping["target_field_id"],
                                left,
                                right,
                            ),
                        }
                        for mapping in merged_mappings[transform_id]
                    ],
                )
            )
        self.store.drafts[result.id] = result
        session.status = "completed"
        session.result_draft_id = result.id
        self.store.save()
        return result

    def abandon_merge(self, merge_id: str) -> MergeSession:
        """Give up a merge; both source drafts and all history stay intact."""

        session = self._require_merge(merge_id)
        if session.status != "open":
            raise ValidationError(f"merge session '{merge_id}' is {session.status}")
        session.status = "abandoned"
        self.store.save()
        return session

    def merge_session(self, merge_id: str, *, refresh: bool = False) -> MergeSession:
        session = self._require_merge(merge_id)
        if refresh and session.status == "open":
            self._refresh_conflicts(session)
            self.store.save()
        return session


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
    """Repeated preview of an in-progress two-draft merge."""

    merge_id: str
    current_revision: int
    left_base_revision: int
    right_base_revision: int
    baseline_moved: bool
    left_draft_id: str
    right_draft_id: str
    added_dependencies: list[dict[str, Any]]
    removed_dependencies: list[dict[str, Any]]
    removed_fields: list[str]
    lost_sources: dict[str, list[str]]
    source_less_results: list[str]
    new_cycles: list[list[str]]
    unresolved_conflicts: list[dict[str, Any]]
    resolved_conflict_ids: list[str]
    changes_from_left: list[str]
    changes_from_right: list[str]
    left_removed_fields: list[str]
    right_removed_fields: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "merge_id": self.merge_id,
            "current_revision": self.current_revision,
            "left_base_revision": self.left_base_revision,
            "right_base_revision": self.right_base_revision,
            "baseline_moved": self.baseline_moved,
            "left_draft_id": self.left_draft_id,
            "right_draft_id": self.right_draft_id,
            "added_dependencies": self.added_dependencies,
            "removed_dependencies": self.removed_dependencies,
            "removed_fields": self.removed_fields,
            "lost_sources": self.lost_sources,
            "source_less_results": self.source_less_results,
            "new_cycles": self.new_cycles,
            "unresolved_conflicts": self.unresolved_conflicts,
            "resolved_conflict_ids": self.resolved_conflict_ids,
            "changes_from_left": self.changes_from_left,
            "changes_from_right": self.changes_from_right,
            "left_removed_fields": self.left_removed_fields,
            "right_removed_fields": self.right_removed_fields,
        }
