"""Platform assembly.

One object wires the services together and owns the shared infrastructure
(database, clock, audit log, configuration store, security layer). Tests, the HTTP
API, the provisioning script and background jobs all construct the same object, which
is why an enforcement rule proved in a test is the same rule the API runs.

Cross-service references that would otherwise be circular are injected *after*
construction rather than passed to constructors — for example the calendar reads
availability from the inventory service, and booking publishes through the
notification service. Each of those attributes is documented on the receiving class so
the dependency is visible from either side.

Implementation status
---------------------
The services below are implemented and covered by the suite. Seating, gate access,
counter POS, partner API and reporting are specified but not yet written; the
composition root does not pretend otherwise, and the optional hooks that would use
them (``booking.seating``, for instance) stay ``None`` until they exist.
"""

from __future__ import annotations

from typing import Any

from .core.audit import AuditLog
from .core.clock import Clock, SystemClock
from .core.config import ConfigStore
from .core.context import RequestContext, guest_context, system_context
from .core.db import Database
from .security.csrf import CsrfProtection
from .security.headers import SecurityHeaderPolicy, default_policy
from .security.monitoring import SecurityMonitor
from .security.ratelimit import RateLimiter
from .security.secrets import EnvironmentSecretProvider, SecretProvider
from .security.ssrf import OutboundPolicy
from .reporting.service import ReportingService
from .services.access import AccessService
from .services.counter import CounterService
from .services.payment_types import PaymentTypeService
from .services.authz import AuthorizationService
from .services.booking import BookingService
from .services.calendar_rules import CalendarService
from .services.catalog import CatalogService
from .services.consent import ConsentService
from .services.customers import CustomerService
from .services.documents import DocumentService
from .services.inventory import InventoryService
from .services.members import MemberService
from .services.notifications import NotificationService, SimulatedEmailProvider, SmtpEmailProvider
from .services.payments import PaymentService, SimulatedProvider
from .services.pricing import PricingService
from .services.promotions import PromotionService
from .services.settings import SettingsService
from .services.settings_pages import SettingsConfigService
from .services.shows import ShowService
from .services.staff import StaffService
from .services.tenancy import TenancyService
from .services.tickets import TicketService


class Platform:
    """Composition root."""

    def __init__(
        self,
        *,
        db_path: str = ":memory:",
        clock: Clock | None = None,
        migrate: bool = True,
        payment_provider: Any = None,
        email_provider: Any = None,
        secret_provider: SecretProvider | None = None,
        header_policy: SecurityHeaderPolicy | None = None,
        outbound_policy: OutboundPolicy | None = None,
        alert_sink: Any = None,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.db = Database(db_path, clock=self.clock)
        if migrate:
            self.db.migrate()

        self.audit = AuditLog(self.db, self.clock)
        self.config = ConfigStore(self.db, self.clock, self.audit)
        self.authz = AuthorizationService(self.db, self.clock, self.audit, self.config)

        # --- security layer --------------------------------------------- #
        # Constructed before the services so a service can take a limiter, and so a
        # deployment fails at start-up rather than at first use if secrets are absent.
        self.secrets: SecretProvider = secret_provider or EnvironmentSecretProvider()
        self.header_policy = header_policy or default_policy()
        self.outbound_policy = outbound_policy or OutboundPolicy()
        self.csrf = CsrfProtection(clock=self.clock)
        self.rate_limiter = RateLimiter(self.db, self.clock, config=self.config, audit=self.audit)
        self.security_monitor = SecurityMonitor(
            self.db, self.clock, config=self.config, sink=alert_sink
        )

        common = (self.db, self.clock, self.audit, self.authz, self.config)

        # --- configuration & catalog ------------------------------------ #
        self.tenancy = TenancyService(*common)
        self.catalog = CatalogService(*common)
        self.staff = StaffService(*common)
        self.pricing = PricingService(*common)
        self.settings = SettingsService(*common)
        self.settings_pages = SettingsConfigService(*common)
        self.payment_types = PaymentTypeService(*common)
        self.calendar = CalendarService(*common)
        self.inventory = InventoryService(*common)
        self.promotions = PromotionService(*common)

        # --- privacy & identity ----------------------------------------- #
        self.consent = ConsentService(*common)
        self.customers = CustomerService(*common, consent=self.consent)
        self.members = MemberService(*common)

        # --- commerce ---------------------------------------------------- #
        self.payments = PaymentService(*common, provider=payment_provider or SimulatedProvider())
        self.tickets = TicketService(*common)
        self.documents = DocumentService(*common)
        # Real email when SMTP is configured (UTP_SMTP_HOST etc.), the simulated
        # in-app mailbox otherwise. An explicitly injected provider always wins, so
        # tests stay deterministic.
        self.notifications = NotificationService(
            *common,
            provider=email_provider or SmtpEmailProvider.from_env() or SimulatedEmailProvider(),
        )
        self.shows = ShowService(
            *common, inventory=self.inventory, catalog=self.catalog, calendar=self.calendar
        )
        self.booking = BookingService(
            *common,
            catalog=self.catalog,
            pricing=self.pricing,
            calendar=self.calendar,
            inventory=self.inventory,
            promotions=self.promotions,
            consent=self.consent,
            customers=self.customers,
            payments=self.payments,
            tickets=self.tickets,
        )

        # --- gate access ------------------------------------------------- #
        self.access = AccessService(*common)

        # --- counter POS ------------------------------------------------- #
        self.counter = CounterService(*common)

        # --- reporting, analytics and the two dashboards ------------------ #
        self.reporting = ReportingService(*common)

        # --- specified but not yet implemented --------------------------- #
        # Left explicitly None so a caller gets an obvious AttributeError rather than
        # silently skipping a control that is supposed to exist.
        self.seating = None
        self.partners = None

        self._wire()

    def _wire(self) -> None:
        """Inject post-construction references (documented on each receiver)."""
        # The calendar must never compute availability itself; it reads the
        # authoritative inventory numbers so cart and calendar cannot disagree (R10.11).
        self.calendar.availability_fn = self.inventory.availability_snapshot

        self.booking.notifications = self.notifications
        self.booking.documents = self.documents
        self.booking.seating = self.seating

        self.notifications.booking = self.booking
        self.notifications.tickets = self.tickets
        # The designed HTML e-ticket. Injected as a callable so the notification
        # service depends on no presentation code and a text-only tenant simply
        # leaves this unwired.
        self.notifications.eticket_renderer = self._render_eticket_html

        # A captured payment completes the booking and emails the e-ticket, whether the
        # capture came from the inline checkout or from a gateway webhook arriving after
        # the customer's browser closed (R14.6, R14.7).
        self.payments.on_payment_captured = lambda ctx, payment: self.booking.finalize_paid_booking(
            ctx, booking_id=payment["booking_id"], payment_id=payment["payment_id"]
        )

        self.shows.notifications = self.notifications
        self.shows.customers = self.customers

        # Ticket validity follows the venue's configured policy (settings spec §11),
        # so the issuer reads it from the settings service rather than hard-coding
        # per-model expiry. Booking computes VAT and service charge from the same
        # service so the rate stored on an order is the effective rate on that date.
        self.tickets.settings = self.settings
        self.booking.settings = self.settings

        # Loyalty points redeemed at checkout settle the bill like a gift card
        # (add_features §32, §69); booking calls the member service to spend them
        # exactly once inside the confirmed transaction.
        self.booking.members = self.members

        # Gate validation reads tickets and authenticates devices; injected here so
        # the access service, tickets and tenancy are not a construction cycle (R32).
        self.access.tickets = self.tickets
        self.access.tenancy = self.tenancy

        # The counter drives sales through the booking service and reconciles cash via
        # payments; injected here so counter → booking → payments is not a cycle (R34).
        self.counter.booking = self.booking
        self.counter.payments = self.payments
        self.counter.settings = self.settings

        # Reporting reads through the database directly for aggregation, but wants
        # tenancy for venue naming.
        self.reporting.tenancy = self.tenancy

    # ------------------------------------------------------------------ #
    # Ticket artefacts
    # ------------------------------------------------------------------ #

    def ticket_payload(self, ctx: RequestContext, *, ticket_id: str, language: str | None = None):
        """The render payload behind both the e-ticket and the thermal ticket."""
        from .ticketdesign import ticket_render_payload

        return ticket_render_payload(self, ctx, ticket_id=ticket_id, language=language)

    def render_eticket(
        self,
        ctx: RequestContext,
        *,
        ticket_id: str,
        language: str | None = None,
        auto_print: bool = False,
    ) -> str:
        from .ticketdesign import render_email_ticket

        return render_email_ticket(
            self.ticket_payload(ctx, ticket_id=ticket_id, language=language),
            auto_print=auto_print,
        )

    def render_thermal_ticket(
        self,
        ctx: RequestContext,
        *,
        ticket_id: str,
        language: str | None = None,
        auto_print: bool = False,
    ) -> str:
        from .ticketdesign import render_thermal_ticket

        return render_thermal_ticket(
            self.ticket_payload(ctx, ticket_id=ticket_id, language=language),
            auto_print=auto_print,
        )

    def _render_eticket_html(
        self, ctx: RequestContext, booking_id: str, language: str | None = None
    ) -> tuple[str, dict[str, bytes]] | None:
        """Every ticket in a booking, one card each, as one HTML document.

        A family buying four tickets gets four scannable codes in the message
        rather than one code and three guests turned away at the gate (R15.1).

        How the QR travels is configuration (``notification.qr_delivery``), because
        it is a deliverability question rather than a design one: ``CID`` attaches
        the image to the message, which is the only mode every client renders —
        Gmail strips ``data:`` URLs. Returns the HTML and any images to attach.
        """
        from .ticketdesign.email_ticket import CidSource, qr_source_for, render_eticket_document
        from .ticketdesign.payload import ticket_render_payload

        tickets = self.tickets.list_for_booking(ctx, booking_id)
        if not tickets:
            return None
        payloads = [
            ticket_render_payload(self, ctx, ticket_id=ticket["id"], language=language)
            for ticket in tickets
        ]
        mode = self.config.get(ctx, "notification.qr_delivery") or "CID"
        base_url = self.config.get(ctx, "notification.public_base_url") or ""
        source = qr_source_for(str(mode), base_url=str(base_url))
        html = render_eticket_document(payloads, qr_source=source)
        images = dict(source.images) if isinstance(source, CidSource) else {}
        return html, images

    # ------------------------------------------------------------------ #
    # Context helpers
    # ------------------------------------------------------------------ #

    def system_context(self, tenant_id: str, *, venue_id: str | None = None) -> RequestContext:
        return system_context(tenant_id, venue_id=venue_id)

    def guest_context(self, tenant_id: str, **kwargs: Any) -> RequestContext:
        return guest_context(tenant_id, **kwargs)

    # ------------------------------------------------------------------ #
    # Security posture
    # ------------------------------------------------------------------ #

    def security_posture(self, tenant_id: str | None = None) -> dict[str, Any]:
        """Machine-readable security state, for a review pack or a health endpoint.

        Deliberately reports the *unverified* parts too: a posture report that only
        lists successes is worse than none.
        """
        from .security import owasp, secrets as secrets_module

        register = owasp.verify_register()
        configuration = secrets_module.verify_configuration(self.secrets)
        posture: dict[str, Any] = {
            "owasp_register": {
                "controls_total": register["controls_total"],
                "coverage": owasp.coverage_by_status(),
                "valid": register["valid"],
                "broken_references": register["broken_references"],
            },
            "secrets": {
                "complete": configuration["complete"],
                "missing": configuration["missing"],
            },
            "transport": {
                "hsts_enabled": self.header_policy.enforce_https,
                "cors_allow_list": list(self.header_policy.allowed_origins),
                "csp_nonce_based": True,
            },
            "outbound": {
                "allow_list": list(self.outbound_policy.allowed_hosts),
                "insecure_permitted": self.outbound_policy.allow_insecure,
            },
            "at_rest": secrets_module.AT_REST_REQUIREMENTS,
        }
        if tenant_id:
            ctx = system_context(tenant_id)
            posture["open_security_alerts"] = self.security_monitor.open_alerts(ctx)
        return posture

    def run_security_scan(self, tenant_id: str, *, venue_id: str | None = None) -> dict[str, Any]:
        """Evaluate the monitoring detectors for one tenant (R73.14)."""
        ctx = system_context(tenant_id, venue_id=venue_id)
        alerts = self.security_monitor.evaluate(ctx)
        return {
            "alerts": [alert.as_dict() for alert in alerts],
            "override_review": self.security_monitor.override_review(ctx),
            "partner_anomalies": self.security_monitor.partner_anomalies(ctx),
        }

    # ------------------------------------------------------------------ #
    # Background maintenance
    # ------------------------------------------------------------------ #

    def run_maintenance(self, tenant_id: str, *, venue_id: str | None = None) -> dict[str, Any]:
        """One pass of every scheduled job.

        A single entry point so a deployment schedules one thing and the ordering is
        explicit: reclaim capacity first so availability is accurate, then complete
        sessions, then send anything due.
        """
        ctx = system_context(tenant_id, venue_id=venue_id)
        result: dict[str, Any] = {
            "holds_reclaimed": self.inventory.reclaim_expired_holds(ctx),
            "allocations_released": self.inventory.release_due_allocations(ctx, venue_id=venue_id),
            "sessions_completed": self.inventory.complete_due_sessions(ctx, venue_id=venue_id),
            "tickets_expired": self.tickets.expire_due(ctx),
            "schedule_materialized": self.shows.extend_materialization_horizon(ctx, venue_id=venue_id),
            "reminders": self.notifications.dispatch_reminders(ctx, customers=self.customers),
            "notifications": self.notifications.dispatch_due(ctx),
            "rate_limit_rows_purged": self.rate_limiter.purge_old_windows(),
        }
        if self.seating is not None:
            result["seat_holds_reclaimed"] = self.seating.reclaim_expired_holds(ctx)
        return result

    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Platform:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["Platform"]
