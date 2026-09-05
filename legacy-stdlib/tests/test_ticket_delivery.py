"""How the e-ticket's QR reaches the guest, per delivery mode.

The e-ticket is only as good as the QR that arrives with it, and mail clients
disagree about how an image may travel:

* ``data:`` renders in a browser and in several desktop clients, but **Gmail
  strips it** — which would leave the guest holding a ticket with a blank code;
* ``cid:`` attaches the image to the message and is the mode every client
  renders, so it is the default;
* a signed remote URL is what Gmail expects, and must work with no session while
  still not being guessable.

So the assertions here are mostly about the things that silently produce an
unscannable ticket: a ``cid:`` reference with no attachment, an attachment nobody
references, a MIME tree without a plain-text fallback, a capability token that
outlives its expiry or survives tampering — and, most directly, whether the
bytes actually attached to the message decode back to the real credential.

The PNG is decoded here rather than assumed: ``tests/test_ticket_qr._decode``
provides an independent read path, so "the image in the email is the right QR"
is verified rather than trusted.
"""

from __future__ import annotations

import datetime as _dt
import re
import struct
import unittest
import zlib

import seed
from utp.app import Platform
from utp.services.booking import QuoteLineRequest
from utp.services.mail_mime import build_message, describe, referenced_cids
from utp.ticketdesign import qr
from utp.ticketdesign.email_ticket import (
    CidSource,
    LinkSource,
    data_url_source,
    qr_source_for,
    render_eticket_document,
)
from utp.ticketdesign.links import sign_qr_token, verify_qr_token
from utp.ticketdesign.payload import ticket_render_payload

from tests.test_ticket_qr import _FORMAT_STRINGS, _decode, _read_format


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _png_to_matrix(blob: bytes, *, scale: int, quiet: int) -> list[list[bool]]:
    """Recover the module matrix from a rendered PNG."""
    width, height, depth, colour = struct.unpack(">IIBB", blob[16:26])
    assert (depth, colour) == (8, 0), "expected 8-bit greyscale"
    offset, idat = 8, b""
    while offset < len(blob):
        length, kind = struct.unpack(">I4s", blob[offset:offset + 8])
        if kind == b"IDAT":
            idat += blob[offset + 8:offset + 8 + length]
        offset += 12 + length
    raw = zlib.decompress(idat)
    stride = width + 1
    span = width // scale
    size = span - quiet * 2
    matrix = []
    for my in range(size):
        row = []
        py = (my + quiet) * scale + scale // 2
        for mx in range(size):
            px = (mx + quiet) * scale + scale // 2
            row.append(raw[py * stride + 1 + px] == 0)
        matrix.append(row)
    return matrix


def _decode_png_qr(blob: bytes, *, scale: int = 8, quiet: int = qr.QUIET_ZONE) -> str:
    """Decode a rendered QR PNG back to its payload via the independent decoder."""
    matrix = _png_to_matrix(blob, scale=scale, quiet=quiet)
    size = len(matrix)
    code = qr.QrCode.__new__(qr.QrCode)
    code.version = (size - 17) // 4
    code.level = "M"
    code.size = size
    code.modules = matrix
    code._function = [[False] * size for _ in range(size)]
    code.mask = _FORMAT_STRINGS["M"].index(_read_format(code))
    return _decode(code).decode()


# --------------------------------------------------------------------------- #
# Signed capability tokens
# --------------------------------------------------------------------------- #


class QrTokenTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        token = sign_qr_token("tkt_abc123")
        self.assertEqual(verify_qr_token(token), "tkt_abc123")

    def test_token_is_url_safe(self) -> None:
        token = sign_qr_token("tkt_abc123")
        self.assertRegex(token, r"^[A-Za-z0-9_\-.]+$")

    def test_expired_token_is_refused(self) -> None:
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
        self.assertIsNone(verify_qr_token(sign_qr_token("tkt_abc", expires_at=past)))

    def test_token_valid_right_up_to_its_expiry(self) -> None:
        soon = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=30)
        self.assertEqual(verify_qr_token(sign_qr_token("tkt_abc", expires_at=soon)), "tkt_abc")

    def test_tampering_with_the_claim_is_detected(self) -> None:
        token = sign_qr_token("tkt_abc")
        encoded, _, signature = token.rpartition(".")
        forged = sign_qr_token("tkt_other").rpartition(".")[0] + "." + signature
        self.assertIsNone(verify_qr_token(forged))
        self.assertIsNone(verify_qr_token(encoded + ".0000000000000000000000"))

    def test_garbage_is_refused_without_raising(self) -> None:
        for bad in ("", "nonsense", "a.b", "....", "%%%.%%%"):
            self.assertIsNone(verify_qr_token(bad))

    def test_failures_are_indistinguishable(self) -> None:
        """Expired and forged both return None, so neither confirms a ticket exists."""
        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        self.assertIsNone(verify_qr_token(sign_qr_token("tkt_real", expires_at=past)))
        self.assertIsNone(verify_qr_token("clearly-not-a-token"))


# --------------------------------------------------------------------------- #
# MIME composition
# --------------------------------------------------------------------------- #


class MimeStructureTests(unittest.TestCase):
    def test_text_only_message_is_plain(self) -> None:
        message = build_message(
            sender="a@b.test", to="c@d.test", subject="s", text_body="hello"
        )
        self.assertEqual(message.get_content_type(), "text/plain")

    def test_html_message_is_multipart_alternative_with_text_first(self) -> None:
        message = build_message(
            sender="a@b.test", to="c@d.test", subject="s",
            text_body="plain", html_body="<p>rich</p>",
        )
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        shape = describe(message)
        self.assertEqual(shape["parts"], ["text/plain", "text/html"])

    def test_inline_images_produce_multipart_related_with_matching_cids(self) -> None:
        png = qr.qr_png("UTP1.t.tok.sig", scale=4)
        html = '<img src="cid:qr-one"><img src="cid:qr-two">'
        message = build_message(
            sender="a@b.test", to="c@d.test", subject="s",
            text_body="plain", html_body=html,
            inline_images={"qr-one": png, "qr-two": png},
        )
        shape = describe(message)
        self.assertEqual(shape["content_type"], "multipart/alternative")
        self.assertTrue(shape["has_plain_text"])
        self.assertTrue(shape["has_html"])
        self.assertEqual(shape["parts"].count("image/png"), 2)
        self.assertEqual(shape["cids"], ["qr-one", "qr-two"])
        self.assertIn("multipart/related", [p.get_content_type() for p in message.walk()])

    def test_images_are_marked_inline_not_as_downloads(self) -> None:
        png = qr.qr_png("UTP1.t.tok.sig", scale=4)
        message = build_message(
            sender="a@b.test", to="c@d.test", subject="s",
            text_body="plain", html_body='<img src="cid:qr-one">',
            inline_images={"qr-one": png},
        )
        image = next(p for p in message.walk() if p.get_content_type() == "image/png")
        self.assertEqual(image.get_content_disposition(), "inline")
        self.assertEqual(image.get_payload(decode=True), png)

    def test_a_dangling_cid_reference_is_refused(self) -> None:
        """A reference with no attachment is a blank QR at the gate."""
        with self.assertRaises(ValueError) as caught:
            build_message(
                sender="a@b.test", to="c@d.test", subject="s",
                text_body="plain", html_body='<img src="cid:missing">',
                inline_images={"present": b"x"},
            )
        self.assertIn("missing", str(caught.exception))

    def test_an_unreferenced_attachment_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_message(
                sender="a@b.test", to="c@d.test", subject="s",
                text_body="plain", html_body="<p>no images</p>",
                inline_images={"orphan": b"x"},
            )

    def test_images_without_html_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            build_message(
                sender="a@b.test", to="c@d.test", subject="s",
                text_body="plain", inline_images={"orphan": b"x"},
            )

    def test_reference_scanner_handles_both_quote_styles(self) -> None:
        self.assertEqual(
            referenced_cids("""<img src="cid:a"><img src='cid:b'><img SRC="CID:c">"""),
            {"a", "b", "c"},
        )

    def test_message_carries_our_own_id_for_tracing(self) -> None:
        message = build_message(
            sender="a@b.test", to="c@d.test", subject="s", text_body="x", message_id="msg_123"
        )
        self.assertEqual(message["X-UTP-Message-Id"], "msg_123")
        self.assertTrue(message["Message-ID"])


# --------------------------------------------------------------------------- #
# QR sources
# --------------------------------------------------------------------------- #


class QrSourceTests(unittest.TestCase):
    def payload(self, number: str = "AQP-0001-01", ticket_id: str = "tkt_abcdefgh12345678") -> dict:
        return {
            "language": "en",
            "ticket": {"id": ticket_id, "number": number, "qr_payload": "UTP1.ten.tok.sig"},
        }

    def test_data_url_source_inlines_the_image(self) -> None:
        self.assertTrue(data_url_source(self.payload()).startswith("data:image/png;base64,"))

    def test_cid_source_collects_bytes_and_returns_a_reference(self) -> None:
        source = CidSource()
        src = source(self.payload())
        self.assertTrue(src.startswith("cid:"))
        cid = src.split(":", 1)[1]
        self.assertIn(cid, source.images)
        self.assertTrue(source.images[cid].startswith(b"\x89PNG"))

    def test_cid_is_token_safe_even_for_awkward_ticket_numbers(self) -> None:
        source = CidSource()
        src = source(self.payload(number="AQP/00 01#ก"))
        cid = src.split(":", 1)[1]
        self.assertRegex(cid, r"^[a-z0-9.\-]+$")

    def test_each_ticket_gets_its_own_cid(self) -> None:
        source = CidSource()
        a = source(self.payload(number="A-1", ticket_id="tkt_aaaaaaaa11111111"))
        b = source(self.payload(number="A-2", ticket_id="tkt_bbbbbbbb22222222"))
        self.assertNotEqual(a, b)
        self.assertEqual(len(source.images), 2)

    def test_repeated_render_of_one_ticket_reuses_the_same_attachment(self) -> None:
        source = CidSource()
        first = source(self.payload())
        second = source(self.payload())
        self.assertEqual(first, second)
        self.assertEqual(len(source.images), 1)

    def test_link_source_returns_a_signed_url_that_verifies(self) -> None:
        url = LinkSource(base_url="https://book.example")(self.payload())
        self.assertTrue(url.startswith("https://book.example/qr/"))
        self.assertEqual(verify_qr_token(url.rsplit("/", 1)[1]), "tkt_abcdefgh12345678")

    def test_mode_resolution(self) -> None:
        self.assertIsInstance(qr_source_for("CID"), CidSource)
        self.assertIsInstance(qr_source_for("LINK"), LinkSource)
        self.assertIs(qr_source_for("DATA_URL"), data_url_source)
        # An unrecognised mode must still produce a ticket, not an exception.
        self.assertIs(qr_source_for("nonsense"), data_url_source)
        self.assertIs(qr_source_for(""), data_url_source)


# --------------------------------------------------------------------------- #
# End to end, against a real booking
# --------------------------------------------------------------------------- #


class DeliveryModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.platform = Platform(db_path=":memory:")
        info = seed.provision(cls.platform)
        cls.tenant_id = info["tenant_id"]
        cls.venue_id = info["venue_id"]
        cls.staff_ctx = cls.platform.system_context(cls.tenant_id).for_venue(cls.venue_id)
        guest = cls.platform.guest_context(
            cls.tenant_id, venue_id=cls.venue_id, channel="ONLINE", language="en"
        )
        quote = cls.platform.booking.quote(
            guest,
            venue_id=cls.venue_id,
            visit_date=(_dt.date.today() + _dt.timedelta(days=7)).isoformat(),
            lines=[QuoteLineRequest(ticket_type_id=info["ticket_types"]["GA-INTL-ADULT"], quantity=2)],
        )
        quote = cls.platform.booking.start_checkout(guest, quote)
        cls.result = cls.platform.booking.confirm(
            guest,
            quote,
            customer={"email": "delivery@example.test", "full_name": "Delivery Test"},
            consent_items={"BOOKING_SERVICE": True, "MARKETING": False},
            payment_method="CARD",
            idempotency_key="delivery-tests",
        )
        cls.booking_id = cls.result["booking_id"]

    def payloads(self) -> list[dict]:
        tickets = self.platform.tickets.list_for_booking(self.staff_ctx, self.booking_id)
        return [
            ticket_render_payload(self.platform, self.staff_ctx, ticket_id=t["id"], language="en")
            for t in tickets
        ]

    def test_cid_mode_emits_references_and_no_data_urls(self) -> None:
        source = CidSource()
        html = render_eticket_document(self.payloads(), qr_source=source)
        self.assertNotIn("data:image/png", html, "Gmail strips data: images")
        self.assertEqual(len(referenced_cids(html)), 2)
        self.assertEqual(set(source.images), referenced_cids(html))

    def test_cid_mode_composes_a_valid_message(self) -> None:
        source = CidSource()
        html = render_eticket_document(self.payloads(), qr_source=source)
        message = build_message(
            sender="tickets@aquaria.test", to="delivery@example.test",
            subject="Your tickets", text_body="plain text ticket",
            html_body=html, inline_images=source.images,
        )
        shape = describe(message)
        self.assertTrue(shape["has_plain_text"])
        self.assertEqual(shape["parts"].count("image/png"), 2)

    def test_the_attached_image_decodes_to_the_real_credential(self) -> None:
        """The bytes in the email must be the ticket's own QR, not a placeholder."""
        payloads = self.payloads()
        source = CidSource()
        render_eticket_document(payloads, qr_source=source)
        expected = {p["ticket"]["qr_payload"] for p in payloads}
        decoded = {_decode_png_qr(blob) for blob in source.images.values()}
        self.assertEqual(decoded, expected)

    def test_the_attached_image_carries_no_personal_data(self) -> None:
        source = CidSource()
        render_eticket_document(self.payloads(), qr_source=source)
        for blob in source.images.values():
            payload = _decode_png_qr(blob)
            self.assertTrue(payload.startswith("UTP1."))
            for secret in ("Delivery Test", "delivery@example.test"):
                self.assertNotIn(secret, payload)

    def test_link_mode_emits_verifiable_urls(self) -> None:
        payloads = self.payloads()
        html = render_eticket_document(payloads, qr_source=LinkSource(base_url="https://book.example"))
        urls = re.findall(r'src="(https://book\.example/qr/[^"]+)"', html)
        self.assertEqual(len(urls), 2)
        resolved = {verify_qr_token(url.rsplit("/", 1)[1]) for url in urls}
        self.assertEqual(resolved, {p["ticket"]["id"] for p in payloads})

    def test_confirmation_email_defaults_to_cid_and_attaches_the_images(self) -> None:
        self.platform.notifications.dispatch_due(self.staff_ctx)
        sent = [m for m in self.platform.notifications.provider.sent
                if m["to"] == "delivery@example.test" and m.get("html")]
        self.assertTrue(sent, "no HTML e-ticket was delivered")
        message = sent[0]
        self.assertNotIn("data:image/png", message["html"])
        self.assertEqual(len(message["inline_images"]), 2)
        self.assertEqual(set(message["inline_images"]), referenced_cids(message["html"]))
        # The provider composed real MIME, so the structure is verifiable.
        self.assertEqual(message["structure"]["content_type"], "multipart/alternative")
        self.assertTrue(message["structure"]["has_plain_text"])
        self.assertEqual(message["structure"]["parts"].count("image/png"), 2)

    def test_inline_images_are_stored_on_the_message_row(self) -> None:
        row = self.platform.db.query_one(
            "SELECT inline_images_json FROM notification_messages "
            "WHERE tenant_id = ? AND event_type = 'BOOKING_CONFIRMATION' AND booking_id = ?",
            (self.tenant_id, self.booking_id),
        )
        self.assertIsNotNone(row["inline_images_json"])
        restored = self.platform.notifications._row_inline_images(row)
        self.assertEqual(len(restored), 2)
        for blob in restored.values():
            self.assertTrue(blob.startswith(b"\x89PNG"))

    def test_delivery_mode_is_configuration(self) -> None:
        """Switching modes must need no code change (configuration over code)."""
        self.platform.config.set(
            self.staff_ctx,
            key="notification.qr_delivery",
            value="DATA_URL",
            scope_type="VENUE",
            scope_id=self.venue_id,
        )
        try:
            html, images = self.platform._render_eticket_html(self.staff_ctx, self.booking_id, "en")
            self.assertIn("data:image/png;base64,", html)
            self.assertEqual(images, {})
        finally:
            self.platform.config.set(
                self.staff_ctx,
                key="notification.qr_delivery",
                value="CID",
                scope_type="VENUE",
                scope_id=self.venue_id,
            )
        html, images = self.platform._render_eticket_html(self.staff_ctx, self.booking_id, "en")
        self.assertNotIn("data:image/png", html)
        self.assertEqual(len(images), 2)


if __name__ == "__main__":
    unittest.main()
