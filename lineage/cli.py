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

    list_items = subparsers.add_parser("list")
    list_items.add_argument("kind", choices=["objects", "fields", "transforms", "contributions", "drafts", "revisions"])

    draft_create = subparsers.add_parser("draft-create")
    draft_create.add_argument("draft_id")
    draft_create.add_argument("transform_id")
    draft_create.add_argument("--mapping", action="append", nargs=3,
                              metavar=("SOURCE_FIELD", "TARGET_FIELD", "EXPRESSION"), required=True)
    draft_create.add_argument("--name")
    draft_create.add_argument("--version")
    draft_create.add_argument("--code-ref")
    draft_create.add_argument("--description")

    draft_diff = subparsers.add_parser("draft-diff")
    draft_diff.add_argument("draft_id")

    draft_rebase = subparsers.add_parser("draft-rebase")
    draft_rebase.add_argument("draft_id")

    draft_publish = subparsers.add_parser("draft-publish")
    draft_publish.add_argument("draft_id")
    draft_publish.add_argument("--force", action="store_true")

    draft_withdraw = subparsers.add_parser("withdraw-revision")
    draft_withdraw.add_argument("revision", type=int)

    revision_graph = subparsers.add_parser("revision-graph")
    revision_graph.add_argument("revision", type=int)
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
    elif args.command == "list":
        if args.kind == "objects":
            payload = [_object_dict(x) for x in service.store.objects.values()]
        elif args.kind == "fields":
            payload = [_field_dict(x) for x in service.store.fields.values()]
        elif args.kind == "transforms":
            payload = [_transform_dict(x) for x in service.store.transforms.values()]
        elif args.kind == "drafts":
            payload = [x.to_dict() for x in service.list_drafts(include_closed=True)]
        elif args.kind == "revisions":
            payload = [
                {k: v for k, v in x.to_dict().items() if not k.startswith("graph_")}
                for x in service.list_revisions()
            ]
        else:
            payload = [_contribution_dict(service, x) for x in service.store.contributions.values()]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "draft-create":
        proposal = {
            "transform_id": args.transform_id,
            "mappings": [
                [source, target, expression or None]
                for source, target, expression in args.mapping
            ],
            "name": args.name,
            "version": args.version,
            "code_ref": args.code_ref,
            "description": args.description,
        }
        draft = service.create_draft(args.draft_id, [proposal])
        print(json.dumps(draft.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "draft-diff":
        payload = service.draft_diff(args.draft_id).to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "draft-rebase":
        draft = service.rebase_draft(args.draft_id)
        print(json.dumps(draft.to_dict(), ensure_ascii=False, indent=2))
        return
    elif args.command == "draft-publish":
        payload = service.publish_draft(args.draft_id, force=args.force)
        payload["diff"] = {
            key: value for key, value in payload["diff"].items()
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "withdraw-revision":
        payload = service.withdraw_revision(args.revision)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    elif args.command == "revision-graph":
        payload = service.revision_graph(args.revision)
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
