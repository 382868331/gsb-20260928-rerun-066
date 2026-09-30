from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable

from .drafting import DraftDiff, diff_draft
from .exceptions import (
    AlreadyExistsError,
    BaselineChangedError,
    NotFoundError,
    ValidationError,
)
from .models import (
    Contribution,
    DataObject,
    Draft,
    DraftTransformProposal,
    Field,
    GraphRevision,
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

    # ------------------------------------------------------------------
    # Pending drafts, trial diffs, optimistic publish and graph history
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ensure_baseline_revision(self) -> None:
        """Lazily record revision 0 so every published graph has a parent."""

        if not self.store.revisions:
            self.store.revisions.append(
                GraphRevision(
                    revision=0,
                    kind="baseline",
                    created_at=self._now(),
                    graph_before=None,
                    graph_after=self.store.snapshot_graph(),
                )
            )
            self.store.current_revision = 0

    def create_draft(
        self,
        draft_id: str,
        proposals: Iterable[
            dict[str, Any] | DraftTransformProposal
            | tuple[str, Iterable[tuple[str, str, str | None]]]
        ],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Draft:
        """Create an isolated pending mapping scheme against the current graph.

        Each proposal replaces one transform's full active mapping set inside
        the draft; transforms may be brand new (``name``/``version`` given).
        The whole batch is validated before anything is saved, and the draft
        never touches the effective graph used by ordinary queries.
        """

        if draft_id in self.store.drafts:
            raise AlreadyExistsError(f"draft '{draft_id}' already exists")

        normalized = [self._normalize_proposal(item) for item in proposals]
        if not normalized:
            raise ValidationError("a draft must contain at least one proposal")
        seen_transforms: set[str] = set()
        for proposal in normalized:
            if proposal.transform_id in seen_transforms:
                raise ValidationError(
                    f"duplicate proposal for transform '{proposal.transform_id}' in draft"
                )
            seen_transforms.add(proposal.transform_id)
            self._validate_proposal(proposal)

        self._ensure_baseline_revision()
        draft = Draft(
            id=draft_id,
            proposals=normalized,
            base_revision=self.store.current_revision,
            status="open",
            created_at=self._now(),
            metadata=metadata or {},
        )
        self.store.drafts[draft_id] = draft
        try:
            self.store.save()
        except Exception:
            self.store.drafts.pop(draft_id, None)
            raise
        return draft

    @staticmethod
    def _normalize_proposal(item: Any) -> DraftTransformProposal:
        if isinstance(item, DraftTransformProposal):
            normalized_mappings = [tuple(mapping) for mapping in item.mappings]
            return DraftTransformProposal(
                transform_id=item.transform_id,
                mappings=normalized_mappings,
                name=item.name,
                version=item.version,
                code_ref=item.code_ref,
                description=item.description,
            )
        if isinstance(item, dict):
            return DraftTransformProposal(
                transform_id=item["transform_id"],
                mappings=[tuple(mapping) for mapping in item.get("mappings", [])],
                name=item.get("name"),
                version=item.get("version"),
                code_ref=item.get("code_ref"),
                description=item.get("description"),
            )
        if isinstance(item, tuple) and len(item) == 2:
            transform_id, mappings = item
            return DraftTransformProposal(
                transform_id=transform_id,
                mappings=[tuple(mapping) for mapping in mappings],
            )
        raise ValidationError("invalid draft proposal")

    def _validate_proposal(self, proposal: DraftTransformProposal) -> None:
        if not proposal.transform_id:
            raise ValidationError("proposal transform_id is required")
        if not proposal.mappings:
            raise ValidationError(
                f"proposal for transform '{proposal.transform_id}' must have at least one mapping"
            )
        existing = self.store.transforms.get(proposal.transform_id)
        if existing is None and not (proposal.name and proposal.version):
            raise NotFoundError(
                f"transform '{proposal.transform_id}' does not exist; new transforms need name and version"
            )
        planned_keys: set[tuple[str, str]] = set()
        for mapping in proposal.mappings:
            source_id, target_id = mapping[0], mapping[1]
            self._check_mapping_fields(source_id, target_id)
            key = (source_id, target_id)
            if key in planned_keys:
                raise AlreadyExistsError(
                    f"duplicate mapping {source_id} -> {target_id} in proposal "
                    f"for transform '{proposal.transform_id}'"
                )
            planned_keys.add(key)

    def get_draft(self, draft_id: str) -> Draft:
        try:
            return self.store.drafts[draft_id]
        except KeyError:
            raise NotFoundError(f"draft '{draft_id}' does not exist") from None

    def list_drafts(self, *, include_closed: bool = False) -> list[Draft]:
        return [
            draft
            for draft in self.store.drafts.values()
            if include_closed or draft.status == "open"
        ]

    def draft_diff(self, draft_id: str) -> DraftDiff:
        """Trial-run a draft against the current effective graph.

        Trial edges are never written, so ordinary queries stay unaffected.
        ``baseline_changed`` flags drafts prepared against an older revision.
        """

        draft = self.get_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}, not open")
        for proposal in draft.proposals:
            self._validate_proposal(proposal)
        return diff_draft(
            self.store, draft.id, draft.base_revision, draft.proposals
        )

    def rebase_draft(self, draft_id: str) -> Draft:
        """Re-anchor an outdated draft to the current revision for re-confirmation."""

        draft = self.get_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}, not open")
        for proposal in draft.proposals:
            self._validate_proposal(proposal)
        old_base = draft.base_revision
        draft.base_revision = self.store.current_revision
        try:
            self.store.save()
        except Exception:
            draft.base_revision = old_base
            raise
        return draft

    def publish_draft(self, draft_id: str, *, force: bool = False) -> dict[str, Any]:
        """Publish one confirmed scheme as a new effective-graph revision.

        Drafts based on an outdated revision are rejected with
        :class:`BaselineChangedError`; callers show the refreshed diff and use
        :meth:`rebase_draft` before re-publishing. Publication replaces each
        proposed transform's edge set atomically and keeps the complete graph
        before/after on the revision timeline. Nothing already published is
        overwritten by a stale draft.
        """

        draft = self.get_draft(draft_id)
        if draft.status != "open":
            raise ValidationError(f"draft '{draft_id}' is {draft.status}, not open")
        for proposal in draft.proposals:
            self._validate_proposal(proposal)
        if draft.base_revision != self.store.current_revision and not force:
            raise BaselineChangedError(
                (
                    f"draft '{draft_id}' is based on revision {draft.base_revision} "
                    f"but the effective graph is now at revision "
                    f"{self.store.current_revision}; review the refreshed diff and "
                    "rebase before publishing"
                ),
                draft_id=draft_id,
                base_revision=draft.base_revision,
                current_revision=self.store.current_revision,
            )

        self._ensure_baseline_revision()
        graph_before = self.store.snapshot_graph()
        diff = diff_draft(
            self.store, draft.id, draft.base_revision, draft.proposals
        )

        checkpoint = self.store.snapshot_graph()
        saved_draft_status = draft.status
        previous_revision = self.store.current_revision
        revision_recorded = False
        try:
            self._apply_proposals(draft.proposals)
            draft.status = "published"
            new_revision = self.store.current_revision + 1
            draft.published_revision = new_revision
            self.store.current_revision = new_revision
            self.store.revisions.append(
                GraphRevision(
                    revision=new_revision,
                    kind="publish",
                    created_at=self._now(),
                    draft_id=draft_id,
                    graph_before=graph_before,
                    graph_after=self.store.snapshot_graph(),
                )
            )
            self.store.save()
        except Exception:
            self.store.restore_graph(checkpoint)
            draft.status = saved_draft_status
            draft.published_revision = None
            self.store.current_revision = previous_revision
            if revision_recorded and self.store.revisions:
                self.store.revisions.pop()
            raise
        return {
            "draft_id": draft_id,
            "revision": new_revision,
            "diff": diff.to_dict(),
        }

    def _apply_proposals(self, proposals: list[DraftTransformProposal]) -> None:
        for proposal in proposals:
            transform = self.store.transforms.get(proposal.transform_id)
            if transform is None:
                transform = TransformVersion(
                    id=proposal.transform_id,
                    name=proposal.name,
                    version=proposal.version,
                    code_ref=proposal.code_ref,
                    description=proposal.description,
                )
                self.store.transforms[proposal.transform_id] = transform
            else:
                if proposal.name is not None:
                    transform.name = proposal.name
                if proposal.version is not None:
                    transform.version = proposal.version
                if proposal.code_ref is not None:
                    transform.code_ref = proposal.code_ref
                if proposal.description is not None:
                    transform.description = proposal.description
                transform.active = True

            keep: set[str] = set()
            for source_id, target_id, expression in proposal.mappings:
                contribution_id = self._contribution_id(
                    proposal.transform_id, source_id, target_id
                )
                keep.add(contribution_id)
                existing = self.store.contributions.get(contribution_id)
                if existing is None:
                    self.store.contributions[contribution_id] = Contribution(
                        id=contribution_id,
                        transform_id=proposal.transform_id,
                        source_field_id=source_id,
                        target_field_id=target_id,
                        expression=expression,
                    )
                else:
                    existing.active = True
                    existing.expression = expression
            for edge in list(self.store.contributions.values()):
                if edge.transform_id == proposal.transform_id and edge.id not in keep:
                    edge.active = False

    def withdraw_revision(self, revision: int) -> dict[str, Any]:
        """Withdraw a published revision by restoring its pre-publish graph.

        The withdrawn revision and its graph snapshots stay on the timeline;
        the restored graph becomes a new revision, so withdrawal itself is a
        full historical graph rather than a destructive rewrite.
        """

        record = next(
            (item for item in self.store.revisions if item.revision == revision),
            None,
        )
        if record is None:
            raise NotFoundError(f"revision {revision} does not exist")
        if record.kind != "publish":
            raise ValidationError(f"revision {revision} is {record.kind}, not publish")
        if record.graph_before is None:
            raise ValidationError(f"revision {revision} has no graph snapshot to restore")

        graph_before = self.store.snapshot_graph()
        checkpoint = graph_before
        new_revision = self.store.current_revision + 1
        revision_recorded = False
        try:
            self.store.restore_graph(record.graph_before)
            if record.draft_id:
                draft = self.store.drafts.get(record.draft_id)
                if draft is not None:
                    draft.status = "withdrawn"
                    draft.withdrawn_revision = new_revision
            self.store.current_revision = new_revision
            self.store.revisions.append(
                GraphRevision(
                    revision=new_revision,
                    kind="withdraw",
                    created_at=self._now(),
                    draft_id=record.draft_id,
                    withdrawn_from=revision,
                    graph_before=graph_before,
                    graph_after=self.store.snapshot_graph(),
                )
            )
            revision_recorded = True
            self.store.save()
        except Exception:
            self.store.restore_graph(checkpoint)
            self.store.current_revision = new_revision - 1
            if revision_recorded and self.store.revisions:
                self.store.revisions.pop()
            if record.draft_id:
                draft = self.store.drafts.get(record.draft_id)
                if draft is not None:
                    draft.status = "published"
                    draft.withdrawn_revision = None
            raise
        return {
            "withdrawn_revision": revision,
            "revision": new_revision,
            "draft_id": record.draft_id,
        }

    def list_revisions(self) -> list[GraphRevision]:
        return list(self.store.revisions)

    def revision_graph(self, revision: int) -> dict[str, Any]:
        record = next(
            (item for item in self.store.revisions if item.revision == revision),
            None,
        )
        if record is None:
            raise NotFoundError(f"revision {revision} does not exist")
        snapshot = record.graph_after or record.graph_before
        if snapshot is None:
            raise NotFoundError(f"revision {revision} has no graph snapshot")
        return snapshot

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
