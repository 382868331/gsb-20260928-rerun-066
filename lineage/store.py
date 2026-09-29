from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .exceptions import NotFoundError, StateConflictError, ValidationError
from .models import DataObject, FieldRef, Relation, Transformation, utc_now


class LineageStore:
    """JSON-backed repository for objects, transformation versions, and relations."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else None
        self.objects: dict[str, DataObject] = {}
        self.transformations: dict[str, Transformation] = {}
        self.relations: list[Relation] = []
        if self.path is not None and self.path.exists():
            self.load()

    def add_object(self, obj: DataObject) -> DataObject:
        self._require_identifier("object id", obj.id)
        self._require_identifier("object name", obj.name)
        self._require_identifier("object version", obj.version)
        if not obj.fields:
            raise ValidationError("DataObject must declare at least one field")
        if len(obj.fields) != len(set(obj.fields)):
            raise ValidationError(f"Duplicate fields in object {obj.id!r}")
        existing = next(
            (
                candidate
                for candidate in self.objects.values()
                if candidate.name == obj.name
                and candidate.version == obj.version
                and candidate.branch == obj.branch
            ),
            None,
        )
        if existing is not None and existing.id != obj.id:
            raise StateConflictError(
                f"Object coordinate {obj.branch}/{obj.name}@{obj.version} already uses id {existing.id!r}"
            )
        if obj.id in self.objects:
            raise StateConflictError(f"Object {obj.id!r} already exists")
        self.objects[obj.id] = obj
        return obj

    def add_transformation(self, transformation: Transformation) -> Transformation:
        self._require_identifier("transformation id", transformation.id)
        self._require_identifier("transformation name", transformation.name)
        self._require_identifier("transformation version", transformation.version)
        if transformation.id in self.transformations:
            raise StateConflictError(f"Transformation {transformation.id!r} already exists")
        self.transformations[transformation.id] = transformation
        return transformation

    def add_relation(self, relation: Relation) -> Relation:
        self._validate_field_ref(relation.source, "source")
        self._validate_field_ref(relation.target, "target")
        transformation = self._get_transformation(relation.transformation_id)
        if transformation.is_deleted:
            raise StateConflictError(
                f"Cannot add relation to deleted transformation {transformation.id!r}"
            )
        key = (relation.source, relation.target, relation.transformation_id)
        if any(
            (existing.source, existing.target, existing.transformation_id) == key
            for existing in self.relations
        ):
            raise StateConflictError(
                "Relation already exists for source, target, and transformation"
            )
        self.relations.append(relation)
        return relation

    def add_relations(self, relations: list[Relation]) -> list[Relation]:
        added: list[Relation] = []
        try:
            for relation in relations:
                added.append(self.add_relation(relation))
        except Exception:
            for relation in added:
                self.relations.remove(relation)
            raise
        return added

    def delete_transformation(self, transformation_id: str) -> list[Relation]:
        """Withdraw exactly the relations contributed by one transformation."""

        transformation = self._get_transformation(transformation_id)
        removed = [
            relation
            for relation in self.relations
            if relation.transformation_id == transformation_id
        ]
        if removed:
            self.relations = [
                relation
                for relation in self.relations
                if relation.transformation_id != transformation_id
            ]
        if not transformation.is_deleted:
            transformation.deleted_at = utc_now()
        return removed

    def get_object(self, object_id: str) -> DataObject:
        try:
            return self.objects[object_id]
        except KeyError as exc:
            raise NotFoundError(f"Object {object_id!r} not found") from exc

    def find_object(self, name: str, version: str, branch: str = "main") -> DataObject:
        for obj in self.objects.values():
            if obj.name == name and obj.version == version and obj.branch == branch:
                return obj
        raise NotFoundError(f"Object {branch}/{name}@{version} not found")

    def find_field(
        self, name: str, field_name: str, version: str, branch: str = "main"
    ) -> FieldRef:
        obj = self.find_object(name=name, version=version, branch=branch)
        return self.field(obj.id, field_name)

    def field(self, object_id: str, field_name: str) -> FieldRef:
        ref = FieldRef(object_id=object_id, field_name=field_name)
        self._validate_field_ref(ref, "field")
        return ref

    def relations_from(self, ref: FieldRef) -> list[Relation]:
        return [relation for relation in self.relations if relation.source == ref]

    def relations_to(self, ref: FieldRef) -> list[Relation]:
        return [relation for relation in self.relations if relation.target == ref]

    def save(self) -> None:
        if self.path is None:
            raise ValidationError("No persistence path was configured")
        payload = {
            "objects": {key: value.as_dict() for key, value in self.objects.items()},
            "transformations": {
                key: value.as_dict() for key, value in self.transformations.items()
            },
            "relations": [relation.as_dict() for relation in self.relations],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.objects = {
            key: DataObject.from_dict(value)
            for key, value in payload.get("objects", {}).items()
        }
        self.transformations = {
            key: Transformation.from_dict(value)
            for key, value in payload.get("transformations", {}).items()
        }
        self.relations = [Relation.from_dict(item) for item in payload.get("relations", [])]

    def _validate_field_ref(self, ref: FieldRef, label: str) -> None:
        self._require_identifier(f"{label} object id", ref.object_id)
        self._require_identifier(f"{label} field name", ref.field_name)
        obj = self.objects.get(ref.object_id)
        if obj is None:
            raise NotFoundError(f"{label.title()} object {ref.object_id!r} not found")
        if not obj.has_field(ref.field_name):
            raise NotFoundError(
                f"{label.title()} field {ref.field_name!r} not found on object {ref.object_id!r}"
            )

    def _get_transformation(self, transformation_id: str) -> Transformation:
        try:
            return self.transformations[transformation_id]
        except KeyError as exc:
            raise NotFoundError(
                f"Transformation {transformation_id!r} not found"
            ) from exc

    @staticmethod
    def _require_identifier(label: str, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label} must be a non-empty string")
