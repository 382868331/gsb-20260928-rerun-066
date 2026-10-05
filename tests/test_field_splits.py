import tempfile
import unittest
from pathlib import Path

from lineage import JsonLineageStore, LineageService, ValidationError


class FieldSplitMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "lineage.json"
        self.service = LineageService(JsonLineageStore(self.path))
        for object_id, name in [("raw.v1", "raw"), ("mid.v1", "mid"), ("out.v1", "out")]:
            self.service.create_object(object_id, name, "v1")
        for object_id, field_name in [
            ("raw.v1", "contact"),
            ("raw.v1", "email"),
            ("raw.v1", "phone"),
            ("mid.v1", "profile"),
            ("mid.v1", "profile_email"),
            ("mid.v1", "profile_billing"),
            ("out.v1", "customer"),
            ("out.v1", "billing"),
            ("out.v1", "customer_email"),
        ]:
            self.service.create_field(f"{object_id}.{field_name}", object_id, field_name)
        self.service.submit_transform(
            "t.profile",
            "build_profile",
            "1",
            [("raw.v1.contact", "mid.v1.profile", "parse(contact)")],
        )
        self.service.submit_transform(
            "t.customer",
            "build_customer",
            "1",
            [("mid.v1.profile", "out.v1.customer", "profile")],
        )
        self.service.submit_transform(
            "t.billing",
            "build_billing",
            "1",
            [("mid.v1.profile", "out.v1.billing", "profile.billing")],
        )

    def reload(self):
        self.service = LineageService(JsonLineageStore(self.path))

    def test_withdraw_one_split_target_keeps_other_target_from_same_transform(self):
        self.service.create_field("out.v1.shipping", "out.v1", "shipping")
        self.service.submit_transform(
            "t.order",
            "build_order",
            "1",
            [
                ("raw.v1.contact", "out.v1.shipping", "contact.shipping"),
                ("raw.v1.contact", "out.v1.billing", "contact.billing"),
            ],
        )
        self.service.create_field_split(
            "split.contact", "raw.v1.contact", ["raw.v1.email"]
        )
        self.service.record_split_decision(
            "split.contact",
            "t.order",
            "out.v1.shipping",
            "raw.v1.email",
            rationale="shipping semantics explicitly use email",
        )
        batch = self.service.create_split_migration_batch(
            "shipping-only", "split.contact", ["out.v1.shipping"]
        )

        entry = self.service.publish_split_migration_batch("shipping-only")
        old_shipping = "contrib:t.order:raw.v1.contact->out.v1.shipping"
        new_shipping = "contrib:t.order:raw.v1.email->out.v1.shipping"
        old_billing = "contrib:t.order:raw.v1.contact->out.v1.billing"
        self.assertEqual(set(entry.activated), {new_shipping})
        self.assertEqual(set(entry.deactivated), {old_shipping})
        self.assertNotIn(old_billing, entry.activated)
        self.assertNotIn(old_billing, entry.deactivated)
        self.assertTrue(self.service.store.contributions[old_billing].active)

        withdrawal = self.service.withdraw_publish(entry.id)
        self.assertEqual(set(withdrawal.activated), {old_shipping})
        self.assertEqual(set(withdrawal.deactivated), {new_shipping})
        self.assertTrue(self.service.store.contributions[old_shipping].active)
        self.assertFalse(self.service.store.contributions[new_shipping].active)
        self.assertTrue(self.service.store.contributions[old_billing].active)
        self.assertEqual(
            self.service.store.contributions[old_shipping].expression,
            "contact.shipping",
        )
        self.assertEqual(
            self.service.store.contributions[old_billing].expression,
            "contact.billing",
        )
        self.reload()
        stored_batch = self.service.migration_batch("shipping-only")
        self.assertEqual(stored_batch.status, "withdrawn")
        self.assertEqual(stored_batch.history_id, entry.id)
        self.assertEqual(stored_batch.published_at, entry.created_at)
        self.assertEqual(
            stored_batch.adopted_decisions[0]["new_field_id"], "raw.v1.email"
        )
        self.assertTrue(self.service.store.contributions[old_billing].active)
        self.assertTrue(self.service.store.contributions[old_shipping].active)
        self.assertFalse(self.service.store.contributions[new_shipping].active)

    def test_phased_split_keeps_shared_old_interpretation_and_preserves_withdraw_audit(self):
        split = self.service.create_field_split(
            "split.contact",
            "raw.v1.contact",
            ["raw.v1.email", "raw.v1.phone"],
        )

        report = self.service.split_report("split.contact")
        self.assertEqual(
            [(item["transform_id"], item["target_field_id"]) for item in report["old_dependencies"]],
            [("t.profile", "mid.v1.profile")],
        )
        self.service.create_field_split(
            "split.nearby",
            "raw.v1.email",
            ["raw.v1.phone"],
        )
        nearby = self.service.split_report("split.nearby")
        self.assertEqual(nearby["old_dependencies"], [])
        self.assertEqual(nearby["adoptable_dependencies"], [])

        self.service.record_manual_split_dependency(
            "split.contact",
            "t.profile",
            "mid.v1.profile",
            "billing may mean phone but customer may mean email",
        )
        report = self.service.split_report("split.contact")
        self.assertEqual(report["remaining_old_dependency_count"], 1)
        self.assertEqual(report["manual_dependencies"][0]["impact_chain"][:3],
                         ["mid.v1.profile", "out.v1.billing", "out.v1.customer"])  # sorted chain
        with self.assertRaises(ValidationError):
            self.service.create_split_retirement_draft("split.contact")

        # A batch may migrate one downstream by adding a separate path; the old
        # shared intermediate stays available to the unmigrated downstream.
        self.service.record_split_decision(
            "split.contact",
            "t.profile",
            "mid.v1.profile",
            "raw.v1.email",
            new_target_field_id="mid.v1.profile_email",
            rationale="customer semantics explicitly use email",
        )
        plan = self.service.plan_split_migration(
            "split.contact",
            ["mid.v1.profile", "mid.v1.profile_email", "out.v1.customer_email"],
        )
        self.assertEqual(plan["unresolved"], [])
        self.assertEqual(plan["conflicts"], [])

        batch = self.service.create_split_migration_batch(
            "b1",
            "split.contact",
            [
                "mid.v1.profile",
                "mid.v1.profile_email",
                "out.v1.customer_email",
            ],
            additional_paths=[
                {
                    "transform_id": "t.customer",
                    "source_field_id": "mid.v1.profile_email",
                    "target_field_id": "out.v1.customer_email",
                    "expression": "email",
                }
            ],
            note="customer email batch",
        )
        self.assertEqual(
            batch.source_comparison["before"]["out.v1.customer_email"], [],
        )
        self.assertEqual(
            batch.source_comparison["after"]["out.v1.customer_email"],
            ["mid.v1.profile_email"],
        )

        entry = self.service.publish_split_migration_batch("b1", note="release b1")
        self.assertEqual(self.service.migration_batch("b1").status, "published")
        self.assertEqual(
            set(self.service.upstream("mid.v1.profile").direct), {"raw.v1.contact"},
        )
        self.assertEqual(
            set(self.service.upstream("out.v1.customer_email").direct),
            {"mid.v1.profile_email"},
        )
        self.assertEqual(
            set(self.service.upstream("out.v1.billing").direct),
            {"mid.v1.profile"},
        )
        self.reload()
        self.assertEqual(self.service.migration_batch("b1").published_at, entry.created_at)
        self.assertEqual(
            self.service.migration_batch("b1").adopted_decisions[0]["new_field_id"],
            "raw.v1.email",
        )

        # The second batch depends on the first and must be rechecked after
        # withdrawal; the adopted interpretation and publish time remain stored.
        second = self.service.create_split_migration_batch(
            "b2",
            "split.contact",
            ["mid.v1.profile", "mid.v1.profile_billing", "out.v1.billing"],
            additional_paths=[
                {
                    "transform_id": "t.profile",
                    "source_field_id": "raw.v1.email",
                    "target_field_id": "mid.v1.profile_billing",
                    "expression": "parse(email)",
                },
                {
                    "transform_id": "t.billing",
                    "source_field_id": "mid.v1.profile_billing",
                    "target_field_id": "out.v1.billing",
                    "expression": "email_billing",
                }
            ],
        )
        second_entry = self.service.publish_split_migration_batch(
            "b2", confirm_stale=True
        )
        withdrawal = self.service.withdraw_publish(entry.id)
        self.assertEqual(self.service.migration_batch("b1").status, "withdrawn")
        self.assertTrue(self.service.migration_batch("b2").requires_recheck)
        self.assertEqual(self.service.migration_batch("b2").recheck_reasons,
                         ["upstream batch 'b1' was withdrawn"])
        self.assertEqual(self.service.migration_batch("b2").status, "published")
        self.assertEqual(self.service.migration_batch("b2").history_id, second_entry.id)
        self.assertEqual(
            self.service.migration_batch("b2").published_at, second_entry.created_at
        )
        self.assertEqual(
            self.service.migration_batch("b2").adopted_decisions[0]["rationale"],
            "customer semantics explicitly use email",
        )
        self.assertTrue(
            self.service.store.contributions[
                "contrib:t.profile:raw.v1.email->mid.v1.profile_billing"
            ].active
        )
        self.assertTrue(
            self.service.store.contributions[
                "contrib:t.billing:mid.v1.profile_billing->out.v1.billing"
            ].active
        )
        self.assertEqual(
            self.service.store.contributions[
                "contrib:t.profile:raw.v1.email->mid.v1.profile_billing"
            ].expression,
            "parse(email)",
        )
        self.assertEqual(withdrawal.created_at is not None, True)
        self.assertEqual(
            set(self.service.upstream("mid.v1.profile").direct),
            {"raw.v1.contact"},
        )

    def test_new_cycle_blocks_migration_publish_and_rolls_back(self):
        self.service.create_field_split(
            "split.contact", "raw.v1.contact", ["raw.v1.email", "raw.v1.phone"]
        )
        self.service.record_split_decision(
            "split.contact", "t.profile", "mid.v1.profile", "raw.v1.email"
        )
        with self.assertRaises(ValidationError):
            self.service.create_split_migration_batch(
                "b1",
                "split.contact",
                ["mid.v1.profile"],
                additional_paths=[
                    {
                        "transform_id": "t.customer",
                        "source_field_id": "out.v1.customer",
                        "target_field_id": "raw.v1.email",
                        "expression": "feedback",
                    }
                ],
            )
        self.assertEqual(self.service.store.revision, 0)
        self.assertEqual(self.service.store.history, [])
        self.assertEqual(
            set(self.service.upstream("mid.v1.profile").direct),
            {"raw.v1.contact"},
        )

    def test_split_never_guesses_and_retirement_requires_every_real_dependency(self):
        self.service.create_field_split(
            "split.contact", "raw.v1.contact", ["raw.v1.email", "raw.v1.phone"]
        )
        report = self.service.split_report("split.contact")
        self.assertEqual(report["adoptable_dependencies"], [])
        self.assertEqual(report["manual_dependencies"], [])
        with self.assertRaises(ValidationError):
            self.service.create_split_migration_batch(
                "b1", "split.contact", ["mid.v1.profile"]
            )
        self.service.record_manual_split_dependency(
            "split.contact",
            "t.profile",
            "mid.v1.profile",
            "email versus phone is not evident from the name",
        )
        with self.assertRaises(ValidationError):
            self.service.create_split_migration_batch(
                "b2", "split.contact", ["mid.v1.profile"]
            )
        with self.assertRaises(ValidationError):
            self.service.create_split_retirement_draft("split.contact")

    def test_shared_intermediate_conflict_is_visible_before_batch_creation(self):
        self.service.create_field_split(
            "split.contact", "raw.v1.contact", ["raw.v1.email"]
        )
        self.service.record_split_decision(
            "split.contact", "t.profile", "mid.v1.profile", "raw.v1.email"
        )
        plan = self.service.plan_split_migration(
            "split.contact", ["mid.v1.profile", "out.v1.customer"]
        )
        self.assertEqual(
            plan["conflicts"][0]["type"], "shared_intermediate"
        )
        self.assertEqual(
            plan["conflicts"][0]["affected_chain"],
            ["mid.v1.profile", "out.v1.billing"],
        )
        with self.assertRaises(ValidationError):
            self.service.create_split_migration_batch(
                "b1",
                "split.contact",
                ["mid.v1.profile", "out.v1.customer"],
            )


if __name__ == "__main__":
    unittest.main()
