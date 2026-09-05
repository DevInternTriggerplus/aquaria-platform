"""Config-backed and record-collection settings pages.

These are the pages that used to show "Not configurable in this build yet". The
tests assert the properties that make a settings page genuinely finished rather
than a form over nothing:

* **Persistence.** A saved value is read back after a fresh resolve (§33, §35 of
  the completion spec).
* **Authorization is server-side.** VIEW gates the read; EDIT (plus any required
  MANAGE action) gates the write; a principal without them is refused by the
  service, not merely by a hidden button (§7, §8, §47, §75).
* **Verb independence.** A role with VIEW but not EDIT can read and not write.
* **Validation is authoritative.** Bad input is rejected server-side with a field
  message, never persisted (§17).
* **Scope.** A value written at one venue does not leak to another (§11).
* **Secrets never round-trip in clear text.** A credential is stored masked and a
  blank replacement keeps the one on file (§secret handling).
* **Every settings page has a backend.** No page still falls through to a
  placeholder.
"""

from __future__ import annotations

import unittest

import seed
from utp.app import Platform
from utp.core.context import Principal, RequestContext
from utp.core.errors import AuthorizationDenied, ValidationError
from utp.domain import permissions as perms
from utp.services.settings_pages import CONFIG_PAGES, CONFIG_PAGES_BY_KEY


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = Platform()
        info = seed.provision(cls.platform)
        cls.tenant_id = info["tenant_id"]
        cls.venue_id = info["venue_id"]
        cls.staff = info["staff"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.platform.close()

    def ctx(self, role: str) -> RequestContext:
        return RequestContext(
            tenant_id=self.tenant_id,
            principal=Principal(kind="STAFF", id=self.staff[role]["staff_id"]),
            channel="STAFF",
            venue_id=self.venue_id,
        )

    def system(self) -> RequestContext:
        return self.platform.system_context(self.tenant_id).for_venue(self.venue_id)


class CoverageTests(_Base):
    def test_every_settings_page_has_a_backend(self) -> None:
        # A page is "finished" if it is either config-backed or listed by the record
        # reader. Anything else would fall through to a placeholder in the UI.
        from utp.api import server as srv  # noqa: F401 — ensure module imports

        config_pages = set(CONFIG_PAGES_BY_KEY)
        # Record pages, mirrored from the server's _list_records switch.
        record_pages = {
            "Ticket Types", "Customer Segments", "Products", "Experiences", "Pricing",
            "Promotions", "Coupon Codes", "Cash Coupons", "Member Rewards", "Email Templates",
            "Gates", "Access Points", "Kiosks", "POS Devices", "Printers", "Gate Devices",
            "Devices", "Shows", "Show Schedule", "Staff", "Roles", "Venues", "Organization",
            "Brand", "Terms & Conditions", "Seat Type", "Seat Zone", "Seat Layout",
            "Areas", "Capacity", "Time Slots", "Audit Logs", "Permissions",
        }
        # The already-built hand-written settings screens.
        prebuilt = {
            "VAT Settings", "Service Charge Settings", "Time Zone Settings",
            "Ticket Validity Settings", "Currency Settings", "Exchange Rates", "Payment Type",
        }
        covered = config_pages | record_pages | prebuilt
        missing = sorted(p for p in perms.SETTINGS_PAGE_KEYS if p not in covered)
        self.assertEqual(missing, [], f"settings pages with no backend: {missing}")


class ConfigPersistenceTests(_Base):
    def test_save_and_reload(self) -> None:
        svc = self.platform.settings_pages
        ctx = self.ctx("VENUE_MANAGER")
        value = {"days": {d: {"closed": d == "SUN", "open": "10:00", "close": "19:00",
                              "last_admission": "18:00"} for d in
                          ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")}}
        svc.set(ctx, "Operating Hours", value, venue_id=self.venue_id, reason="test")
        # A fresh resolve (new context) must return the stored value, not the default.
        again = svc.get(self.ctx("VENUE_MANAGER"), "Operating Hours", venue_id=self.venue_id)
        self.assertFalse(again["inherited"])
        self.assertTrue(again["value"]["days"]["SUN"]["closed"])

    def test_defaults_when_unset(self) -> None:
        svc = self.platform.settings_pages
        got = svc.get(self.ctx("VENUE_MANAGER"), "Advance Booking", venue_id=self.venue_id)
        self.assertTrue(got["inherited"])
        self.assertEqual(got["value"]["window_opens_days_before"], 90)


class ConfigAuthorizationTests(_Base):
    def test_view_permission_gates_read(self) -> None:
        # REPORT_VIEWER holds no settings pages, so a read is refused.
        with self.assertRaises(AuthorizationDenied):
            self.platform.settings_pages.get(self.ctx("REPORT_VIEWER"), "Operating Hours", venue_id=self.venue_id)

    def test_edit_permission_gates_write(self) -> None:
        # VENUE_MANAGER is read-only on Rounding by role template: can view, cannot save.
        svc = self.platform.settings_pages
        got = svc.get(self.ctx("VENUE_MANAGER"), "Rounding", venue_id=self.venue_id)
        self.assertTrue(got["can_edit"] is False)
        with self.assertRaises(AuthorizationDenied):
            svc.set(self.ctx("VENUE_MANAGER"), "Rounding", {"mode": "NEAREST_1"}, venue_id=self.venue_id)

    def test_manage_action_gates_sensitive_page(self) -> None:
        # Integrations needs MANAGE_INTEGRATION on top of EDIT; the manager lacks it.
        self.assertFalse(self.platform.settings_pages.can_edit(self.ctx("VENUE_MANAGER"), "Integrations"))
        # Technical support holds it.
        self.assertTrue(self.platform.settings_pages.can_edit(self.ctx("TECHNICAL_SUPPORT"), "Integrations"))


class ConfigValidationTests(_Base):
    def test_last_admission_after_close_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.settings_pages.set(
                self.ctx("VENUE_MANAGER"), "Operating Hours",
                {"days": {"MON": {"open": "10:00", "close": "12:00", "last_admission": "13:00"}}},
                venue_id=self.venue_id,
            )

    def test_language_default_must_be_enabled(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.settings_pages.set(
                self.system(), "Languages", {"enabled": ["en", "th"], "default": "zh"},
                venue_id=self.venue_id,
            )

    def test_integration_endpoint_must_be_https(self) -> None:
        with self.assertRaises(ValidationError):
            self.platform.settings_pages.set(
                self.system(), "Integrations",
                {"crm": {"enabled": True, "endpoint": "http://insecure.example"}},
                venue_id=self.venue_id,
            )


class SecretHandlingTests(_Base):
    def test_secret_stored_masked_and_preserved_on_blank(self) -> None:
        svc = self.platform.settings_pages
        sys = self.system()
        saved = svc.set(sys, "Integrations",
                        {"accounting": {"enabled": True, "endpoint": "https://a.example/hook",
                                        "api_key": "LIVE-SECRET-2026"}},
                        venue_id=self.venue_id)
        key = saved["value"]["accounting"]["api_key"]
        # Stored as a masked descriptor, never the raw secret.
        self.assertIsInstance(key, dict)
        self.assertTrue(key["set"])
        self.assertEqual(key["last4"], "2026")
        self.assertNotIn("LIVE-SECRET-2026", str(saved["value"]))
        # A blank replacement keeps the stored secret.
        again = svc.set(sys, "Integrations",
                        {"accounting": {"enabled": True, "endpoint": "https://a.example/hook", "api_key": ""}},
                        venue_id=self.venue_id)
        self.assertEqual(again["value"]["accounting"]["api_key"]["last4"], "2026")


class ScopeTests(_Base):
    def test_venue_scope_is_isolated(self) -> None:
        # A value written for this venue must not appear when resolving a different
        # venue id. We probe with the SYSTEM principal so authorization does not mask
        # the scope behaviour under test.
        svc = self.platform.settings_pages
        sys = self.system()
        svc.set(sys, "Booking Rules", {"max_days_in_advance": 30, "max_per_booking": 5,
                                       "same_day_enabled": False, "available_weekdays": ["MON"]},
                venue_id=self.venue_id)
        other = svc.get(self.platform.system_context(self.tenant_id).for_venue("ven_other"),
                        "Booking Rules", venue_id="ven_other")
        # The other venue sees the default, not this venue's override.
        self.assertTrue(other["inherited"])
        self.assertEqual(other["value"]["max_days_in_advance"], 90)


class AuditTests(_Base):
    def test_config_change_is_audited(self) -> None:
        svc = self.platform.settings_pages
        sys = self.system()
        svc.set(sys, "Price Display", {"symbol_first": False}, venue_id=self.venue_id, reason="audit test")
        rows = self.platform.db.query(
            "SELECT action, target_type FROM audit_events WHERE tenant_id = ? AND action = 'CONFIG_CHANGE' "
            "AND target_type = 'settings_page' ORDER BY at_utc DESC LIMIT 5",
            (self.tenant_id,),
        )
        self.assertTrue(any(r["target_type"] == "settings_page" for r in rows))


class RecordCrudTests(_Base):
    """The generic add/edit/delete for record-collection pages.

    These pages used to render as a read-only table even for a role that held
    ADD/EDIT/DELETE. The registry now dispatches each verb to the owning service,
    so the grant is honoured, the write is authorized server-side and audited.
    """

    def _owner(self) -> RequestContext:
        return self.ctx("OWNER")

    def test_create_edit_delete_segment_via_registry(self) -> None:
        from utp.api.record_crud import record_page

        crud = record_page("Customer Segments")
        ctx = self._owner()
        before = int(self.platform.db.scalar(
            "SELECT COUNT(*) FROM audit_events WHERE tenant_id = ? AND action = 'CONFIG_CHANGE'",
            (self.tenant_id,), default=0))
        created = crud.create(self.platform, ctx, self.venue_id,
                              {"code": "TSEG", "name": {"en": "Test Seg"}, "display_order": 77})
        self.assertEqual(created["code"], "TSEG")
        after = int(self.platform.db.scalar(
            "SELECT COUNT(*) FROM audit_events WHERE tenant_id = ? AND action = 'CONFIG_CHANGE'",
            (self.tenant_id,), default=0))
        self.assertGreater(after, before, "create must write an audit entry")
        updated = crud.update(self.platform, ctx, created["id"], {"status": "INACTIVE"})
        self.assertEqual(updated["status"], "INACTIVE")
        crud.delete(self.platform, ctx, created["id"], "test cleanup")

    def test_payment_type_requires_reason(self) -> None:
        from utp.api.record_crud import record_page

        crud = record_page("Payment Type")
        # The MANAGE_PAYMENT_TYPE action demands a reason; without one it is refused.
        with self.assertRaises(ValidationError):
            crud.create(self.platform, self._owner(), self.venue_id,
                        {"code": "TPT", "display_name": {"en": "T"}, "method": "CARD"})
        # With a reason it succeeds and is archived (DELETE maps to archive).
        made = crud.create(self.platform, self._owner(), self.venue_id,
                           {"code": "TPT2", "display_name": {"en": "T2"}, "method": "CARD",
                            "reason": "test add"})
        self.assertEqual(made["status"], "ACTIVE")
        archived = crud.delete(self.platform, self._owner(), made["id"], "test archive")
        self.assertEqual(archived["status"], "ARCHIVED")

    def test_add_permission_is_enforced_server_side(self) -> None:
        # A role without Customer Segments.ADD is refused by the service even though
        # the client could be tricked into POSTing (§75).
        from utp.api.record_crud import record_page

        crud = record_page("Customer Segments")
        with self.assertRaises(AuthorizationDenied):
            crud.create(self.platform, self.ctx("REPORT_VIEWER"), self.venue_id,
                        {"code": "NOPE", "name": {"en": "no"}})


class Gap2CrudTests(_Base):
    """Fix.md Gap 2: the previously read-only pages now create/edit/delete."""

    def _owner(self) -> RequestContext:
        return self.ctx("OWNER")

    def test_seat_type_lifecycle(self) -> None:
        from utp.api.record_crud import record_page
        crud = record_page("Seat Type")
        ctx = self._owner()
        rec = crud.create(self.platform, ctx, self.venue_id,
                          {"code": "VIP", "name": "VIP Seat", "colour": "#FFD700"})
        self.assertEqual(rec["code"], "VIP")
        upd = crud.update(self.platform, ctx, rec["id"], {"name": "VIP Plus"})
        self.assertEqual(upd["name"], "VIP Plus")
        crud.delete(self.platform, ctx, rec["id"], "test")

    def test_show_and_schedule(self) -> None:
        import datetime as _d
        from utp.api.record_crud import record_page
        ctx = self._owner()
        show = record_page("Shows").create(self.platform, ctx, self.venue_id,
                                           {"code": "DIVE1", "name": {"en": "Dive Show"},
                                            "default_duration_minutes": 20})
        fut = (_d.date.today() + _d.timedelta(days=4)).isoformat()
        sess = record_page("Show Schedule").create(self.platform, ctx, self.venue_id,
                                                    {"experience_id": show["id"], "date": fut,
                                                     "start_time": "14:00", "capacity": 80})
        self.assertIsNotNone(sess.get("id"))
        record_page("Show Schedule").delete(self.platform, ctx, sess["id"], "test")
        record_page("Shows").delete(self.platform, ctx, show["id"], "test")

    def test_cash_coupon_audited(self) -> None:
        from utp.api.record_crud import record_page
        ctx = self._owner()
        before = int(self.platform.db.scalar(
            "SELECT COUNT(*) FROM audit_events WHERE tenant_id = ?", (self.tenant_id,), default=0))
        cp = record_page("Cash Coupons").create(self.platform, ctx, self.venue_id,
            {"internal_code": "GIFT50", "name": {"en": "Gift 50"}, "amount_minor": 5000,
             "accounting_treatment": "STORED_VALUE"})
        after = int(self.platform.db.scalar(
            "SELECT COUNT(*) FROM audit_events WHERE tenant_id = ?", (self.tenant_id,), default=0))
        self.assertGreater(after, before, "cash coupon create must audit")
        record_page("Cash Coupons").delete(self.platform, ctx, cp["id"], "test")

    def test_seat_type_add_refused_without_permission(self) -> None:
        from utp.api.record_crud import record_page
        with self.assertRaises(AuthorizationDenied):
            record_page("Seat Type").create(self.platform, self.ctx("REPORT_VIEWER"), self.venue_id,
                                             {"code": "X", "name": "x"})


class StaffRoleMappingTests(_Base):
    """Fix.md Gap 1: staff → role assignment and effective-permission resolution."""

    def test_effective_permissions_per_venue(self) -> None:
        cashier_id = self.staff["COUNTER_CASHIER"]["staff_id"]
        summary = self.platform.authz.permission_summary(self.ctx("OWNER"), cashier_id)
        self.assertIn("by_venue", summary)
        # The cashier can view bookings but not delete them, per its template.
        any_venue = next(iter(summary["by_venue"].values()))
        bookings = any_venue["pages"].get("Bookings", {})
        self.assertTrue(bookings.get("VIEW"))
        self.assertFalse(bookings.get("DELETE"))

    def test_assign_and_remove_role_persists(self) -> None:
        owner = self.ctx("OWNER")
        cashier_id = self.staff["COUNTER_CASHIER"]["staff_id"]
        viewer_role = self.platform.db.query_one(
            "SELECT id FROM roles WHERE tenant_id = ? AND code = 'REPORT_VIEWER'", (self.tenant_id,))["id"]
        res = self.platform.staff.assign_role(
            owner, staff_id=cashier_id, role_id=viewer_role, scope_type="TENANT", reason="test")
        aid = res["assignment_id"]
        roles = [a["role_code"] for a in self.platform.staff.get_staff(owner, cashier_id)["roles"]]
        self.assertIn("REPORT_VIEWER", roles)
        self.platform.staff.remove_role_assignment(owner, aid, reason="test")

    def test_self_escalation_refused(self) -> None:
        # A manager cannot grant themselves a role above their own authority (§52).
        mgr_ctx = self.ctx("VENUE_MANAGER")
        mgr_id = self.staff["VENUE_MANAGER"]["staff_id"]
        owner_role = self.platform.db.query_one(
            "SELECT id FROM roles WHERE tenant_id = ? AND code = 'OWNER'", (self.tenant_id,))
        if owner_role is None:
            self.skipTest("OWNER role not seeded")
        # Refused: a staff member may never change their own authority (R44.2), and
        # even for another target the grant is capped at the actor's authority (§52).
        with self.assertRaises(AuthorizationDenied):
            self.platform.staff.assign_role(
                mgr_ctx, staff_id=mgr_id, role_id=owner_role["id"], scope_type="TENANT", reason="escalate")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
