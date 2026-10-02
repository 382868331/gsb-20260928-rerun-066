import tempfile
import unittest
from pathlib import Path

from lineage import (
    JsonLineageStore,
    LineageService,
    StaleBaselineError,
    ValidationError,
)


class MergeDraftTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "lineage.json"
        self.service = LineageService(JsonLineageStore(self.path))
        for object_id, name in [("o.v1", "orders"), ("c.v1", "customers"), ("e.v1", "enriched")]:
            self.service.create_object(object_id, name, "v1")
        for object_id, field_name in [
            ("o.v1", "region"),
            ("o.v1", "amount"),
            ("c.v1", "tier"),
            ("e.v1", "segment"),
        ]:
            self.service.create_field(f"{object_id}.{field_name}", object_id, field_name)
        self.service.submit_transform(
            "t.seg.v1",
            "build_segment",
            "1.0.0",
            [("o.v1.region", "e.v1.segment", "upper(region)")],
        )

    def reload(self):
        self.service = LineageService(JsonLineageStore(self.path))

    def test_distinct_sources_merge_without_conflict(self):
        self.service.create_draft("a", "tier source")
        self.service.stage_transform(
            "a", "t.tier", "tier_t", "1",
            [("c.v1.tier", "e.v1.segment", "tier")],
        )
        self.service.create_draft("b", "amount source")
        self.service.stage_transform(
            "b", "t.amount", "amount_t", "1",
            [("o.v1.amount", "e.v1.segment", "amount")],
        )
        session = self.service.start_merge("m", "a", "b")
        self.assertEqual(session.conflicts, [])
        preview = self.service.merge_preview("m")
        origins = {(d["transform_id"]): d["origin"] for d in preview.added_dependencies}
        self.assertEqual(origins, {"t.tier": "a", "t.amount": "b"})
        merged = self.service.complete_merge("m", "m_draft", name="merged")
        self.assertEqual(merged.sources, ["a", "b"])
        self.service.publish_draft("m_draft")
        self.assertEqual(
            set(self.service.upstream("e.v1.segment").direct),
            {"o.v1.region", "c.v1.tier", "o.v1.amount"},
        )

    def test_same_mapping_replaced_requires_choice_and_previews_keep_it(self):
        self.service.create_draft("a", "left expr")
        self.service.stage_transform(
            "a", "t.seg.v1", "build_segment", "2",
            [("o.v1.region", "e.v1.segment", "left_expr")],
        )
        self.service.create_draft("b", "right expr")
        self.service.stage_transform(
            "b", "t.seg.v1", "build_segment", "2",
            [("o.v1.region", "e.v1.segment", "right_expr")],
        )
        session = self.service.start_merge("m", "a", "b")
        self.assertEqual(len(session.conflicts), 1)
        conflict = session.conflicts[0]
        self.assertEqual(conflict.kind, "mapping_replaced")
        self.assertEqual(set(conflict.options), {"left", "right"})
        # Unresolved edge is absent from the simulated graph.
        preview = self.service.merge_preview("m")
        self.assertEqual(len(preview.unresolved_conflicts), 1)
        self.assertEqual(preview.source_less_results, ["e.v1.segment"])
        with self.assertRaises(ValidationError):
            self.service.complete_merge("m", "m_draft")
        self.service.resolve_merge_conflict("m", conflict.id, "left")
        preview = self.service.merge_preview("m")
        self.assertEqual(preview.unresolved_conflicts, [])
        # Resolution survives reload and repeated previews.
        self.reload()
        preview = self.service.merge_preview("m")
        self.assertEqual(preview.resolved_conflict_ids, [conflict.id])
        merged = self.service.complete_merge("m", "m_draft", name="merged")
        self.assertEqual(
            merged.transforms[0].mappings[0]["expression"], "left_expr"
        )

    def test_drop_vs_keep_conflict_offers_baseline_option(self):
        self.service.create_draft("a", "drop region")
        self.service.stage_transform(
            "a", "t.seg.v1", "build_segment", "2",
            [("c.v1.tier", "e.v1.segment", "tier")],
        )
        self.service.create_draft("b", "keep region")
        self.service.stage_transform(
            "b", "t.seg.v1", "build_segment", "2",
            [("o.v1.region", "e.v1.segment", "upper(region)")],
        )
        session = self.service.start_merge("m", "a", "b")
        conflict = session.conflicts[0]
        self.assertEqual(conflict.kind, "mapping_dropped")
        self.assertIn("keep_baseline", conflict.options)
        self.service.resolve_merge_conflict("m", conflict.id, "right")
        preview = self.service.merge_preview("m")
        added = {(d["source_field_id"]) for d in preview.added_dependencies}
        self.assertEqual(added, {"c.v1.tier"})
        self.assertEqual(preview.removed_dependencies, [])
        merged = self.service.complete_merge("m", "m_draft", name="merged")
        self.service.publish_draft("m_draft")
        self.assertEqual(
            set(self.service.upstream("e.v1.segment").direct),
            {"o.v1.region", "c.v1.tier"},
        )

    def test_field_removed_vs_referenced_conflict_with_impact_chain(self):
        self.service.create_draft("a", "delete region")
        self.service.remove_field_from_draft("a", "o.v1.region")
        self.service.create_draft("b", "use region")
        self.service.stage_transform(
            "b", "t.seg.v1", "build_segment", "2",
            [("o.v1.region", "e.v1.segment", "lower(region)")],
        )
        session = self.service.start_merge("m", "a", "b")
        conflict = session.conflicts[0]
        self.assertEqual(conflict.kind, "field_removed")
        self.assertEqual(
            conflict.impact_chain["downstream"]["direct"], ["e.v1.segment"]
        )
        # Choose removal: edge disappears with the field and result loses source.
        self.service.resolve_merge_conflict("m", conflict.id, "left")
        merged = self.service.complete_merge("m", "m_draft", name="merged")
        self.assertEqual(merged.removed_fields, ["o.v1.region"])
        self.service.publish_draft("m_draft")
        self.assertFalse(self.service.store.fields["o.v1.region"].active)
        # Withdraw restores the field from history.
        entry = next(item for item in self.service.history() if item.draft_id == "m_draft")
        self.service.withdraw_publish(entry.id)
        self.assertTrue(self.service.store.fields["o.v1.region"].active)

    def test_preview_reports_new_cycle_and_source_less_result(self):
        # Left introduces segment -> region, which together with the baseline
        # region -> segment forms a cycle.
        self.service.create_draft("a", "feedback")
        self.service.stage_transform(
            "a", "t.loop", "loop", "1",
            [("e.v1.segment", "o.v1.region", "segment")],
        )
        self.service.create_draft("b", "replace away region")
        self.service.stage_transform(
            "b", "t.seg.v1", "build_segment", "2",
            [("c.v1.tier", "e.v1.segment", "tier"),
             ("e.v1.segment", "o.v1.region", "segment")],
        )
        session = self.service.start_merge("m", "a", "b")
        preview = self.service.merge_preview("m")
        # Baseline region->segment is replaced, but the merged graph still has
        # tier->segment and segment->region; no closure, so force a closure:
        self.assertEqual(preview.new_cycles, [])
        # Add a draft that keeps the baseline edge plus the feedback edge.
        self.service.discard_draft("b")
        self.service.create_draft("b2", "tier keepers")
        self.service.stage_transform(
            "b2", "t.extra", "extra", "1",
            [("c.v1.tier", "e.v1.segment", "tier")],
        )
        self.service.abandon_merge("m")
        session = self.service.start_merge("m2", "a", "b2")
        preview = self.service.merge_preview("m2")
        self.assertEqual(len(preview.new_cycles), 1)
        self.assertIn("o.v1.region", preview.new_cycles[0])
        self.assertIn("t.loop", preview.changes_from_left)
        self.assertIn("t.extra", preview.changes_from_right)

    def test_baseline_move_flags_only_affected_decisions(self):
        self.service.create_draft("a", "delete region")
        self.service.remove_field_from_draft("a", "o.v1.region")
        self.service.create_draft("b", "tweak segment")
        self.service.stage_transform(
            "b", "t.seg.v1", "build_segment", "2",
            [("o.v1.region", "e.v1.segment", "lower(region)")],
        )
        session = self.service.start_merge("m", "a", "b")
        self.service.resolve_merge_conflict("m", session.conflicts[0].id, "right")
        # An unrelated publish advances the baseline.
        self.service.create_draft("other", "amount")
        self.service.stage_transform(
            "other", "t.other", "other", "1",
            [("o.v1.amount", "e.v1.segment", "amount")],
        )
        self.service.publish_draft("other")
        conflicts = self.service.merge_conflicts("m")
        self.assertTrue(all(c.needs_reconfirm for c in conflicts if c.baseline_dependent))
        preview = self.service.merge_preview("m")
        self.assertTrue(preview.baseline_moved)
        with self.assertRaises(StaleBaselineError):
            self.service.complete_merge(
                "m", "m_draft", confirm_stale=False
            )
        self.service.resolve_merge_conflict("m", conflicts[0].id, "right")
        merged = self.service.complete_merge(
            "m", "m_draft", name="merged", confirm_stale=True
        )
        self.assertEqual(merged.base_revision, 1)

    def test_abandon_keeps_drafts_review_evidence_and_history(self):
        self.service.create_draft("a", "x")
        self.service.stage_transform(
            "a", "t.a", "a", "1",
            [("c.v1.tier", "e.v1.segment", "tier")],
        )
        self.service.create_draft("b", "y")
        self.service.stage_transform(
            "b", "t.b", "b", "1",
            [("o.v1.amount", "e.v1.segment", "amount")],
        )
        self.service.start_merge("m", "a", "b")
        session = self.service.abandon_merge("m")
        self.assertEqual(session.status, "abandoned")
        self.assertEqual(self.service.store.drafts["a"].status, "open")
        self.assertEqual(self.service.store.drafts["b"].status, "open")
        self.assertEqual(self.service.history(), [])
        with self.assertRaises(ValidationError):
            self.service.merge_preview("m")


if __name__ == "__main__":
    unittest.main()
