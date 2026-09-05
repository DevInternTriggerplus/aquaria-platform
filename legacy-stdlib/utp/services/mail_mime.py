"""Build the MIME message an e-ticket email actually needs.

The reason this exists rather than handing a provider one HTML string: a QR has to
survive the recipient's mail client, and clients disagree about how an image may
arrive. Gmail strips ``data:`` URLs outright, which would leave a guest holding a
ticket with a blank code. The reliable answer is the oldest one — attach the image
to the message and reference it by Content-ID — and that requires composing real
MIME rather than a body string.

The structure produced is the conventional one:

```
multipart/alternative
├── text/plain            the complete plain-text ticket, always present
└── multipart/related
    ├── text/html         the designed e-ticket
    └── image/png         one inline QR per ticket, Content-ID matched
```

Two properties matter and are asserted in the tests:

* the plain-text part is never omitted, so a client that renders no HTML still
  receives a usable ticket (R37.13);
* every ``cid:`` reference in the HTML resolves to an attached part, and every
  attached part is referenced. A dangling reference is a blank QR at the gate.

Built on :mod:`email` from the standard library, which handles transfer encoding,
header folding and boundary generation correctly — all things worth not
reimplementing.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

#: ``<img src="cid:...">`` in either quoting style.
_CID_REFERENCE = re.compile(r"""src=["']cid:([^"']+)["']""", re.I)


def referenced_cids(html: str) -> set[str]:
    """Every Content-ID the HTML points at."""
    return {match.group(1).strip() for match in _CID_REFERENCE.finditer(html or "")}


def build_message(
    *,
    sender: str,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    inline_images: dict[str, bytes] | None = None,
    message_id: str | None = None,
    language: str | None = None,
) -> EmailMessage:
    """Compose the message. Raises if the HTML and the attachments disagree."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    if message_id:
        # Our own id, kept alongside the RFC one so a delivery can be traced back
        # to the queue row without parsing the provider's identifier.
        message["X-UTP-Message-Id"] = message_id
    if language:
        message["Content-Language"] = language

    # Plain text first: it is the fallback, and in multipart/alternative the least
    # rich part must come first for clients to choose correctly.
    message.set_content(text_body or "")

    if not html_body:
        if inline_images:
            raise ValueError("inline images were supplied without an HTML body")
        return message

    message.add_alternative(html_body, subtype="html")
    if not inline_images:
        return message

    html_part = message.get_payload()[-1]
    wanted = referenced_cids(html_body)
    supplied = set(inline_images)
    missing = wanted - supplied
    if missing:
        raise ValueError(f"HTML references Content-IDs with no attachment: {sorted(missing)}")
    unused = supplied - wanted
    if unused:
        raise ValueError(f"inline images are never referenced by the HTML: {sorted(unused)}")

    for cid, blob in inline_images.items():
        # add_related turns the html part into multipart/related and angle-brackets
        # the cid, which is what the reference in the markup resolves against.
        html_part.add_related(
            blob,
            maintype="image",
            subtype="png",
            cid=f"<{cid}>",
            disposition="inline",
            filename=f"{_safe_filename(cid)}.png",
        )
    return message


def _safe_filename(cid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", cid) or "qr"


def describe(message: EmailMessage) -> dict[str, Any]:
    """A summary of the composed structure, for logs and for assertions."""
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        parts.append(part.get_content_type())
    return {
        "content_type": message.get_content_type(),
        "parts": parts,
        "cids": sorted(
            (part.get("Content-ID") or "").strip("<>")
            for part in message.walk()
            if part.get("Content-ID")
        ),
        "has_plain_text": any(p == "text/plain" for p in parts),
        "has_html": any(p == "text/html" for p in parts),
    }
