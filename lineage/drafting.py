from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import Contribution, DraftTransformProposal
from .store import JsonLineageStore


EdgeKey = tuple[str, str, str]


def edge_key(edge: Contribution) -> EdgeKey:
    return (edge.transform_id, edge.source_field_id, edge.target_field_id)


def effective_edges(store: JsonLineageStore) -> dict[EdgeKey, Contribution]:
    """Edges visible to normal queries: active edge, active transform, live fields."""

    edges: dict[EdgeKey, Contribution] = {}
    for edge in store.contributions.values():
        if not edge.active:
            continue
        transform = store.transforms.get(edge.transform_id)
        if transform is None or not transform.active:
            continue
        if (
            edge.source_field_id not in store.fields
            or edge.target_field_id not in store.fields
        ):
            continue
        edges[edge_key(edge)] = edge
    return edges


def trial_edges(
    store: JsonLineageStore, proposals: list[DraftTransformProposal]
) -> dict[EdgeKey, Contribution]:
    """Effective graph as a draft would publish it, without touching the store."""

    replaced = {proposal.transform_id for proposal in proposals}
    edges = {
        key: edge
        for key, edge in effective_edges(store).items()
        if edge.transform_id not in replaced
    }
    for proposal in proposals:
        for source_id, target_id, expression in proposal.mappings:
            edge = Contribution(
                id=f"draft:{proposal.transform_id}:{source_id}->{target_id}",
                transform_id=proposal.transform_id,
                source_field_id=source_id,
                target_field_id=target_id,
                expression=expression,
            )
            edges[edge_key(edge)] = edge
    return edges


def adjacency(edges: dict[EdgeKey, Contribution]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for _, source_id, target_id in edges:
        graph[source_id].add(target_id)
    return graph


def reachable_pairs(graph: dict[str, set[str]]) -> set[tuple[str, str]]:
    """All transitive source -> downstream reachability pairs (length >= 1)."""

    pairs: set[tuple[str, str]] = set()
    for start in graph:
        stack = [start]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            for nxt in graph.get(current, ()):  # an edge back to start is still reachable
                if nxt in seen:
                    continue
                seen.add(nxt)
                pairs.add((start, nxt))
                stack.append(nxt)
    return pairs


def simple_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Enumerate simple cycles, canonicalized by smallest starting node.

    Each cycle is returned as ``[start, ..., start]``. Suitable for the small
    graphs this tool manages; duplicate orientations are removed.
    """

    nodes = sorted(graph)
    cycles: list[list[str]] = []
    seen_signatures: set[tuple[str, ...]] = set()
    for start in nodes:
        allowed = {node for node in graph if node >= start}

        def walk(current: str, path: list[str], visited: frozenset[str]) -> None:
            for nxt in graph.get(current, ()):
                if nxt not in allowed:
                    continue
                if nxt == start and len(path) >= 1:
                    cycle = [*path, start]
                    rotation = _canonical_rotation(cycle)
                    if rotation not in seen_signatures:
                        seen_signatures.add(rotation)
                        cycles.append(cycle)
                elif nxt not in visited and nxt > start:
                    walk(nxt, [*path, nxt], visited | {nxt})

        walk(start, [start], frozenset({start}))
    cycles.sort(key=lambda cycle: (len(cycle), cycle))
    return cycles


def _canonical_rotation(closed_cycle: list[str]) -> tuple[str, ...]:
    nodes = closed_cycle[:-1]
    rotations = [tuple(nodes[index:] + nodes[:index]) for index in range(len(nodes))]
    return min(rotations)


@dataclass(slots=True)
class DraftDiff:
    draft_id: str
    base_revision: int
    current_revision: int
    baseline_changed: bool
    added_edges: list[dict[str, str | None]] = field(default_factory=list)
    removed_edges: list[dict[str, str | None]] = field(default_factory=list)
    new_dependencies: list[dict[str, str]] = field(default_factory=list)
    lost_sources: list[dict[str, object]] = field(default_factory=list)
    cycles: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "draft_id": self.draft_id,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
            "baseline_changed": self.baseline_changed,
            "added_edges": self.added_edges,
            "removed_edges": self.removed_edges,
            "new_dependencies": self.new_dependencies,
            "lost_sources": self.lost_sources,
            "cycles": self.cycles,
        }


def diff_draft(
    store: JsonLineageStore,
    draft_id: str,
    base_revision: int,
    proposals: list[DraftTransformProposal],
) -> DraftDiff:
    before = effective_edges(store)
    after = trial_edges(store, proposals)

    added_keys = sorted(set(after) - set(before))
    removed_keys = sorted(set(before) - set(after))
    graph_before = adjacency(before)
    graph_after = adjacency(after)
    pairs_before = reachable_pairs(graph_before)
    pairs_after = reachable_pairs(graph_after)
    new_pair_keys = sorted(pairs_after - pairs_before)

    sources_before: dict[str, set[str]] = defaultdict(set)
    sources_after: dict[str, set[str]] = defaultdict(set)
    for _, source_id, target_id in before:
        sources_before[target_id].add(source_id)
    for _, source_id, target_id in after:
        sources_after[target_id].add(source_id)
    affected_targets = sorted(
        {target for _, _, target in removed_keys} | set(sources_before)
    )
    lost: list[dict[str, object]] = []
    for target_id in affected_targets:
        missing = sorted(sources_before.get(target_id, set()) - sources_after.get(target_id, set()))
        if not missing:
            continue
        lost.append(
            {
                "target_field_id": target_id,
                "lost_source_field_ids": missing,
                "remaining_source_field_ids": sorted(sources_after.get(target_id, set())),
                "orphaned": not sources_after.get(target_id),
            }
        )

    before_signatures = {
        _canonical_rotation([*nodes, nodes[0]])
        for nodes in (cycle[:-1] for cycle in simple_cycles(graph_before))
    }
    cycle_reports: list[dict[str, object]] = []
    for cycle in simple_cycles(graph_after):
        cycle_reports.append(
            {
                "path": cycle,
                "new": _canonical_rotation(cycle) not in before_signatures,
            }
        )

    return DraftDiff(
        draft_id=draft_id,
        base_revision=base_revision,
        current_revision=store.current_revision,
        baseline_changed=base_revision != store.current_revision,
        added_edges=[_edge_dict(after[key]) for key in added_keys],
        removed_edges=[_edge_dict(before[key]) for key in removed_keys],
        new_dependencies=[
            {"upstream_field_id": source_id, "downstream_field_id": target_id}
            for source_id, target_id in new_pair_keys
        ],
        lost_sources=lost,
        cycles=cycle_reports,
    )


def _edge_dict(edge: Contribution) -> dict[str, str | None]:
    return {
        "transform_id": edge.transform_id,
        "source_field_id": edge.source_field_id,
        "target_field_id": edge.target_field_id,
        "expression": edge.expression,
    }
