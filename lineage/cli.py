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

    restore = subparsers.add_parser("restore-transform")
    restore.add_argument("transform_id")

    identity = subparsers.add_parser("field-identity")
    identity.add_argument("field_id")

    upstream = subparsers.add_parser("upstream")
    upstream.add_argument("field_id")

    impact = subparsers.add_parser("impact")
    impact.add_argument("field_id")

    list_items = subparsers.add_parser("list")
    list_items.add_argument("kind", choices=["objects", "fields", "transforms", "contributions"])
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
