from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable

from .exceptions import AlreadyExistsError, NotFoundError, ValidationError
from .models import Contribution, DataObject, Field, TransformVersion
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
        with self.store.atomic():
            existing = self.store.objects.get(object_id)
            if existing is not None:
                if not exist_ok:
                    raise AlreadyExistsError(
                        f"data object '{object_id}' already exists"
                    )
                return existing
            obj = DataObject(
                id=object_id,
                name=name,
                version=version,
                branch=branch,
                metadata=metadata or {},
            )
            self.store.objects[object_id] = obj
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
        with self.store.atomic():
            existing = self.store.fields.get(field_id)
            if existing is not None:
                if not exist_ok:
                    raise AlreadyExistsError(f"field '{field_id}' already exists")
                return existing
            if object_id not in self.store.objects:
                raise NotFoundError(
                    f"data object '{object_id}' does not exist"
                )
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
        with self.store.atomic():
            return self._ensure_transform(
                transform_id,
                name,
                version,
                code_ref=code_ref,
                description=description,
                metadata=metadata,
                exist_ok=exist_ok,
            )

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
        with self.store.atomic():
            return self._add_contribution(
                source_field_id,
                target_field_id,
                transform_id,
                expression=expression,
                metadata=metadata,
                exist_ok=exist_ok,
            )

    def _validate_mappings(
        self,
        transform_id: str,
        mappings: list[tuple[str, str, str | None]],
    ) -> None:
        """Validate a whole mapping batch before any graph mutation."""

        if not mappings:
            raise ValidationError("a transform must contribute at least one mapping")
        if transform_id not in self.store.transforms:
            raise NotFoundError(f"transform '{transform_id}' does not exist")
        seen_pairs: set[tuple[str, str]] = set()
        for source_id, target_id, _expression in mappings:
            if source_id not in self.store.fields:
                raise NotFoundError(
                    f"source field '{source_id}' does not exist"
                )
            if target_id not in self.store.fields:
                raise NotFoundError(
                    f"target field '{target_id}' does not exist"
                )
            if source_id == target_id:
                raise ValidationError("a field cannot contribute to itself")
            pair = (source_id, target_id)
            if pair in seen_pairs:
                raise ValidationError(
                    f"duplicate mapping {source_id} -> {target_id} in same batch"
                )
            seen_pairs.add(pair)

    def _ensure_transform(
        self,
        transform_id: str,
        name: str,
        version: str,
        *,
        code_ref: str | None,
        description: str | None,
        metadata: dict[str, Any] | None,
        exist_ok: bool,
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
        return transform

    def _add_contribution(
        self,
        source_field_id: str,
        target_field_id: str,
        transform_id: str,
        *,
        expression: str | None,
        metadata: dict[str, Any] | None,
        exist_ok: bool,
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
        return contribution

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
        """Create one transform and all of its source-to-target edges atomically."""

        mappings = list(mappings)
        with self.store.atomic():
            self._ensure_transform(
                transform_id,
                name,
                version,
                code_ref=code_ref,
                description=description,
                metadata=transform_metadata,
                exist_ok=exist_ok,
            )
            self._validate_mappings(transform_id, mappings)
            return [
                self._add_contribution(
                    source_id,
                    target_id,
                    transform_id,
                    expression=expression,
                    metadata=None,
                    exist_ok=exist_ok,
                )
                for source_id, target_id, expression in mappings
            ]

    def replace_transform_mappings(
        self,
        transform_id: str,
        mappings: Iterable[tuple[str, str, str | None]],
    ) -> list[Contribution]:
        """Atomically replace every active edge of one transform version.

        The old contributions and the new ones are never active in the same
        graph version: validation runs first, then all edits commit with a
        single atomic save.  Removed edges are soft-deactivated so their audit
        history is retained while excluded from effective queries.
        """

        mappings = list(mappings)
        with self.store.atomic():
            self._validate_mappings(transform_id, mappings)
            new_pairs = {
                (source_id, target_id): expression
                for source_id, target_id, expression in mappings
            }
            for edge in self.store.contributions.values():
                if edge.transform_id == transform_id and edge.active:
                    pair = (edge.source_field_id, edge.target_field_id)
                    if pair not in new_pairs:
                        edge.active = False
            result: list[Contribution] = []
            for source_id, target_id, expression in mappings:
                contribution_id = (
                    f"contrib:{transform_id}:{source_id}->{target_id}"
                )
                existing = self.store.contributions.get(contribution_id)
                if existing is not None:
                    existing.active = True
                    existing.expression = expression
                    result.append(existing)
                else:
                    result.append(
                        self._add_contribution(
                            source_id,
                            target_id,
                            transform_id,
                            expression=expression,
                            metadata=None,
                            exist_ok=False,
                        )
                    )
            return result

    def delete_transform(self, transform_id: str) -> list[str]:
        """Withdraw only edges contributed by one transform version."""

        with self.store.atomic():
            transform = self.store.transforms.get(transform_id)
            if transform is None:
                raise NotFoundError(
                    f"transform '{transform_id}' does not exist"
                )
            affected = [
                contribution_id
                for contribution_id, item in self.store.contributions.items()
                if item.transform_id == transform_id and item.active
            ]
            for contribution_id in affected:
                self.store.contributions[contribution_id].active = False
            transform.active = False
            return affected

    def restore_transform(self, transform_id: str) -> list[str]:
        with self.store.atomic():
            transform = self.store.transforms.get(transform_id)
            if transform is None:
                raise NotFoundError(
                    f"transform '{transform_id}' does not exist"
                )
            affected = [
                contribution_id
                for contribution_id, item in self.store.contributions.items()
                if item.transform_id == transform_id and not item.active
            ]
            for contribution_id in affected:
                self.store.contributions[contribution_id].active = True
            transform.active = True
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
            if transform is not None and not transform.active:
                continue
            if edge.source_field_id not in self.store.fields:
                continue
            if edge.target_field_id not in self.store.fields:
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
