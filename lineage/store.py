from __future__ import annotations

import copy
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Contribution, DataObject, Field, TransformVersion


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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "objects": [item.to_dict() for item in self.objects.values()],
            "fields": [item.to_dict() for item in self.fields.values()],
            "transforms": [item.to_dict() for item in self.transforms.values()],
            "contributions": [
                item.to_dict() for item in self.contributions.values()
            ],
        }
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def snapshot(self) -> dict[str, dict]:
        return {
            "objects": copy.deepcopy(self.objects),
            "fields": copy.deepcopy(self.fields),
            "transforms": copy.deepcopy(self.transforms),
            "contributions": copy.deepcopy(self.contributions),
        }

    def restore(self, state: dict[str, dict]) -> None:
        self.objects = copy.deepcopy(state["objects"])
        self.fields = copy.deepcopy(state["fields"])
        self.transforms = copy.deepcopy(state["transforms"])
        self.contributions = copy.deepcopy(state["contributions"])

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Commit a batch of in-memory edits with a single save.

        On failure the in-memory graph is rolled back to the snapshot taken at
        entry, so a rejected batch can never leave half of its edges in the
        current graph.  Nothing is persisted until the whole batch succeeds.
        """

        state = self.snapshot()
        try:
            yield
            self.save()
        except BaseException:
            self.restore(state)
            raise
