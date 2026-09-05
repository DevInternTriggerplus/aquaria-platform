"""A QR Code encoder written against the standard library only.

The platform may not add dependencies, and a ticket whose QR cannot be scanned
is not a ticket, so the encoder is implemented here in full rather than
approximated. It follows ISO/IEC 18004: byte-mode segments, Reed-Solomon error
correction over GF(256), block interleaving, the eight data masks with the
standard penalty scoring, and BCH-protected format and version information.

Scope is deliberately bounded to versions 1-15 (21x21 up to 77x77). At error
correction level M that carries 415 data codewords — comfortably more than the
~160-character signed access token a ticket holds (see ``tickets.build_qr_payload``)
— while keeping the module count low enough that a 45mm thermal print stays
readable. A payload that will not fit raises rather than silently truncating.

What goes *in* the QR is decided elsewhere and matters as much as the encoding:
only the opaque signed credential, never personal data (R15.2, ticketDesign.md).

Output is either SVG (sharp at any size, ideal on screen and in print) or an
8-bit greyscale PNG data URL (universally renderable, including in email and
when a print dialog will not wait for a network fetch). Both always include the
mandatory 4-module quiet zone, because a QR flush against surrounding ink is
the single most common cause of a scanner failing at a gate.
"""

from __future__ import annotations

import struct
import zlib

# --------------------------------------------------------------------------- #
# GF(256) arithmetic, primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 (0x11D)
# --------------------------------------------------------------------------- #

_PRIMITIVE = 0x11D
_EXP: list[int] = [0] * 512
_LOG: list[int] = [0] * 256


def _build_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= _PRIMITIVE
    # Duplicated upper half so a log sum never needs a modulo.
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator_poly(degree: int) -> list[int]:
    """Product of (x - a^i) for i in [0, degree), coefficients high order first."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            nxt[j] ^= coeff                          # coeff * x
            nxt[j + 1] ^= _gf_mul(coeff, _EXP[i])    # coeff * a^i
        poly = nxt
    return poly


def reed_solomon_ecc(data: list[int], ecc_len: int) -> list[int]:
    """Remainder of data * x^ecc_len divided by the generator polynomial."""
    gen = _generator_poly(ecc_len)
    residue = list(data) + [0] * ecc_len
    for i in range(len(data)):
        coeff = residue[i]
        if coeff:
            for j, g in enumerate(gen):
                residue[i + j] ^= _gf_mul(g, coeff)
    return residue[len(data):]


# --------------------------------------------------------------------------- #
# Version / error-correction tables (ISO/IEC 18004 tables 13-22)
# --------------------------------------------------------------------------- #

# version -> level -> (ecc codewords per block,
#                      group 1 block count, group 1 data codewords,
#                      group 2 block count, group 2 data codewords)
_ECC_TABLE: dict[int, dict[str, tuple[int, int, int, int, int]]] = {
    1:  {"L": (7, 1, 19, 0, 0),    "M": (10, 1, 16, 0, 0),   "Q": (13, 1, 13, 0, 0),   "H": (17, 1, 9, 0, 0)},
    2:  {"L": (10, 1, 34, 0, 0),   "M": (16, 1, 28, 0, 0),   "Q": (22, 1, 22, 0, 0),   "H": (28, 1, 16, 0, 0)},
    3:  {"L": (15, 1, 55, 0, 0),   "M": (26, 1, 44, 0, 0),   "Q": (18, 2, 17, 0, 0),   "H": (22, 2, 13, 0, 0)},
    4:  {"L": (20, 1, 80, 0, 0),   "M": (18, 2, 32, 0, 0),   "Q": (26, 2, 24, 0, 0),   "H": (16, 4, 9, 0, 0)},
    5:  {"L": (26, 1, 108, 0, 0),  "M": (24, 2, 43, 0, 0),   "Q": (18, 2, 15, 2, 16),  "H": (22, 2, 11, 2, 12)},
    6:  {"L": (18, 2, 68, 0, 0),   "M": (16, 4, 27, 0, 0),   "Q": (24, 4, 19, 0, 0),   "H": (28, 4, 15, 0, 0)},
    7:  {"L": (20, 2, 78, 0, 0),   "M": (18, 4, 31, 0, 0),   "Q": (18, 2, 14, 4, 15),  "H": (26, 4, 13, 1, 14)},
    8:  {"L": (24, 2, 97, 0, 0),   "M": (22, 2, 38, 2, 39),  "Q": (22, 4, 18, 2, 19),  "H": (26, 4, 14, 2, 15)},
    9:  {"L": (30, 2, 116, 0, 0),  "M": (22, 3, 36, 2, 37),  "Q": (20, 4, 16, 4, 17),  "H": (24, 4, 12, 4, 13)},
    10: {"L": (18, 2, 68, 2, 69),  "M": (26, 4, 43, 1, 44),  "Q": (24, 6, 19, 2, 20),  "H": (28, 6, 15, 2, 16)},
    11: {"L": (20, 4, 81, 0, 0),   "M": (30, 1, 50, 4, 51),  "Q": (28, 4, 22, 4, 23),  "H": (24, 3, 12, 8, 13)},
    12: {"L": (24, 2, 92, 2, 93),  "M": (22, 6, 36, 2, 37),  "Q": (26, 4, 20, 6, 21),  "H": (28, 7, 14, 4, 15)},
    13: {"L": (26, 4, 107, 0, 0),  "M": (22, 8, 37, 1, 38),  "Q": (24, 8, 20, 4, 21),  "H": (22, 12, 11, 4, 12)},
    14: {"L": (30, 3, 115, 1, 116), "M": (24, 4, 40, 5, 41), "Q": (20, 11, 16, 5, 17), "H": (24, 11, 12, 5, 13)},
    15: {"L": (22, 5, 87, 1, 88),  "M": (24, 5, 41, 5, 42),  "Q": (30, 5, 24, 7, 25),  "H": (24, 11, 12, 7, 13)},
}

# Row/column centres of the alignment patterns per version.
_ALIGNMENT: dict[int, tuple[int, ...]] = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
    11: (6, 30, 54),
    12: (6, 32, 58),
    13: (6, 34, 62),
    14: (6, 26, 46, 66),
    15: (6, 26, 48, 70),
}

# Two-bit level indicator used in the format information.
_LEVEL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

_MAX_VERSION = 15
_BYTE_MODE = 0b0100


def _data_capacity_codewords(version: int, level: str) -> int:
    ecc_len, g1, d1, g2, d2 = _ECC_TABLE[version][level]
    return g1 * d1 + g2 * d2


def _format_bits(level: str, mask: int) -> int:
    """15-bit BCH(15,5) format information, XOR-ed with the standard mask."""
    value = (_LEVEL_BITS[level] << 3) | mask
    rem = value
    for _ in range(10):
        rem = (rem << 1) ^ (0x537 if (rem >> 9) & 1 else 0)
    return ((value << 10) | (rem & 0x3FF)) ^ 0x5412


def _version_bits(version: int) -> int:
    """18-bit BCH(18,6) version information (versions 7 and above only)."""
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ (0x1F25 if (rem >> 11) & 1 else 0)
    return (version << 12) | (rem & 0xFFF)


class QrCode:
    """An encoded QR symbol. ``modules[y][x]`` is True where the module is dark."""

    __slots__ = ("version", "level", "size", "modules", "_function", "mask")

    def __init__(self, data: bytes, *, level: str = "M", version: int | None = None):
        if level not in _LEVEL_BITS:
            raise ValueError(f"unknown error correction level: {level}")
        self.level = level
        self.version = version or self._smallest_version(len(data), level)
        if self.version < 1 or self.version > _MAX_VERSION:
            raise ValueError(f"version out of supported range 1-{_MAX_VERSION}: {self.version}")
        self.size = self.version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self._function = [[False] * self.size for _ in range(self.size)]
        self.mask = 0

        codewords = self._encode_codewords(data)
        self._draw_function_patterns()
        self._draw_codewords(codewords)
        self._apply_best_mask()

    # ------------------------------------------------------------------ #
    # Encoding
    # ------------------------------------------------------------------ #

    @staticmethod
    def _char_count_bits(version: int) -> int:
        # Byte mode: 8 bits for versions 1-9, 16 bits for 10 and above.
        return 8 if version <= 9 else 16

    @classmethod
    def _smallest_version(cls, byte_len: int, level: str) -> int:
        for version in range(1, _MAX_VERSION + 1):
            capacity_bits = _data_capacity_codewords(version, level) * 8
            needed = 4 + cls._char_count_bits(version) + byte_len * 8
            if needed <= capacity_bits:
                return version
        raise ValueError(
            f"payload of {byte_len} bytes does not fit a version {_MAX_VERSION} "
            f"level {level} symbol"
        )

    def _encode_codewords(self, data: bytes) -> list[int]:
        capacity = _data_capacity_codewords(self.version, self.level)
        count_bits = self._char_count_bits(self.version)
        if len(data) >= (1 << count_bits):
            raise ValueError("payload too long for the character count indicator")

        bits: list[int] = []

        def push(value: int, width: int) -> None:
            for i in range(width - 1, -1, -1):
                bits.append((value >> i) & 1)

        push(_BYTE_MODE, 4)
        push(len(data), count_bits)
        for byte in data:
            push(byte, 8)

        capacity_bits = capacity * 8
        if len(bits) > capacity_bits:
            raise ValueError("payload does not fit the selected version")

        # Terminator, then pad to a whole codeword, then alternating pad bytes.
        push(0, min(4, capacity_bits - len(bits)))
        if len(bits) % 8:
            push(0, 8 - (len(bits) % 8))

        codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
        for pad in _pad_cycle(capacity - len(codewords)):
            codewords.append(pad)

        return self._interleave(codewords)

    def _interleave(self, codewords: list[int]) -> list[int]:
        ecc_len, g1, d1, g2, d2 = _ECC_TABLE[self.version][self.level]
        blocks: list[list[int]] = []
        cursor = 0
        for index in range(g1 + g2):
            length = d1 if index < g1 else d2
            blocks.append(codewords[cursor:cursor + length])
            cursor += length
        ecc_blocks = [reed_solomon_ecc(block, ecc_len) for block in blocks]

        result: list[int] = []
        longest = max(len(block) for block in blocks)
        for i in range(longest):
            for block in blocks:
                if i < len(block):
                    result.append(block[i])
        for i in range(ecc_len):
            for ecc in ecc_blocks:
                result.append(ecc[i])
        return result

    # ------------------------------------------------------------------ #
    # Module placement
    # ------------------------------------------------------------------ #

    def _set_function(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self._function[y][x] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        # Timing patterns.
        for i in range(size):
            self._set_function(6, i, i % 2 == 0)
            self._set_function(i, 6, i % 2 == 0)
        # Finder patterns with their separators.
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        distance = max(abs(dx), abs(dy))
                        self._set_function(x, y, distance not in (2, 4))
        # Alignment patterns, skipping the three finder corners.
        positions = _ALIGNMENT[self.version]
        last = len(positions) - 1
        for i, cy in enumerate(positions):
            for j, cx in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_function(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        # Reserve the format and version areas; real bits are written per mask.
        self._draw_format_bits(0)
        self._draw_version_bits()

    def _draw_format_bits(self, mask: int) -> None:
        size = self.size
        value = _format_bits(self.level, mask)
        bits = [(value >> i) & 1 for i in range(15)]
        # Copy beside the top-left finder.
        for i in range(6):
            self._set_function(8, i, bool(bits[i]))
        self._set_function(8, 7, bool(bits[6]))
        self._set_function(8, 8, bool(bits[7]))
        self._set_function(7, 8, bool(bits[8]))
        for i in range(9, 15):
            self._set_function(14 - i, 8, bool(bits[i]))
        # Redundant copy split across the other two finders.
        for i in range(8):
            self._set_function(size - 1 - i, 8, bool(bits[i]))
        for i in range(8, 15):
            self._set_function(8, size - 15 + i, bool(bits[i]))
        # The always-dark module below the top-right format block.
        self._set_function(8, size - 8, True)

    def _draw_version_bits(self) -> None:
        if self.version < 7:
            return
        value = _version_bits(self.version)
        for i in range(18):
            bit = bool((value >> i) & 1)
            a = self.size - 11 + i % 3
            b = i // 3
            self._set_function(a, b, bit)
            self._set_function(b, a, bit)

    def _draw_codewords(self, codewords: list[int]) -> None:
        size = self.size
        total_bits = len(codewords) * 8
        index = 0
        # Two-module-wide columns, right to left, alternating upward and downward.
        # A while loop is required, not ``for right in range(...)``: skipping the
        # vertical timing column rewrites the cursor to 5, and the *next* column
        # pair must then be 3, which a range would not honour.
        right = size - 1
        upward = True
        while right >= 1:
            if right == 6:              # the vertical timing pattern column
                right = 5
            rows = range(size - 1, -1, -1) if upward else range(size)
            for y in rows:
                for x in (right, right - 1):
                    if not self._function[y][x] and index < total_bits:
                        byte = codewords[index >> 3]
                        self.modules[y][x] = bool((byte >> (7 - (index & 7))) & 1)
                        index += 1
            upward = not upward
            right -= 2

    # ------------------------------------------------------------------ #
    # Masking
    # ------------------------------------------------------------------ #

    @staticmethod
    def _mask_condition(mask: int, x: int, y: int) -> bool:
        if mask == 0:
            return (x + y) % 2 == 0
        if mask == 1:
            return y % 2 == 0
        if mask == 2:
            return x % 3 == 0
        if mask == 3:
            return (x + y) % 3 == 0
        if mask == 4:
            return (y // 2 + x // 3) % 2 == 0
        if mask == 5:
            return (x * y) % 2 + (x * y) % 3 == 0
        if mask == 6:
            return ((x * y) % 2 + (x * y) % 3) % 2 == 0
        if mask == 7:
            return ((x + y) % 2 + (x * y) % 3) % 2 == 0
        raise ValueError(f"mask out of range: {mask}")

    def _apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if not self._function[y][x] and self._mask_condition(mask, x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def _apply_best_mask(self) -> None:
        best_mask = 0
        best_penalty = None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            penalty = self._penalty()
            if best_penalty is None or penalty < best_penalty:
                best_penalty, best_mask = penalty, mask
            self._apply_mask(mask)      # XOR is its own inverse
        self._apply_mask(best_mask)
        self._draw_format_bits(best_mask)
        self.mask = best_mask

    def _penalty(self) -> int:
        size = self.size
        modules = self.modules
        score = 0

        # Rule 1: runs of five or more identical modules in a line.
        for line in list(modules) + [list(col) for col in zip(*modules)]:
            run_colour = line[0]
            run_length = 1
            for value in line[1:]:
                if value == run_colour:
                    run_length += 1
                else:
                    if run_length >= 5:
                        score += 3 + (run_length - 5)
                    run_colour, run_length = value, 1
            if run_length >= 5:
                score += 3 + (run_length - 5)

        # Rule 2: 2x2 blocks of a single colour.
        for y in range(size - 1):
            row, below = modules[y], modules[y + 1]
            for x in range(size - 1):
                if row[x] == row[x + 1] == below[x] == below[x + 1]:
                    score += 3

        # Rule 3: the finder-like 1:1:3:1:1 sequence with four light modules
        # beside it, which is what actually confuses a scanner.
        finder = [True, False, True, True, True, False, True]
        light = [False] * 4
        pattern_a = finder + light
        pattern_b = light + finder
        for line in list(modules) + [list(col) for col in zip(*modules)]:
            for x in range(size - 10):
                window = line[x:x + 11]
                if window == pattern_a or window == pattern_b:
                    score += 40

        # Rule 4: deviation of the dark-module proportion from 50%.
        dark = sum(row.count(True) for row in modules)
        total = size * size
        deviation = abs(dark * 20 - total * 10) // total
        score += deviation * 10
        return score


def _pad_cycle(count: int) -> list[int]:
    """The standard alternating pad bytes 236, 17."""
    pads = []
    for i in range(max(0, count)):
        pads.append(0xEC if i % 2 == 0 else 0x11)
    return pads


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

QUIET_ZONE = 4      # modules; mandated by the specification and by real scanners


def encode(payload: str, *, level: str = "M") -> QrCode:
    """Encode a text payload. The access token is ASCII, so UTF-8 is safe."""
    return QrCode(payload.encode("utf-8"), level=level)


def qr_svg(
    payload: str,
    *,
    level: str = "M",
    quiet_zone: int = QUIET_ZONE,
    dark: str = "#000000",
    light: str = "#ffffff",
    size_attr: str | None = None,
    title: str | None = None,
) -> str:
    """Render as SVG.

    One ``<path>`` of module squares rather than thousands of ``<rect>``
    elements, so the markup stays small enough to sit inside an email or a
    print page. ``shape-rendering="crispEdges"`` keeps module boundaries hard
    at any scale, which is what a scanner needs.
    """
    code = encode(payload, level=level)
    span = code.size + quiet_zone * 2
    segments: list[str] = []
    for y, row in enumerate(code.modules):
        for x, is_dark in enumerate(row):
            if is_dark:
                segments.append(f"M{x + quiet_zone} {y + quiet_zone}h1v1h-1z")
    dimension = f' width="{size_attr}" height="{size_attr}"' if size_attr else ""
    label = f"<title>{title}</title>" if title else ""
    role = ' role="img"' if title else ' aria-hidden="true"'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {span} {span}"'
        f'{dimension}{role} shape-rendering="crispEdges">{label}'
        f'<rect width="{span}" height="{span}" fill="{light}"/>'
        f'<path fill="{dark}" d="{"".join(segments)}"/></svg>'
    )


def qr_png(
    payload: str,
    *,
    level: str = "M",
    scale: int = 8,
    quiet_zone: int = QUIET_ZONE,
) -> bytes:
    """Render as an 8-bit greyscale PNG.

    Greyscale rather than palletised: it needs no PLTE chunk, every renderer
    supports it, and the file is still small because zlib collapses the long
    runs of identical bytes.
    """
    code = encode(payload, level=level)
    span = code.size + quiet_zone * 2
    width = span * scale

    dark_run = b"\x00" * scale
    light_run = b"\xff" * scale
    raw = bytearray()
    for my in range(-quiet_zone, code.size + quiet_zone):
        row = bytearray()
        in_symbol = 0 <= my < code.size
        module_row = code.modules[my] if in_symbol else None
        for mx in range(-quiet_zone, code.size + quiet_zone):
            is_dark = bool(module_row[mx]) if (in_symbol and 0 <= mx < code.size) else False
            row += dark_run if is_dark else light_run
        scanline = b"\x00" + bytes(row)        # filter type 0 (None)
        raw += scanline * scale

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, width, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def qr_png_data_url(payload: str, *, level: str = "M", scale: int = 8, quiet_zone: int = QUIET_ZONE) -> str:
    """A ``data:`` URL, so the QR is present the instant the page or mail opens.

    Deliberate: a print dialog will not wait for a network fetch, and many mail
    clients block remote images. The CSP allows ``img-src 'self' data:``.
    """
    import base64

    png = qr_png(payload, level=level, scale=scale, quiet_zone=quiet_zone)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
