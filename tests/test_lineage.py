from pathlib import Path
import tempfile
import unittest

from lineage import (
    DataObject,
    FieldRef,
    LineageStore,
    NotFoundError,
    Relation,
    StateConflictError,
    Transformation,
    analyze_impact,
    branch_convergence,
    compare_version_fields,
    trace_lineage,
)


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.store = LineageStore()
        for obj in [
            DataObject(id="orders", name="orders", version="v1", fields=["id", "amount"]),
            DataObject(id="users", name="users", version="v1", fields=["id", "country"]),
            DataObject(
                id="report_v1",
                name="report",
                version="v1",
                fields=["customer_amount", "bucket"],
            ),
            DataObject(
                id="report_v2",
                name="report",
                version="v2",
                fields=["customer_amount", "bucket"],
            ),
            DataObject(
                id="feature_branch_report",
                name="report",
                version="v1",
                branch="feature-a",
                fields=["customer_amount"],
            ),
        ]:
            self.store.add_object(obj)

        self.join = Transformation(
            id="join_orders_users",
            name="join_orders_users",
            version="1",
            code_ref="jobs/report.py:10",
        )
        self.bucket = Transformation(
            id="bucketize",
            name="bucketize",
            version="1",
            code_ref="jobs/report.py:22",
        )
        self.cyclic = Transformation(id="cyclic", name="cyclic", version="1")
        self.store.add_transformation(self.join)
        self.store.add_transformation(self.bucket)
        self.store.add_transformation(self.cyclic)

    def relation(
        self,
        source: FieldRef,
        target: FieldRef,
        transformation_id="join_orders_users",
        expression="",
    ):
        return Relation(
            source=source,
            target=target,
            transformation_id=transformation_id,
            expression=expression,
        )

    def test_multiple_sources_are_all_retained_with_evidence(self):
        amount = self.store.field("orders", "amount")
        country = self.store.field("users", "country")
        result = self.store.field("report_v1", "customer_amount")

        self.store.add_relations(
            [
                self.relation(amount, result, expression="orders.amount"),
                self.relation(country, result, expression="users.country"),
            ]
        )

        lineage = trace_lineage(self.store, result)
        self.assertEqual([item.field for item in lineage.direct], [amount, country])
        expressions = {
            dependency.evidence[0].expression for dependency in lineage.direct
        }
        self.assertEqual(expressions, {"orders.amount", "users.country"})
        self.assertEqual(lineage.direct[0].evidence[0].transformation_id, "join_orders_users")

    def test_deleting_transformation_only_withdraws_its_edges(self):
        amount = self.store.field("orders", "amount")
        country = self.store.field("users", "country")
        result = self.store.field("report_v1", "customer_amount")
        other = self.store.field("report_v2", "customer_amount")

        self.store.add_relations(
            [
                self.relation(amount, result),
                self.relation(country, result),
                self.relation(amount, other, transformation_id="bucketize"),
            ]
        )

        removed = self.store.delete_transformation("join_orders_users")

        self.assertEqual(len(removed), 2)
        self.assertTrue(self.store.transformations["join_orders_users"].is_deleted)
        self.assertEqual(trace_lineage(self.store, result).direct, [])
        self.assertEqual(
            [dependency.field for dependency in trace_lineage(self.store, other).direct],
            [amount],
        )

    def test_impact_separates_direct_and_transitive_dependencies(self):
        amount = self.store.field("orders", "amount")
        result = self.store.field("report_v1", "customer_amount")
        bucket = self.store.field("report_v1", "bucket")
        published = self.store.field("report_v2", "bucket")

        self.store.add_relations(
            [
                self.relation(amount, result),
                self.relation(result, bucket, "bucketize"),
                self.relation(bucket, published, "bucketize"),
            ]
        )

        impact = analyze_impact(self.store, amount)
        self.assertEqual([dependency.field for dependency in impact.direct], [result])
        self.assertEqual(
            [dependency.field for dependency in impact.transitive], [bucket, published]
        )
        self.assertEqual(
            [edge.transformation_id for edge in impact.transitive[1].evidence],
            ["join_orders_users", "bucketize", "bucketize"],
        )

    def test_cycles_terminate_and_do_not_duplicate_fields(self):
        first = self.store.field("report_v1", "customer_amount")
        second = self.store.field("report_v1", "bucket")

        self.store.add_relations(
            [
                self.relation(first, second, "cyclic"),
                self.relation(second, first, "cyclic"),
                self.relation(first, first, "cyclic"),
            ]
        )

        impact = analyze_impact(self.store, first)
        self.assertEqual([dependency.field for dependency in impact.direct], [second])
        self.assertEqual(impact.transitive, [])

    def test_branch_convergence_groups_sources_and_version_fields_remain_distinct(self):
        main_field = self.store.find_field(
            name="report", field_name="customer_amount", version="v1", branch="main"
        )
        branch_field = self.store.find_field(
            name="report",
            field_name="customer_amount",
            version="v1",
            branch="feature-a",
        )
        target = self.store.field("report_v2", "customer_amount")

        self.store.add_relations(
            [
                self.relation(main_field, target, expression="main merge input"),
                self.relation(branch_field, target, expression="feature merge input"),
            ]
        )

        convergence = branch_convergence(self.store, target)
        self.assertTrue(convergence.is_converged)
        self.assertEqual(set(convergence.branches), {"main", "feature-a"})
        self.assertEqual(
            convergence.branches["main"][0].evidence.transformation_id,
            "join_orders_users",
        )

        versions = compare_version_fields(
            self.store,
            object_name="report",
            field_name="customer_amount",
            coordinates=[("v1", "main"), ("v2", "main"), ("v1", "feature-a")],
        )
        self.assertEqual(
            [item.ref for item in versions], [main_field, target, branch_field]
        )
        self.assertNotEqual(main_field, target)

    def test_relations_must_reference_existing_fields_and_active_transformation(self):
        with self.assertRaises(NotFoundError):
            self.store.add_relation(
                self.relation(
                    self.store.field("orders", "amount"),
                    FieldRef("missing", "x"),
                )
            )

        self.store.delete_transformation("join_orders_users")
        with self.assertRaises(StateConflictError):
            self.store.add_relation(
                self.relation(
                    self.store.field("orders", "amount"),
                    self.store.field("report_v1", "bucket"),
                )
            )

    def test_json_persistence_round_trip(self):
        amount = self.store.field("orders", "amount")
        result = self.store.field("report_v1", "customer_amount")
        self.store.add_relations([self.relation(amount, result)])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lineage.json"
            saved = LineageStore(path)
            saved.objects = self.store.objects
            saved.transformations = self.store.transformations
            saved.relations = self.store.relations
            saved.save()

            loaded = LineageStore(path)
            self.assertEqual(loaded.field("orders", "amount"), amount)
            impact = analyze_impact(loaded, amount)
            self.assertEqual([dependency.field for dependency in impact.direct], [result])
            self.assertEqual(
                impact.direct[0].evidence[0].transformation_id, "join_orders_users"
            )


if __name__ == "__main__":
    unittest.main()
