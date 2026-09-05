"""OWASP control tests.

Each test names the OWASP category and the control it exercises. The register in
:mod:`utp.security.owasp` claims these controls exist; these tests are what make the
claim checkable.
"""

from __future__ import annotations

import socket
import unittest

from utp.core.clock import FixedClock
from utp.core.errors import AuthorizationDenied, RateLimited, ValidationError
from utp.security import csrf, headers, monitoring, owasp, ratelimit, secrets, ssrf, uploads, validation


# --------------------------------------------------------------------------- #
# Register integrity
# --------------------------------------------------------------------------- #


class RegisterTests(unittest.TestCase):
    def test_every_referenced_implementation_exists(self) -> None:
        """The register cannot drift from the code without failing here."""
        result = owasp.verify_register()
        self.assertEqual(result["broken_references"], [], msg=f"broken: {result['broken_references']}")
        self.assertEqual(result["partial_without_stated_gap"], [])
        self.assertTrue(result["valid"])

    def test_all_ten_categories_present(self) -> None:
        self.assertEqual(
            [c.id for c in owasp.REGISTER],
            ["A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10"],
        )

    def test_every_control_traces_to_a_requirement(self) -> None:
        for control in owasp.all_controls():
            with self.subTest(control=control.id):
                self.assertTrue(control.requirements, f"{control.id} traces to no requirement")

    def test_report_renders(self) -> None:
        text = owasp.report()
        self.assertIn("A01 Broken Access Control", text)
        self.assertIn("A10 Server-Side Request Forgery", text)


# --------------------------------------------------------------------------- #
# A03 Injection
# --------------------------------------------------------------------------- #


class ValidationTests(unittest.TestCase):
    def test_unexpected_fields_are_rejected_not_ignored(self) -> None:
        """A03-3 / A01-7: mass assignment must fail loudly."""
        schema = validation.Schema(validation.Field("email", "email", required=True))
        with self.assertRaises(ValidationError) as caught:
            schema.validate({"email": "a@b.com", "tenant_id": "ten_other", "authority_level": 100})
        fields = caught.exception.field_errors
        self.assertIn("tenant_id", fields)
        self.assertIn("authority_level", fields)

    def test_required_and_typed_fields(self) -> None:
        schema = validation.Schema(
            validation.Field("visit_date", "date", required=True),
            validation.Field("quantity", "int", required=True, min_value=1, max_value=10),
            validation.Field("channel", "enum", required=True, choices=("ONLINE", "KIOSK")),
        )
        clean = schema.validate({"visit_date": "2026-09-10", "quantity": "3", "channel": "ONLINE"})
        self.assertEqual(clean, {"visit_date": "2026-09-10", "quantity": 3, "channel": "ONLINE"})
        with self.assertRaises(ValidationError):
            schema.validate({"visit_date": "10/09/2026", "quantity": 3, "channel": "ONLINE"})
        with self.assertRaises(ValidationError):
            schema.validate({"visit_date": "2026-09-10", "quantity": 99, "channel": "ONLINE"})
        with self.assertRaises(ValidationError):
            schema.validate({"visit_date": "2026-09-10", "quantity": 1, "channel": "PARTNER"})

    def test_control_characters_stripped_so_log_lines_cannot_be_forged(self) -> None:
        """A03-7."""
        dirty = "Somchai\r\nAUDIT: actor=root action=REFUND"
        self.assertNotIn("\n", validation.normalize_text(dirty))
        self.assertNotIn("\r", validation.sanitize_log_value(dirty))

    def test_unicode_is_nfc_normalized_before_comparison(self) -> None:
        composed = "Café"
        decomposed = "Cafe\u0301"
        self.assertEqual(
            validation.normalize_text(composed), validation.normalize_text(decomposed)
        )

    def test_dynamic_identifiers_must_be_allow_listed(self) -> None:
        """A03-2: the only strings reaching SQL text come from a fixed list."""
        self.assertEqual(validation.safe_table("bookings"), "bookings")
        with self.assertRaises(ValidationError):
            validation.safe_table("bookings; DROP TABLE bookings")
        with self.assertRaises(ValidationError):
            validation.safe_table("secret_table")

    def test_order_by_is_allow_listed(self) -> None:
        self.assertEqual(
            validation.safe_order_by("created_at", ["created_at", "visit_date"], direction="DESC"),
            "created_at DESC",
        )
        with self.assertRaises(ValidationError):
            validation.safe_order_by("(SELECT 1)", ["created_at"])

    def test_output_encoding_is_context_specific(self) -> None:
        """A03-4."""
        payload = '<img src=x onerror="alert(1)">'
        self.assertNotIn("<", validation.encode_html(payload))
        self.assertNotIn('"', validation.encode_attribute(payload))
        js = validation.encode_js_string("</script><script>alert(1)</script>")
        self.assertNotIn("</script>", js)
        self.assertIn("\\u003c", js)

    def test_csv_formula_injection_is_neutralised(self) -> None:
        """A03-6: an export must not become code execution on the analyst's machine."""
        for payload in ("=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1)"):
            with self.subTest(payload=payload):
                self.assertTrue(validation.encode_csv_cell(payload).startswith("'"))
        self.assertEqual(validation.encode_csv_cell("Aquaria Phuket"), "Aquaria Phuket")


# --------------------------------------------------------------------------- #
# A01 CSRF
# --------------------------------------------------------------------------- #


class CsrfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock("2026-09-01T03:00:00Z")
        self.csrf = csrf.CsrfProtection(clock=self.clock, secret=b"unit-test-key")

    def test_valid_double_submit_passes(self) -> None:
        token = self.csrf.issue(session_id="sess_1")
        self.assertTrue(
            self.csrf.verify(
                method="POST", session_id="sess_1", header_token=token, cookie_token=token
            )
        )

    def test_token_is_bound_to_its_session(self) -> None:
        """A01-8: a token minted for one session cannot be replayed into another."""
        token = self.csrf.issue(session_id="sess_1")
        self.assertFalse(
            self.csrf.verify(
                method="POST", session_id="sess_2", header_token=token, cookie_token=token
            )
        )

    def test_cookie_alone_is_insufficient(self) -> None:
        token = self.csrf.issue(session_id="sess_1")
        self.assertFalse(
            self.csrf.verify(method="POST", session_id="sess_1", header_token=None, cookie_token=token)
        )

    def test_mismatched_submissions_rejected(self) -> None:
        a = self.csrf.issue(session_id="sess_1")
        b = self.csrf.issue(session_id="sess_1")
        self.assertFalse(
            self.csrf.verify(method="POST", session_id="sess_1", header_token=a, cookie_token=b)
        )

    def test_forged_signature_rejected(self) -> None:
        token = self.csrf.issue(session_id="sess_1")
        nonce, expires, _ = token.split(".")
        forged = f"{nonce}.{expires}.{'0' * 24}"
        self.assertFalse(
            self.csrf.verify(method="POST", session_id="sess_1", header_token=forged, cookie_token=forged)
        )

    def test_expired_token_rejected(self) -> None:
        token = self.csrf.issue(session_id="sess_1")
        self.clock.advance(hours=9)
        self.assertFalse(
            self.csrf.verify(method="POST", session_id="sess_1", header_token=token, cookie_token=token)
        )

    def test_safe_methods_and_bearer_auth_are_exempt(self) -> None:
        self.assertTrue(
            self.csrf.verify(method="GET", session_id=None, header_token=None, cookie_token=None)
        )
        self.assertTrue(
            self.csrf.verify(
                method="POST",
                session_id=None,
                header_token=None,
                cookie_token=None,
                has_bearer_auth=True,
            )
        )

    def test_require_raises_generic_denial(self) -> None:
        with self.assertRaises(AuthorizationDenied) as caught:
            self.csrf.require(
                method="POST", session_id="sess_1", header_token=None, cookie_token=None
            )
        # The public payload must not explain what was missing.
        self.assertNotIn("csrf", caught.exception.public_dict()["error"]["message"].lower())

    def test_origin_check_is_defence_in_depth(self) -> None:
        allowed = ["https://book.aquaria.test"]
        self.assertTrue(csrf.origin_allowed("https://book.aquaria.test", None, allowed))
        self.assertFalse(csrf.origin_allowed("https://evil.test", None, allowed))
        self.assertTrue(csrf.origin_allowed(None, "https://book.aquaria.test/checkout", allowed))
        self.assertFalse(csrf.origin_allowed(None, "https://evil.test/x", allowed))


# --------------------------------------------------------------------------- #
# A10 SSRF
# --------------------------------------------------------------------------- #


def _resolver_for(mapping: dict[str, str]):
    """Deterministic resolver so SSRF branches are testable without DNS."""

    def resolve(host, port, *args, **kwargs):
        if host not in mapping:
            raise OSError(f"no such host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (mapping[host], port))]

    return resolve


class SsrfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ssrf.OutboundPolicy(allowed_hosts=("api.provider.test", "*.cdn.test"))

    def test_allow_listed_public_host_passes(self) -> None:
        target = ssrf.assert_safe_url(
            "https://api.provider.test/charge",
            self.policy,
            resolver=_resolver_for({"api.provider.test": "93.184.216.34"}),
        )
        self.assertEqual(target.resolved_ip, "93.184.216.34")
        self.assertEqual(target.connect_to, ("93.184.216.34", 443))

    def test_host_not_on_allow_list_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ssrf.assert_safe_url(
                "https://evil.test/x", self.policy, resolver=_resolver_for({"evil.test": "93.184.216.35"})
            )

    def test_allow_listed_host_resolving_to_metadata_is_rejected(self) -> None:
        """A10-2: the DNS answer is what matters, not the name."""
        with self.assertRaises(ValidationError):
            ssrf.assert_safe_url(
                "https://api.provider.test/x",
                self.policy,
                resolver=_resolver_for({"api.provider.test": "169.254.169.254"}),
            )

    def test_allow_listed_host_resolving_to_private_range_is_rejected(self) -> None:
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.1.1"):
            with self.subTest(address=address):
                with self.assertRaises(ValidationError):
                    ssrf.assert_safe_url(
                        "https://api.provider.test/x",
                        self.policy,
                        resolver=_resolver_for({"api.provider.test": address}),
                    )

    def test_plain_http_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ssrf.assert_safe_url(
                "http://api.provider.test/x",
                self.policy,
                resolver=_resolver_for({"api.provider.test": "93.184.216.34"}),
            )

    def test_credentials_in_url_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ssrf.assert_safe_url(
                "https://user:pass@api.provider.test/x",
                self.policy,
                resolver=_resolver_for({"api.provider.test": "93.184.216.34"}),
            )

    def test_blocked_port_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ssrf.assert_safe_url(
                "https://api.provider.test:5432/x",
                self.policy,
                resolver=_resolver_for({"api.provider.test": "93.184.216.34"}),
            )

    def test_wildcard_matches_subdomain_but_not_parent(self) -> None:
        self.assertTrue(self.policy.host_allowed("media.cdn.test"))
        self.assertFalse(self.policy.host_allowed("cdn.test"))
        self.assertFalse(self.policy.host_allowed("cdn.test.evil.com"))

    def test_documentation_ranges_are_treated_as_non_routable(self) -> None:
        """TEST-NET and documentation ranges are not valid outbound targets."""
        for address in ("203.0.113.10", "198.51.100.9", "192.0.2.1"):
            with self.subTest(address=address):
                self.assertTrue(ssrf.is_private_address(address))
        self.assertFalse(ssrf.is_private_address("93.184.216.34"))

    def test_unparseable_address_is_not_assumed_safe(self) -> None:
        self.assertTrue(ssrf.is_private_address("not-an-ip"))

    def test_ipv4_mapped_ipv6_loopback_rejected(self) -> None:
        """::ffff:127.0.0.1 must not slip past an IPv4-only check."""
        self.assertTrue(ssrf.is_private_address("::ffff:127.0.0.1"))

    def test_metadata_hostname_rejected_directly(self) -> None:
        policy = ssrf.OutboundPolicy(allowed_hosts=("metadata.google.internal",))
        with self.assertRaises(ValidationError):
            ssrf.assert_safe_url("https://metadata.google.internal/", policy)

    def test_open_redirect_prevention(self) -> None:
        """A01-9."""
        self.assertEqual(ssrf.safe_redirect_target("/manage/ABC", allowed_prefixes=("/manage",)), "/manage/ABC")
        for hostile in ("//evil.test", "https://evil.test", "\\\\evil.test", "manage/ABC"):
            with self.subTest(target=hostile):
                with self.assertRaises(ValidationError):
                    ssrf.safe_redirect_target(hostile, allowed_prefixes=("/manage",))
        with self.assertRaises(ValidationError):
            ssrf.safe_redirect_target("/admin/secret", allowed_prefixes=("/manage",))


# --------------------------------------------------------------------------- #
# A08 Uploads
# --------------------------------------------------------------------------- #

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
_GIF = b"GIF89a" + b"\x00" * 200
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 200
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>' + b" " * 200


class UploadTests(unittest.TestCase):
    def test_object_key_is_generated_not_taken_from_client(self) -> None:
        """A08-4: traversal and cross-tenant overwrite are structurally impossible."""
        plan = uploads.validate_upload_request(
            tenant_id="ten_1",
            kind="SHOW_COVER",
            content_type="image/png",
            declared_bytes=5000,
            owner_entity="experience",
            owner_id="exp_1",
        )
        self.assertTrue(plan.object_key.startswith("tenants/ten_1/show_cover/experience/exp_1/"))
        self.assertTrue(plan.object_key.endswith(".png"))
        self.assertNotIn("..", plan.object_key)
        self.assertEqual(plan.required_headers["Content-Disposition"], "attachment")
        self.assertEqual(plan.required_headers["x-amz-server-side-encryption"], "aws:kms")

    def test_svg_is_rejected(self) -> None:
        """A08-3: an SVG is a script-execution vector; no requirement needs it."""
        with self.assertRaises(ValidationError):
            uploads.validate_upload_request(
                tenant_id="ten_1",
                kind="SHOW_COVER",
                content_type="image/svg+xml",
                declared_bytes=1000,
                owner_entity="experience",
                owner_id="exp_1",
            )

    def test_magic_bytes_decide_the_type(self) -> None:
        self.assertEqual(uploads.sniff_content_type(_PNG), "image/png")
        self.assertEqual(uploads.sniff_content_type(_JPEG), "image/jpeg")
        self.assertEqual(uploads.sniff_content_type(_GIF), "image/gif")
        self.assertEqual(uploads.sniff_content_type(_WEBP), "image/webp")
        self.assertIsNone(uploads.sniff_content_type(_SVG))

    def test_declared_type_must_match_actual_content(self) -> None:
        """A PNG renamed to .jpg, or an SVG declared as PNG, is refused."""
        with self.assertRaises(ValidationError) as caught:
            uploads.validate_bytes(_PNG, kind="SHOW_COVER", declared_content_type="image/jpeg")
        self.assertEqual(caught.exception.code, "content_type_mismatch")
        with self.assertRaises(ValidationError):
            uploads.validate_bytes(_SVG, kind="SHOW_COVER", declared_content_type="image/png")

    def test_valid_image_accepted_with_checksum(self) -> None:
        result = uploads.validate_bytes(_PNG, kind="SHOW_COVER", declared_content_type="image/png")
        self.assertEqual(result["content_type"], "image/png")
        self.assertEqual(result["bytes"], len(_PNG))
        # Re-validating with the returned checksum must succeed.
        uploads.validate_bytes(
            _PNG,
            kind="SHOW_COVER",
            declared_content_type="image/png",
            expected_checksum=result["sha256"],
        )

    def test_checksum_mismatch_rejected(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            uploads.validate_bytes(
                _PNG, kind="SHOW_COVER", declared_content_type="image/png", expected_checksum="deadbeef"
            )
        self.assertEqual(caught.exception.code, "checksum_mismatch")

    def test_polyglot_with_markup_in_header_rejected(self) -> None:
        polyglot = b"\x89PNG\r\n\x1a\n<script>alert(1)</script>" + b"\x00" * 200
        with self.assertRaises(ValidationError) as caught:
            uploads.validate_bytes(polyglot, kind="SHOW_COVER", declared_content_type="image/png")
        self.assertEqual(caught.exception.code, "embedded_markup_rejected")

    def test_size_limits_enforced_per_kind(self) -> None:
        with self.assertRaises(ValidationError):
            uploads.validate_upload_request(
                tenant_id="ten_1",
                kind="VENUE_LOGO",
                content_type="image/png",
                declared_bytes=9 * 1024 * 1024,  # over the 2 MB logo limit
                owner_entity="venue",
                owner_id="ven_1",
            )
        with self.assertRaises(ValidationError):
            uploads.validate_bytes(b"\x89PNG\r\n\x1a\n", kind="SHOW_COVER", declared_content_type="image/png")

    def test_unsafe_object_keys_rejected(self) -> None:
        for key in ("../../etc/passwd", "/absolute/key", "tenants//double", "Tenants/Upper"):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    uploads.assert_safe_object_key(key)

    def test_variant_keys_stay_in_the_same_prefix(self) -> None:
        key = "tenants/ten_1/show_cover/experience/exp_1/med_abc.png"
        self.assertEqual(
            uploads.variant_key(key, "thumb"),
            "tenants/ten_1/show_cover/experience/exp_1/med_abc.thumb.png",
        )
        with self.assertRaises(ValidationError):
            uploads.variant_key(key, "../../evil")


# --------------------------------------------------------------------------- #
# A05 Headers and cookies
# --------------------------------------------------------------------------- #


class HeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = headers.default_policy(
            allowed_origins=("https://book.aquaria.test",), cdn_host="https://media.aquaria.test"
        )

    def test_csp_has_no_unsafe_inline_script(self) -> None:
        """A05-1."""
        nonce = headers.new_nonce()
        csp = self.policy.csp(nonce=nonce)
        self.assertIn(f"'nonce-{nonce}'", csp)
        script_directive = next(d for d in csp.split("; ") if d.startswith("script-src"))
        self.assertNotIn("unsafe-inline", script_directive)
        self.assertNotIn("unsafe-eval", script_directive)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("base-uri 'none'", csp)

    def test_nonces_are_never_reused(self) -> None:
        self.assertNotEqual(headers.new_nonce(), headers.new_nonce())

    def test_hardened_header_set_present(self) -> None:
        result = self.policy.headers()
        self.assertEqual(result["X-Content-Type-Options"], "nosniff")
        self.assertEqual(result["X-Frame-Options"], "DENY")
        self.assertIn("max-age=", result["Strict-Transport-Security"])
        self.assertIn("includeSubDomains", result["Strict-Transport-Security"])
        self.assertIn("no-store", result["Cache-Control"])
        self.assertIn("geolocation=()", result["Permissions-Policy"])

    def test_cors_never_reflects_an_unvetted_origin(self) -> None:
        """A05-4."""
        self.assertEqual(self.policy.cors_headers("https://evil.test"), {})
        allowed = self.policy.cors_headers("https://book.aquaria.test")
        self.assertEqual(allowed["Access-Control-Allow-Origin"], "https://book.aquaria.test")
        self.assertEqual(allowed["Access-Control-Allow-Credentials"], "true")
        self.assertNotIn("*", allowed["Access-Control-Allow-Origin"])
        self.assertEqual(allowed["Vary"], "Origin")

    def test_session_cookie_is_hardened_and_host_only(self) -> None:
        """A05-3."""
        rendered = headers.SESSION_COOKIE.render("utp_session", "abc", max_age_seconds=1800)
        self.assertIn("Secure", rendered)
        self.assertIn("HttpOnly", rendered)
        self.assertIn("SameSite=Strict", rendered)
        self.assertNotIn("Domain=", rendered)

    def test_csrf_cookie_is_readable_by_script_by_design(self) -> None:
        rendered = headers.CSRF_COOKIE.render("utp_csrf", "token")
        self.assertNotIn("HttpOnly", rendered)
        self.assertIn("Secure", rendered)

    def test_api_profile_locks_everything_down(self) -> None:
        csp = self.policy.csp(nonce="n", profile="API")
        self.assertIn("default-src 'none'", csp)

    def test_kiosk_profile_prevents_navigating_away(self) -> None:
        csp = self.policy.csp(nonce="n", profile="KIOSK")
        self.assertIn("navigate-to 'self'", csp)


# --------------------------------------------------------------------------- #
# A02 Secrets
# --------------------------------------------------------------------------- #


class SecretTests(unittest.TestCase):
    def test_missing_secret_fails_fast_at_startup(self) -> None:
        """A05-6: better a refused deployment than a failure at the gate."""
        provider = secrets.EnvironmentSecretProvider(overrides={"qr.signing_key": "k"})
        result = secrets.verify_configuration(provider, required=("qr.signing_key", "payment.api_key"))
        self.assertFalse(result["complete"])
        self.assertIn("payment.api_key", result["missing"])

    def test_field_cipher_round_trips(self) -> None:
        cipher = secrets.FieldCipher(key=b"a" * 32, key_id="k1")
        token = cipher.encrypt("0105558xxxxxx")
        self.assertNotIn("0105558", token)
        self.assertEqual(cipher.decrypt(token), "0105558xxxxxx")

    def test_tampered_ciphertext_is_refused_before_decryption(self) -> None:
        cipher = secrets.FieldCipher(key=b"a" * 32, key_id="k1")
        token = cipher.encrypt("sensitive")
        version, key_id, nonce, ciphertext, tag = token.split(".")
        tampered = ".".join([version, key_id, nonce, ciphertext, "AAAAAAAAAAAAAAAAAAAAAA"])
        with self.assertRaises(ValueError):
            cipher.decrypt(tampered)

    def test_key_rotation_is_observable(self) -> None:
        """A02-5: ciphertext carries the key that produced it."""
        old = secrets.FieldCipher(key=b"a" * 32, key_id="k1")
        token = old.encrypt("value")
        new = secrets.FieldCipher(key=b"b" * 32, key_id="k2")
        self.assertEqual(new.key_id_of(token), "k1")
        self.assertTrue(new.needs_rotation(token))
        self.assertFalse(old.needs_rotation(token))

    def test_at_rest_requirements_documented(self) -> None:
        self.assertIn("object_storage", secrets.AT_REST_REQUIREMENTS)
        self.assertIn("KMS", secrets.AT_REST_REQUIREMENTS["database"])


# --------------------------------------------------------------------------- #
# A04 Rate limiting and abuse quotas
# --------------------------------------------------------------------------- #


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        from utp.core.db import Database

        self.clock = FixedClock("2026-09-01T03:00:00Z")
        self.db = Database(clock=self.clock)
        self.db.migrate()
        self.db.insert(
            "tenants", {"id": "ten_1", "code": "t", "name": "T", "created_at": "2026-01-01T00:00:00Z"}
        )
        self.limiter = ratelimit.RateLimiter(self.db, self.clock)

        class Ctx:
            tenant_id = "ten_1"
            venue_id = None

        self.ctx = Ctx()

    def tearDown(self) -> None:
        self.db.close()

    def test_limit_enforced_then_backs_off(self) -> None:
        quota = ratelimit.QUOTAS["booking_lookup_per_identifier"]
        for _ in range(quota.limit):
            self.limiter.check(self.ctx, "booking_lookup_per_identifier", subject="AQP-1111-2222")
        with self.assertRaises(RateLimited) as first:
            self.limiter.check(self.ctx, "booking_lookup_per_identifier", subject="AQP-1111-2222")
        with self.assertRaises(RateLimited) as second:
            self.limiter.check(self.ctx, "booking_lookup_per_identifier", subject="AQP-1111-2222")
        # Exponential backoff: the second rejection waits longer than the first.
        self.assertGreater(second.exception.retry_after_seconds, first.exception.retry_after_seconds)

    def test_subjects_are_counted_independently(self) -> None:
        self.limiter.check(self.ctx, "booking_lookup_per_identifier", subject="A")
        result = self.limiter.check(self.ctx, "booking_lookup_per_identifier", subject="B")
        self.assertEqual(result["count"], 1)

    def test_subject_is_not_stored_in_the_clear(self) -> None:
        self.limiter.check(self.ctx, "login_per_account", subject="victim@example.com")
        buckets = [r["bucket"] for r in self.db.query("SELECT bucket FROM rate_limit_counters")]
        self.assertTrue(buckets)
        for bucket in buckets:
            self.assertNotIn("victim@example.com", bucket)

    def test_window_rolls_over(self) -> None:
        for _ in range(ratelimit.QUOTAS["booking_lookup_per_identifier"].limit):
            self.limiter.check(self.ctx, "booking_lookup_per_identifier", subject="X")
        self.clock.advance(hours=1)
        result = self.limiter.check(self.ctx, "booking_lookup_per_identifier", subject="X")
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["exceeded"])

    def test_hold_quota_bounds_outstanding_inventory_not_request_rate(self) -> None:
        """A04-2: residual risk D.5."""
        limit = ratelimit.QUOTAS["concurrent_holds_per_source"].limit
        self.limiter.assert_hold_quota(self.ctx, source="198.51.100.7", concurrent_holds=limit - 1)
        with self.assertRaises(RateLimited) as caught:
            self.limiter.assert_hold_quota(self.ctx, source="198.51.100.7", concurrent_holds=limit)
        self.assertEqual(caught.exception.code, "hold_quota_exceeded")

    def test_promo_code_quota_is_tighter_than_generic(self) -> None:
        """A04-3: residual risk D.6."""
        self.assertLess(
            ratelimit.QUOTAS["promo_code_per_source"].limit,
            ratelimit.QUOTAS["login_per_source"].limit,
        )

    def test_token_bucket_refills(self) -> None:
        bucket = ratelimit.TokenBucket(capacity=2, refill_per_second=1)
        self.assertTrue(bucket.allow(now=100.0))
        self.assertTrue(bucket.allow(now=100.0))
        self.assertFalse(bucket.allow(now=100.0))
        self.assertTrue(bucket.allow(now=102.0))


# --------------------------------------------------------------------------- #
# A09 Monitoring
# --------------------------------------------------------------------------- #


class MonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        from utp.core.audit import AuditLog
        from utp.core.context import Principal, RequestContext
        from utp.core.db import Database

        self.clock = FixedClock("2026-09-01T03:00:00Z")
        self.db = Database(clock=self.clock)
        self.db.migrate()
        self.db.insert(
            "tenants", {"id": "ten_1", "code": "t", "name": "T", "created_at": "2026-01-01T00:00:00Z"}
        )
        self.audit = AuditLog(self.db, self.clock)
        self.alerts: list[monitoring.Alert] = []
        self.monitor = monitoring.SecurityMonitor(
            self.db, self.clock, sink=self.alerts.append
        )
        self.ctx = RequestContext(
            tenant_id="ten_1",
            principal=Principal(kind="STAFF", id="stf_1"),
            ip_address="198.51.100.9",
        )

    def tearDown(self) -> None:
        self.db.close()

    def test_credential_stuffing_detected_by_source(self) -> None:
        """A09-4 / R73.14."""
        detector = monitoring.DETECTORS_BY_KEY["credential_stuffing"]
        for _ in range(detector.threshold):
            self.audit.security(self.ctx, "LOGIN_FAILED", reason="bad_credential")
        alerts = self.monitor.evaluate(self.ctx, detectors=[detector])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].detector, "credential_stuffing")
        self.assertEqual(alerts[0].severity, "CRITICAL")
        self.assertEqual(alerts[0].group_value, "198.51.100.9")
        self.assertEqual(self.alerts, alerts)

    def test_below_threshold_does_not_alert(self) -> None:
        detector = monitoring.DETECTORS_BY_KEY["credential_stuffing"]
        for _ in range(detector.threshold - 1):
            self.audit.security(self.ctx, "LOGIN_FAILED")
        self.assertEqual(self.monitor.evaluate(self.ctx, detectors=[detector]), [])

    def test_single_cross_tenant_attempt_is_critical(self) -> None:
        detector = monitoring.DETECTORS_BY_KEY["cross_tenant_probing"]
        self.audit.security(self.ctx, "CROSS_TENANT_ATTEMPT", reason="tenant_mismatch")
        alerts = self.monitor.evaluate(self.ctx, detectors=[detector])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, "CRITICAL")

    def test_alerts_become_operational_exceptions(self) -> None:
        detector = monitoring.DETECTORS_BY_KEY["cross_tenant_probing"]
        self.audit.security(self.ctx, "CROSS_TENANT_ATTEMPT")
        self.monitor.evaluate(self.ctx, detectors=[detector])
        open_alerts = self.monitor.open_alerts(self.ctx)
        self.assertEqual(len(open_alerts), 1)
        self.assertEqual(open_alerts[0]["kind"], "SECURITY_CROSS_TENANT_PROBING")

    def test_events_outside_the_window_are_ignored(self) -> None:
        detector = monitoring.DETECTORS_BY_KEY["credential_stuffing"]
        for _ in range(detector.threshold):
            self.audit.security(self.ctx, "LOGIN_FAILED")
        self.clock.advance(minutes=detector.window_minutes + 1)
        self.assertEqual(self.monitor.evaluate(self.ctx, detectors=[detector]), [])

    def test_override_review_flags_missing_reasons(self) -> None:
        """A09-5 / residual risk D.2."""
        self.audit.record(self.ctx, "MANUAL_DISCOUNT", reason="Goodwill")
        self.audit.record(self.ctx, "OVERRIDE_ACCESS")  # no reason
        report = self.monitor.override_review(self.ctx, days=30)
        self.assertEqual(report["events_without_reason"], 1)
        self.assertEqual(report["actors"][0]["actor_id"], "stf_1")
        self.assertEqual(report["actors"][0]["total"], 2)

    def test_every_named_r73_14_pattern_has_a_detector(self) -> None:
        required = {"credential_stuffing", "authorization_probing", "abnormal_refunds", "abnormal_exports"}
        self.assertTrue(required.issubset(set(monitoring.DETECTORS_BY_KEY)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
