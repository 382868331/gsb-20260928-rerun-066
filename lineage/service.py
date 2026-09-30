from __future__ import annotations

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
        if source_field_id not in self.store.fields:
            raise NotFoundError(f"source field '{source_field_id}' does not exist")
        if target_field_id not in self.store.fields:
            raise NotFoundError(f"target field '{target_field_id}' does not exist")
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
            if (
                edge.source_field_id not in self.store.fields
                or edge.target_field_id not in self.store.fields
            ):
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

    def draft_diff(self, draft_id: str) -> DraftDiff:
        """Compare a draft against the current effective graph.

        Reports dependencies the draft would add, active dependencies it would
        remove, target fields that lose sources, and cycles that would appear.
        """

        draft = self._require_draft(draft_id)
        current_edges = self._effective_edges()
        simulated = dict(current_edges)
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
        if not draft.transforms:
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

        backup_transforms = dict(self.store.transforms)
        backup_contributions = {
            key: Contribution(**item.to_dict())
            for key, item in self.store.contributions.items()
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
                snapshot=self._graph_snapshot(),
            )
            self.store.history.append(entry)
            self.store.save()
        except Exception:
            self.store.transforms = backup_transforms
            self.store.contributions = backup_contributions
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
