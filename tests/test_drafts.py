import tempfile
import unittest
from pathlib import Path

from lineage import (
    JsonLineageStore,
    LineageService,
    NotFoundError,
    StaleBaselineError,
    ValidationError,
)


class DraftProposalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "lineage.json"
        self.service = LineageService(JsonLineageStore(self.path))
        self.service.create_object("orders.v1", "orders", "v1")
        self.service.create_object("customers.v1", "customers", "v1")
        self.service.create_object("enriched.v1", "enriched", "v1")
        for object_id, name in [
            ("orders.v1", "region"),
            ("orders.v1", "amount"),
            ("customers.v1", "tier"),
            ("enriched.v1", "segment"),
            ("enriched.v1", "total"),
        ]:
            self.service.create_field(f"{object_id}.{name}", object_id, name)
        self.service.submit_transform(
            "t.segment.v1",
            "build_segment",
            "1.0.0",
            [("orders.v1.region", "enriched.v1.segment", "upper(region)")],
        )

    def reload(self):
        self.service = LineageService(JsonLineageStore(self.path))

    def test_draft_isolated_from_queries(self):
        self.service.create_draft("d1", "merge tier into segment")
        self.service.stage_transform(
            "d1",
            "t.segment.v2",
            "build_segment",
            "2.0.0",
            [("customers.v1.tier", "enriched.v1.segment", "coalesce(tier)")],
        )
        result = self.service.upstream("enriched.v1.segment")
        self.assertEqual(set(result.direct), {"orders.v1.region"})
        self.assertEqual(self.service.impact("customers.v1.tier").direct, {})

    def test_diff_reports_added_removed_lost_and_cycles(self):
        self.service.create_draft("d1", "replace region with tier")
        self.service.stage_transform(
            "d1",
            "t.segment.v1",
            "build_segment",
            "1.1.0",
            [("customers.v1.tier", "enriched.v1.segment", "tier")],
        )
        diff = self.service.draft_diff("d1")
        self.assertFalse(diff.stale)
        self.assertEqual(
            diff.added_dependencies,
            [{"transform_id": "t.segment.v1",
              "source_field_id": "customers.v1.tier",
              "target_field_id": "enriched.v1.segment"}],
        )
        self.assertEqual(
            diff.removed_dependencies,
            [{"transform_id": "t.segment.v1",
              "source_field_id": "orders.v1.region",
              "target_field_id": "enriched.v1.segment"}],
        )
        self.assertEqual(diff.lost_sources, {"enriched.v1.segment": ["orders.v1.region"]})
        self.assertEqual(diff.new_cycles, [])

    def test_diff_reports_new_cycle(self):
        self.service.create_draft("d1", "feedback loop")
        self.service.stage_transform(
            "d1",
            "t.feedback.v1",
            "feedback",
            "1.0.0",
            [("enriched.v1.segment", "orders.v1.region", "segment")],
        )
        diff = self.service.draft_diff("d1")
        self.assertEqual(len(diff.new_cycles), 1)
        self.assertIn("orders.v1.region", diff.new_cycles[0])

    def test_publish_applies_and_records_history(self):
        self.service.create_draft("d1", "add tier source")
        self.service.stage_transform(
            "d1",
            "t.segment.v2",
            "build_segment",
            "2.0.0",
            [("customers.v1.tier", "enriched.v1.segment", "tier")],
        )
        entry = self.service.publish_draft("d1", note="merge tier")
        self.assertEqual(entry.revision, 1)
        self.assertEqual(entry.action, "publish")
        self.assertIn("orders.v1.region", self.service.upstream("enriched.v1.segment").direct)
        self.assertIn("customers.v1.tier", self.service.upstream("enriched.v1.segment").direct)
        self.assertTrue(entry.snapshot["contributions"])
        self.assertEqual(self.service.store.drafts["d1"].status, "published")
        self.reload()
        self.assertEqual(len(self.service.history()), 1)
        self.assertEqual(self.service.store.revision, 1)

    def test_stale_baseline_requires_reconfirm(self):
        self.service.create_draft("d1", "first")
        self.service.stage_transform(
            "d1", "t.a", "a", "1", [("orders.v1.amount", "enriched.v1.total", "amount")]
        )
        self.service.create_draft("d2", "second")
        self.service.stage_transform(
            "d2", "t.b", "b", "1", [("customers.v1.tier", "enriched.v1.segment", "tier")]
        )
        self.service.publish_draft("d1")
        with self.assertRaises(StaleBaselineError) as ctx:
            self.service.publish_draft("d2")
        self.assertEqual(ctx.exception.base_revision, 0)
        self.assertEqual(ctx.exception.current_revision, 1)
        # d1's成果未被覆盖
        self.assertIn("t.a", self.service.store.transforms)
        self.assertNotIn("t.b", self.service.store.transforms)
        # 用户重新确认后发布
        diff = self.service.draft_diff("d2")
        self.assertTrue(diff.stale)
        self.service.publish_draft("d2", confirm_stale=True)
        self.assertIn("t.a", self.service.store.transforms)
        self.assertIn("t.b", self.service.store.transforms)

    def test_publish_failure_rolls_back(self):
        self.service.create_draft("d1", "bad batch")
        self.service.stage_transform(
            "d1", "t.a", "a", "1", [("orders.v1.amount", "enriched.v1.total", "amount")]
        )
        store = self.service.store
        original_save = store.save

        def failing_save():
            raise OSError("disk full")

        store.save = failing_save
        with self.assertRaises(OSError):
            self.service.publish_draft("d1")
        store.save = original_save
        self.assertNotIn("t.a", store.transforms)
        self.assertEqual(store.revision, 0)
        self.assertEqual(store.history, [])
        self.assertEqual(store.drafts["d1"].status, "open")
        self.reload()
        self.assertNotIn("t.a", self.service.store.transforms)

    def test_stage_invalid_mapping_leaves_draft_unchanged(self):
        self.service.create_draft("d1", "x")
        with self.assertRaises(NotFoundError):
            self.service.stage_transform(
                "d1", "t.a", "a", "1", [("missing.field", "enriched.v1.total", "x")]
            )
        self.assertEqual(self.service.store.drafts["d1"].transforms, [])

    def test_withdraw_publish_restores_graph_and_keeps_history(self):
        self.service.create_draft("d1", "swap source")
        self.service.stage_transform(
            "d1", "t.segment.v1", "build_segment", "1.1.0",
            [("customers.v1.tier", "enriched.v1.segment", "tier")],
        )
        entry = self.service.publish_draft("d1")
        self.assertEqual(
            set(self.service.upstream("enriched.v1.segment").direct),
            {"customers.v1.tier"},
        )
        withdraw = self.service.withdraw_publish(entry.id)
        self.assertEqual(withdraw.action, "withdraw")
        self.assertEqual(
            set(self.service.upstream("enriched.v1.segment").direct),
            {"orders.v1.region"},
        )
        history = self.service.history()
        self.assertEqual([item.action for item in history], ["publish", "withdraw"])
        self.assertTrue(all(item.snapshot["contributions"] for item in history))
        self.reload()
        self.assertEqual(len(self.service.history()), 2)
        with self.assertRaises(ValidationError):
            self.service.withdraw_publish(entry.id)

    def test_discard_draft(self):
        self.service.create_draft("d1", "x")
        self.service.discard_draft("d1")
        with self.assertRaises(ValidationError):
            self.service.stage_transform(
                "d1", "t.a", "a", "1", [("orders.v1.amount", "enriched.v1.total", "a")]
            )

    def test_merge_keeps_different_sources_and_requires_same_mapping_choice(self):
        self.service.create_draft("a", "add tier")
        self.service.stage_transform(
            "a", "t.segment.v2", "build_segment", "2",
            [("customers.v1.tier", "enriched.v1.segment", "tier")],
        )
        self.service.create_draft("b", "change region expression")
        self.service.stage_transform(
            "b", "t.segment.v2", "build_segment", "2",
            [("orders.v1.region", "enriched.v1.segment", "lower(region)")],
        )

        preview = self.service.begin_merge("m1", "merged", "a", "b")
        self.assertFalse(preview.unresolved_conflicts)
        self.assertEqual(
            {
                (item["source_field_id"], item["target_field_id"])
                for item in preview.added_dependencies
            },
            {
                ("customers.v1.tier", "enriched.v1.segment"),
                ("orders.v1.region", "enriched.v1.segment"),
            },
        )
        self.service.publish_merge("m1")
        self.assertEqual(
            set(self.service.upstream("enriched.v1.segment").direct),
            {"orders.v1.region", "customers.v1.tier"},
        )
        self.assertEqual(self.service.store.drafts["a"].status, "open")
        self.assertEqual(self.service.store.drafts["b"].status, "open")

    def test_merge_reports_delete_reference_conflict_and_retains_decision_on_preview(self):
        self.service.submit_transform(
            "t.total.v1", "build_total", "1",
            [("orders.v1.amount", "enriched.v1.total", "amount")],
        )
        self.service.create_draft("delete", "remove amount")
        self.service.stage_field_deletion("delete", "orders.v1.amount")
        self.service.create_draft("use", "reuse amount")
        self.service.stage_transform(
            "use", "t.total.v2", "build_total", "2",
            [("orders.v1.amount", "enriched.v1.total", "amount * 2")],
        )

        preview = self.service.begin_merge("m1", "merged", "delete", "use")
        conflict_id = "merge:m1:deleted-field:orders.v1.amount"
        self.assertEqual(preview.unresolved_conflicts, [conflict_id])
        self.assertEqual(
            self.service.store.drafts["merged"].transforms,
            [],
        )
        preview = self.service.resolve_merge_conflict(
            "m1", conflict_id, "keep_field"
        )
        self.assertEqual(preview.unresolved_conflicts, [])
        self.service.preview_merge("m1")
        self.reload()
        preview = self.service.preview_merge("m1")
        self.assertEqual(preview.unresolved_conflicts, [])
        self.service.publish_merge("m1")
        self.assertTrue(self.service.store.fields["orders.v1.amount"].active)
        self.assertIn("orders.v1.amount", self.service.upstream("enriched.v1.total").direct)

    def test_merge_same_mapping_conflict_does_not_enter_graph_until_selected(self):
        self.service.create_draft("a", "upper")
        self.service.stage_transform(
            "a", "t.segment.v1", "build_segment", "1",
            [("orders.v1.region", "enriched.v1.segment", "upper(region)")],
        )
        self.service.create_draft("b", "lower")
        self.service.stage_transform(
            "b", "t.segment.v1", "build_segment", "1",
            [("orders.v1.region", "enriched.v1.segment", "lower(region)")],
        )

        preview = self.service.begin_merge("m1", "merged", "a", "b")
        conflict_id = preview.unresolved_conflicts[0]
        with self.assertRaises(ValidationError):
            self.service.publish_merge("m1")
        self.service.resolve_merge_conflict("m1", conflict_id, "right")
        self.service.publish_merge("m1")
        edge = self.service.store.contributions[
            "contrib:t.segment.v1:orders.v1.region->enriched.v1.segment"
        ]
        self.assertEqual(edge.expression, "lower(region)")

    def test_merge_baseline_change_only_reconfirms_related_decision(self):
        self.service.create_draft("a", "segment conflict left")
        self.service.stage_transform(
            "a", "t.segment.v1", "build_segment", "1",
            [("orders.v1.region", "enriched.v1.segment", "upper(region)")],
        )
        self.service.create_draft("b", "segment conflict right")
        self.service.stage_transform(
            "b", "t.segment.v1", "build_segment", "1",
            [("orders.v1.region", "enriched.v1.segment", "lower(region)")],
        )
        self.service.begin_merge("m1", "merged", "a", "b")
        conflict_id = self.service.preview_merge("m1").unresolved_conflicts[0]
        self.service.resolve_merge_conflict("m1", conflict_id, "left")

        other = self.service.create_draft("other", "unrelated baseline move")
        self.service.stage_transform(
            other.id, "t.total.v1", "build_total", "1",
            [("customers.v1.tier", "enriched.v1.total", "tier")],
        )
        self.service.publish_draft(other.id)

        preview = self.service.preview_merge("m1")
        self.assertTrue(preview.stale)
        self.assertEqual(preview.reconfirm_conflicts, [])
        self.service.publish_merge("m1", confirm_stale=True)
        self.assertEqual(
            self.service.store.contributions[
                "contrib:t.segment.v1:orders.v1.region->enriched.v1.segment"
            ].expression,
            "upper(region)",
        )

    def test_abandon_merge_preserves_source_drafts(self):
        self.service.create_draft("a", "x")
        self.service.stage_transform(
            "a", "t.a", "a", "1",
            [("customers.v1.tier", "enriched.v1.segment", "tier")],
        )
        self.service.create_draft("b", "y")
        self.service.stage_transform(
            "b", "t.b", "b", "1",
            [("orders.v1.amount", "enriched.v1.total", "amount")],
        )
        self.service.begin_merge("m1", "merged", "a", "b")
        self.service.abandon_merge("m1")
        self.assertEqual(self.service.store.drafts["a"].status, "open")
        self.assertEqual(self.service.store.drafts["b"].status, "open")
        self.assertEqual(self.service.store.drafts["merged"].status, "discarded")
        self.assertEqual(self.service.store.history, [])


if __name__ == "__main__":
    unittest.main()
