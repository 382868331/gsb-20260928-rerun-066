from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .models import FieldRef, Relation
from .store import LineageStore


@dataclass(frozen=True, slots=True)
class Dependency:
    field: FieldRef
    evidence: list[Relation]


@dataclass(frozen=True, slots=True)
class ImpactAnalysis:
    origin: FieldRef
    direct: list[Dependency]
    transitive: list[Dependency]

    @property
    def all_fields(self) -> list[FieldRef]:
        return [item.field for item in [*self.direct, *self.transitive]]


def analyze_impact(store: LineageStore, origin: FieldRef) -> ImpactAnalysis:
    """Return downstream fields changed when origin changes.

    Direct dependencies are one edge away. Transitive dependencies are reached
    after at least two edges. BFS visits each field once, so cycles terminate
    and each result carries one concrete evidence path.
    """

    return _traverse(store, origin, forward=True)


def trace_lineage(store: LineageStore, origin: FieldRef) -> ImpactAnalysis:
    """Return upstream fields that contribute to origin."""

    return _traverse(store, origin, forward=False)


def _traverse(store: LineageStore, origin: FieldRef, forward: bool) -> ImpactAnalysis:
    direct_edges = store.relations_from(origin) if forward else store.relations_to(origin)
    direct: list[Dependency] = []
    direct_refs: set[FieldRef] = set()
    queue: deque[tuple[FieldRef, list[Relation]]] = deque()

    for edge in direct_edges:
        neighbor = edge.target if forward else edge.source
        if neighbor == origin:
            continue
        if neighbor in direct_refs:
            direct = _append_evidence(direct, neighbor, [edge])
            continue
        direct_refs.add(neighbor)
        path = [edge]
        direct.append(Dependency(neighbor, path))
        queue.append((neighbor, path))

    transitive_by_field: dict[FieldRef, Dependency] = {}
    visited = {origin, *direct_refs}

    while queue:
        current, current_path = queue.popleft()
        edges = store.relations_from(current) if forward else store.relations_to(current)
        for edge in edges:
            neighbor = edge.target if forward else edge.source
            if neighbor == origin or neighbor in direct_refs or neighbor in visited:
                continue
            visited.add(neighbor)
            path = [*current_path, edge]
            transitive_by_field[neighbor] = Dependency(neighbor, path)
            queue.append((neighbor, path))

    transitive = list(transitive_by_field.values())
    return ImpactAnalysis(origin=origin, direct=direct, transitive=transitive)


def _append_evidence(
    dependencies: list[Dependency], field: FieldRef, evidence: list[Relation]
) -> list[Dependency]:
    for index, dependency in enumerate(dependencies):
        if dependency.field == field:
            dependencies[index] = Dependency(
                field=field, evidence=[*dependency.evidence, *evidence]
            )
            return dependencies
    dependencies.append(Dependency(field, evidence))
    return dependencies
