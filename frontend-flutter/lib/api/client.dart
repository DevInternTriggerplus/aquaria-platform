import 'dart:convert';

import 'package:http/http.dart' as http;

/// Typed client for the Django API.
///
/// The backend returns one error envelope for everything —
/// `{ error: { code, message, message_key, reference, details } }` — so this is the
/// only place that unwraps it. `message` is already customer-safe and localized.
class ApiException implements Exception {
  ApiException(this.message, {this.code = 'request_failed', this.reference, this.fields = const {}});

  final String message;
  final String code;
  final String? reference;
  final Map<String, String> fields;

  @override
  String toString() => message;
}

class ApiClient {
  ApiClient({required this.baseUrl, required this.venueCode, http.Client? httpClient})
      : _http = httpClient ?? http.Client();

  final String baseUrl;
  final String venueCode;
  final http.Client _http;

  Uri _uri(String path, [Map<String, String>? query]) =>
      Uri.parse('$baseUrl/api/venues/$venueCode$path').replace(queryParameters: query);

  Future<Map<String, dynamic>> _get(String path, [Map<String, String>? query]) async {
    final response = await _http.get(_uri(path, query), headers: const {
      'Accept': 'application/json',
    });
    final body = response.body.isEmpty
        ? <String, dynamic>{}
        : jsonDecode(response.body) as Map<String, dynamic>;

    if (response.statusCode >= 400) {
      final error = (body['error'] as Map<String, dynamic>?) ?? const {};
      final details = (error['details'] as Map<String, dynamic>?) ?? const {};
      final fields = (details['fields'] as Map<String, dynamic>?) ?? const {};
      // Prefer the specific per-field message: far more useful than a generic
      // "check the highlighted fields".
      final firstField = fields.values.isEmpty ? null : fields.values.first?.toString();
      throw ApiException(
        firstField ?? (error['message'] as String? ?? 'Something went wrong. Please try again.'),
        code: error['code'] as String? ?? 'request_failed',
        reference: error['reference'] as String?,
        fields: fields.map((k, v) => MapEntry(k, v.toString())),
      );
    }
    return body;
  }

  Future<Venue> venue() async => Venue.fromJson(await _get('/'));

  Future<ProductsResponse> products(String date, {String channel = 'ONLINE'}) async =>
      ProductsResponse.fromJson(await _get('/products/', {'date': date, 'channel': channel}));

  Future<ChargeBreakdown> chargePreview(int baseMinor, String date) async =>
      ChargeBreakdown.fromJson(
        await _get('/charge-preview/', {'base_minor': '$baseMinor', 'date': date}),
      );

  Future<List<PaymentType>> paymentTypes({String channel = 'ONLINE'}) async {
    final body = await _get('/payment-types/', {'channel': channel});
    return ((body['payment_types'] as List<dynamic>?) ?? const [])
        .map((e) => PaymentType.fromJson(e as Map<String, dynamic>))
        .toList();
  }
}

/// Picks a language out of a translatable map, falling back to English.
String pick(Map<String, dynamic>? text, {String lang = 'en', String fallback = ''}) {
  if (text == null || text.isEmpty) return fallback;
  return (text[lang] ?? text['en'] ?? text.values.first ?? fallback).toString();
}

class Venue {
  Venue({
    required this.id,
    required this.code,
    required this.name,
    required this.timezone,
    required this.currency,
    required this.taxModel,
    required this.address,
    required this.operatingHours,
  });

  final String id;
  final String code;
  final Map<String, dynamic> name;
  final String timezone;
  final String currency;
  final String taxModel;
  final String address;
  final Map<String, dynamic> operatingHours;

  factory Venue.fromJson(Map<String, dynamic> json) => Venue(
        id: json['id'] as String,
        code: json['code'] as String,
        name: (json['name'] as Map<String, dynamic>?) ?? const {},
        timezone: json['timezone'] as String? ?? 'UTC',
        currency: json['currency'] as String? ?? 'THB',
        taxModel: json['tax_model'] as String? ?? 'INCLUSIVE',
        address: json['address'] as String? ?? '',
        operatingHours: (json['operating_hours'] as Map<String, dynamic>?) ?? const {},
      );

  String get displayName => pick(name, fallback: code);
}

class TicketType {
  TicketType({
    required this.id,
    required this.code,
    required this.name,
    required this.description,
    required this.segmentCode,
    required this.maxQuantity,
    required this.unitPriceMinor,
    required this.currency,
  });

  final String id;
  final String code;
  final Map<String, dynamic> name;
  final Map<String, dynamic> description;
  final String segmentCode;
  final int? maxQuantity;

  /// Null means no price rule matched, so this type is not sellable for the
  /// requested date and channel. The UI says so rather than showing it as free.
  final int? unitPriceMinor;
  final String currency;

  factory TicketType.fromJson(Map<String, dynamic> json) => TicketType(
        id: json['id'] as String,
        code: json['code'] as String,
        name: (json['name'] as Map<String, dynamic>?) ?? const {},
        description: (json['description'] as Map<String, dynamic>?) ?? const {},
        segmentCode: json['segment_code'] as String? ?? '',
        maxQuantity: json['max_quantity'] as int?,
        unitPriceMinor: json['unit_price_minor'] as int?,
        currency: json['currency'] as String? ?? 'THB',
      );

  bool get sellable => unitPriceMinor != null;
  String get displayName => pick(name, fallback: code);
}

class Product {
  Product({
    required this.id,
    required this.code,
    required this.name,
    required this.description,
    required this.maxPerBooking,
    required this.ticketTypes,
  });

  final String id;
  final String code;
  final Map<String, dynamic> name;
  final Map<String, dynamic> description;
  final int maxPerBooking;
  final List<TicketType> ticketTypes;

  factory Product.fromJson(Map<String, dynamic> json) => Product(
        id: json['id'] as String,
        code: json['code'] as String,
        name: (json['name'] as Map<String, dynamic>?) ?? const {},
        description: (json['description'] as Map<String, dynamic>?) ?? const {},
        maxPerBooking: json['max_per_booking'] as int? ?? 10,
        ticketTypes: ((json['ticket_types'] as List<dynamic>?) ?? const [])
            .map((e) => TicketType.fromJson(e as Map<String, dynamic>))
            .toList(),
      );

  String get displayName => pick(name, fallback: code);
}

class ProductsResponse {
  ProductsResponse({required this.date, required this.currency, required this.products});

  final String date;
  final String currency;
  final List<Product> products;

  factory ProductsResponse.fromJson(Map<String, dynamic> json) => ProductsResponse(
        date: json['date'] as String? ?? '',
        currency: json['currency'] as String? ?? 'THB',
        products: ((json['products'] as List<dynamic>?) ?? const [])
            .map((e) => Product.fromJson(e as Map<String, dynamic>))
            .toList(),
      );
}

/// The server's authoritative money breakdown. The app displays these numbers and
/// never recomputes them.
class ChargeBreakdown {
  ChargeBreakdown({
    required this.subtotalMinor,
    required this.serviceChargeMinor,
    required this.serviceChargeIncluded,
    required this.vatMinor,
    required this.vatIncluded,
    required this.roundingAdjustmentMinor,
    required this.grandTotalMinor,
    required this.currency,
  });

  final int subtotalMinor;
  final int serviceChargeMinor;
  final bool serviceChargeIncluded;
  final int vatMinor;
  final bool vatIncluded;
  final int roundingAdjustmentMinor;
  final int grandTotalMinor;
  final String currency;

  factory ChargeBreakdown.fromJson(Map<String, dynamic> json) => ChargeBreakdown(
        subtotalMinor: json['subtotal_minor'] as int? ?? 0,
        serviceChargeMinor: json['service_charge_minor'] as int? ?? 0,
        serviceChargeIncluded: json['service_charge_included'] as bool? ?? false,
        vatMinor: json['vat_minor'] as int? ?? 0,
        vatIncluded: json['vat_included'] as bool? ?? false,
        roundingAdjustmentMinor: json['rounding_adjustment_minor'] as int? ?? 0,
        grandTotalMinor: json['grand_total_minor'] as int? ?? 0,
        currency: json['currency'] as String? ?? 'THB',
      );
}

class PaymentType {
  PaymentType({required this.id, required this.code, required this.method, required this.displayName});

  final String id;
  final String code;
  final String method;
  final Map<String, dynamic> displayName;

  factory PaymentType.fromJson(Map<String, dynamic> json) => PaymentType(
        id: json['id'] as String,
        code: json['code'] as String,
        method: json['method'] as String? ?? '',
        displayName: (json['display_name'] as Map<String, dynamic>?) ?? const {},
      );

  String get label => pick(displayName, fallback: code);
}
