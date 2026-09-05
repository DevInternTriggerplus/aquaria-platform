"""Staff authentication, page-level permissions and the Settings surface.

This is the settings/reports specification made executable. The properties under
test are the ones a screenshot cannot prove and a demo cannot guarantee:

* **Authentication is not authorization** (§4). A signed-in principal reaches only
  what their effective permissions allow, resolved server-side on every request.
* **Verbs are independent** (§9, §67-69). Holding VIEW never confers ADD; ADD never
  confers EDIT; EDIT never confers DELETE. A role granted one is refused the others
  by the API, not merely in the UI.
* **The Settings home is built from permissions** (§26, §70, §71). A category with
  no viewable page inside it does not appear, and search never returns a page the
  principal cannot open (§27).
* **Reports and Settings are independent axes** (§72). A report-only account sees no
  settings; a settings account is not thereby given reports.
* **The registry is language-free; only the display is translated** (§49, §50). The
  same permission key resolves to five languages without the key ever changing.
* **Logout ends access** (§58) and a permission change is reflected promptly (§74).

The subjects are the seeded demo roles, because they are what ships: VENUE_MANAGER
(broad settings), REPORT_VIEWER (reports, zero settings), COUNTER_CASHIER (sells,
configures nothing), GATE_STAFF (neither).
"""

from __future__ import annotations

import unittest

import seed
from utp.app import Platform
from utp.core.context import Principal, RequestContext
from utp.core.errors import AuthenticationRequired, AuthorizationDenied
from utp.domain import permission_labels as plabels
from utp.domain import permissions as perms


class _Base(unittest.TestCase):
    """One provisioned platform shared across a class; each test resolves fresh."""

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

    def ctx_for(self, role_code: str, *, venue_id: str | None = None) -> RequestContext:
        """A staff request context for one of the seeded demo accounts.

        Resolution goes through the real principal, so what the test sees is exactly
        what an HTTP request from that account would be granted.
        """
        staff_id = self.staff[role_code]["staff_id"]
        return RequestContext(
            tenant_id=self.tenant_id,
            principal=Principal(kind="STAFF", id=staff_id),
            channel="STAFF",
            venue_id=self.venue_id if venue_id is None else venue_id,
        )

    def perms_of(self, role_code: str) -> set[str]:
        return set(self.platform.authz.effective_permissions(self.ctx_for(role_code)).granted)


class RegistryShapeTests(_Base):
    """The registry keeps the guarantees the rest of the system relies on."""

    def test_settings_pages_map_to_exactly_one_category(self) -> None:
        # A page in two categories would double-count the home and make navigation
        # ambiguous. The import-time check enforces it; this pins it as a contract.
        seen: dict[str, int] = {}
        for category in perms.SETTINGS_CATEGORIES:
            for page in category.pages:
                seen[page] = seen.get(page, 0) + 1
        self.assertTrue(all(count == 1 for count in seen.values()))
        self.assertEqual(set(seen), set(perms.SETTINGS_PAGE_KEYS))

    def test_every_settings_page_exists(self) -> None:
        for page in perms.SETTINGS_PAGE_KEYS:
            self.assertIn(page, perms.PAGES_BY_KEY)

    def test_singleton_settings_pages_have_no_add_or_delete(self) -> None:
        # The spec's "-" cells: VAT is viewed and edited, never added or deleted.
        for page_key in ("VAT Settings", "Service Charge Settings", "Time Zone Settings",
                         "Ticket Validity Settings", "Operating Hours", "Last Admission"):
            page = perms.PAGES_BY_KEY[page_key]
            self.assertEqual(page.verbs, ("VIEW", "EDIT"), page_key)

    def test_new_actions_registered(self) -> None:
        for action in ("RESET_ACCESS", "ASSIGN_ROLE", "APPROVE_EXCHANGE_RATE",
                       "APPROVE_TAX_CHANGE", "MANAGE_LOGIN_SECURITY", "MANAGE_INTEGRATION"):
            self.assertIn(action, perms.ACTIONS_BY_KEY, action)

    def test_no_verb_implies_another(self) -> None:
        # The registry stores no implication edges; assert the property directly on a
        # page that declares all four verbs.
        page = perms.PAGES_BY_KEY["Staff"]
        self.assertEqual(page.verbs, ("VIEW", "ADD", "EDIT", "DELETE"))
        # Independence is a property of enforcement, tested against a real role below;
        # here we simply confirm the keys are distinct strings, not aliases.
        keys = page.permission_keys()
        self.assertEqual(len(set(keys)), 4)


class LabelCoverageTests(_Base):
    """§50: every grantable thing is translated into all five languages."""

    def test_no_translation_gaps(self) -> None:
        gaps = plabels.coverage_gaps()
        self.assertEqual(gaps, {}, f"untranslated permission labels: {gaps}")

    def test_key_is_language_independent(self) -> None:
        # §49: the same internal key renders differently per language, and the key is
        # never any of those renderings.
        key = "Payment Type.EDIT"
        labels = {lang: plabels.permission_label(key, lang) for lang in ("en", "th", "zh", "ja", "ru")}
        self.assertEqual(len(set(labels.values())), 5, labels)
        self.assertNotIn(key, labels.values())

    def test_category_labels_localized(self) -> None:
        self.assertNotEqual(
            plabels.category_label("pricing_tax", "en"),
            plabels.category_label("pricing_tax", "th"),
        )


class VerbIndependenceTests(_Base):
    """§9, §67-69: granting one verb does not grant the others, enforced server-side."""

    def test_cashier_can_view_and_add_tickets_but_not_edit_or_delete(self) -> None:
        p = self.platform
        ctx = self.ctx_for("COUNTER_CASHIER")
        # The cashier template is VIEW+ADD on Tickets, deliberately not EDIT/DELETE.
        self.assertTrue(p.authz.can_page(ctx, "Tickets", "VIEW"))
        self.assertTrue(p.authz.can_page(ctx, "Tickets", "ADD"))
        self.assertFalse(p.authz.can_page(ctx, "Tickets", "EDIT"))
        self.assertFalse(p.authz.can_page(ctx, "Tickets", "DELETE"))
        # And require_page enforces the refusal, not just the query.
        p.authz.require_page(ctx, "Tickets", "VIEW")
        with self.assertRaises(AuthorizationDenied):
            p.authz.require_page(ctx, "Tickets", "EDIT")
        with self.assertRaises(AuthorizationDenied):
            p.authz.require_page(ctx, "Tickets", "DELETE")

    def test_manager_edit_without_delete_on_venue(self) -> None:
        # §22: a venue manager edits the venue but cannot delete it.
        p = self.platform
        ctx = self.ctx_for("VENUE_MANAGER")
        self.assertTrue(p.authz.can_page(ctx, "Venues", "EDIT"))
        self.assertFalse(p.authz.can_page(ctx, "Venues", "DELETE"))


class SettingsHomeTests(_Base):
    """§11, §26, §70, §71: the home is permission-filtered, not fixed."""

    def test_manager_sees_most_categories(self) -> None:
        home = self.platform.authz.settings_home(self.ctx_for("VENUE_MANAGER"))
        self.assertGreaterEqual(len(home), 8)
        # Every returned category carries at least one viewable page.
        for category in home:
            self.assertGreaterEqual(category["page_count"], 1)

    def test_report_viewer_sees_no_settings(self) -> None:
        # §72: reports access is not settings access.
        home = self.platform.authz.settings_home(self.ctx_for("REPORT_VIEWER"))
        self.assertEqual(home, [])

    def test_gate_staff_sees_no_settings(self) -> None:
        home = self.platform.authz.settings_home(self.ctx_for("GATE_STAFF"))
        self.assertEqual(home, [])

    def test_cashier_sees_only_categories_with_a_viewable_page(self) -> None:
        # §71: the cashier holds Time Slots.VIEW, which lives in Booking & Ticketing,
        # so exactly that category surfaces — no Devices, no Staff & Security.
        home = self.platform.authz.settings_home(self.ctx_for("COUNTER_CASHIER"))
        returned = {c["category"] for c in home}
        self.assertEqual(returned, {"booking_ticketing"})

    def test_home_only_lists_pages_the_principal_can_view(self) -> None:
        ctx = self.ctx_for("VENUE_MANAGER")
        granted = self.perms_of("VENUE_MANAGER")
        for category in self.platform.authz.settings_home(ctx):
            for page in category["pages"]:
                self.assertIn(f"{page['page']}.VIEW", granted)


class SettingsSearchTests(_Base):
    """§27, §32: search respects VIEW, ranks name over description, is script-aware."""

    def test_name_hit_ranked_before_description_hit(self) -> None:
        results = self.platform.authz.settings_search(self.ctx_for("VENUE_MANAGER"), "QR")
        pages = [r["page"] for r in results]
        self.assertIn("QR Access Rules", pages)
        self.assertIn("Ticket Validity Settings", pages)  # matched via description
        self.assertLess(pages.index("QR Access Rules"), pages.index("Ticket Validity Settings"))

    def test_search_does_not_falsely_match_substring_in_description(self) -> None:
        # "VAT" must not surface pages whose description merely contains "deactivates"
        # or "reservation". The word-anchored matcher is what prevents this.
        results = self.platform.authz.settings_search(self.ctx_for("VENUE_MANAGER"), "VAT")
        pages = [r["page"] for r in results]
        self.assertEqual(pages[0], "VAT Settings")
        # No page whose only "vat" is inside deactivates/reservation should appear.
        self.assertNotIn("Seat Reservation Rules", pages)

    def test_search_filtered_by_view_permission(self) -> None:
        # §27: a report viewer searching for VAT gets nothing, because VAT.VIEW is not
        # granted — not a result they are then refused.
        results = self.platform.authz.settings_search(self.ctx_for("REPORT_VIEWER"), "VAT")
        self.assertEqual(results, [])

    def test_thai_search_matches_localized_name(self) -> None:
        results = self.platform.authz.settings_search(
            self.ctx_for("VENUE_MANAGER"), "ภาษี", language="th"
        )
        self.assertIn("VAT Settings", [r["page"] for r in results])

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(self.platform.authz.settings_search(self.ctx_for("VENUE_MANAGER"), "  "), [])


class PermissionMatrixTests(_Base):
    """§19, §50: the grantable registry, localized, gated by Roles/Permissions VIEW."""

    def test_manager_can_load_matrix(self) -> None:
        matrix = self.platform.authz.permission_matrix(self.ctx_for("VENUE_MANAGER"), language="ja")
        self.assertEqual(len(matrix["pages"]), len(perms.PAGES))
        self.assertEqual(len(matrix["actions"]), len(perms.ACTIONS))
        # Localized: a Japanese verb label is not the English key.
        view_label = [v["label"] for v in matrix["verbs"] if v["verb"] == "VIEW"][0]
        self.assertNotEqual(view_label, "VIEW")

    def test_matrix_refused_without_roles_or_permissions_view(self) -> None:
        # §75: the registry is not public; a report viewer is refused.
        with self.assertRaises(AuthorizationDenied):
            self.platform.authz.permission_matrix(self.ctx_for("REPORT_VIEWER"))

    def test_matrix_marks_undeclared_verbs_false(self) -> None:
        matrix = self.platform.authz.permission_matrix(self.ctx_for("VENUE_MANAGER"))
        vat = [row for row in matrix["pages"] if row["page"] == "VAT Settings"][0]
        self.assertFalse(vat["verbs"]["ADD"])
        self.assertFalse(vat["verbs"]["DELETE"])
        self.assertTrue(vat["verbs"]["EDIT"])


class GrantSummaryTests(_Base):
    """§21: the pre-save summary answers the four questions in counts + plain words."""

    def test_summary_counts_and_sensitive_levels(self) -> None:
        summary = self.platform.authz.grant_summary(self.perms_of("VENUE_MANAGER"))
        self.assertGreater(summary["pages_viewable"], 0)
        self.assertGreater(summary["can_edit"], 0)
        levels = {row["page"]: row["level"] for row in summary["sensitive"]}
        # A venue manager holds Exchange Rates FULL but Roles read-only-ish; assert the
        # shape rather than exact numbers so the test survives template tuning.
        self.assertIn("VAT Settings", levels)
        self.assertIn("Roles", levels)

    def test_no_access_is_distinct_from_read_only(self) -> None:
        # §21 insists these are different states. A report viewer has no VAT access.
        summary = self.platform.authz.grant_summary(self.perms_of("REPORT_VIEWER"))
        levels = {row["page"]: row["level"] for row in summary["sensitive"]}
        self.assertEqual(levels["VAT Settings"], "NONE")


class SessionProfileTests(_Base):
    """§3: one call returns everything the back office needs, all permission-scoped."""

    def test_profile_shape(self) -> None:
        profile = self.platform.staff.session_profile(self.ctx_for("VENUE_MANAGER"))
        for key in ("staff", "tenant", "organization", "venues", "roles",
                    "authority_level", "scope", "permissions", "navigation", "settings"):
            self.assertIn(key, profile)
        self.assertTrue(profile["venues"])
        self.assertIn("VENUE_MANAGER", [r["code"] for r in profile["roles"]])

    def test_profile_navigation_matches_view_permissions(self) -> None:
        # §70: navigation is generated from VIEW permissions, nothing more.
        profile = self.platform.staff.session_profile(self.ctx_for("VENUE_MANAGER"))
        granted = self.perms_of("VENUE_MANAGER")
        for item in profile["navigation"]:
            self.assertIn(f"{item['page']}.VIEW", granted)

    def test_report_viewer_profile_has_no_settings(self) -> None:
        profile = self.platform.staff.session_profile(self.ctx_for("REPORT_VIEWER"))
        self.assertEqual(profile["settings"], [])
        # But it does carry the report/dashboard navigation.
        nav_pages = {n["page"] for n in profile["navigation"]}
        self.assertTrue(nav_pages <= {"Dashboard", "Reports", "Operations Dashboard"})
        self.assertIn("Reports", nav_pages)

    def test_scope_reports_venue_ids_not_flattened(self) -> None:
        # A venue-scoped role reports its venue list; None (all venues) would be wrong.
        profile = self.platform.staff.session_profile(self.ctx_for("VENUE_MANAGER"))
        self.assertIsNotNone(profile["scope"]["venue_ids"])
        self.assertIn(self.venue_id, profile["scope"]["venue_ids"])


class AuthenticationFlowTests(_Base):
    """Login, token resolution and logout — the §61 flow at the service layer."""

    def _guest_ctx(self) -> RequestContext:
        return self.platform.guest_context(self.tenant_id, venue_id=self.venue_id, channel="STAFF")

    def test_login_then_authenticate_token(self) -> None:
        p = self.platform
        result = p.staff.login(
            self._guest_ctx(), email="manager@aquaria.test", credential="Aquaria-Demo-2026", channel="STAFF"
        )
        self.assertIn("token", result)
        principal = p.staff.authenticate_token(self._guest_ctx(), result["token"])
        self.assertTrue(principal.is_staff)
        self.assertEqual(principal.id, self.staff["VENUE_MANAGER"]["staff_id"])

    def test_bad_credential_refused(self) -> None:
        with self.assertRaises(AuthenticationRequired):
            self.platform.staff.login(
                self._guest_ctx(), email="manager@aquaria.test", credential="wrong", channel="STAFF"
            )

    def test_unknown_principal_refused(self) -> None:
        with self.assertRaises(AuthenticationRequired):
            self.platform.staff.login(
                self._guest_ctx(), email="ghost@aquaria.test", credential="whatever", channel="STAFF"
            )

    def test_logout_revokes_token(self) -> None:
        p = self.platform
        result = p.staff.login(
            self._guest_ctx(), email="cashier@aquaria.test", credential="Aquaria-Demo-2026", channel="STAFF"
        )
        principal = p.staff.authenticate_token(self._guest_ctx(), result["token"])
        staff_ctx = self._guest_ctx().with_principal(principal)
        self.assertTrue(p.staff.logout(staff_ctx)["logged_out"])
        # §58: the token no longer authenticates.
        with self.assertRaises(AuthenticationRequired):
            p.staff.authenticate_token(self._guest_ctx(), result["token"])


class VenueScopeTests(_Base):
    """§73: a venue-scoped principal is refused at a venue outside their scope."""

    def test_out_of_scope_venue_is_refused(self) -> None:
        p = self.platform
        ctx = self.ctx_for("VENUE_MANAGER")
        # In scope, the manager may view VAT.
        p.authz.require_page(ctx, "VAT Settings", "VIEW")
        # A different venue id is outside their assignment, so scope fails first.
        other = ctx.for_venue("ven_does_not_belong")
        with self.assertRaises(AuthorizationDenied):
            p.authz.require_page(other, "VAT Settings", "VIEW")


class PermissionChangeTests(_Base):
    """§53, §74: a permission change is reflected on the next request, not hours later."""

    def test_permission_change_takes_effect_immediately(self) -> None:
        # There is deliberately no cross-request permission cache, so authority is
        # re-resolved from the database on the next request. We prove that by
        # suspending a throwaway account and confirming its authority collapses on a
        # fresh context — the mechanism §74 relies on, with no stale window.
        p = self.platform
        sys = p.system_context(self.tenant_id)
        org_id = self._org_id()
        invited = p.staff.invite_staff(
            sys, email="temp-perm@aquaria.test", first_name="Temp", last_name="Perm",
            organization_id=org_id,
        )
        p.staff.complete_enrolment(
            sys, staff_id=invited["id"], token=invited["enrolment_token"], credential="Temp-Pass-2026-Aa"
        )
        roles = {r["code"]: r["id"] for r in p.staff.list_roles(sys)}
        p.staff.assign_role(
            sys, staff_id=invited["id"], role_id=roles["VENUE_MANAGER"],
            scope_type="VENUE", scope_id=self.venue_id,
        )

        def fresh_ctx() -> RequestContext:
            return RequestContext(
                tenant_id=self.tenant_id,
                principal=Principal(kind="STAFF", id=invited["id"]),
                channel="STAFF",
                venue_id=self.venue_id,
            )

        self.assertTrue(p.authz.can_page(fresh_ctx(), "VAT Settings", "VIEW"))

        # Suspending revokes authority; the next request resolves to nothing (§53).
        p.staff.set_staff_status(sys, invited["id"], "SUSPENDED", reason="test")
        self.assertFalse(p.authz.can_page(fresh_ctx(), "VAT Settings", "VIEW"))

    def _org_id(self) -> str:
        row = self.platform.db.query_one(
            "SELECT organization_id FROM staff WHERE id = ?",
            (self.staff["VENUE_MANAGER"]["staff_id"],),
        )
        return row["organization_id"]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
