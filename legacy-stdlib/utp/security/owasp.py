"""OWASP Top 10 (2021) control register.

The point of this module is that it is *checkable*. Each control names the module and
symbol that implements it, and :func:`verify_register` imports every one of them. A
control that is deleted, renamed or never written fails the check, so the register
cannot quietly become a work of fiction.

``status`` is honest about verification:

``ENFORCED``      implemented in code and covered by the test suite
``INFRASTRUCTURE``correct implementation lives in deployment config, not application code
``PARTIAL``       implemented, with a stated gap
``EXTERNAL``      depends on a third party or on a manual process
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Literal

Status = Literal["ENFORCED", "INFRASTRUCTURE", "PARTIAL", "EXTERNAL"]


@dataclass(frozen=True, slots=True)
class Control:
    """One security control."""

    id: str
    title: str
    status: Status
    #: ``module:symbol`` implementing it, or ``None`` for infrastructure controls.
    implementation: str | None
    requirements: tuple[str, ...]
    notes: str = ""
    #: Stated gap. Mandatory when status is PARTIAL.
    gap: str = ""


@dataclass(frozen=True, slots=True)
class Category:
    """An OWASP Top 10 category and its controls."""

    id: str
    title: str
    controls: tuple[Control, ...]
    summary: str = ""


A01 = Category(
    id="A01",
    title="Broken Access Control",
    summary=(
        "Authorization is enforced in the service layer, below every transport, so no "
        "client can bypass it. Default deny; permissions are grants, absence is denial."
    ),
    controls=(
        Control(
            "A01-1",
            "Server-side permission enforcement on every protected operation",
            "ENFORCED",
            "utp.services.authz:AuthorizationService.require_page",
            ("R42.1", "R42.2", "R42.4", "R42.5", "R42.6", "R42.10"),
            "Checks run in the documented order: principal, tenant, org, venue, page, action.",
        ),
        Control(
            "A01-2",
            "Default deny for new roles and newly added permissions",
            "ENFORCED",
            "utp.domain.permissions:ALL_PERMISSION_KEYS",
            ("R40.11", "R44.1"),
            "Grants are rows; a new page adds keys that no existing role holds.",
        ),
        Control(
            "A01-3",
            "Tenant isolation with non-disclosing responses",
            "ENFORCED",
            "utp.services.authz:AuthorizationService.assert_same_tenant",
            ("R1.1", "R1.2", "R44.6"),
            "Cross-tenant reads return the same NotFound as genuine absence, and audit.",
        ),
        Control(
            "A01-4",
            "IDOR prevention: every record load is tenant-scoped by id",
            "ENFORCED",
            "utp.services.authz:AuthorizationService.load_scoped",
            ("R1.2", "R42.9"),
            "Identifiers are unguessable and scoping is applied on load, not on render.",
        ),
        Control(
            "A01-5",
            "Venue scope on records, lists, aggregates and exports",
            "ENFORCED",
            "utp.services.authz:AuthorizationService.scoped_venue_ids",
            ("R43.3", "R43.4", "R43.7", "R42.10"),
            "Out-of-scope data cannot contribute to a figure the principal can see.",
        ),
        Control(
            "A01-6",
            "Privilege-escalation guards on grants and assignments",
            "ENFORCED",
            "utp.services.authz:AuthorizationService.assert_may_grant",
            ("R44.2", "R44.3", "R44.4", "R44.11"),
            "No self-modification; cannot grant unheld permissions or exceed own authority.",
        ),
        Control(
            "A01-7",
            "Mass-assignment protection on protected fields",
            "ENFORCED",
            "utp.services.authz:AuthorizationService.assert_no_forbidden_fields",
            ("R42.12", "R44.11"),
            "tenant_id, authority_level, counters and permission fields are never settable.",
        ),
        Control(
            "A01-8",
            "CSRF protection bound to the session",
            "ENFORCED",
            "utp.security.csrf:CsrfProtection.require",
            ("R73.7",),
            "Signed double-submit; safe methods and bearer-auth endpoints exempt.",
        ),
        Control(
            "A01-9",
            "Open-redirect prevention",
            "ENFORCED",
            "utp.security.ssrf:safe_redirect_target",
            ("R73.7",),
            "Relative same-site paths only; protocol-relative URLs rejected explicitly.",
        ),
        Control(
            "A01-10",
            "Booking lookup resists enumeration",
            "ENFORCED",
            "utp.services.booking:BookingService.request_access_code",
            ("R16.2", "R16.3"),
            "Identical response for unknown and known bookings; ownership proven by code.",
        ),
        Control(
            "A01-11",
            "Financial and audit records cannot be erased by any DELETE grant",
            "ENFORCED",
            "utp.core.schema:PROTECTED_TABLES",
            ("R46.1", "R46.2", "R46.6"),
            "Database triggers abort deletion, so no API path or import can bypass it.",
        ),
    ),
)

A02 = Category(
    id="A02",
    title="Cryptographic Failures",
    summary=(
        "No card data is ever stored. Personal data is separated and masked. Signing "
        "and encryption keys are resolved at runtime and carry rotation labels."
    ),
    controls=(
        Control(
            "A02-1",
            "No raw card data stored anywhere",
            "ENFORCED",
            "utp.services.payments:PaymentService",
            ("R14.2", "R73.11"),
            "There is no column for PAN, CVV or track data in the schema.",
        ),
        Control(
            "A02-2",
            "QR payloads are opaque, signed and free of personal data",
            "ENFORCED",
            "utp.services.tickets:build_qr_payload",
            ("R15.2",),
            "32 bytes of entropy plus a detached HMAC; forged codes fail before any query.",
        ),
        Control(
            "A02-3",
            "Personal data separated from operational rows and masked by default",
            "ENFORCED",
            "utp.services.customers:CustomerService.get",
            ("R12.24", "R42.9", "R38.9"),
            "Unmasked reads require VIEW_PII and are themselves audited.",
        ),
        Control(
            "A02-4",
            "Credentials stored as PBKDF2-HMAC-SHA256 with per-value salt",
            "ENFORCED",
            "utp.core.ids:hash_secret",
            ("R73.1",),
            "Applied to staff credentials, device secrets, partner keys and one-time codes.",
        ),
        Control(
            "A02-5",
            "Secrets resolved at runtime, never committed, rotation-aware",
            "ENFORCED",
            "utp.security.secrets:SecretProvider",
            ("R73.9",),
            "Ciphertext and signatures carry a key id so rotation is incremental.",
        ),
        Control(
            "A02-6",
            "Field-level encryption available for columns needing it",
            "PARTIAL",
            "utp.security.secrets:FieldCipher",
            ("R73.8",),
            "Encrypt-then-MAC using only the standard library.",
            gap="Not AES-GCM. Production must back this with KMS; the register states so.",
        ),
        Control(
            "A02-7",
            "Encryption in transit and at rest",
            "INFRASTRUCTURE",
            None,
            ("R73.8", "R75.3", "R75.4"),
            "TLS at CloudFront/ALB; RDS, S3, backups, queues and logs KMS-encrypted.",
        ),
        Control(
            "A02-8",
            "Lookup indexes are keyed hashes, not readable directories",
            "ENFORCED",
            "utp.core.ids:hash_identifier",
            ("R12.24",),
            "Email lookup uses a keyed BLAKE2b digest.",
        ),
    ),
)

A03 = Category(
    id="A03",
    title="Injection",
    summary=(
        "Every value is bound as a parameter. The few structural names that reach SQL "
        "text come from an allow-list. Output encoding is chosen by destination."
    ),
    controls=(
        Control(
            "A03-1",
            "Parameterized queries throughout",
            "ENFORCED",
            "utp.core.db:Database.execute",
            ("R73.6", "R73.7"),
            "No caller concatenates a value into SQL.",
        ),
        Control(
            "A03-2",
            "Allow-listed identifiers for dynamic table and column names",
            "ENFORCED",
            "utp.security.validation:safe_identifier",
            ("R73.6",),
            "Covers sort parameters, which are a common injection route.",
        ),
        Control(
            "A03-3",
            "Allow-list request validation rejecting unexpected fields",
            "ENFORCED",
            "utp.security.validation:Schema.validate",
            ("R73.6", "R42.12"),
            "Unicode NFC normalization and control-character stripping applied first.",
        ),
        Control(
            "A03-4",
            "Context-aware output encoding",
            "ENFORCED",
            "utp.security.validation:encode_html",
            ("R73.7",),
            "Separate encoders for HTML, attributes, JS strings and URL components.",
        ),
        Control(
            "A03-5",
            "Notification templates use an allow-list, not an expression evaluator",
            "ENFORCED",
            "utp.services.notifications:NotificationService.validate_template",
            ("R37.3", "R37.7"),
            "Answers residual risk D.7: there is no sandbox to escape.",
        ),
        Control(
            "A03-6",
            "CSV formula injection neutralised in exports",
            "ENFORCED",
            "utp.security.validation:encode_csv_cell",
            ("R41.7", "R71.9"),
            "Cells beginning =, +, - or @ are prefixed so they are not executed.",
        ),
        Control(
            "A03-7",
            "Log injection prevented by stripping control characters",
            "ENFORCED",
            "utp.security.validation:sanitize_log_value",
            ("R74.1",),
            "Prevents forged log lines and header splitting.",
        ),
    ),
)

A04 = Category(
    id="A04",
    title="Insecure Design",
    summary=(
        "The abuse cases from the requirements analysis are treated as design "
        "constraints: inventory denial, code brute force, and money-affecting overrides."
    ),
    controls=(
        Control(
            "A04-1",
            "No-oversell enforced at the data layer, not only in service code",
            "ENFORCED",
            "utp.core.schema:TABLES",
            ("R10.5", "R10.6", "R57.9"),
            "CHECK constraint plus partial unique indexes plus conditional increments.",
        ),
        Control(
            "A04-2",
            "Seat and capacity hold quotas prevent inventory denial",
            "ENFORCED",
            "utp.security.ratelimit:RateLimiter.assert_hold_quota",
            ("R73.5",),
            "Answers residual risk D.5: bounds outstanding holds, not just request rate.",
        ),
        Control(
            "A04-3",
            "Promotion code brute force bounded and non-disclosing",
            "ENFORCED",
            "utp.security.ratelimit:QUOTAS",
            ("R13.5", "R73.5"),
            "Answers D.6; rejection reasons never reveal unrelated promotions.",
        ),
        Control(
            "A04-4",
            "Money-affecting actions gated separately from CRUD and require a reason",
            "ENFORCED",
            "utp.domain.permissions:ACTIONS",
            ("R41.1", "R41.3", "R67.4"),
            "Holding Bookings.EDIT never confers REFUND, VOID or CANCEL_BOOKING.",
        ),
        Control(
            "A04-5",
            "Refunds cannot exceed what was collected, in aggregate",
            "ENFORCED",
            "utp.services.booking:BookingService.refund",
            ("R17.6",),
            "Enforced in service code and by a CHECK on bookings.refunded_minor.",
        ),
        Control(
            "A04-6",
            "Payment taken but inventory gone resolves without silent loss",
            "ENFORCED",
            "utp.services.booking:BookingService._payment_without_inventory",
            ("R10.8", "R10.9", "R57.12"),
            "Booking goes to RECONCILIATION, refund or void starts, customer is told.",
        ),
        Control(
            "A04-7",
            "Second-authorization flow for high-authority changes",
            "ENFORCED",
            "utp.services.staff:StaffService._require_second_approval",
            ("R41.5", "R44.10"),
            "Approver must hold APPROVE and must differ from the requester.",
        ),
        Control(
            "A04-8",
            "Sensitive actions require informed confirmation before execution",
            "ENFORCED",
            "utp.core.errors:ConfirmationRequired",
            ("R67.1", "R67.2", "R67.6"),
            "The confirmation states scope, amounts, reversibility and the real action.",
        ),
    ),
)

A05 = Category(
    id="A05",
    title="Security Misconfiguration",
    summary=(
        "Strict response headers, nonce-based CSP, host-only cookies, an explicit CORS "
        "allow-list, and errors that never leak internals."
    ),
    controls=(
        Control(
            "A05-1",
            "Nonce-based Content Security Policy",
            "ENFORCED",
            "utp.security.headers:SecurityHeaderPolicy.csp",
            ("R73.7",),
            "No unsafe-inline for script; the UI is built to fit the policy.",
        ),
        Control(
            "A05-2",
            "Hardened response header set",
            "ENFORCED",
            "utp.security.headers:SecurityHeaderPolicy.headers",
            ("R73.7", "R73.8"),
            "HSTS with preload, nosniff, DENY framing, restrictive Permissions-Policy.",
        ),
        Control(
            "A05-3",
            "Secure cookie attributes; session cookie is host-only",
            "ENFORCED",
            "utp.security.headers:SESSION_COOKIE",
            ("R73.3",),
            "Secure, HttpOnly, SameSite=Strict, no Domain attribute.",
        ),
        Control(
            "A05-4",
            "CORS allow-list with no wildcard alongside credentials",
            "ENFORCED",
            "utp.security.headers:SecurityHeaderPolicy.cors_headers",
            ("R73.7",),
            "An unvetted origin is never reflected.",
        ),
        Control(
            "A05-5",
            "Errors expose no SQL, stack traces, provider payloads or internal names",
            "ENFORCED",
            "utp.core.errors:PlatformError.public_dict",
            ("R66.4", "R66.5", "R42.3"),
            "Detail goes to the server log with a correlation id.",
        ),
        Control(
            "A05-6",
            "Start-up secret completeness check",
            "ENFORCED",
            "utp.security.secrets:verify_configuration",
            ("R73.9",),
            "A missing signing key refuses deployment rather than failing at the gate.",
        ),
        Control(
            "A05-7",
            "Environment separation with no shared data",
            "INFRASTRUCTURE",
            None,
            ("R75.10", "R75.11"),
            "Separate dev/staging/production, provisioned by infrastructure as code.",
        ),
        Control(
            "A05-8",
            "WAF, bot and DDoS mitigation on public endpoints",
            "INFRASTRUCTURE",
            None,
            ("R73.10", "R75.3"),
            "AWS WAF in front of CloudFront; TLS terminated at the managed edge.",
        ),
        Control(
            "A05-9",
            "Private data in private subnets or managed services",
            "INFRASTRUCTURE",
            None,
            ("R75.4",),
            "No public network exposure for RDS or the media bucket.",
        ),
    ),
)

A06 = Category(
    id="A06",
    title="Vulnerable and Outdated Components",
    summary=(
        "The runtime dependency surface is deliberately near-zero: the platform core "
        "uses only the standard library. Driver and SDK dependencies are pinned."
    ),
    controls=(
        Control(
            "A06-1",
            "Minimal runtime dependency surface",
            "ENFORCED",
            "utp:__version__",
            ("R73.15",),
            "Core domain, capacity and authorization logic import nothing third-party.",
        ),
        Control(
            "A06-2",
            "Pinned dependency manifest",
            "PARTIAL",
            None,
            ("R73.15",),
            "psycopg and boto3 are the only production additions.",
            gap="requirements.txt with hashes and a lockfile still to be committed.",
        ),
        Control(
            "A06-3",
            "Dependency and image scanning blocking release on critical findings",
            "EXTERNAL",
            None,
            ("R73.15",),
            "Belongs in the build pipeline; cannot be enforced from application code.",
        ),
    ),
)

A07 = Category(
    id="A07",
    title="Identification and Authentication Failures",
    summary=(
        "Strong password policy, throttling and lockout, mandatory MFA for privileged "
        "roles, short-lived bound sessions, and immediate revocation."
    ),
    controls=(
        Control(
            "A07-1",
            "Password policy on length and character variety",
            "ENFORCED",
            "utp.services.staff:StaffService._validate_credential",
            ("R73.1",),
            "Twelve characters minimum; no forced rotation, which drives weak patterns.",
        ),
        Control(
            "A07-2",
            "Account lockout and throttling after repeated failures",
            "ENFORCED",
            "utp.services.staff:StaffService.login",
            ("R73.1", "R73.5"),
            "Five failures locks for fifteen minutes; every failure is audited.",
        ),
        Control(
            "A07-3",
            "MFA mandatory for Platform Super Admin and Organization Admin",
            "ENFORCED",
            "utp.domain.permissions:HIGH_AUTHORITY_ROLE_CODES",
            ("R73.2",),
            "Derived from effective roles at login, not from a per-user flag alone.",
        ),
        Control(
            "A07-4",
            "Sessions bound to tenant and principal with idle and absolute timeouts",
            "ENFORCED",
            "utp.services.staff:StaffService.authenticate_token",
            ("R73.3", "R44.12"),
            "Tokens are stored hashed; no authoritative grant travels in the token.",
        ),
        Control(
            "A07-5",
            "Immediate session revocation on suspend or deactivate",
            "ENFORCED",
            "utp.services.staff:StaffService.revoke_sessions",
            ("R38.5", "R44.9"),
            "Revocation happens in the same transaction as the status change.",
        ),
        Control(
            "A07-6",
            "Permission changes take effect on the next request",
            "ENFORCED",
            "utp.services.authz:AuthorizationService.effective_permissions",
            ("R44.7", "R44.8"),
            "No cross-request permission cache exists to invalidate.",
        ),
        Control(
            "A07-7",
            "Single-use, short-lived enrolment and verification codes",
            "ENFORCED",
            "utp.services.booking:BookingService.verify_access",
            ("R16.11", "R38.4"),
            "Consumed on use; attempts counted; stored hashed.",
        ),
        Control(
            "A07-8",
            "Devices individually authenticated and remotely revocable",
            "ENFORCED",
            "utp.services.tenancy:TenancyService.deactivate_device",
            ("R32.12", "R73.12"),
            "Revoking a device also erases the offline cache issued to it.",
        ),
        Control(
            "A07-9",
            "Breached-password screening",
            "PARTIAL",
            None,
            ("R73.1",),
            "Policy is enforced locally.",
            gap="No k-anonymity check against a breach corpus; needs an approved provider.",
        ),
    ),
)

A08 = Category(
    id="A08",
    title="Software and Data Integrity Failures",
    summary=(
        "Webhooks are signature-verified and processed exactly once. Uploads are "
        "identified by content. Audit and consent records are append-only."
    ),
    controls=(
        Control(
            "A08-1",
            "Payment webhooks verified and processed idempotently",
            "ENFORCED",
            "utp.services.payments:PaymentService.handle_webhook",
            ("R14.4", "R14.7"),
            "Unsigned callbacks rejected and audited; the event row is the dedupe key.",
        ),
        Control(
            "A08-2",
            "Payment idempotency prevents double charging",
            "ENFORCED",
            "utp.services.payments:PaymentService.start_payment",
            ("R14.3", "R14.5"),
            "UNIQUE idempotency key per tenant; a replay returns the original result.",
        ),
        Control(
            "A08-3",
            "Uploads validated by magic bytes, with SVG rejected",
            "ENFORCED",
            "utp.security.uploads:validate_bytes",
            ("R73.6",),
            "Declared type must match actual; polyglot markers rejected.",
        ),
        Control(
            "A08-4",
            "Storage keys generated by the platform, never from client filenames",
            "ENFORCED",
            "utp.security.uploads:validate_upload_request",
            ("R73.6",),
            "Removes traversal and cross-tenant overwrite structurally.",
        ),
        Control(
            "A08-5",
            "Audit and consent records are append-only",
            "ENFORCED",
            "utp.core.schema:APPEND_ONLY_TABLES",
            ("R45.3", "R12.11", "R12.12"),
            "UPDATE and DELETE both abort at the database.",
        ),
        Control(
            "A08-6",
            "Offline gate caches are signed and age-bounded",
            "ENFORCED",
            "utp.core.ids:sign_payload",
            ("R32.6", "R32.7"),
            "A tampered cache fails verification; a stale one warns the operator.",
        ),
        Control(
            "A08-7",
            "Published seat layouts and seat identities are immutable",
            "ENFORCED",
            "utp.core.schema:triggers",
            ("R53.3", "R61.1", "R61.2"),
            "A confirmed reservation cannot be detached by editing a layout.",
        ),
        Control(
            "A08-8",
            "Gap-controlled, duplicate-free tax invoice numbering",
            "ENFORCED",
            "utp.services.documents:DocumentService._allocate_number",
            ("R72.3", "R72.5"),
            "Allocated under a write lock; corrections are credit notes.",
        ),
    ),
)

A09 = Category(
    id="A09",
    title="Security Logging and Monitoring Failures",
    summary=(
        "An immutable audit trail plus threshold detection over it, so the record is "
        "both complete and actually watched."
    ),
    controls=(
        Control(
            "A09-1",
            "Comprehensive audit trail with actor, scope, correlation and both clocks",
            "ENFORCED",
            "utp.core.audit:AuditLog.record",
            ("R45.1", "R45.2", "R45.7"),
            "UTC and venue-local timestamps; correlation id ties one operation together.",
        ),
        Control(
            "A09-2",
            "Secrets and unmasked personal data never enter audit payloads",
            "ENFORCED",
            "utp.core.audit:AuditLog",
            ("R45.9", "R74.9"),
            "Redaction is applied to every payload, not left to callers.",
        ),
        Control(
            "A09-3",
            "Failed authorization attempts recorded with probing detail",
            "ENFORCED",
            "utp.services.authz:AuthorizationService._deny",
            ("R45.8", "R42.3"),
            "Records the required permission privately while returning a generic error.",
        ),
        Control(
            "A09-4",
            "Threshold detection for the named attack patterns",
            "ENFORCED",
            "utp.security.monitoring:SecurityMonitor.evaluate",
            ("R73.14",),
            "Credential stuffing, authz probing, abnormal refunds and exports.",
        ),
        Control(
            "A09-5",
            "Periodic override review report",
            "ENFORCED",
            "utp.security.monitoring:SecurityMonitor.override_review",
            ("R45.2",),
            "Answers residual risk D.2: auditing only deters if somebody looks.",
        ),
        Control(
            "A09-6",
            "Partner volume anomaly detection",
            "ENFORCED",
            "utp.security.monitoring:SecurityMonitor.partner_anomalies",
            ("R35.3",),
            "Answers residual risk D.3 on partner credential compromise.",
        ),
        Control(
            "A09-7",
            "Alert delivery to an on-call channel",
            "EXTERNAL",
            "utp.security.monitoring:SecurityMonitor",
            ("R74.3",),
            "The sink is injected; SNS or PagerDuty wiring is deployment configuration.",
        ),
    ),
)

A10 = Category(
    id="A10",
    title="Server-Side Request Forgery",
    summary=(
        "Outbound destinations are allow-listed, resolved, and checked against private "
        "and metadata address ranges before any connection is made."
    ),
    controls=(
        Control(
            "A10-1",
            "Outbound URL guard failing closed",
            "ENFORCED",
            "utp.security.ssrf:assert_safe_url",
            ("R73.7",),
            "HTTPS only, host allow-list, blocked ports, credentials-in-URL rejected.",
        ),
        Control(
            "A10-2",
            "Private, loopback, link-local and metadata addresses blocked",
            "ENFORCED",
            "utp.security.ssrf:is_private_address",
            ("R73.7",),
            "Every resolved answer must be public, not merely the first.",
        ),
        Control(
            "A10-3",
            "DNS-rebinding resistance by pinning the checked address",
            "ENFORCED",
            "utp.security.ssrf:SafeTarget",
            ("R73.7",),
            "The caller connects to the verified IP and passes the hostname as Host.",
        ),
        Control(
            "A10-4",
            "Egress restriction at the network layer",
            "INFRASTRUCTURE",
            None,
            ("R75.4",),
            "Security groups and NAT egress rules limit destinations independently.",
        ),
    ),
)

REGISTER: tuple[Category, ...] = (A01, A02, A03, A04, A05, A06, A07, A08, A09, A10)

CATEGORIES_BY_ID: dict[str, Category] = {c.id: c for c in REGISTER}


def all_controls() -> tuple[Control, ...]:
    return tuple(control for category in REGISTER for control in category.controls)


def verify_register() -> dict[str, object]:
    """Import every referenced implementation and report anything missing.

    This is what stops the register drifting from the code. A renamed method or a
    deleted module shows up here as a broken reference.
    """
    broken: list[dict[str, str]] = []
    checked = 0
    for control in all_controls():
        if not control.implementation:
            continue
        checked += 1
        module_name, _, symbol = control.implementation.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            broken.append({"control": control.id, "reference": control.implementation, "error": str(exc)})
            continue
        target: object = module
        for part in symbol.split("."):
            if not part:
                continue
            if not hasattr(target, part):
                broken.append(
                    {
                        "control": control.id,
                        "reference": control.implementation,
                        "error": f"missing attribute {part!r}",
                    }
                )
                target = None
                break
            target = getattr(target, part)
    missing_gaps = [c.id for c in all_controls() if c.status == "PARTIAL" and not c.gap]
    return {
        "controls_total": len(all_controls()),
        "controls_with_code": checked,
        "broken_references": broken,
        "partial_without_stated_gap": missing_gaps,
        "valid": not broken and not missing_gaps,
    }


def coverage_by_status() -> dict[str, int]:
    counts: dict[str, int] = {}
    for control in all_controls():
        counts[control.status] = counts.get(control.status, 0) + 1
    return counts


def requirement_index() -> dict[str, list[str]]:
    """Requirement id -> the controls that satisfy it, for the traceability matrix."""
    index: dict[str, list[str]] = {}
    for control in all_controls():
        for requirement in control.requirements:
            index.setdefault(requirement, []).append(control.id)
    return {key: sorted(value) for key, value in sorted(index.items())}


def report() -> str:
    """Human-readable register, for a security review pack."""
    lines = ["OWASP Top 10 (2021) control register", "=" * 38, ""]
    counts = coverage_by_status()
    lines.append(
        "Controls: "
        + ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
        + f" (total {len(all_controls())})"
    )
    lines.append("")
    for category in REGISTER:
        lines.append(f"{category.id} {category.title}")
        lines.append("-" * (len(category.id) + len(category.title) + 1))
        if category.summary:
            lines.append(category.summary)
            lines.append("")
        for control in category.controls:
            lines.append(f"  [{control.status:<14}] {control.id} {control.title}")
            if control.implementation:
                lines.append(f"                    code: {control.implementation}")
            if control.requirements:
                lines.append(f"                    reqs: {', '.join(control.requirements)}")
            if control.notes:
                lines.append(f"                    note: {control.notes}")
            if control.gap:
                lines.append(f"                    GAP:  {control.gap}")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "CATEGORIES_BY_ID",
    "REGISTER",
    "Category",
    "Control",
    "Status",
    "all_controls",
    "coverage_by_status",
    "report",
    "requirement_index",
    "verify_register",
]
