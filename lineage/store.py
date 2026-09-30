from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Contribution, DataObject, Draft, Field, GraphRevision, TransformVersion


class JsonLineageStore:
    """Tiny atomic JSON repository.

    The store is deliberately schema-light so other processing tools can use the
    same service API without deploying a database or data warehouse.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.objects: dict[str, DataObject] = {}
        self.fields: dict[str, Field] = {}
        self.transforms: dict[str, TransformVersion] = {}
        self.contributions: dict[str, Contribution] = {}
        self.drafts: dict[str, Draft] = {}
        self.revisions: list[GraphRevision] = []
        self.current_revision: int = 0
        if self.path.exists():
            self.load()

    def load(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.objects = {
            item["id"]: DataObject.from_dict(item)
            for item in raw.get("objects", [])
        }
        self.fields = {
            item["id"]: Field.from_dict(item)
            for item in raw.get("fields", [])
        }
        self.transforms = {
            item["id"]: TransformVersion.from_dict(item)
            for item in raw.get("transforms", [])
        }
        self.contributions = {
            item["id"]: Contribution.from_dict(item)
            for item in raw.get("contributions", [])
        }
        self.drafts = {
            item["id"]: Draft.from_dict(item)
            for item in raw.get("drafts", [])
        }
        self.revisions = [
            GraphRevision.from_dict(item) for item in raw.get("revisions", [])
        ]
        self.current_revision = raw.get("current_revision", 0)

    def snapshot_graph(self) -> dict[str, object]:
        """Serialize the whole effective graph (objects, fields, transforms, edges)."""

        return {
            "objects": [item.to_dict() for item in self.objects.values()],
            "fields": [item.to_dict() for item in self.fields.values()],
            "transforms": [item.to_dict() for item in self.transforms.values()],
            "contributions": [
                item.to_dict() for item in self.contributions.values()
            ],
        }

    def restore_graph(self, snapshot: dict[str, Any]) -> None:
        """Replace the in-memory effective graph from a revision snapshot."""

        self.objects = {
            item["id"]: DataObject.from_dict(item)
            for item in snapshot.get("objects", [])
        }
        self.fields = {
            item["id"]: Field.from_dict(item)
            for item in snapshot.get("fields", [])
        }
        self.transforms = {
            item["id"]: TransformVersion.from_dict(item)
            for item in snapshot.get("transforms", [])
        }
        self.contributions = {
            item["id"]: Contribution.from_dict(item)
            for item in snapshot.get("contributions", [])
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "objects": [item.to_dict() for item in self.objects.values()],
            "fields": [item.to_dict() for item in self.fields.values()],
            "transforms": [item.to_dict() for item in self.transforms.values()],
            "contributions": [
                item.to_dict() for item in self.contributions.values()
            ],
            "drafts": [item.to_dict() for item in self.drafts.values()],
            "revisions": [item.to_dict() for item in self.revisions],
            "current_revision": self.current_revision,
        }
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary_name, self.path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
