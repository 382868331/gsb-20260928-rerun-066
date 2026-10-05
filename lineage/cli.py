from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .models import Contribution, DataObject, Field, TransformVersion
from .service import LineageQueryResult, LineageService
from .store import JsonLineageStore


def _service(path: str) -> LineageService:
    return LineageService(JsonLineageStore(Path(path)))


def _metadata(value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    return json.loads(value)


def _object_dict(item: DataObject) -> dict[str, Any]:
    return item.to_dict()


def _field_dict(item: Field) -> dict[str, Any]:
    return item.to_dict()


def _transform_dict(item: TransformVersion) -> dict[str, Any]:
    return item.to_dict()


def _contribution_dict(
    service: LineageService, item: Contribution
) -> dict[str, Any]:
    data = item.to_dict()
    transform = service.store.transforms.get(item.transform_id)
    source = service.store.fields.get(item.source_field_id)
    target = service.store.fields.get(item.target_field_id)
    data["evidence"] = {
        "transform_name": transform.name if transform else None,
        "transform_version": transform.version if transform else None,
        "code_ref": transform.code_ref if transform else None,
        "expression": item.expression,
        "source_field": source.name if source else None,
        "target_field": target.name if target else None,
    }
    return data


def _query_dict(result: LineageQueryResult, service: LineageService) -> dict[str, Any]:
    def node_dict(field_id: str, node) -> dict[str, Any]:
        return {
            "field_id": field_id,
            "field_name": node.field.name,
            "object_id": node.object.id,
            "object_name": node.object.name,
            "object_version": node.object.version,
            "branch": node.object.branch,
            "distance": node.distance,
            "example_path": node.paths[0] if node.paths else [field_id],
        }

    return {
        "start_field_id": result.start_field_id,
        "direct": [node_dict(k, v) for k, v in result.direct.items()],
        "transitive": [node_dict(k, v) for k, v in result.transitive.items()],
        "evidence": [
            _contribution_dict(service, edge) for edge in result.edges.values()
        ],
        "cycles": result.cycles,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lineage")
    parser.add_argument("--store", default="lineage.json", help="JSON store path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    obj = subparsers.add_parser("create-object")
    obj.add_argument("id")
    obj.add_argument("name")
    obj.add_argument("version")
    obj.add_argument("--branch")
    obj.add_argument("--metadata")

    field = subparsers.add_parser("create-field")
    field.add_argument("id")
    field.add_argument("object_id")
    field.add_argument("name")
    field.add_argument("--type")
    field.add_argument("--metadata")

    transform = subparsers.add_parser("create-transform")
    transform.add_argument("id")
    transform.add_argument("name")
    transform.add_argument("version")
    transform.add_argument("--code-ref")
    transform.add_argument("--description")
    transform.add_argument("--metadata")

    contribution = subparsers.add_parser("add-contribution")
    contribution.add_argument("source_field_id")
    contribution.add_argument("target_field_id")
    contribution.add_argument("transform_id")
    contribution.add_argument("--expression")
    contribution.add_argument("--metadata")

    delete = subparsers.add_parser("delete-transform")
    delete.add_argument("transform_id")

    replace = subparsers.add_parser("replace-transform")
    replace.add_argument("transform_id")
    replace.add_argument(
        "--mapping",
        action="append",
        nargs=3,
        metavar=("SOURCE_FIELD", "TARGET_FIELD", "EXPRESSION"),
        required=True,
    )

    restore = subparsers.add_parser("restore-transform")
    restore.add_argument("transform_id")

    identity = subparsers.add_parser("field-identity")
    identity.add_argument("field_id")

    upstream = subparsers.add_parser("upstream")
    upstream.add_argument("field_id")

    impact = subparsers.add_parser("impact")
    impact.add_argument("field_id")


    draft = subparsers.add_parser("create-draft")
    draft.add_argument("id")
    draft.add_argument("name")
    draft.add_argument("--metadata")

    stage = subparsers.add_parser("stage-transform")
    stage.add_argument("draft_id")
    stage.add_argument("transform_id")
    stage.add_argument("name")
    stage.add_argument("version")
    stage.add_argument("--code-ref")
    stage.add_argument("--mapping", action="append", nargs=3,
                       metavar=("SOURCE_FIELD", "TARGET_FIELD", "EXPRESSION"), required=True)

    stage_delete = subparsers.add_parser("stage-field-deletion")
    stage_delete.add_argument("draft_id")
    stage_delete.add_argument("field_id")

    diff = subparsers.add_parser("draft-diff")
    diff.add_argument("draft_id")

    publish = subparsers.add_parser("publish-draft")
    publish.add_argument("draft_id")
    publish.add_argument("--confirm-stale", action="store_true")
    publish.add_argument("--note")

    discard = subparsers.add_parser("discard-draft")
    discard.add_argument("draft_id")

    begin_merge = subparsers.add_parser("begin-merge")
    begin_merge.add_argument("session_id")
    begin_merge.add_argument("output_draft_id")
    begin_merge.add_argument("left_draft_id")
    begin_merge.add_argument("right_draft_id")

    preview_merge = subparsers.add_parser("preview-merge")
    preview_merge.add_argument("session_id")

    resolve_merge = subparsers.add_parser("resolve-merge")
    resolve_merge.add_argument("session_id")
    resolve_merge.add_argument("conflict_id")
    resolve_merge.add_argument("choice", choices=["left", "right", "delete_field", "keep_field"])

    publish_merge = subparsers.add_parser("publish-merge")
    publish_merge.add_argument("session_id")
    publish_merge.add_argument("--confirm-stale", action="store_true")
    publish_merge.add_argument("--note")

    abandon_merge = subparsers.add_parser("abandon-merge")
    abandon_merge.add_argument("session_id")

    withdraw = subparsers.add_parser("withdraw-publish")
    withdraw.add_argument("history_id")
    withdraw.add_argument("--note")

    split_create = subparsers.add_parser("create-field-split")
    split_create.add_argument("plan_id")
    split_create.add_argument("old_field_id")
    split_create.add_argument("new_field_id", nargs="+")

    split_report = subparsers.add_parser("field-split-report")
    split_report.add_argument("plan_id")

    split_decide = subparsers.add_parser("decide-field-split")
    split_decide.add_argument("plan_id")
    split_decide.add_argument("transform_id")
    split_decide.add_argument("target_field_id")
    split_decide.add_argument("status", choices=["keep_old", "use_new", "manual"])
    split_decide.add_argument("--source-field")
    split_decide.add_argument("--new-field")
    split_decide.add_argument("--interpretation")
    split_decide.add_argument("--rationale")

    split_batch = subparsers.add_parser("create-split-batch")
    split_batch.add_argument("plan_id")
    split_batch.add_argument("batch_id")
    split_batch.add_argument(
        "--dependency",
        action="append",
        nargs="+",
        metavar="DEPENDENCY",
        required=True,
    )

    split_publish = subparsers.add_parser("publish-split-batch")
    split_publish.add_argument("plan_id")
    split_publish.add_argument("batch_id")
    split_publish.add_argument("--note")

    split_withdraw = subparsers.add_parser("withdraw-split-batch")
    split_withdraw.add_argument("plan_id")
    split_withdraw.add_argument("batch_id")
    split_withdraw.add_argument("--note")

    split_recheck = subparsers.add_parser("recheck-split-batch")
    split_recheck.add_argument("plan_id")
    split_recheck.add_argument("batch_id")

    split_retire = subparsers.add_parser("retire-split-old-field")
    split_retire.add_argument("plan_id")
    split_retire.add_argument("--note")

    subparsers.add_parser("history")

    list_items = subparsers.add_parser("list")
    list_items.add_argument("kind", choices=["objects", "fields", "transforms", "contributions", "drafts", "field-splits"])
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = _service(args.store)

    if args.command == "create-object":
        result = service.create_object(
            args.id,
            args.name,
            args.version,
            branch=args.branch,
            metadata=_metadata(args.metadata),
        )
    elif args.command == "create-field":
        result = service.create_field(
            args.id,
            args.object_id,
            args.name,
            data_type=args.type,
            metadata=_metadata(args.metadata),
        )
    elif args.command == "create-transform":
        result = service.create_transform(
            args.id,
            args.name,
            args.version,
            code_ref=args.code_ref,
            description=args.description,
            metadata=_metadata(args.metadata),
        )
    elif args.command == "add-contribution":
        result = service.add_contribution(
            args.source_field_id,
            args.target_field_id,
            args.transform_id,
            expression=args.expression,
            metadata=_metadata(args.metadata),
        )
    elif args.command == "delete-transform":
        payload = {"deactivated_contributions": service.delete_transform(args.transform_id)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "replace-transform":
        mappings = [
            (source, target, expression or None)
            for source, target, expression in args.mapping
        ]
        contributions = service.replace_transform(args.transform_id, mappings)
        payload = {
            "active_contributions": [
                _contribution_dict(service, item) for item in contributions
            ]
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "restore-transform":
        payload = {"reactivated_contributions": service.restore_transform(args.transform_id)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "field-identity":
        payload = service.field_identity_report(args.field_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "upstream":
        payload = _query_dict(service.upstream(args.field_id), service)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "impact":
        payload = _query_dict(service.impact(args.field_id), service)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "create-draft":
        result = service.create_draft(args.id, args.name, metadata=_metadata(args.metadata))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "stage-transform":
        mappings = [(s, t, e or None) for s, t, e in args.mapping]
        result = service.stage_transform(
            args.draft_id, args.transform_id, args.name, args.version, mappings,
            code_ref=args.code_ref,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "stage-field-deletion":
        result = service.stage_field_deletion(args.draft_id, args.field_id)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "draft-diff":
        print(json.dumps(service.draft_diff(args.draft_id).to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "publish-draft":
        result = service.publish_draft(
            args.draft_id, confirm_stale=args.confirm_stale, note=args.note
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "discard-draft":
        print(json.dumps(service.discard_draft(args.draft_id).to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "begin-merge":
        preview = service.begin_merge(
            args.session_id,
            args.output_draft_id,
            args.left_draft_id,
            args.right_draft_id,
        )
        print(json.dumps(preview.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "preview-merge":
        print(json.dumps(service.preview_merge(args.session_id).to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "resolve-merge":
        preview = service.resolve_merge_conflict(
            args.session_id, args.conflict_id, args.choice
        )
        print(json.dumps(preview.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "publish-merge":
        entry = service.publish_merge(
            args.session_id, confirm_stale=args.confirm_stale, note=args.note
        )
        print(json.dumps(entry.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "abandon-merge":
        session = service.abandon_merge(args.session_id)
        print(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "withdraw-publish":
        print(json.dumps(service.withdraw_publish(args.history_id, note=args.note).to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "create-field-split":
        result = service.create_field_split(
            args.plan_id, args.old_field_id, args.new_field_id
        )
    elif args.command == "field-split-report":
        print(json.dumps(service.field_split_report(args.plan_id), ensure_ascii=False, indent=2))
        return
    elif args.command == "decide-field-split":
        result = service.decide_field_split_dependency(
            args.plan_id,
            args.transform_id,
            args.target_field_id,
            args.status,
            source_field_id=args.source_field,
            new_field_id=args.new_field,
            interpretation=args.interpretation,
            rationale=args.rationale,
        )
    elif args.command == "create-split-batch":
        dependency_keys = []
        for item in args.dependency:
            if len(item) == 2:
                dependency_keys.append(tuple(item))
            elif len(item) == 3:
                transform_id, target_field_id, source_field_id = item
                dependency_keys.append((transform_id, source_field_id, target_field_id))
            else:
                parser.error("dependency requires TRANSFORM TARGET [SOURCE]")
        result = service.create_split_migration_batch(
            args.plan_id,
            args.batch_id,
            dependency_keys,
        )
    elif args.command == "publish-split-batch":
        result = service.publish_split_migration_batch(
            args.plan_id, args.batch_id, note=args.note
        )
    elif args.command == "withdraw-split-batch":
        result = service.withdraw_split_migration_batch(
            args.plan_id, args.batch_id, note=args.note
        )
    elif args.command == "recheck-split-batch":
        result = service.recheck_split_migration_batch(args.plan_id, args.batch_id)
    elif args.command == "retire-split-old-field":
        result = service.retire_split_old_field(args.plan_id, note=args.note)
    elif args.command == "history":
        print(json.dumps([item.to_dict() for item in service.history()], ensure_ascii=False, indent=2))
        return
    elif args.command == "list":
        if args.kind == "objects":
            payload = [_object_dict(x) for x in service.store.objects.values()]
        elif args.kind == "fields":
            payload = [_field_dict(x) for x in service.store.fields.values()]
        elif args.kind == "transforms":
            payload = [_transform_dict(x) for x in service.store.transforms.values()]
        elif args.kind == "drafts":
            payload = [x.to_dict() for x in service.store.drafts.values()]
        elif args.kind == "field-splits":
            payload = [x.to_dict() for x in service.store.field_splits.values()]
        else:
            payload = [_contribution_dict(service, x) for x in service.store.contributions.values()]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    else:  # pragma: no cover
        parser.error("unknown command")

    if hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif isinstance(result, Contribution):
        payload = _contribution_dict(service, result)
    elif isinstance(result, TransformVersion):
        payload = _transform_dict(result)
    elif isinstance(result, DataObject):
        payload = _object_dict(result)
    else:
        payload = _field_dict(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
