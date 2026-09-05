"""Proof that the QR encoder produces symbols a real scanner will read.

A QR cannot be checked by eye, and "it looks like a QR" is not evidence, so this
module verifies the encoder against the specification from two directions:

* **Known-answer tests.** The BCH format and version information strings are
  published constants (ISO/IEC 18004 Annex C/D); the encoder must reproduce them
  exactly. Reed-Solomon is checked by evaluating each finished codeword at the
  generator's roots — a valid codeword is zero at every root, which is the
  algebraic definition rather than a restatement of the implementation.

* **An independent read path.** :func:`_decode` walks the matrix back to bytes
  using its own traversal, its own mask conditions and its own de-interleaving,
  written from the specification rather than by calling into the encoder. If a
  payload survives encode → place → mask → read → de-interleave → decode, and
  every recovered block is a valid RS codeword, then module placement, bit
  order, interleaving and masking are all correct together.

The structural assertions (finder patterns, timing, dark module, quiet zone)
catch the class of bug that still decodes in software but fails against a real
scanner that is looking for those features to lock on to.
"""

from __future__ import annotations

import base64
import re
import struct
import unittest
import zlib

from utp.ticketdesign import qr


# --------------------------------------------------------------------------- #
# Published constants (ISO/IEC 18004). Independent of the implementation.
# --------------------------------------------------------------------------- #

#: Format information for every (level, mask) pair, Annex C.
_FORMAT_STRINGS = {
    "L": (0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976),
    "M": (0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0),
    "Q": (0x355F, 0x3068, 0x3F31, 0x3A06, 0x24B4, 0x2183, 0x2EDA, 0x2BED),
    "H": (0x1689, 0x13BE, 0x1CE7, 0x19D0, 0x0762, 0x0255, 0x0D0C, 0x083B),
}

#: Version information for versions 7-15, Annex D.
_VERSION_STRINGS = {
    7: 0x07C94,
    8: 0x085BC,
    9: 0x09A99,
    10: 0x0A4D3,
    11: 0x0BBF6,
    12: 0x0C762,
    13: 0x0D847,
    14: 0x0E60D,
    15: 0x0F928,
}


# --------------------------------------------------------------------------- #
# Independent decoder
# --------------------------------------------------------------------------- #


def _mask_bit(mask: int, x: int, y: int) -> bool:
    """Mask conditions transcribed from the specification's table, not reused."""
    conditions = (
        lambda: (y + x) % 2 == 0,
        lambda: y % 2 == 0,
        lambda: x % 3 == 0,
        lambda: (y + x) % 3 == 0,
        lambda: ((y // 2) + (x // 3)) % 2 == 0,
        lambda: ((y * x) % 2) + ((y * x) % 3) == 0,
        lambda: (((y * x) % 2) + ((y * x) % 3)) % 2 == 0,
        lambda: (((y + x) % 2) + ((y * x) % 3)) % 2 == 0,
    )
    return conditions[mask]()


def _function_map(version: int, size: int) -> list[list[bool]]:
    """Rebuild the reserved-module map from the specification's geometry."""
    reserved = [[False] * size for _ in range(size)]

    def mark(x: int, y: int) -> None:
        if 0 <= x < size and 0 <= y < size:
            reserved[y][x] = True

    for i in range(size):
        mark(6, i)
        mark(i, 6)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                mark(cx + dx, cy + dy)
    positions = qr._ALIGNMENT[version]
    last = len(positions) - 1
    for i, cy in enumerate(positions):
        for j, cx in enumerate(positions):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    mark(cx + dx, cy + dy)
    # Format information areas plus the dark module.
    for i in range(9):
        mark(8, i)
        mark(i, 8)
    for i in range(8):
        mark(size - 1 - i, 8)
        mark(8, size - 1 - i)
    # Version information areas.
    if version >= 7:
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            mark(a, b)
            mark(b, a)
    return reserved


def _read_format(code: qr.QrCode) -> int:
    """Read the primary format block straight off the matrix."""
    bits = []
    for i in range(6):
        bits.append(1 if code.modules[i][8] else 0)
    bits.append(1 if code.modules[7][8] else 0)
    bits.append(1 if code.modules[8][8] else 0)
    bits.append(1 if code.modules[8][7] else 0)
    for i in range(9, 15):
        bits.append(1 if code.modules[8][14 - i] else 0)
    value = 0
    for i, bit in enumerate(bits):
        value |= bit << i
    return value


def _rs_syndromes_zero(block: list[int], ecc_len: int) -> bool:
    """A codeword is valid exactly when it evaluates to zero at every root a^i."""
    for i in range(ecc_len):
        root = qr._EXP[i]
        acc = 0
        for coeff in block:                     # Horner, high order first
            acc = qr._gf_mul(acc, root) ^ coeff
        if acc != 0:
            return False
    return True


def _decode(code: qr.QrCode) -> bytes:
    """Recover the payload from the matrix using an independent read path."""
    size = code.size
    reserved = _function_map(code.version, size)

    # Undo the mask on data modules only.
    grid = [row[:] for row in code.modules]
    for y in range(size):
        for x in range(size):
            if not reserved[y][x] and _mask_bit(code.mask, x, y):
                grid[y][x] = not grid[y][x]

    # Walk the interleaved bitstream in the standard zigzag.
    bits: list[int] = []
    column = size - 1
    upward = True
    while column > 0:
        if column == 6:
            column = 5
        rows = range(size - 1, -1, -1) if upward else range(size)
        for y in rows:
            for x in (column, column - 1):
                if not reserved[y][x]:
                    bits.append(1 if grid[y][x] else 0)
        upward = not upward
        column -= 2

    codewords = []
    for i in range(0, (len(bits) // 8) * 8, 8):
        byte = 0
        for bit in bits[i:i + 8]:
            byte = (byte << 1) | bit
        codewords.append(byte)

    ecc_len, g1, d1, g2, d2 = qr._ECC_TABLE[code.version][code.level]
    block_count = g1 + g2
    lengths = [d1] * g1 + [d2] * g2
    total_data = sum(lengths)

    # De-interleave data, then ECC.
    blocks: list[list[int]] = [[] for _ in range(block_count)]
    index = 0
    for position in range(max(lengths)):
        for b, length in enumerate(lengths):
            if position < length:
                blocks[b].append(codewords[index])
                index += 1
    ecc_blocks: list[list[int]] = [[] for _ in range(block_count)]
    for position in range(ecc_len):
        for b in range(block_count):
            ecc_blocks[b].append(codewords[index])
            index += 1

    # Every recovered block must be a valid Reed-Solomon codeword.
    for data_block, ecc_block in zip(blocks, ecc_blocks):
        if not _rs_syndromes_zero(data_block + ecc_block, ecc_len):
            raise AssertionError("recovered block is not a valid Reed-Solomon codeword")

    data = [byte for block in blocks for byte in block]
    assert len(data) == total_data

    # Parse the byte-mode segment.
    stream = []
    for byte in data:
        for i in range(7, -1, -1):
            stream.append((byte >> i) & 1)

    def take(width: int) -> int:
        nonlocal stream
        chunk, stream = stream[:width], stream[width:]
        value = 0
        for bit in chunk:
            value = (value << 1) | bit
        return value

    mode = take(4)
    if mode != 0b0100:
        raise AssertionError(f"expected byte mode, read {mode:04b}")
    count = take(8 if code.version <= 9 else 16)
    return bytes(take(8) for _ in range(count))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class GaloisFieldTests(unittest.TestCase):
    def test_tables_form_a_multiplicative_cycle(self):
        self.assertEqual(qr._EXP[0], 1)
        self.assertEqual(qr._LOG[1], 0)
        # a^255 == a^0 == 1 in GF(256).
        self.assertEqual(qr._EXP[255], 1)
        # Every non-zero element appears exactly once as a power of the generator.
        self.assertEqual(sorted(qr._EXP[:255]), list(range(1, 256)))

    def test_multiplication_matches_logarithms(self):
        for a in (1, 2, 3, 17, 200, 255):
            for b in (1, 5, 32, 99, 254):
                expected = qr._EXP[(qr._LOG[a] + qr._LOG[b]) % 255]
                self.assertEqual(qr._gf_mul(a, b), expected)

    def test_multiplication_by_zero(self):
        self.assertEqual(qr._gf_mul(0, 42), 0)
        self.assertEqual(qr._gf_mul(42, 0), 0)


class ReedSolomonTests(unittest.TestCase):
    def test_generator_polynomial_degree(self):
        for degree in (7, 10, 13, 17, 26, 30):
            self.assertEqual(len(qr._generator_poly(degree)), degree + 1)
            self.assertEqual(qr._generator_poly(degree)[0], 1)

    def test_codeword_is_zero_at_every_generator_root(self):
        """The algebraic definition of a valid RS codeword."""
        data = [ord(c) for c in "AQUARIA-PHUKET-TICKET-0001"]
        for ecc_len in (7, 10, 13, 17, 22, 26, 30):
            ecc = qr.reed_solomon_ecc(data, ecc_len)
            self.assertEqual(len(ecc), ecc_len)
            self.assertTrue(
                _rs_syndromes_zero(data + ecc, ecc_len),
                f"syndromes non-zero for ecc_len={ecc_len}",
            )

    def test_single_symbol_error_is_detected(self):
        data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        ecc = qr.reed_solomon_ecc(data, 10)
        corrupted = list(data)
        corrupted[3] ^= 0x5A
        self.assertFalse(_rs_syndromes_zero(corrupted + ecc, 10))


class BchInformationTests(unittest.TestCase):
    def test_format_information_matches_published_table(self):
        for level, expected in _FORMAT_STRINGS.items():
            for mask, value in enumerate(expected):
                self.assertEqual(
                    qr._format_bits(level, mask),
                    value,
                    f"format bits wrong for level {level} mask {mask}",
                )

    def test_version_information_matches_published_table(self):
        for version, expected in _VERSION_STRINGS.items():
            self.assertEqual(qr._version_bits(version), expected, f"version {version}")


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.code = qr.encode("UTP1.ten_abc.tokentokentoken.signaturesignature", level="M")

    def test_size_follows_the_version(self):
        self.assertEqual(self.code.size, self.code.version * 4 + 17)

    def test_finder_patterns_are_exact(self):
        size = self.code.size
        expected = [
            [True, True, True, True, True, True, True],
            [True, False, False, False, False, False, True],
            [True, False, True, True, True, False, True],
            [True, False, True, True, True, False, True],
            [True, False, True, True, True, False, True],
            [True, False, False, False, False, False, True],
            [True, True, True, True, True, True, True],
        ]
        for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
            actual = [[self.code.modules[oy + y][ox + x] for x in range(7)] for y in range(7)]
            self.assertEqual(actual, expected, f"finder at ({ox},{oy})")

    def test_timing_patterns_alternate(self):
        for i in range(8, self.code.size - 8):
            self.assertEqual(self.code.modules[6][i], i % 2 == 0)
            self.assertEqual(self.code.modules[i][6], i % 2 == 0)

    def test_dark_module_is_set(self):
        self.assertTrue(self.code.modules[self.code.size - 8][8])

    def test_format_bits_on_the_matrix_are_canonical(self):
        self.assertEqual(_read_format(self.code), _FORMAT_STRINGS["M"][self.code.mask])

    def test_mask_is_in_range(self):
        self.assertIn(self.code.mask, range(8))


class RoundTripTests(unittest.TestCase):
    """Encode, then read the matrix back with the independent decoder."""

    def test_realistic_access_token(self):
        payload = "UTP1.ten_01k1bmkimufenlmothkui8." + "a3f9" * 8 + "." + "9c2e" * 11
        for level in ("L", "M", "Q", "H"):
            code = qr.encode(payload, level=level)
            self.assertEqual(_decode(code).decode(), payload, f"level {level}")

    def test_lengths_across_the_character_count_boundary(self):
        # Versions 1-9 use an 8-bit count indicator, 10+ use 16 bits, so the
        # transition is the interesting case.
        for length in (1, 8, 16, 40, 100, 154, 155, 180, 250, 300):
            payload = "T" * length
            code = qr.encode(payload, level="M")
            self.assertEqual(_decode(code).decode(), payload, f"length {length}")

    def test_every_supported_version_round_trips(self):
        seen_versions = set()
        for length in range(1, 380, 7):
            code = qr.encode("X" * length, level="M")
            seen_versions.add(code.version)
            self.assertEqual(_decode(code).decode(), "X" * length)
        # The sweep should have exercised a broad span of versions, not just one.
        self.assertGreaterEqual(len(seen_versions), 10)

    def test_payload_beyond_capacity_is_refused_not_truncated(self):
        with self.assertRaises(ValueError):
            qr.encode("X" * 5000, level="H")

    def test_version_grows_with_error_correction_level(self):
        payload = "Y" * 200
        versions = {level: qr.encode(payload, level=level).version for level in ("L", "M", "Q", "H")}
        self.assertLessEqual(versions["L"], versions["M"])
        self.assertLessEqual(versions["M"], versions["Q"])
        self.assertLessEqual(versions["Q"], versions["H"])


class SvgRenderingTests(unittest.TestCase):
    def setUp(self):
        self.payload = "UTP1.ten_abc.tok.sig"

    def test_svg_is_well_formed_and_has_a_quiet_zone(self):
        svg = qr_svg = qr.qr_svg(self.payload)
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.rstrip().endswith("</svg>"))
        code = qr.encode(self.payload)
        span = code.size + qr.QUIET_ZONE * 2
        self.assertIn(f'viewBox="0 0 {span} {span}"', qr_svg)

    def test_svg_paints_a_white_background_behind_the_modules(self):
        # The quiet zone must be white, never transparent over a coloured panel.
        svg = qr.qr_svg(self.payload, light="#ffffff")
        self.assertIn('fill="#ffffff"', svg)
        self.assertLess(svg.index('fill="#ffffff"'), svg.index('<path'))

    def test_module_count_in_path_matches_dark_modules(self):
        code = qr.encode(self.payload)
        dark = sum(row.count(True) for row in code.modules)
        svg = qr.qr_svg(self.payload)
        self.assertEqual(len(re.findall(r"h1v1h-1z", svg)), dark)

    def test_title_makes_it_an_accessible_image(self):
        svg = qr.qr_svg(self.payload, title="Entrance QR code")
        self.assertIn("<title>Entrance QR code</title>", svg)
        self.assertIn('role="img"', svg)
        self.assertNotIn('aria-hidden', svg)


class PngRenderingTests(unittest.TestCase):
    def setUp(self):
        self.payload = "UTP1.ten_abc.tok.sig"

    def test_png_header_and_dimensions(self):
        code = qr.encode(self.payload)
        scale = 6
        png = qr.qr_png(self.payload, scale=scale)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        length, kind = struct.unpack(">I4s", png[8:16])
        self.assertEqual(kind, b"IHDR")
        width, height, depth, colour = struct.unpack(">IIBB", png[16:16 + 10])
        expected = (code.size + qr.QUIET_ZONE * 2) * scale
        self.assertEqual((width, height), (expected, expected))
        self.assertEqual((depth, colour), (8, 0))       # 8-bit greyscale
        self.assertTrue(png.endswith(b"IEND\xae\x42\x60\x82"))

    def test_png_pixels_reproduce_the_matrix(self):
        """Decompress the PNG and check a sample of pixels against the modules."""
        code = qr.encode(self.payload)
        scale, quiet = 4, qr.QUIET_ZONE
        png = qr.qr_png(self.payload, scale=scale, quiet_zone=quiet)

        # Gather IDAT payloads and inflate.
        offset, idat = 8, b""
        while offset < len(png):
            length, kind = struct.unpack(">I4s", png[offset:offset + 8])
            if kind == b"IDAT":
                idat += png[offset + 8:offset + 8 + length]
            offset += 12 + length
        raw = zlib.decompress(idat)

        span = code.size + quiet * 2
        width = span * scale
        stride = width + 1                              # one filter byte per scanline
        for my in (0, 1, code.size // 2, code.size - 1):
            for mx in (0, code.size // 3, code.size - 1):
                py = (my + quiet) * scale + scale // 2
                px = (mx + quiet) * scale + scale // 2
                self.assertEqual(raw[py * stride], 0, "expected filter type 0")
                pixel = raw[py * stride + 1 + px]
                expected = 0 if code.modules[my][mx] else 255
                self.assertEqual(pixel, expected, f"module ({mx},{my})")

    def test_quiet_zone_pixels_are_white(self):
        scale, quiet = 4, qr.QUIET_ZONE
        code = qr.encode(self.payload)
        png = qr.qr_png(self.payload, scale=scale, quiet_zone=quiet)
        offset, idat = 8, b""
        while offset < len(png):
            length, kind = struct.unpack(">I4s", png[offset:offset + 8])
            if kind == b"IDAT":
                idat += png[offset + 8:offset + 8 + length]
            offset += 12 + length
        raw = zlib.decompress(idat)
        width = (code.size + quiet * 2) * scale
        stride = width + 1
        # Top-left corner of the quiet zone.
        self.assertEqual(raw[0 * stride + 1 + 0], 255)
        # Bottom-right corner.
        self.assertEqual(raw[(width - 1) * stride + 1 + (width - 1)], 255)

    def test_data_url_is_a_decodable_png(self):
        url = qr.qr_png_data_url(self.payload, scale=3)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        blob = base64.b64decode(url.split(",", 1)[1])
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_data_url_stays_small_enough_to_embed(self):
        # A ticket-sized payload must not bloat an email beyond reason.
        payload = "UTP1.ten_01k1bmkimufenlmothkui8." + "a3f9" * 8 + "." + "9c2e" * 11
        url = qr.qr_png_data_url(payload, scale=6)
        self.assertLess(len(url), 40_000, "QR data URL is unexpectedly large")


if __name__ == "__main__":
    unittest.main()
