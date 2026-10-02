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

    diff = subparsers.add_parser("draft-diff")
    diff.add_argument("draft_id")

    publish = subparsers.add_parser("publish-draft")
    publish.add_argument("draft_id")
    publish.add_argument("--confirm-stale", action="store_true")
    publish.add_argument("--note")

    discard = subparsers.add_parser("discard-draft")
    discard.add_argument("draft_id")

    remove_field = subparsers.add_parser("remove-field-draft")
    remove_field.add_argument("draft_id")
    remove_field.add_argument("field_id")

    merge_start = subparsers.add_parser("merge-start")
    merge_start.add_argument("merge_id")
    merge_start.add_argument("left_draft_id")
    merge_start.add_argument("right_draft_id")
    merge_start.add_argument("--name")

    merge_conflicts = subparsers.add_parser("merge-conflicts")
    merge_conflicts.add_argument("merge_id")

    merge_resolve = subparsers.add_parser("merge-resolve")
    merge_resolve.add_argument("merge_id")
    merge_resolve.add_argument("conflict_id")
    merge_resolve.add_argument(
        "decision",
        help="left | right | drop | keep_baseline (per conflict options)",
    )

    merge_preview = subparsers.add_parser("merge-preview")
    merge_preview.add_argument("merge_id")

    merge_complete = subparsers.add_parser("merge-complete")
    merge_complete.add_argument("merge_id")
    merge_complete.add_argument("result_draft_id")
    merge_complete.add_argument("--name")
    merge_complete.add_argument("--confirm-stale", action="store_true")

    merge_abandon = subparsers.add_parser("merge-abandon")
    merge_abandon.add_argument("merge_id")

    withdraw = subparsers.add_parser("withdraw-publish")
    withdraw.add_argument("history_id")
    withdraw.add_argument("--note")

    subparsers.add_parser("history")

    list_items = subparsers.add_parser("list")
    list_items.add_argument("kind", choices=["objects", "fields", "transforms", "contributions", "drafts", "merges"])
    delete_field = subparsers.add_parser("delete-field")
    delete_field.add_argument("field_id")

    restore_field = subparsers.add_parser("restore-field")
    restore_field.add_argument("field_id")
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
    elif args.command == "remove-field-draft":
        result = service.remove_field_from_draft(args.draft_id, args.field_id)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "merge-start":
        result = service.start_merge(
            args.merge_id,
            args.left_draft_id,
            args.right_draft_id,
            name=args.name,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "merge-conflicts":
        conflicts = service.merge_conflicts(args.merge_id)
        print(json.dumps([c.to_dict() for c in conflicts], ensure_ascii=False, indent=2))
        return
    elif args.command == "merge-resolve":
        result = service.resolve_merge_conflict(
            args.merge_id, args.conflict_id, args.decision
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "merge-preview":
        print(json.dumps(service.merge_preview(args.merge_id).to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "merge-complete":
        result = service.complete_merge(
            args.merge_id,
            args.result_draft_id,
            name=args.name,
            confirm_stale=args.confirm_stale,
        )
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "merge-abandon":
        result = service.abandon_merge(args.merge_id)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "delete-field":
        result = service.delete_field(args.field_id)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "restore-field":
        result = service.restore_field(args.field_id)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "withdraw-publish":
        print(json.dumps(service.withdraw_publish(args.history_id, note=args.note).to_dict(), ensure_ascii=False, indent=2))
        return
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
        elif args.kind == "merges":
            payload = [x.to_dict() for x in service.store.merges.values()]
        else:
            payload = [_contribution_dict(service, x) for x in service.store.contributions.values()]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    else:  # pragma: no cover
        parser.error("unknown command")

    if isinstance(result, Contribution):
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
