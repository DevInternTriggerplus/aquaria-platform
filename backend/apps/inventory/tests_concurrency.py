"""Real concurrency proof for the never-oversell guarantee.

These tests are the reason the project needs PostgreSQL rather than SQLite. The
capacity mechanism relies on ``SELECT ... FOR UPDATE`` to serialize competing
requests; SQLite does not implement row-level locking, so the equivalent test there
proves only that the arithmetic is right, not that it is safe under contention.

Each thread opens its own database connection and they are released simultaneously
from a barrier, so the availability check and the increment genuinely interleave.
The assertion is the invariant that matters: confirmed + held never exceeds capacity,
and exactly as many callers succeed as there were units.

Run against PostgreSQL:
    python manage.py test apps.inventory.tests_concurrency
"""

from __future__ import annotations

import datetime as dt
import threading

from django.db import connection, connections
from django.test import TransactionTestCase, skipUnlessDBFeature

from apps.catalog.models import Product
from apps.core.errors import JustSoldOut
from apps.tenancy.models import Organization, Tenant, Venue

from .models import Hold, Session, acquire_hold, confirm_hold


def _fixture():
    tenant = Tenant.objects.create(code="conc", name="Concurrency")
    org = Organization.objects.create(tenant=tenant, code="o", name="O")
    venue = Venue.objects.create(
        tenant=tenant, organization=org, code="v", name={"en": "V"},
        timezone="Asia/Bangkok", currency="THB",
    )
    product = Product.objects.create(
        tenant=tenant, venue=venue, code="ga", name={"en": "GA"},
        session_requirement="REQUIRED",
    )
    return tenant, venue, product


class OversellUnderContentionTests(TransactionTestCase):
    """The last unit is sold exactly once, however many callers race for it."""

    reset_sequences = True
    #: Threads must see committed rows from the main thread.
    databases = {"default"}

    def _race(self, *, capacity: int, contenders: int, quantity: int = 1):
        """Release ``contenders`` threads at once against ``capacity`` units.

        Returns (successes, sold_out_refusals, other_errors).
        """
        tenant, venue, product = _fixture()
        session = Session.objects.create(
            tenant=tenant, venue=venue, kind="PRODUCT", product=product,
            session_date=dt.date(2027, 5, 1), start_time=dt.time(10, 30),
            capacity=capacity,
        )

        barrier = threading.Barrier(contenders)
        successes: list[str] = []
        sold_out: list[str] = []
        other: list[str] = []
        lock = threading.Lock()

        def contend(index: int) -> None:
            try:
                # Line every thread up so they hit the row lock together.
                barrier.wait(timeout=30)
                hold = acquire_hold(
                    session=session,
                    quantity=quantity,
                    cart_ref=f"cart-{index}",
                    channel="ONLINE",
                )
                with lock:
                    successes.append(hold.id if hold else "uncapped")
            except JustSoldOut:
                with lock:
                    sold_out.append(f"t{index}")
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
                with lock:
                    other.append(f"t{index}: {type(exc).__name__}: {exc}")
            finally:
                # A thread must not leak its connection back into the pool.
                connections.close_all()

        threads = [threading.Thread(target=contend, args=(i,)) for i in range(contenders)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        session.refresh_from_db()
        return session, successes, sold_out, other

    @skipUnlessDBFeature("has_select_for_update")
    def test_ten_threads_one_unit(self):
        session, ok, sold_out, other = self._race(capacity=1, contenders=10)

        self.assertEqual(other, [], f"unexpected errors: {other}")
        # Exactly one caller may win the single unit.
        self.assertEqual(len(ok), 1, f"{len(ok)} threads got the last unit")
        self.assertEqual(len(sold_out), 9)
        # The invariant.
        self.assertLessEqual(session.confirmed_count + session.held_count, session.capacity)
        self.assertEqual(session.held_count, 1)
        self.assertEqual(session.status, "FULL")

    @skipUnlessDBFeature("has_select_for_update")
    def test_twenty_threads_five_units(self):
        session, ok, sold_out, other = self._race(capacity=5, contenders=20)

        self.assertEqual(other, [], f"unexpected errors: {other}")
        self.assertEqual(len(ok), 5, f"{len(ok)} succeeded, expected exactly 5")
        self.assertEqual(len(sold_out), 15)
        self.assertEqual(session.held_count, 5)
        self.assertLessEqual(session.confirmed_count + session.held_count, session.capacity)

    @skipUnlessDBFeature("has_select_for_update")
    def test_partial_group_requests_never_oversell(self):
        """Threads asking for 2 units each against a capacity of 5.

        At most two can succeed (4 units); the fifth unit is not enough for a third
        request, so it must be refused rather than partially filled.
        """
        session, ok, sold_out, other = self._race(capacity=5, contenders=8, quantity=2)

        self.assertEqual(other, [], f"unexpected errors: {other}")
        self.assertEqual(len(ok), 2, f"{len(ok)} succeeded, expected exactly 2")
        self.assertEqual(session.held_count, 4)
        self.assertLessEqual(session.confirmed_count + session.held_count, session.capacity)

    @skipUnlessDBFeature("has_select_for_update")
    def test_confirmations_race_without_exceeding_capacity(self):
        """Confirm every hold concurrently; confirmed_count must land exactly on capacity."""
        tenant, venue, product = _fixture()
        session = Session.objects.create(
            tenant=tenant, venue=venue, kind="PRODUCT", product=product,
            session_date=dt.date(2027, 5, 2), start_time=dt.time(11, 0), capacity=6,
        )
        holds = [
            acquire_hold(session=session, quantity=1, cart_ref=f"c{i}", channel="ONLINE")
            for i in range(6)
        ]

        barrier = threading.Barrier(len(holds))
        errors: list[str] = []
        lock = threading.Lock()

        def finish(hold: Hold) -> None:
            try:
                barrier.wait(timeout=30)
                confirm_hold(hold)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                connections.close_all()

        threads = [threading.Thread(target=finish, args=(h,)) for h in holds]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        session.refresh_from_db()
        self.assertEqual(errors, [], f"unexpected errors: {errors}")
        self.assertEqual(session.confirmed_count, 6)
        self.assertEqual(session.held_count, 0)
        self.assertEqual(session.remaining, 0)


class ConfirmPathContentionTests(TransactionTestCase):
    """Two customers race the whole confirm flow for the last seat.

    This is the end-to-end version of the oversell test: not just the hold, but
    quote → consent → payment → confirm, run concurrently against one remaining
    unit. Exactly one booking may confirm; the other must not, and capacity must not
    be exceeded.
    """

    reset_sequences = True
    databases = {"default"}

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_confirms_one_seat(self):
        import datetime as dt

        from apps.catalog.models import CustomerSegment, Product, TicketType
        from apps.pricing.models import PriceRule
        from apps.payments.gateway import SimulatedGateway
        from apps.venuesettings.models import VatSetting
        from apps.booking import consent_service
        from apps.booking.models import Booking
        from apps.booking.services import QuoteLine, confirm, quote

        tenant = Tenant.objects.create(code="race", name="Race")
        org = Organization.objects.create(tenant=tenant, code="o", name="O")
        venue = Venue.objects.create(
            tenant=tenant, organization=org, code="v", name={"en": "V"},
            timezone="Asia/Bangkok", currency="THB", tax_model="INCLUSIVE", tax_rate_bp=700,
        )
        VatSetting.objects.create(
            tenant=tenant, venue=venue, enabled=True, rate_bp=700, mode="INCLUSIVE",
            effective_from=dt.date(2026, 1, 1),
        )
        seg = CustomerSegment.objects.create(tenant=tenant, code="adult", name={"en": "Adult"})
        product = Product.objects.create(
            tenant=tenant, venue=venue, code="show", name={"en": "Show"},
            session_requirement="REQUIRED",
        )
        tt = TicketType.objects.create(
            tenant=tenant, product=product, segment=seg, code="show-adult",
            name={"en": "Adult"}, entry_allowance=1,
        )
        PriceRule.objects.create(
            tenant=tenant, venue=venue, ticket_type=tt, amount_minor=50000, currency="THB",
        )
        session = Session.objects.create(
            tenant=tenant, venue=venue, kind="PRODUCT", product=product,
            session_date=dt.date(2026, 9, 1), start_time=dt.time(14, 0), capacity=1,
        )
        consent_service.publish_notice(
            tenant=tenant, version="v1", controller_name="C", controller_contact="c@x.test",
            dpo_contact="dpo@x.test", body={},
        )

        gateway = SimulatedGateway()
        confirmed: list[str] = []
        refused: list[str] = []
        other: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def attempt(index: int) -> None:
            try:
                # The hold is taken during quote(), so the race is here. Line the
                # threads up first, then let them both try to quote the last seat.
                barrier.wait(timeout=30)
                q = quote(
                    venue=venue, visit_date=dt.date(2026, 9, 1),
                    lines=[QuoteLine(ticket_type_id=tt.id, quantity=1, session_id=session.id)],
                )
                result = confirm(
                    quote_result=q,
                    customer_data={"email": f"racer{index}@x.test", "full_name": f"Racer {index}"},
                    consent_items={"BOOKING_SERVICE": True},
                    payment_method="CARD",
                    gateway=gateway,
                    idempotency_key=f"race-{index}",
                )
                with lock:
                    (confirmed if result.get("confirmed") else refused).append(str(index))
            except JustSoldOut:
                with lock:
                    refused.append(f"soldout-{index}")
            except Exception as exc:  # noqa: BLE001
                with lock:
                    other.append(f"{index}: {type(exc).__name__}: {exc}")
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        session.refresh_from_db()
        self.assertEqual(other, [], f"unexpected errors: {other}")
        # At most one booking confirmed; capacity never exceeded.
        self.assertLessEqual(session.confirmed_count, 1)
        self.assertEqual(Booking.objects.filter(status="CONFIRMED").count(), len(confirmed))
        self.assertLessEqual(len(confirmed), 1)
        # The loser was refused at the hold, before any oversell.
        self.assertEqual(len(confirmed) + len(refused), 2)


class GateDoubleScanTests(TransactionTestCase):
    """Two scanners hit the same single-entry ticket at once (R32.3).

    Entry consumption uses select_for_update on the ticket, so exactly one scan
    admits and the other is refused as already-used — even simultaneously.
    """

    reset_sequences = True
    databases = {"default"}

    @skipUnlessDBFeature("has_select_for_update")
    def test_simultaneous_scans_admit_exactly_once(self):
        import datetime as dt

        from apps.access import services as access
        from apps.access.models import ScanEvent
        from apps.booking import consent_service
        from apps.booking.services import QuoteLine, confirm, quote
        from apps.catalog.models import CustomerSegment, Product, TicketType
        from apps.payments.gateway import SimulatedGateway
        from apps.pricing.models import PriceRule
        from apps.tenancy.models import AccessPoint
        from apps.ticketing.models import Ticket
        from apps.venuesettings.models import VatSetting

        tenant = Tenant.objects.create(code="gate", name="Gate")
        org = Organization.objects.create(tenant=tenant, code="o", name="O")
        venue = Venue.objects.create(
            tenant=tenant, organization=org, code="v", name={"en": "V"},
            timezone="Asia/Bangkok", currency="THB", tax_model="INCLUSIVE", tax_rate_bp=700,
        )
        VatSetting.objects.create(
            tenant=tenant, venue=venue, enabled=True, rate_bp=700, mode="INCLUSIVE",
            effective_from=dt.date(2026, 1, 1),
        )
        gate_a = AccessPoint.objects.create(tenant=tenant, venue=venue, code="gate-a", name={"en": "A"})
        gate_b = AccessPoint.objects.create(tenant=tenant, venue=venue, code="gate-b", name={"en": "B"})
        seg = CustomerSegment.objects.create(tenant=tenant, code="adult", name={"en": "Adult"})
        product = Product.objects.create(
            tenant=tenant, venue=venue, code="ga", name={"en": "GA"}, session_requirement="NOT_USED",
        )
        tt = TicketType.objects.create(
            tenant=tenant, product=product, segment=seg, code="ga-adult", name={"en": "Adult"},
            entry_allowance=1,
        )
        PriceRule.objects.create(
            tenant=tenant, venue=venue, ticket_type=tt, amount_minor=50000, currency="THB",
        )
        consent_service.publish_notice(
            tenant=tenant, version="v1", controller_name="C", controller_contact="c@x.test",
            dpo_contact="dpo@x.test", body={},
        )
        gateway = SimulatedGateway()
        q = quote(venue=venue, visit_date=dt.date(2026, 9, 1),
                  lines=[QuoteLine(ticket_type_id=tt.id, quantity=1)])
        result = confirm(
            quote_result=q,
            customer_data={"email": "race@x.test", "full_name": "Racer"},
            consent_items={"BOOKING_SERVICE": True}, payment_method="CARD", gateway=gateway,
        )
        ticket = Ticket.objects.get(id=result["tickets"][0]["id"])
        moment = dt.datetime(2026, 9, 1, 5, 0, 0, tzinfo=dt.timezone.utc)

        admits: list[str] = []
        refusals: list[str] = []
        other: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def do_scan(gate):
            try:
                barrier.wait(timeout=30)
                r = access.scan(tenant=tenant, venue=venue, qr_payload=ticket.qr_payload,
                                access_point=gate, now=moment)
                with lock:
                    (admits if r["admit"] else refusals).append(r["decision"])
            except Exception as exc:  # noqa: BLE001
                with lock:
                    other.append(f"{type(exc).__name__}: {exc}")
            finally:
                connections.close_all()

        threads = [threading.Thread(target=do_scan, args=(g,)) for g in (gate_a, gate_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        ticket.refresh_from_db()
        self.assertEqual(other, [], f"unexpected errors: {other}")
        self.assertEqual(len(admits), 1, f"{len(admits)} admits, expected exactly 1")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0], "REJECT_ALREADY_USED")
        # The ticket recorded exactly one entry, and both scans are on the log.
        self.assertEqual(ticket.entries_used, 1)
        self.assertEqual(ScanEvent.objects.filter(ticket=ticket).count(), 2)


class DatabaseCapabilityTests(TransactionTestCase):
    """Record, in the suite itself, which engine the guarantees were proven on."""

    def test_engine_supports_row_locking(self):
        vendor = connection.vendor
        supports = connection.features.has_select_for_update
        # Not an assertion against SQLite — the suite still has to run there — but a
        # visible statement of what was actually exercised.
        print(f"\n[capacity] vendor={vendor} has_select_for_update={supports}")
        if vendor == "postgresql":
            self.assertTrue(supports, "PostgreSQL must support SELECT FOR UPDATE")
            self.assertTrue(connection.features.has_select_for_update_skip_locked)
