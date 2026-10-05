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
    Field,
    FieldSplitBatch,
    FieldSplitDecision,
    FieldSplitPlan,
    HistoryEntry,
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

    # --- Explicit field splits ------------------------------------------

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_field_split(
        self,
        plan_id: str,
        old_field_id: str,
        new_field_ids: Iterable[str],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> FieldSplitPlan:
        if plan_id in self.store.field_splits:
            raise AlreadyExistsError(f"field split plan '{plan_id}' already exists")
        self._require_field(old_field_id)
        new_field_ids = list(new_field_ids)
        if not new_field_ids:
            raise ValidationError("a field split must introduce at least one new field")
        if len(new_field_ids) != len(set(new_field_ids)):
            raise ValidationError("new fields in a split must be unique")
        for new_field_id in new_field_ids:
            field_item = self._require_field(new_field_id)
            if not field_item.active:
                raise ValidationError(f"new field '{new_field_id}' is deleted")
            if new_field_id == old_field_id:
                raise ValidationError("a split new field cannot be the old field")
        plan = FieldSplitPlan(
            id=plan_id,
            old_field_id=old_field_id,
            new_field_ids=list(new_field_ids),
            base_revision=self.store.revision,
            metadata=metadata or {},
        )
        self.store.field_splits[plan_id] = plan
        self.store.save()
        return plan

    def _require_field_split(self, plan_id: str) -> FieldSplitPlan:
        try:
            return self.store.field_splits[plan_id]
        except KeyError:
            raise NotFoundError(f"field split plan '{plan_id}' does not exist") from None

    @staticmethod
    def _decision_key(transform_id: str, source_id: str, target_id: str) -> tuple[str, str, str]:
        return transform_id, source_id, target_id

    def _split_active_dependencies(self, plan: FieldSplitPlan) -> dict[tuple[str, str, str], Contribution]:
        edges = self._effective_edges()
        outgoing: dict[str, list[Contribution]] = defaultdict(list)
        for edge in edges.values():
            outgoing[edge.source_field_id].append(edge)
        queue = deque(outgoing[plan.old_field_id])
        dependencies: dict[tuple[str, str, str], Contribution] = {}
        while queue:
            edge = queue.popleft()
            key = self._decision_key(edge.transform_id, edge.source_field_id, edge.target_field_id)
            if key in dependencies:
                continue
            dependencies[key] = edge
            queue.extend(outgoing[edge.target_field_id])
        return dependencies

    def _split_decision_map(self, plan: FieldSplitPlan) -> dict[tuple[str, str, str], FieldSplitDecision]:
        return {
            self._decision_key(item.transform_id, item.source_field_id, item.target_field_id): item
            for item in plan.decisions
        }

    def field_split_report(self, plan_id: str) -> dict[str, Any]:
        plan = self._require_field_split(plan_id)
        active = self._split_active_dependencies(plan)
        decisions = self._split_decision_map(plan)
        legacy = []
        explicit = []
        manual = []
        recheck = []
        unhandled = []
        for key in sorted(active):
            edge = active[key]
            decision = decisions.get(key)
            impact = self.impact(edge.target_field_id)
            item = {
                "transform_id": edge.transform_id,
                "source_field_id": edge.source_field_id,
                "target_field_id": edge.target_field_id,
                "expression": edge.expression,
                "downstream": sorted(impact.direct),
                "transitive_downstream": sorted(impact.transitive),
            }
            if decision is None:
                unhandled.append(item)
            elif decision.status == "keep_old":
                legacy.append({**item, "decision": decision.to_dict()})
            elif decision.status == "use_new":
                explicit.append({**item, "decision": decision.to_dict()})
            elif decision.status == "recheck":
                recheck.append({**item, "decision": decision.to_dict()})
            else:
                manual.append({**item, "decision": decision.to_dict()})
        stale = [
            decision.to_dict()
            for decision in plan.decisions
            if self._decision_key(
                decision.transform_id, decision.source_field_id, decision.target_field_id
            )
            not in active
        ]
        return {
            "plan_id": plan.id,
            "old_field_id": plan.old_field_id,
            "new_field_ids": list(plan.new_field_ids),
            "status": plan.status,
            "base_revision": plan.base_revision,
            "current_revision": self.store.revision,
            "stale": plan.base_revision != self.store.revision,
            "legacy_dependencies": legacy,
            "new_field_dependencies": explicit,
            "recheck_dependencies": recheck,
            "manual_review_dependencies": manual,
            "unhandled_dependencies": unhandled,
            "obsolete_decisions": stale,
            "batches": [batch.to_dict() for batch in plan.batches],
        }

    def decide_field_split_dependency(
        self,
        plan_id: str,
        transform_id: str,
        target_field_id: str,
        status: str,
        *,
        source_field_id: str | None = None,
        new_field_id: str | None = None,
        interpretation: str | None = None,
        rationale: str | None = None,
    ) -> FieldSplitDecision:
        plan = self._require_field_split(plan_id)
        if plan.status != "open":
            raise ValidationError(f"field split plan '{plan_id}' is {plan.status}")
        if status not in {"keep_old", "use_new", "manual"}:
            raise ValidationError("status must be keep_old, use_new, or manual")
        source_field_id = source_field_id or plan.old_field_id
        key = self._decision_key(transform_id, source_field_id, target_field_id)
        active = self._split_active_dependencies(plan)
        if key not in active:
            raise NotFoundError("the specified dependency is not an active dependency of the old field")
        edge = active[key]
        if status == "use_new":
            if edge.source_field_id == plan.old_field_id:
                if new_field_id not in plan.new_field_ids:
                    raise ValidationError("new_field_id must be one explicitly declared by this split plan")
            elif new_field_id is not None:
                raise ValidationError("downstream dependencies choose their source through new_paths")
            if not interpretation:
                raise ValidationError("use_new requires an explicit interpretation")
        elif edge.source_field_id == plan.old_field_id and new_field_id is not None:
            raise ValidationError("only use_new decisions can choose a new field")
        decision = FieldSplitDecision(
            transform_id=transform_id,
            source_field_id=source_field_id,
            target_field_id=target_field_id,
            status=status,
            new_field_id=new_field_id,
            interpretation=interpretation,
            rationale=rationale,
            decided_at=self._utc_now(),
        )
        plan.decisions = [
            item
            for item in plan.decisions
            if self._decision_key(item.transform_id, item.source_field_id, item.target_field_id) != key
        ]
        plan.decisions.append(decision)
        self.store.save()
        return decision

    def create_split_migration_batch(
        self,
        plan_id: str,
        batch_id: str,
        dependency_keys: Iterable[tuple[str, ...]],
        *,
        new_paths: Iterable[DraftMapping] | None = None,
    ) -> FieldSplitBatch:
        plan = self._require_field_split(plan_id)
        if any(batch.id == batch_id for batch in plan.batches):
            raise AlreadyExistsError(f"split batch '{batch_id}' already exists")
        if plan.status != "open":
            raise ValidationError(f"field split plan '{plan_id}' is {plan.status}")
        active = self._split_active_dependencies(plan)
        decisions = self._split_decision_map(plan)
        selected: list[tuple[str, str, str]] = []
        for item in dependency_keys:
            if len(item) == 2:
                transform_id, target_id = item
                source_id = plan.old_field_id
            elif len(item) == 3:
                transform_id, source_id, target_id = item
            else:
                raise ValidationError("dependency key must be (transform_id, target_field_id) or (transform_id, source_field_id, target_field_id)")
            key = self._decision_key(transform_id, source_id, target_id)
            edge = active.get(key)
            if edge is None:
                raise NotFoundError("selected dependency is not an active dependency of the old field")
            decision = decisions.get(key)
            if decision is None:
                raise ValidationError("cannot migrate an undecided dependency")
            if decision.status != "use_new":
                raise ValidationError(f"dependency '{key}' is {decision.status}, not explicitly migrated")
            selected.append(key)
        if not selected:
            raise ValidationError("a migration batch must select at least one decided dependency")
        new_path_transforms = list(new_paths or [])
        self._validate_new_paths(plan, new_path_transforms)
        backup_plan = FieldSplitPlan.from_dict(plan.to_dict())
        backup_drafts = {
            key: DraftProposal.from_dict(item.to_dict())
            for key, item in self.store.drafts.items()
        }
        batch = FieldSplitBatch(
            id=batch_id,
            plan_id=plan_id,
            dependency_keys=[list(key) for key in selected],
        )
        plan.batches.append(batch)
        try:
            draft = self._materialize_split_batch(plan, batch, new_path_transforms, save=False)
            self.store.drafts[draft.id] = draft
            validation = self._validate_split_batch(plan, batch, draft, save=False)
            batch.affected_chain = validation["affected_chain"]
            batch.source_comparison = validation["source_comparison"]
            batch.adopted = [item.to_dict() for item in draft.transforms]
            self.store.save()
        except Exception:
            self.store.field_splits[plan.id] = backup_plan
            self.store.drafts = backup_drafts
            raise
        return batch

    def _validate_new_paths(self, plan: FieldSplitPlan, staged: Iterable[DraftMapping]) -> None:
        seen = set()
        for item in staged:
            if not item.mappings:
                raise ValidationError("a new path transform must contribute at least one mapping")
            key = item.transform_id
            if key in seen:
                raise AlreadyExistsError("new paths contain a repeated transform id")
            seen.add(key)
            for mapping in item.mappings:
                source_id = mapping["source_field_id"]
                target_id = mapping["target_field_id"]
                self._check_mapping_fields(source_id, target_id)
                if target_id == plan.old_field_id:
                    raise ValidationError("new split paths cannot target the old field")

    def _materialize_split_batch(
        self,
        plan: FieldSplitPlan,
        batch: FieldSplitBatch,
        new_path_transforms: Iterable[DraftMapping],
        *,
        save: bool,
    ) -> DraftProposal:
        decisions = self._split_decision_map(plan)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        identity: dict[str, DraftMapping] = {}
        selected_keys = {tuple(item) for item in batch.dependency_keys}
        for staged in new_path_transforms:
            identity[staged.transform_id] = staged
            grouped[staged.transform_id].extend(staged.mappings)
        current = self._effective_edges()
        active_by_transform: dict[str, list[Contribution]] = defaultdict(list)
        for edge in current.values():
            active_by_transform[edge.transform_id].append(edge)
        for key_tuple in selected_keys:
            decision = decisions[key_tuple]
            if key_tuple[1] != plan.old_field_id:
                continue
            existing = self.store.contributions[
                self._contribution_id(key_tuple[0], key_tuple[1], key_tuple[2])
            ]
            transform = self.store.transforms[key_tuple[0]]
            identity[key_tuple[0]] = DraftMapping(
                transform_id=key_tuple[0],
                name=transform.name,
                version=transform.version,
                code_ref=transform.code_ref,
                description=transform.description,
            )
            grouped[key_tuple[0]].append(
                {
                    "source_field_id": decision.new_field_id,
                    "target_field_id": key_tuple[2],
                    "expression": existing.expression,
                    "split_interpretation": decision.interpretation,
                }
            )
        for transform_id, edges in active_by_transform.items():
            if transform_id not in grouped:
                continue
            for edge in edges:
                key = (edge.transform_id, edge.source_field_id, edge.target_field_id)
                if key in selected_keys:
                    continue
                grouped[transform_id].append(
                    {
                        "source_field_id": edge.source_field_id,
                        "target_field_id": edge.target_field_id,
                        "expression": edge.expression,
                    }
                )
        transforms = []
        for transform_id in sorted(grouped):
            base = identity[transform_id]
            transforms.append(
                DraftMapping(
                    transform_id=base.transform_id,
                    name=base.name,
                    version=base.version,
                    code_ref=base.code_ref,
                    description=base.description,
                    mappings=grouped[transform_id],
                )
            )
        draft_id = f"split-draft:{plan.id}:{batch.id}"
        existing_draft = self.store.drafts.get(draft_id)
        if existing_draft is not None:
            if save:
                raise AlreadyExistsError(f"draft '{draft_id}' already exists")
            existing_draft.transforms = transforms
            existing_draft.deleted_fields = []
            existing_draft.status = "open"
            existing_draft.metadata = {
                "field_split_plan_id": plan.id,
                "split_batch_id": batch.id,
            }
            return existing_draft
        return DraftProposal(
            id=draft_id,
            name=f"Field split {plan.id} batch {batch.id}",
            base_revision=self.store.revision,
            transforms=transforms,
            metadata={"field_split_plan_id": plan.id, "split_batch_id": batch.id},
        )

    def _validate_split_batch(
        self,
        plan: FieldSplitPlan,
        batch: FieldSplitBatch,
        draft: DraftProposal,
        *,
        save: bool,
    ) -> dict[str, Any]:
        conflicts = self._split_batch_mapping_conflicts(plan, batch, draft)
        if conflicts:
            raise ValidationError({"message": "split batch conflicts", "conflicts": conflicts})
        diff = self.draft_diff(draft.id)
        if diff.new_cycles:
            raise ValidationError({"message": "split batch would create a cycle", "new_cycles": diff.new_cycles})
        removed_keys = {
            (item["transform_id"], item["source_field_id"], item["target_field_id"])
            for item in diff.removed_dependencies
        }
        for key in {tuple(item) for item in batch.dependency_keys}:
            if key[1] != plan.old_field_id and key not in removed_keys:
                raise ValidationError(
                    {
                        "message": "downstream migration decision is not disconnected from the old chain",
                        "dependency": list(key),
                    }
                )
        selected_targets = {tuple(item)[2] for item in batch.dependency_keys}
        replaced_old_sources = {
            tuple(item)[2]
            for item in batch.dependency_keys
            if tuple(item)[1] == plan.old_field_id
        }
        affected_chain: dict[str, Any] = {}
        for target_id in sorted(replaced_old_sources):
            impact = self.impact(target_id)
            downstream = sorted(impact.direct)
            if downstream:
                selected_set = set(selected_targets)
                unselected = [item for item in downstream if item not in selected_set]
                if unselected:
                    raise ValidationError(
                        {
                            "message": "shared intermediate would silently change downstream users",
                            "intermediate_field_id": target_id,
                            "unselected_downstream": unselected,
                        }
                    )
                affected_chain[target_id] = {
                    "direct_downstream": downstream,
                    "transitive_downstream": sorted(impact.transitive),
                }
        current_edges = self._effective_edges()
        simulated, _ = self._simulate_changes(current_edges, set(), draft.transforms)
        affected_targets = selected_targets | {
            edge.target_field_id
            for staged in draft.transforms
            for edge in self.store.contributions.values()
            if edge.active and edge.transform_id == staged.transform_id
        }
        comparison = {}
        for target_id in sorted(affected_targets):
            before = sorted({edge.source_field_id for edge in current_edges.values() if edge.target_field_id == target_id})
            after = sorted({edge.source_field_id for edge in simulated.values() if edge.target_field_id == target_id})
            if before != after:
                comparison[target_id] = {"before": before, "after": after}
        return {"affected_chain": affected_chain, "source_comparison": comparison}

    def _split_batch_mapping_conflicts(
        self,
        plan: FieldSplitPlan,
        batch: FieldSplitBatch,
        draft: DraftProposal,
    ) -> list[dict[str, Any]]:
        touched_transforms = {staged.transform_id for staged in draft.transforms}
        conflicts = []
        for other_draft in self.store.drafts.values():
            if other_draft.id == draft.id or other_draft.status != "open":
                continue
            if other_draft.metadata.get("field_split_plan_id") == plan.id:
                other_batch_id = other_draft.metadata.get("split_batch_id")
                other_batch = next((item for item in plan.batches if item.id == other_batch_id), None)
                if other_batch is not None and other_batch.status == "published":
                    continue
            overlap = touched_transforms & {staged.transform_id for staged in other_draft.transforms}
            if overlap:
                conflicts.append(
                    {
                        "type": "concurrent_transform_mapping",
                        "draft_id": other_draft.id,
                        "transform_ids": sorted(overlap),
                        "impact_chain": self._mapping_conflict_chain(sorted(overlap)),
                    }
                )
        return conflicts

    def _mapping_conflict_chain(self, transform_ids: set[str] | list[str]) -> dict[str, list[str]]:
        fields = {
            field_id
            for edge in self._effective_edges().values()
            if edge.transform_id in set(transform_ids)
            for field_id in (edge.source_field_id, edge.target_field_id)
        }
        return {
            "fields": sorted(fields),
            "impact": sorted({downstream for field_id in fields for downstream in self.impact(field_id).all_nodes()}),
        }

    def publish_split_migration_batch(self, plan_id: str, batch_id: str, *, note: str | None = None) -> HistoryEntry:
        plan = self._require_field_split(plan_id)
        batch = next((item for item in plan.batches if item.id == batch_id), None)
        if batch is None:
            raise NotFoundError(f"split batch '{batch_id}' does not exist")
        if batch.status != "open":
            raise ValidationError(f"split batch '{batch_id}' is {batch.status}")
        if batch.recheck_required:
            raise ValidationError({"message": "batch requires recheck", "reasons": batch.recheck_reasons})
        draft_id = f"split-draft:{plan.id}:{batch.id}"
        draft = self._require_draft(draft_id)
        backup_field_splits = {key: FieldSplitPlan.from_dict(item.to_dict()) for key, item in self.store.field_splits.items()}

        def commit_hook() -> None:
            batch.status = "published"
            batch.published_at = self._utc_now()
            for item in batch.dependency_keys:
                decision = self._split_decision_map(plan)[tuple(item)]
                decision.batch_id = batch.id

        try:
            entry = self.publish_draft(draft_id, note=note, commit_hook=commit_hook)
            batch.status = "published"
            batch.history_id = entry.id
            batch.published_revision = entry.revision
        except Exception:
            self.store.field_splits = {
                key: FieldSplitPlan.from_dict(item.to_dict())
                for key, item in backup_field_splits.items()
            }
            raise
        return entry

    def withdraw_split_migration_batch(
        self,
        plan_id: str,
        batch_id: str,
        *,
        note: str | None = None,
    ) -> HistoryEntry:
        plan = self._require_field_split(plan_id)
        batch = next((item for item in plan.batches if item.id == batch_id), None)
        if batch is None:
            raise NotFoundError(f"split batch '{batch_id}' does not exist")
        if batch.status != "published" or batch.history_id is None:
            raise ValidationError(f"split batch '{batch_id}' is {batch.status}")

        def commit_hook() -> None:
            batch.status = "withdrawn"
            batch.withdrawn_at = self._utc_now()
            batch.recheck_required = False
            batch.recheck_reasons = []
            for item in batch.dependency_keys:
                key = tuple(item)
                decision = self._split_decision_map(plan).get(key)
                if decision is not None and decision.batch_id == batch.id:
                    decision.batch_id = None
                    decision.status = "recheck"
            withdrawn_targets = {tuple(item)[2] for item in batch.dependency_keys}
            for other in plan.batches:
                if other.id == batch.id or other.status != "published":
                    continue
                reasons = []
                for other_key in {tuple(item) for item in other.dependency_keys}:
                    upstream = self.upstream(other_key[2])
                    chain = {other_key[2], *upstream.all_nodes()}
                    if chain & withdrawn_targets:
                        reasons.append(
                            f"dependency {other_key[0]}:{other_key[1]}->{other_key[2]} depends on withdrawn chain"
                        )
                if reasons:
                    other.recheck_required = True
                    other.recheck_reasons.extend(reasons)

        return self.withdraw_publish(batch.history_id, note=note, commit_hook=commit_hook)

    def recheck_split_migration_batch(self, plan_id: str, batch_id: str) -> FieldSplitBatch:
        plan = self._require_field_split(plan_id)
        batch = next((item for item in plan.batches if item.id == batch_id), None)
        if batch is None:
            raise NotFoundError(f"split batch '{batch_id}' does not exist")
        if batch.status != "published":
            raise ValidationError(f"split batch '{batch_id}' is {batch.status}")
        batch.recheck_required = False
        batch.recheck_reasons = []
        self.store.save()
        return batch

    def retire_split_old_field(self, plan_id: str, *, note: str | None = None) -> HistoryEntry:
        plan = self._require_field_split(plan_id)
        if plan.status != "open":
            raise ValidationError(f"field split plan '{plan_id}' is {plan.status}")
        active_dependencies = self._split_active_dependencies(plan)
        if active_dependencies:
            keys = [list(key) for key in sorted(active_dependencies)]
            raise ValidationError(
                {
                    "message": "old field still has active dependencies",
                    "dependencies": keys,
                }
            )
        active_keys = set(active_dependencies)
        unresolved = [
            decision.to_dict()
            for decision in plan.decisions
            if self._decision_key(
                decision.transform_id, decision.source_field_id, decision.target_field_id
            )
            in active_keys
            and (decision.status in {"manual", "recheck"} or decision.batch_id is None)
        ]
        if unresolved:
            raise ValidationError(
                {
                    "message": "old field has unresolved migration decisions",
                    "decisions": unresolved,
                }
            )
        draft_id = f"split-retire:{plan.id}"
        existing = self.store.drafts.get(draft_id)
        if existing is None:
            draft = DraftProposal(
                id=draft_id,
                name=f"Retire split old field {plan.old_field_id}",
                base_revision=self.store.revision,
                metadata={"field_split_plan_id": plan.id, "retire_old_field": True},
            )
            self.store.drafts[draft_id] = draft
        else:
            draft = existing
            draft.status = "open"
            draft.base_revision = self.store.revision
        draft.transforms = []
        draft.deleted_fields = [plan.old_field_id]

        def commit_hook() -> None:
            plan.status = "retired"
            plan.retirement_draft_id = draft_id

        return self.publish_draft(draft_id, note=note, commit_hook=commit_hook)

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
        commit_hook=None,
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
        if not draft.transforms and not draft.deleted_fields:
            raise ValidationError("draft has no staged transforms or field deletions")
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

        backup_transforms = dict(self.store.transforms)
        backup_contributions = {
            key: Contribution(**item.to_dict())
            for key, item in self.store.contributions.items()
        }
        backup_fields = {
            key: Field(**item.to_dict()) for key, item in self.store.fields.items()
        }
        backup_drafts = {
            key: DraftProposal.from_dict(item.to_dict())
            for key, item in self.store.drafts.items()
        }
        backup_field_splits = {
            key: FieldSplitPlan.from_dict(item.to_dict())
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
            if commit_hook is not None:
                commit_hook()
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
                deactivated_fields=deactivated_fields,
                created_transforms=created_transforms,
                snapshot=self._graph_snapshot(),
            )
            self.store.history.append(entry)
            self.store.save()
        except Exception:
            self.store.transforms = backup_transforms
            self.store.contributions = backup_contributions
            self.store.fields = backup_fields
            self.store.drafts = backup_drafts
            self.store.field_splits = backup_field_splits
            self.store.revision = backup_revision
            draft.status = "open"
            self.store.history = [
                item for item in self.store.history if item.draft_id != draft_id or item.action != "publish"
            ]
            raise
        return entry

    def withdraw_publish(
        self, history_id: str, *, note: str | None = None, commit_hook=None
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
        backup_fields = {
            key: Field(**item.to_dict()) for key, item in self.store.fields.items()
        }
        backup_drafts = {
            key: DraftProposal.from_dict(item.to_dict())
            for key, item in self.store.drafts.items()
        }
        backup_field_splits = {
            key: FieldSplitPlan.from_dict(item.to_dict())
            for key, item in self.store.field_splits.items()
        }
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
            for field_id in getattr(original, "deactivated_fields", []):
                field_item = self.store.fields.get(field_id)
                if field_item is not None:
                    field_item.active = True
            if commit_hook is not None:
                commit_hook()
            self.store.revision += 1
            entry = HistoryEntry(
                id=f"history:r{self.store.revision}:withdraw:{original.draft_id}",
                revision=self.store.revision,
                action="withdraw",
                draft_id=original.draft_id,
                note=f"withdraw:{original.id}" if note is None else note,
                activated=list(original.deactivated),
                deactivated=list(original.activated),
                deactivated_fields=list(original.deactivated_fields),
                snapshot=self._graph_snapshot(),
            )
            self.store.history.append(entry)
            self.store.save()
        except Exception:
            self.store.transforms = backup_transforms
            self.store.contributions = backup_contributions
            self.store.revision = backup_revision
            self.store.fields = backup_fields
            self.store.drafts = backup_drafts
            self.store.field_splits = backup_field_splits
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
