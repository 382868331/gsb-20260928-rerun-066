import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lineage import JsonLineageStore, LineageService, ValidationError
from lineage.models import DraftMapping


class FieldSplitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LineageService(
            JsonLineageStore(Path(self.temp.name) / "lineage.json")
        )
        self.service.create_object("raw.v1", "raw", "v1")
        self.service.create_object("out.v1", "out", "v1")
        for field_id in ("old", "a", "b", "x", "y", "mid", "keep", "new_path"):
            object_id = "raw.v1" if field_id in {"old", "a", "b"} else "out.v1"
            self.service.create_field(f"raw.{field_id}", "raw.v1", field_id, exist_ok=True) if field_id in {"old", "a", "b"} else self.service.create_field(
                f"out.{field_id}", "out.v1", field_id, exist_ok=True
            )

    def test_explicit_split_decisions_keep_old_and_new_in_one_release(self):
        self.service.submit_transform(
            "t.xy", "xy", "1",
            [("raw.old", "out.x", "x"), ("raw.old", "out.y", "y")],
        )
        self.service.create_field_split("split.old", "raw.old", ["raw.a", "raw.b"])

        with self.assertRaises(ValidationError):
            self.service.decide_field_split_dependency(
                "split.old", "t.xy", "out.x", "use_new", new_field_id="raw.a"
            )
        self.service.decide_field_split_dependency(
            "split.old", "t.xy", "out.x", "use_new",
            new_field_id="raw.a", interpretation="explicit meaning A",
        )
        self.service.decide_field_split_dependency(
            "split.old", "t.xy", "out.y", "manual", rationale="mixed semantics"
        )
        report = self.service.field_split_report("split.old")
        self.assertEqual(len(report["new_field_dependencies"]), 1)
        self.assertEqual(len(report["manual_review_dependencies"]), 1)

        with self.assertRaises(ValidationError):
            self.service.create_split_migration_batch(
                "split.old", "manual", [("t.xy", "out.y")]
            )
        self.service.create_split_migration_batch(
            "split.old", "b1", [("t.xy", "out.x")]
        )
        self.service.publish_split_migration_batch("split.old", "b1")
        self.assertEqual(set(self.service.upstream("out.x").direct), {"raw.a"})
        self.assertEqual(set(self.service.upstream("out.y").direct), {"raw.old"})
        with self.assertRaises(ValidationError):
            self.service.retire_split_old_field("split.old")

    def test_shared_intermediate_requires_explicit_new_path_for_new_consumer(self):
        self.service.submit_transform("t.mid", "mid", "1", [("raw.old", "out.mid", "m")])
        self.service.submit_transform("t.keep", "keep", "1", [("out.mid", "out.keep", "k")])
        self.service.submit_transform("t.np", "np", "1", [("out.mid", "out.new_path", "n")])
        self.service.create_field_split("split.old", "raw.old", ["raw.a"])
        self.service.decide_field_split_dependency(
            "split.old", "t.mid", "out.mid", "use_new",
            new_field_id="raw.a", interpretation="meaning for mid",
        )
        self.service.decide_field_split_dependency(
            "split.old", "t.keep", "out.keep", "keep_old", source_field_id="out.mid"
        )
        self.service.decide_field_split_dependency(
            "split.old", "t.np", "out.new_path", "use_new",
            source_field_id="out.mid", interpretation="meaning for new consumer",
        )
        with self.assertRaises(ValidationError) as blocked:
            self.service.create_split_migration_batch(
                "split.old", "local", [("t.mid", "out.mid")]
            )
        self.assertEqual(blocked.exception.args[0]["intermediate_field_id"], "out.mid")

        new_path = DraftMapping(
            transform_id="t.np",
            name="np",
            version="1",
            mappings=[
                {
                    "source_field_id": "raw.a",
                    "target_field_id": "out.new_path",
                    "expression": "new path",
                }
            ],
        )
        batch = self.service.create_split_migration_batch(
            "split.old", "isolated",
            [("t.np", "out.mid", "out.new_path")],
            new_paths=[new_path],
        )
        self.assertEqual(batch.source_comparison["out.new_path"]["before"], ["out.mid"])
        self.service.publish_split_migration_batch("split.old", "isolated")
        self.assertEqual(set(self.service.upstream("out.keep").direct), {"out.mid"})
        self.assertEqual(set(self.service.upstream("out.mid").direct), {"raw.old"})
        self.assertEqual(set(self.service.upstream("out.new_path").direct), {"raw.a"})

    def test_cycle_failure_publishes_no_part_of_graph(self):
        self.service.submit_transform("t.mid", "mid", "1", [("raw.old", "out.mid", "m")])
        self.service.create_field_split("split.old", "raw.old", ["raw.a"])
        self.service.decide_field_split_dependency(
            "split.old", "t.mid", "out.mid", "use_new",
            new_field_id="raw.a", interpretation="meaning",
        )
        cyclic = DraftMapping(
            transform_id="t.loop",
            name="loop",
            version="1",
            mappings=[
                {
                    "source_field_id": "out.mid",
                    "target_field_id": "raw.a",
                    "expression": "loop",
                }
            ],
        )
        with self.assertRaises(ValidationError):
            self.service.create_split_migration_batch(
                "split.old", "cyclic", [("t.mid", "out.mid")], new_paths=[cyclic]
            )
        self.assertNotIn("split-draft:split.old:cyclic", self.service.store.drafts)
        self.assertEqual(set(self.service.upstream("out.mid").direct), {"raw.old"})

    def test_withdraw_keeps_published_evidence_and_marks_dependent_batches_for_recheck(self):
        self.service.submit_transform("t.x", "x", "1", [("raw.old", "out.x", "x")])
        self.service.create_field_split("split.old", "raw.old", ["raw.a"])
        self.service.decide_field_split_dependency(
            "split.old", "t.x", "out.x", "use_new",
            new_field_id="raw.a", interpretation="meaning",
        )
        batch = self.service.create_split_migration_batch(
            "split.old", "b1", [("t.x", "out.x")]
        )
        entry = self.service.publish_split_migration_batch("split.old", "b1")
        published_at = batch.published_at

        withdrawal = self.service.withdraw_split_migration_batch("split.old", "b1")
        self.assertEqual(withdrawal.note, f"withdraw:{entry.id}")
        stored_batch = self.service.field_split_report("split.old")["batches"][0]
        self.assertEqual(stored_batch["status"], "withdrawn")
        self.assertEqual(stored_batch["published_at"], published_at)
        self.assertTrue(stored_batch["published_at"])
        self.assertEqual(set(self.service.upstream("out.x").direct), {"raw.old"})


if __name__ == "__main__":
    unittest.main()
