from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import (
    Contribution,
    DataObject,
    DraftProposal,
    FieldSplit,
    Field,
    HistoryEntry,
    MigrationBatch,
    MergeSession,
    TransformVersion,
)


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
        self.drafts: dict[str, DraftProposal] = {}
        self.field_splits: dict[str, FieldSplit] = {}
        self.migration_batches: dict[str, MigrationBatch] = {}
        self.merge_sessions: dict[str, MergeSession] = {}
        self.history: list[HistoryEntry] = []
        self.revision: int = 0
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
            item["id"]: DraftProposal.from_dict(item)
            for item in raw.get("drafts", [])
        }
        self.field_splits = {
            item["id"]: FieldSplit.from_dict(item)
            for item in raw.get("field_splits", [])
        }
        self.migration_batches = {
            item["id"]: MigrationBatch.from_dict(item)
            for item in raw.get("migration_batches", [])
        }
        self.merge_sessions = {
            item["id"]: MergeSession.from_dict(item)
            for item in raw.get("merge_sessions", [])
        }
        self.history = [
            HistoryEntry.from_dict(item) for item in raw.get("history", [])
        ]
        self.revision = int(raw.get("revision", 0))

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
            "field_splits": [item.to_dict() for item in self.field_splits.values()],
            "migration_batches": [
                item.to_dict() for item in self.migration_batches.values()
            ],
            "merge_sessions": [
                item.to_dict() for item in self.merge_sessions.values()
            ],
            "history": [item.to_dict() for item in self.history],
            "revision": self.revision,
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
