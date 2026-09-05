import 'package:intl/intl.dart';

/// Money formatting only.
///
/// The server sends integer minor units plus a currency code and is authoritative
/// for every amount. This file formats; it never calculates. And it must not assume
/// two decimal places — JPY has none.
class Money {
  const Money._();

  static const Map<String, int> _minorUnits = {
    'THB': 100,
    'USD': 100,
    'EUR': 100,
    'SGD': 100,
    'MYR': 100,
    'CNY': 100,
    'IDR': 100,
    'JPY': 1,
  };

  static const Map<String, String> _symbols = {
    'THB': '฿',
    'USD': '\$',
    'EUR': '€',
    'SGD': 'S\$',
    'MYR': 'RM',
    'CNY': '¥',
    'JPY': '¥',
  };

  static int minorUnits(String currency) => _minorUnits[currency.toUpperCase()] ?? 100;

  static int decimals(String currency) => minorUnits(currency) > 1 ? 2 : 0;

  /// Renders e.g. `฿1,251.00` or `¥5,000`.
  static String format(int? amountMinor, {String currency = 'THB'}) {
    if (amountMinor == null) return '—';
    final code = currency.toUpperCase();
    final places = decimals(code);
    final major = amountMinor / minorUnits(code);
    final formatter = NumberFormat.decimalPatternDigits(decimalDigits: places);
    return '${_symbols[code] ?? '$code '}${formatter.format(major)}';
  }
}
