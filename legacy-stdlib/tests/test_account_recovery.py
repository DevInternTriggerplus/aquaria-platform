"""Owner-account self-heal and self-service password reset.

These guard the two ways a locked-out administrator gets back in:

* :class:`OwnerSelfHealTests` — ``seed.ensure_owner_access`` restores the durable owner
  account no matter what state it is in (missing, suspended, wrong password, no role),
  which is what makes "I need to access it anytime" true across restarts.
* :class:`PasswordResetTests` — the unauthenticated forgot/reset flow issues a one-time
  token, is enumeration-safe, enforces the password policy, and refuses a bad token.
"""

from __future__ import annotations

import unittest

import seed
from utp.app import Platform
from utp.core.context import Principal
from utp.core.errors import AuthenticationRequired, ValidationError


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = Platform()
        info = seed.provision(cls.platform)
        cls.tenant_id = info["tenant_id"]
        cls.venue_id = info["venue_id"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.platform.close()

    def _guest(self):
        return self.platform.guest_context(self.tenant_id, venue_id=self.venue_id)

    def _can_login(self, credential: str = seed.DEMO_CREDENTIAL, email: str = seed.OWNER_EMAIL):
        base = self._guest()
        result = self.platform.staff.login(base, email=email, credential=credential)
        eff = self.platform.authz.effective_permissions(
            base.with_principal(Principal(kind="STAFF", id=result["staff_id"])).for_venue(None)
        )
        return result["roles"], len(eff.granted)


class OwnerSelfHealTests(_Base):
    def test_owner_signs_in_after_seed(self) -> None:
        roles, perms = self._can_login()
        self.assertIn("OWNER", roles)
        self.assertGreater(perms, 0)

    def test_heals_revoked_role_and_reactivates(self) -> None:
        db = self.platform.db
        owner = db.query_one(
            "SELECT id FROM staff WHERE tenant_id = ? AND email = ?", (self.tenant_id, seed.OWNER_EMAIL)
        )
        # Break it thoroughly: revoke the role, suspend the account, corrupt the password.
        db.execute("UPDATE role_assignments SET status = 'REVOKED' WHERE tenant_id = ? AND staff_id = ?",
                   (self.tenant_id, owner["id"]))
        db.execute("UPDATE staff SET status = 'SUSPENDED', credential_hash = 'x' WHERE tenant_id = ? AND id = ?",
                   (self.tenant_id, owner["id"]))
        with self.assertRaises(AuthenticationRequired):
            self._can_login()
        # Heal restores full access.
        seed.ensure_owner_access(self.platform, tenant_id=self.tenant_id, venue_id=self.venue_id)
        roles, perms = self._can_login()
        self.assertIn("OWNER", roles)
        self.assertGreater(perms, 0)


class PasswordResetTests(_Base):
    def test_enumeration_safe_and_reset_works(self) -> None:
        staff = self.platform.staff
        base = self._guest()
        # Unknown email: same shape, no token leaked.
        unknown = staff.request_password_reset(base, email="does-not-exist@example.com")
        self.assertTrue(unknown["requested"])
        self.assertNotIn("reset_token", unknown)
        # Real account: a token is issued.
        issued = staff.request_password_reset(base, email=seed.OWNER_EMAIL)
        self.assertIn("reset_token", issued)
        token = issued["reset_token"]
        # Wrong token is refused without revealing anything.
        with self.assertRaises(AuthenticationRequired):
            staff.complete_password_reset(
                base, email=seed.OWNER_EMAIL, token="wrong-token", credential="BrandNewPass2027"
            )
        # A weak password is rejected by policy.
        with self.assertRaises(ValidationError):
            staff.complete_password_reset(base, email=seed.OWNER_EMAIL, token=token, credential="short")
        # The correct token + a strong password resets it, and login uses the new one.
        staff.complete_password_reset(
            base, email=seed.OWNER_EMAIL, token=token, credential="BrandNewPass2027"
        )
        roles, _ = self._can_login(credential="BrandNewPass2027")
        self.assertIn("OWNER", roles)
        # The token is single-use: it cannot be replayed.
        with self.assertRaises(AuthenticationRequired):
            staff.complete_password_reset(
                base, email=seed.OWNER_EMAIL, token=token, credential="AnotherPass2027"
            )
        # Put the demo credential back so other tests / the shared fixture are unaffected.
        seed.ensure_owner_access(self.platform, tenant_id=self.tenant_id, venue_id=self.venue_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
