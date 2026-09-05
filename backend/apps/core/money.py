"""Money in integer minor units.

No float ever touches a price, tax, discount or total. Every amount in the
platform is an ``int`` in the venue currency's minor unit (satang for THB), and
every rounding decision goes through :func:`apply_rounding` so that cart,
payment, receipt, tax invoice and reports reconcile exactly (R5.5).

Tax is handled for both inclusive and exclusive venues (R5.4). The split is
computed once, at confirmation, and stored on the booking item; it is never
recomputed from configuration afterwards (R5.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Literal

RoundingMode = Literal["NONE", "NEAREST_1", "NEAREST_5", "NEAREST_10", "UP_1", "DOWN_1"]

#: Minor units per major unit, by ISO currency. Extended by configuration.
DEFAULT_MINOR_UNITS: dict[str, int] = {
    "THB": 100,
    "MYR": 100,
    "USD": 100,
    "EUR": 100,
    "SGD": 100,
    "IDR": 100,
    "JPY": 1,
}


def minor_units(currency: str) -> int:
    return DEFAULT_MINOR_UNITS.get((currency or "THB").upper(), 100)


def to_minor(amount: str | int | float | Decimal, currency: str = "THB") -> int:
    """Convert a major-unit amount to minor units.

    Accepts ``float`` for developer convenience at configuration boundaries only;
    the value is routed through ``Decimal(str(...))`` so 1251.10 does not become
    125109.
    """
    factor = minor_units(currency)
    value = Decimal(str(amount))
    return int((value * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_major(amount_minor: int, currency: str = "THB") -> Decimal:
    """Exact major-unit ``Decimal`` for display and document rendering."""
    factor = minor_units(currency)
    if factor == 1:
        return Decimal(amount_minor)
    return (Decimal(amount_minor) / Decimal(factor)).quantize(Decimal("0.01"))


def format_amount(amount_minor: int, currency: str = "THB", *, locale: str = "en") -> str:
    """Human-readable amount, thousands-separated (R69.6)."""
    major = to_major(amount_minor, currency)
    quantum = "0.01" if minor_units(currency) > 1 else "1"
    text = f"{major.quantize(Decimal(quantum)):,}"
    if locale.startswith("th") and currency.upper() == "THB":
        return f"{text} บาท"
    return f"{currency.upper()} {text}"


def apply_rounding(amount_minor: int, mode: RoundingMode = "NONE", currency: str = "THB") -> int:
    """Apply the venue's configured rounding rule to a minor-unit amount.

    Thai venues commonly round cash totals to the nearest baht. Applying the rule
    in exactly one function is what makes totals reconcile between the cart, the
    receipt and the tax invoice.
    """
    if mode in (None, "NONE"):
        return int(amount_minor)
    step_major = {"NEAREST_1": 1, "NEAREST_5": 5, "NEAREST_10": 10, "UP_1": 1, "DOWN_1": 1}.get(mode)
    if step_major is None:
        raise ValueError(f"unknown rounding mode: {mode}")
    step = step_major * minor_units(currency)
    if step <= 1:
        return int(amount_minor)
    remainder = amount_minor % step
    if remainder == 0:
        return int(amount_minor)
    if mode == "UP_1":
        return int(amount_minor - remainder + step)
    if mode == "DOWN_1":
        return int(amount_minor - remainder)
    # Nearest, half up.
    return int(amount_minor - remainder + (step if remainder * 2 >= step else 0))


@dataclass(frozen=True, slots=True)
class TaxSplit:
    """Result of splitting a gross or net amount into base and tax."""

    net_minor: int
    tax_minor: int
    gross_minor: int
    rate_bp: int
    model: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            "net_minor": self.net_minor,
            "tax_minor": self.tax_minor,
            "gross_minor": self.gross_minor,
            "rate_bp": self.rate_bp,
            "model": self.model,
        }


def split_tax(amount_minor: int, *, rate_bp: int, model: str) -> TaxSplit:
    """Split an amount into tax base and tax.

    ``model`` is ``"INCLUSIVE"`` (the configured price already contains tax) or
    ``"EXCLUSIVE"`` (tax is added on top). ``rate_bp`` is basis points, so Thai
    VAT of 7% is ``700`` — integer configuration avoids any float rate.
    """
    amount_minor = int(amount_minor)
    rate_bp = int(rate_bp)
    if rate_bp <= 0:
        return TaxSplit(amount_minor, 0, amount_minor, 0, model)
    with localcontext() as ctx:
        ctx.prec = 28
        if model == "INCLUSIVE":
            gross = Decimal(amount_minor)
            net = (gross * 10_000 / Decimal(10_000 + rate_bp)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            tax = gross - net
            return TaxSplit(int(net), int(tax), int(gross), rate_bp, model)
        if model == "EXCLUSIVE":
            net = Decimal(amount_minor)
            tax = (net * rate_bp / Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            return TaxSplit(int(net), int(tax), int(net + tax), rate_bp, model)
    raise ValueError(f"unknown tax model: {model}")


@dataclass(frozen=True, slots=True)
class ChargeInput:
    """One venue's tax and service-charge configuration at the moment of sale.

    ``rate_bp`` is basis points (7% VAT is ``700``) so no float rate ever enters
    the calculation. ``mode`` is ``"INCLUSIVE"`` (the price the customer already
    saw contains the charge) or ``"EXCLUSIVE"`` (it is added on top). A disabled
    charge is expressed by ``enabled=False`` rather than a zero rate, so the
    distinction between "no VAT" and "0% VAT" survives into the snapshot.
    """

    enabled: bool = False
    rate_bp: int = 0
    mode: str = "EXCLUSIVE"  # INCLUSIVE | EXCLUSIVE
    display_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rate_bp": int(self.rate_bp),
            "mode": self.mode,
            "display_name": self.display_name,
        }


@dataclass(frozen=True, slots=True)
class ChargeBreakdown:
    """The authoritative, fully-separated result of a charge calculation (R2 of the
    settings spec: base, discount, service charge, VAT and grand total kept apart).

    Every field is minor units. ``grand_total_minor`` is the amount actually charged.
    The three "*_included" flags record whether each component was carved *out of* the
    displayed price (inclusive) rather than added *on top* (exclusive), because the
    receipt and tax invoice must say which, and because a stored booking must be able
    to reproduce this exactly years later regardless of current settings.
    """

    base_minor: int
    line_discount_minor: int
    order_discount_minor: int
    taxable_base_minor: int
    service_charge_minor: int
    service_charge_included: bool
    vat_minor: int
    vat_included: bool
    rounding_adjustment_minor: int
    grand_total_minor: int
    currency: str
    vat_rate_bp: int
    vat_mode: str
    service_charge_rate_bp: int
    service_charge_mode: str

    @property
    def subtotal_minor(self) -> int:
        """Base less all discounts — what the charges are computed against."""
        return self.base_minor - self.line_discount_minor - self.order_discount_minor

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_minor": self.base_minor,
            "line_discount_minor": self.line_discount_minor,
            "order_discount_minor": self.order_discount_minor,
            "subtotal_minor": self.subtotal_minor,
            "taxable_base_minor": self.taxable_base_minor,
            "service_charge_minor": self.service_charge_minor,
            "service_charge_included": self.service_charge_included,
            "vat_minor": self.vat_minor,
            "vat_included": self.vat_included,
            "rounding_adjustment_minor": self.rounding_adjustment_minor,
            "grand_total_minor": self.grand_total_minor,
            "currency": self.currency,
            "vat_rate_bp": self.vat_rate_bp,
            "vat_mode": self.vat_mode,
            "service_charge_rate_bp": self.service_charge_rate_bp,
            "service_charge_mode": self.service_charge_mode,
        }

    def snapshot(self) -> dict[str, Any]:
        """The subset stored on a booking so history never moves (R3 §33)."""
        return {
            "vat_rate_bp": self.vat_rate_bp,
            "vat_mode": self.vat_mode,
            "vat_minor": self.vat_minor,
            "vat_included": self.vat_included,
            "service_charge_rate_bp": self.service_charge_rate_bp,
            "service_charge_mode": self.service_charge_mode,
            "service_charge_minor": self.service_charge_minor,
            "service_charge_included": self.service_charge_included,
            "taxable_base_minor": self.taxable_base_minor,
        }


def compute_charges(
    *,
    base_minor: int,
    line_discount_minor: int = 0,
    order_discount_minor: int = 0,
    service_charge: ChargeInput | None = None,
    vat: ChargeInput | None = None,
    rounding_mode: RoundingMode = "NONE",
    currency: str = "THB",
) -> ChargeBreakdown:
    """Compute the final, authoritative money breakdown for one order.

    Calculation order (settings spec §6, the critical requirement), applied in
    exactly this sequence so cart, receipt, tax invoice and reports agree:

        1. base price (sum of line gross)
        2. line/product-level discount
        3. order-level discount
        4. service charge
        5. VAT
        6. rounding
        7. grand total

    The four combinations the spec enumerates fall out of two independent flags:

    * **Service charge EXCLUSIVE** adds ``subtotal * sc_rate`` on top; **INCLUSIVE**
      means the subtotal already contains it, so the component is carved out for the
      receipt but the customer total does not grow.
    * **VAT EXCLUSIVE** adds VAT to (subtotal + any added service charge);
      **INCLUSIVE** means that amount already contains VAT, carved out, not added.

    So Case A (both included) never grows the price; Case D (both excluded) adds
    both; B and C add one and carve the other. All arithmetic is integer-minor via
    :class:`~decimal.Decimal`, never float.
    """
    sc = service_charge or ChargeInput()
    v = vat or ChargeInput()
    base_minor = int(base_minor)
    line_discount_minor = int(line_discount_minor)
    order_discount_minor = int(order_discount_minor)

    subtotal = max(base_minor - line_discount_minor - order_discount_minor, 0)

    sc_enabled = bool(sc.enabled) and int(sc.rate_bp) > 0
    sc_included = sc_enabled and sc.mode == "INCLUSIVE"
    v_enabled = bool(v.enabled) and int(v.rate_bp) > 0
    v_included = v_enabled and v.mode == "INCLUSIVE"

    # --- 4. service charge -------------------------------------------------- #
    # The amount the charges apply to. For an inclusive service charge the subtotal
    # already contains it; for exclusive it is added.
    if not sc_enabled:
        service_charge_minor = 0
        after_service = subtotal
    elif sc_included:
        # Carve the service-charge component out of the subtotal without changing
        # the total: subtotal = sc_base + sc_base * rate  =>  sc_base = subtotal / (1+rate).
        sc_base = _quantize_div(subtotal, 10_000, 10_000 + int(sc.rate_bp))
        service_charge_minor = subtotal - sc_base
        after_service = subtotal
    else:
        service_charge_minor = _quantize_mul(subtotal, int(sc.rate_bp), 10_000)
        after_service = subtotal + service_charge_minor

    # --- 5. VAT ------------------------------------------------------------- #
    # VAT applies to the subtotal plus any *added* (exclusive) service charge. An
    # inclusive service charge is already part of the price VAT is computed from.
    vat_taxable = after_service
    if not v_enabled:
        vat_minor = 0
        taxable_base = vat_taxable
        pre_round_total = after_service
    elif v_included:
        split = split_tax(vat_taxable, rate_bp=int(v.rate_bp), model="INCLUSIVE")
        vat_minor = split.tax_minor
        taxable_base = split.net_minor
        pre_round_total = after_service  # already contains VAT
    else:
        split = split_tax(vat_taxable, rate_bp=int(v.rate_bp), model="EXCLUSIVE")
        vat_minor = split.tax_minor
        taxable_base = split.net_minor
        pre_round_total = after_service + vat_minor

    # --- 6 & 7. rounding, grand total -------------------------------------- #
    grand_total = apply_rounding(pre_round_total, rounding_mode, currency)
    rounding_adjustment = grand_total - pre_round_total

    return ChargeBreakdown(
        base_minor=base_minor,
        line_discount_minor=line_discount_minor,
        order_discount_minor=order_discount_minor,
        taxable_base_minor=taxable_base,
        service_charge_minor=service_charge_minor,
        service_charge_included=sc_included,
        vat_minor=vat_minor,
        vat_included=v_included,
        rounding_adjustment_minor=rounding_adjustment,
        grand_total_minor=grand_total,
        currency=currency,
        vat_rate_bp=int(v.rate_bp) if v_enabled else 0,
        vat_mode=v.mode if v_enabled else "NONE",
        service_charge_rate_bp=int(sc.rate_bp) if sc_enabled else 0,
        service_charge_mode=sc.mode if sc_enabled else "NONE",
    )


def _quantize_mul(amount_minor: int, numerator: int, denominator: int) -> int:
    value = Decimal(int(amount_minor)) * Decimal(int(numerator)) / Decimal(int(denominator))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _quantize_div(amount_minor: int, numerator: int, denominator: int) -> int:
    """``amount * numerator / denominator`` rounded half up, for inclusive carve-outs."""
    return _quantize_mul(amount_minor, numerator, denominator)


def apply_percentage(amount_minor: int, percent_bp: int) -> int:
    """Percentage of an amount in basis points, rounded half up.

    Used by percentage promotions and percentage cancellation fees so that both
    round identically.
    """
    if percent_bp <= 0:
        return 0
    value = Decimal(int(amount_minor)) * Decimal(int(percent_bp)) / Decimal(10_000)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


#: Currency presentation: symbol, decimal places, and whether the symbol leads.
#: The UI must not assume two decimals — JPY has none (settings spec §23).
CURRENCY_DISPLAY: dict[str, dict[str, Any]] = {
    "THB": {"symbol": "฿", "decimals": 2, "symbol_first": True},
    "USD": {"symbol": "$", "decimals": 2, "symbol_first": True},
    "EUR": {"symbol": "€", "decimals": 2, "symbol_first": True},
    "SGD": {"symbol": "S$", "decimals": 2, "symbol_first": True},
    "MYR": {"symbol": "RM", "decimals": 2, "symbol_first": True},
    "CNY": {"symbol": "¥", "decimals": 2, "symbol_first": True},
    "JPY": {"symbol": "¥", "decimals": 0, "symbol_first": True},
    "IDR": {"symbol": "Rp", "decimals": 2, "symbol_first": True},
}

#: Exchange rates are stored and computed at this precision (settings spec §18):
#: high-precision Decimal, never float.
EXCHANGE_RATE_PRECISION = Decimal("0.000001")


def currency_decimals(currency: str) -> int:
    display = CURRENCY_DISPLAY.get((currency or "THB").upper())
    if display is not None:
        return int(display["decimals"])
    return 2 if minor_units(currency) > 1 else 0


def format_currency(amount_minor: int, currency: str = "THB", *, locale: str = "en") -> str:
    """Render an amount with the currency's own symbol and decimal places.

    ``฿1,500.00``, ``$50.00``, ``¥5,000`` — never a hard-coded two decimals.
    """
    code = (currency or "THB").upper()
    display = CURRENCY_DISPLAY.get(code, {"symbol": code + " ", "decimals": currency_decimals(code), "symbol_first": True})
    major = to_major(amount_minor, code)
    decimals = int(display["decimals"])
    quantum = Decimal(1).scaleb(-decimals) if decimals else Decimal("1")
    text = f"{major.quantize(quantum):,}"
    symbol = display["symbol"]
    if display.get("symbol_first", True):
        return f"{symbol}{text}"
    return f"{text} {symbol}"


def parse_rate(rate: str | int | float | Decimal) -> Decimal:
    """Normalise an exchange rate to the platform's fixed precision.

    Accepts a string at the configuration boundary (the only safe way to carry
    ``33.100000`` from a form) and quantizes to six decimal places. Floats are
    tolerated but routed through ``Decimal(str(...))`` so ``33.1`` is exact.
    """
    value = Decimal(str(rate))
    if value <= 0:
        raise ValueError("exchange rate must be positive")
    return value.quantize(EXCHANGE_RATE_PRECISION, rounding=ROUND_HALF_UP)


def rate_direction_label(from_currency: str, to_currency: str, rate: Decimal | str) -> str:
    """Unambiguous direction string, ``1 USD = 33.10 THB`` (settings spec §21)."""
    normalized = parse_rate(rate)
    # Trim trailing zeros for display without losing the stored precision.
    shown = normalized.normalize()
    if shown == shown.to_integral():
        shown = shown.to_integral()
    return f"1 {from_currency.upper()} = {shown} {to_currency.upper()}"


def convert_currency(
    amount_minor: int,
    *,
    rate: Decimal | str,
    from_currency: str,
    to_currency: str,
) -> int:
    """Convert a minor-unit amount from one currency to another at ``rate``.

    ``rate`` is the number of ``to_currency`` units per one ``from_currency`` unit
    (``1 USD = 33.10 THB`` → ``rate=33.10``, from=USD, to=THB). The conversion is
    done in major units at full rate precision and then re-expressed in the target
    currency's minor units, so JPY (no minor unit) and THB (satang) both come out
    correct. The result is what gets *snapshotted* onto the transaction; it is
    never recomputed later (settings spec §20).
    """
    normalized = parse_rate(rate)
    from_major = to_major(int(amount_minor), from_currency)
    to_major_value = (from_major * normalized)
    to_factor = minor_units(to_currency)
    minor = (to_major_value * to_factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)


def allocate(total_minor: int, weights: list[int]) -> list[int]:
    """Distribute ``total_minor`` across ``weights`` losing not one minor unit.

    A cart-level discount must be pushed down onto individual booking items for
    reporting and partial refunds. Largest-remainder allocation guarantees the
    parts sum exactly to the whole, which is what makes a partial refund of one
    item provably correct.
    """
    total_weight = sum(weights)
    if total_weight <= 0:
        return [0] * len(weights)
    raw = [Decimal(total_minor) * Decimal(w) / Decimal(total_weight) for w in weights]
    floors = [int(v.quantize(Decimal("1"), rounding=ROUND_DOWN)) for v in raw]
    shortfall = int(total_minor) - sum(floors)
    remainders = sorted(
        range(len(weights)), key=lambda i: (raw[i] - floors[i], weights[i]), reverse=True
    )
    for i in range(shortfall):
        floors[remainders[i % len(remainders)]] += 1
    return floors


__all__ = [
    "CURRENCY_DISPLAY",
    "DEFAULT_MINOR_UNITS",
    "EXCHANGE_RATE_PRECISION",
    "ChargeBreakdown",
    "ChargeInput",
    "RoundingMode",
    "TaxSplit",
    "allocate",
    "apply_percentage",
    "apply_rounding",
    "compute_charges",
    "convert_currency",
    "currency_decimals",
    "format_amount",
    "format_currency",
    "minor_units",
    "parse_rate",
    "rate_direction_label",
    "split_tax",
    "to_major",
    "to_minor",
]
