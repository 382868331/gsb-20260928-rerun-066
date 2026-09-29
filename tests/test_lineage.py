import tempfile
import unittest
from pathlib import Path

from lineage import AlreadyExistsError, JsonLineageStore, LineageService


class LineageServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        store = JsonLineageStore(Path(self.temp.name) / "lineage.json")
        self.service = LineageService(store)

    def object(self, object_id, name, version, branch=None):
        return self.service.create_object(
            object_id, name, version, branch=branch, exist_ok=True
        )

    def field(self, field_id, object_id, name):
        return self.service.create_field(
            field_id, object_id, name, exist_ok=True
        )

    def test_multi_source_and_direct_transitive_lineage(self):
        self.object("orders.v1", "orders", "v1", "main")
        self.object("customers.v2", "customers", "v2", "main")
        self.object("enriched.v3", "enriched_orders", "v3", "main")
        self.field("orders.v1.amount", "orders.v1", "amount")
        self.field("orders.v1.region", "orders.v1", "region")
        self.field("customers.v2.tier", "customers.v2", "tier")
        self.field("enriched.v3.amount", "enriched.v3", "amount")
        self.field("enriched.v3.segment", "enriched.v3", "segment")

        self.service.submit_transform(
            "t.join.v1",
            "join_customer",
            "1.0.0",
            [
                ("orders.v1.amount", "enriched.v3.amount", "amount"),
                ("orders.v1.region", "enriched.v3.segment", "region"),
                ("customers.v2.tier", "enriched.v3.segment", "tier"),
            ],
            code_ref="jobs/join.py@abc123",
        )

        sources = self.service.upstream("enriched.v3.segment")
        self.assertEqual(set(sources.direct), {"orders.v1.region", "customers.v2.tier"})
        self.assertEqual(sources.transitive, {})
        impact = self.service.impact("orders.v1.region")
        self.assertEqual(set(impact.direct), {"enriched.v3.segment"})
        evidence = next(iter(impact.edges.values()))
        self.assertEqual(evidence.transform_id, "t.join.v1")
        self.assertEqual(evidence.expression, "region")
        self.assertEqual(
            self.service.store.transforms[evidence.transform_id].code_ref,
            "jobs/join.py@abc123",
        )

    def test_delete_transform_withdraws_only_its_edges(self):
        self.object("a.v1", "a", "v1")
        self.object("b.v1", "b", "v1")
        self.object("c.v1", "c", "v1")
        self.field("a.v1.x", "a.v1", "x")
        self.field("b.v1.x", "b.v1", "x")
        self.field("c.v1.x", "c.v1", "x")
        self.service.submit_transform(
            "t.one", "one", "1", [("a.v1.x", "c.v1.x", "a")]
        )
        self.service.submit_transform(
            "t.two", "two", "1", [("b.v1.x", "c.v1.x", "b")]
        )

        self.service.delete_transform("t.one")
        sources = self.service.upstream("c.v1.x")
        self.assertEqual(set(sources.direct), {"b.v1.x"})
        self.assertFalse(self.service.store.contributions["contrib:t.one:a.v1.x->c.v1.x"].active)
        self.assertTrue(self.service.store.contributions["contrib:t.two:b.v1.x->c.v1.x"].active)

        self.service.restore_transform("t.one")
        restored = self.service.upstream("c.v1.x")
        self.assertEqual(set(restored.direct), {"a.v1.x", "b.v1.x"})

    def test_branch_merge_same_field_name_different_versions_and_cycle(self):
        self.object("raw.main", "raw", "v1", "main")
        self.object("raw.fix", "raw", "v2", "fix-amount")
        self.object("out.main", "output", "v3", "main")
        self.field("raw.main.amount", "raw.main", "amount")
        self.field("raw.fix.amount", "raw.fix", "amount")
        self.field("out.main.amount", "out.main", "amount")
        self.field("raw.main.repaired", "raw.main", "repaired")
        self.service.submit_transform(
            "t.merge",
            "merge_branches",
            "2",
            [
                ("raw.main.amount", "out.main.amount", "main_amount"),
                ("raw.fix.amount", "out.main.amount", "fixed_amount"),
                ("out.main.amount", "raw.main.repaired", "feedback"),
                ("raw.main.repaired", "out.main.amount", "repair_feedback"),
            ],
        )

        identity = self.service.field_identity_report("raw.main.amount")
        self.assertEqual(identity["branch"], "main")
        self.assertEqual(len(identity["same_name_other_versions"]), 2)
        self.assertEqual(
            {item["field_id"] for item in identity["same_name_other_versions"]},
            {"raw.fix.amount", "out.main.amount"},
        )

        sources = self.service.upstream("out.main.amount")
        self.assertEqual(
            set(sources.direct),
            {"raw.main.amount", "raw.fix.amount", "raw.main.repaired"},
        )
        self.assertEqual(sources.transitive, {})
        self.assertTrue(sources.cycles)

        impact = self.service.impact("raw.fix.amount")
        self.assertEqual(set(impact.direct), {"out.main.amount"})
        self.assertEqual(set(impact.transitive), {"raw.main.repaired"})
        self.assertTrue(impact.cycles)

    def test_transitive_distance_and_persistence(self):
        for index, name in enumerate(["a", "b", "c", "d"]):
            self.object(f"{name}.v1", name, "v1")
            self.field(f"{name}.v1.x", f"{name}.v1", "x")
        for left, right in [("a", "b"), ("b", "c"), ("c", "d")]:
            self.service.submit_transform(
                f"t.{left}.{right}",
                f"{left}_to_{right}",
                "1",
                [(f"{left}.v1.x", f"{right}.v1.x", f"{left}_x")],
            )

        reloaded = LineageService(JsonLineageStore(self.service.store.path))
        result = reloaded.impact("a.v1.x")
        self.assertEqual(set(result.direct), {"b.v1.x"})
        self.assertEqual(set(result.transitive), {"c.v1.x", "d.v1.x"})
        self.assertEqual(result.transitive["d.v1.x"].distance, 3)

    def test_duplicate_field_id_and_object_local_field_name_are_rejected(self):
        self.object("a.v1", "a", "v1")
        self.field("a.v1.x", "a.v1", "x")
        with self.assertRaises(AlreadyExistsError):
            self.service.create_field("a.v1.x", "a.v1", "x")
        with self.assertRaises(AlreadyExistsError):
            self.service.create_field("a.v1.x2", "a.v1", "x")


if __name__ == "__main__":
    unittest.main()
