import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lineage import (
    AlreadyExistsError,
    BaselineChangedError,
    JsonLineageStore,
    LineageService,
    NotFoundError,
    ValidationError,
)


class DraftFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LineageService(
            JsonLineageStore(Path(self.temp.name) / "lineage.json")
        )
        for object_id, name in [("a.v1", "a"), ("b.v1", "b"), ("c.v1", "c"), ("d.v1", "d")]:
            self.service.create_object(object_id, name, "v1")
        for field_id, object_id, name in [
            ("a.v1.x", "a.v1", "x"),
            ("b.v1.x", "b.v1", "x"),
            ("c.v1.x", "c.v1", "x"),
            ("d.v1.x", "d.v1", "x"),
        ]:
            self.service.create_field(field_id, object_id, name)
        self.service.submit_transform(
            "t.base", "base", "1", [("a.v1.x", "c.v1.x", "a")]
        )

    def proposal(self, mappings, transform_id="t.base", **kwargs):
        return {"transform_id": transform_id, "mappings": mappings, **kwargs}

    def test_draft_is_isolated_from_normal_queries(self):
        draft = self.service.create_draft(
            "draft-1",
            [self.proposal([("b.v1.x", "c.v1.x", "b"), ("a.v1.x", "c.v1.x", "a2")])],
        )
        self.assertEqual(draft.base_revision, 0)
        # trial edges never enter the effective graph
        self.assertEqual(set(self.service.upstream("c.v1.x").direct), {"a.v1.x"})
        self.assertEqual(
            next(iter(self.service.upstream("c.v1.x").edges.values())).expression, "a"
        )

        diff = self.service.draft_diff("draft-1").to_dict()
        self.assertFalse(diff["baseline_changed"])
        self.assertEqual(
            {(e["source_field_id"], e["target_field_id"])
             for e in diff["added_edges"]},
            {("b.v1.x", "c.v1.x")},
        )
        self.assertEqual(diff["removed_edges"], [])
        lost = {item["target_field_id"]: item for item in diff["lost_sources"]}
        self.assertEqual(lost, {})

    def test_diff_reports_lost_sources_and_new_dependencies(self):
        self.service.submit_transform(
            "t.cd", "cd", "1", [("c.v1.x", "d.v1.x", "c")]
        )
        self.service.create_draft(
            "draft-replace",
            [self.proposal([("b.v1.x", "c.v1.x", "b")])],
        )
        result = self.service.publish_draft("draft-replace")
        self.assertEqual(result["revision"], 1)
        # a -> c edge replaced by b -> c: a loses result c
        self.assertEqual(set(self.service.upstream("c.v1.x").direct), {"b.v1.x"})
        self.assertEqual(set(self.service.upstream("d.v1.x").direct), {"c.v1.x"})
        self.assertEqual(self.service.impact("a.v1.x").direct, {})

    def test_diff_reports_field_replace_with_orphan_source_loss(self):
        self.service.create_draft(
            "draft-swap", [self.proposal([("b.v1.x", "c.v1.x", "b")])]
        )
        diff = self.service.draft_diff("draft-swap").to_dict()
        self.assertEqual(
            {(e["source_field_id"], e["target_field_id"])
             for e in diff["removed_edges"]},
            {("a.v1.x", "c.v1.x")},
        )
        lost = diff["lost_sources"][0]
        self.assertEqual(lost["target_field_id"], "c.v1.x")
        self.assertEqual(lost["lost_source_field_ids"], ["a.v1.x"])
        self.assertEqual(lost["remaining_source_field_ids"], ["b.v1.x"])
        self.assertFalse(lost["orphaned"])

    def test_feedback_cycle_is_reported(self):
        self.service.create_draft(
            "draft-feedback",
            [self.proposal([
                ("a.v1.x", "c.v1.x", "a"),
                ("c.v1.x", "a.v1.x", "feedback"),
            ])],
        )
        diff = self.service.draft_diff("draft-feedback").to_dict()
        self.assertTrue(any(c["new"] for c in diff["cycles"]))
        cycle = next(c for c in diff["cycles"] if c["new"])
        self.assertEqual(cycle["path"][0], cycle["path"][-1])
        self.assertEqual(set(cycle["path"][:-1]), {"a.v1.x", "c.v1.x"})
        # still isolated
        self.assertTrue(self.service.upstream("a.v1.x").cycles == [])

    def test_stale_draft_is_rejected_until_rebased(self):
        self.service.create_draft(
            "draft-a", [self.proposal([("a.v1.x", "c.v1.x", "a-new")])]
        )
        # another draft based on the same old revision is published first
        self.service.create_draft(
            "draft-b", [self.proposal([("b.v1.x", "c.v1.x", "b")])]
        )
        self.service.publish_draft("draft-b")

        diff = self.service.draft_diff("draft-a").to_dict()
        self.assertTrue(diff["baseline_changed"])
        self.assertEqual(diff["base_revision"], 0)
        self.assertEqual(diff["current_revision"], 1)
        with self.assertRaises(BaselineChangedError) as raised:
            self.service.publish_draft("draft-a")
        self.assertEqual(raised.exception.base_revision, 0)
        self.assertEqual(raised.exception.current_revision, 1)
        with self.assertRaises(BaselineChangedError):
            self.service.publish_draft("draft-a")

        # user re-confirms against the new baseline; first draft's work remains
        self.service.rebase_draft("draft-a")
        refreshed = self.service.draft_diff("draft-a").to_dict()
        # the earlier full-replacement proposal would now drop revision 1's b
        # edge: that loss is surfaced for explicit re-confirmation, not silent
        self.assertEqual(
            {(e["source_field_id"], e["target_field_id"])
             for e in refreshed["removed_edges"]},
            {("b.v1.x", "c.v1.x")},
        )
        draft_a = self.service.store.drafts["draft-a"]
        draft_a.proposals[0].mappings = [
            ("a.v1.x", "c.v1.x", "a-new"),
            ("b.v1.x", "c.v1.x", "b"),
        ]
        result = self.service.publish_draft("draft-a")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(
            self.service.store.drafts["draft-b"].status, "published"
        )
        self.assertEqual(
            self.service.store.drafts["draft-b"].published_revision, 1
        )
        # rebased draft is applied on top of revision 1, so b edge is kept
        self.assertEqual(set(self.service.upstream("c.v1.x").direct),
                         {"a.v1.x", "b.v1.x"})
        self.assertEqual(
            self.service.upstream("c.v1.x").edges[
                "contrib:t.base:a.v1.x->c.v1.x"
            ].expression,
            "a-new",
        )

    def test_batch_errors_leave_no_draft_and_intact_graph(self):
        with self.assertRaises(NotFoundError):
            self.service.create_draft(
                "draft-bad",
                [self.proposal([
                    ("a.v1.x", "c.v1.x", "a"),
                    ("missing", "c.v1.x", "x"),
                ])],
            )
        self.assertNotIn("draft-bad", self.service.store.drafts)
        reloaded = LineageService(JsonLineageStore(self.service.store.path))
        self.assertEqual(set(reloaded.upstream("c.v1.x").direct), {"a.v1.x"})
        self.assertEqual(reloaded.list_drafts(), [])
        with self.assertRaises(AlreadyExistsError):
            self.service.create_draft(
                "draft-dup", [self.proposal([
                    ("a.v1.x", "c.v1.x", "a"),
                    ("a.v1.x", "c.v1.x", "a2"),
                ])],
            )

    def test_new_transform_proposal_requires_name_version(self):
        with self.assertRaises(NotFoundError):
            self.service.create_draft(
                "draft-new-x", [self.proposal([("b.v1.x", "c.v1.x", "b")],
                                              transform_id="t.brandnew")]
            )
        self.service.create_draft(
            "draft-new",
            [self.proposal([("b.v1.x", "d.v1.x", "b")],
                           transform_id="t.brandnew", name="new_one", version="9")],
        )
        self.service.publish_draft("draft-new")
        self.assertIn("t.brandnew", self.service.store.transforms)
        self.assertEqual(set(self.service.upstream("d.v1.x").direct), {"b.v1.x"})

    def test_save_failure_during_publish_keeps_old_graph_and_draft_open(self):
        self.service.create_draft(
            "draft-fail", [self.proposal([("b.v1.x", "c.v1.x", "b")])]
        )
        with mock.patch.object(self.service.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.publish_draft("draft-fail")
        draft = self.service.store.drafts["draft-fail"]
        self.assertEqual(draft.status, "open")
        self.assertIsNone(draft.published_revision)
        self.assertEqual(self.service.store.current_revision, 0)
        self.assertEqual(set(self.service.upstream("c.v1.x").direct), {"a.v1.x"})
        reloaded = LineageService(JsonLineageStore(self.service.store.path))
        self.assertEqual(set(reloaded.upstream("c.v1.x").direct), {"a.v1.x"})
        self.assertEqual(len(reloaded.list_revisions()), 1)

    def test_withdraw_restore_full_history_graph(self):
        self.service.create_draft(
            "draft-w", [self.proposal([("b.v1.x", "c.v1.x", "b")])]
        )
        self.service.publish_draft("draft-w")
        outcome = self.service.withdraw_revision(1)
        self.assertEqual(outcome["revision"], 2)
        self.assertEqual(set(self.service.upstream("c.v1.x").direct), {"a.v1.x"})
        revisions = self.service.list_revisions()
        kinds = [(r.revision, r.kind) for r in revisions]
        self.assertEqual(kinds, [(0, "baseline"), (1, "publish"), (2, "withdraw")])
        for record in revisions:
            self.assertIsNotNone(record.graph_after)
        self.assertEqual(
            self.service.store.drafts["draft-w"].status, "withdrawn"
        )
        # withdrawal cannot be withdrawn again
        with self.assertRaises(ValidationError):
            self.service.withdraw_revision(2)
        # revision 1 graph is still available for audit
        graph_at_1 = self.service.revision_graph(1)
        contrib_ids = {c["id"] for c in graph_at_1["contributions"] if c["active"]}
        self.assertIn("contrib:t.base:b.v1.x->c.v1.x", contrib_ids)
        # history survives persistence
        reloaded = LineageService(JsonLineageStore(self.service.store.path))
        self.assertEqual(len(reloaded.list_revisions()), 3)
        self.assertEqual(set(reloaded.upstream("c.v1.x").direct), {"a.v1.x"})

    def test_two_independent_drafts_do_not_interfere(self):
        self.service.create_draft(
            "draft-x", [self.proposal([("a.v1.x", "c.v1.x", "ax")])]
        )
        self.service.create_draft(
            "draft-y", [self.proposal([("b.v1.x", "c.v1.x", "by")])]
        )
        diff_x = self.service.draft_diff("draft-x").to_dict()
        diff_y = self.service.draft_diff("draft-y").to_dict()
        self.assertEqual(diff_x["added_edges"], [])
        self.assertEqual(
            {(e["source_field_id"], e["target_field_id"]) for e in diff_y["added_edges"]},
            {("b.v1.x", "c.v1.x")},
        )
        self.assertEqual(set(self.service.upstream("c.v1.x").direct), {"a.v1.x"})


if __name__ == "__main__":
    unittest.main()
