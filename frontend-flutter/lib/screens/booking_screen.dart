import 'dart:async';

import 'package:flutter/material.dart';

import '../api/client.dart';
import '../theme.dart';
import '../widgets/order_summary.dart';
import '../widgets/ticket_type_row.dart';

/// The booking flow: date, then tickets, with the order summary below on phones
/// and beside the steps on tablets.
///
/// Prices and totals are always fetched from the backend. The app never computes
/// money, so there is exactly one source of truth for what a guest pays.
class BookingScreen extends StatefulWidget {
  const BookingScreen({super.key, required this.client});

  final ApiClient client;

  @override
  State<BookingScreen> createState() => _BookingScreenState();
}

class _BookingScreenState extends State<BookingScreen> {
  Venue? _venue;
  List<Product> _products = const [];
  String? _visitDate;
  final Map<String, int> _quantities = {};
  ChargeBreakdown? _charges;
  String? _error;
  bool _busy = false;
  Timer? _debounce;

  @override
  void initState() {
    super.initState();
    _loadVenue();
  }

  @override
  void dispose() {
    _debounce?.cancel();
    super.dispose();
  }

  Future<void> _loadVenue() async {
    try {
      final venue = await widget.client.venue();
      if (mounted) setState(() => _venue = venue);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  Future<void> _selectDate(String date) async {
    setState(() {
      _visitDate = date;
      _quantities.clear();
      _charges = null;
      _error = null;
    });
    try {
      final response = await widget.client.products(date);
      if (mounted) setState(() => _products = response.products);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  int get _baseMinor {
    var sum = 0;
    for (final product in _products) {
      for (final type in product.ticketTypes) {
        sum += (_quantities[type.id] ?? 0) * (type.unitPriceMinor ?? 0);
      }
    }
    return sum;
  }

  /// Ask the server for the breakdown. Debounced so a rapid tap on the stepper
  /// does not fire a request per tap.
  void _refreshCharges() {
    _debounce?.cancel();
    final date = _visitDate;
    final base = _baseMinor;
    if (date == null || base <= 0) {
      setState(() => _charges = null);
      return;
    }
    _debounce = Timer(const Duration(milliseconds: 220), () async {
      try {
        final charges = await widget.client.chargePreview(base, date);
        if (mounted) setState(() => _charges = charges);
      } on ApiException catch (e) {
        if (mounted) setState(() => _error = e.message);
      }
    });
  }

  void _setQuantity(String id, int next) {
    setState(() => _quantities[id] = next);
    _refreshCharges();
  }

  List<SummaryLine> get _lines {
    final lines = <SummaryLine>[];
    for (final product in _products) {
      for (final type in product.ticketTypes) {
        final qty = _quantities[type.id] ?? 0;
        if (qty > 0) {
          lines.add(SummaryLine(
            code: type.id,
            label: type.displayName,
            quantity: qty,
            amountMinor: qty * (type.unitPriceMinor ?? 0),
          ));
        }
      }
    }
    return lines;
  }

  List<String> get _upcomingDates {
    final today = DateTime.now();
    return List.generate(14, (i) {
      final d = today.add(Duration(days: i));
      return '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';
    });
  }

  void _onContinue() {
    // The confirm flow (PDPA consent, hold, payment) is the next port from the
    // reference implementation. It must not be faked here: taking money is the one
    // path that has to be right before it ships.
    setState(() {
      _error = 'Checkout is not wired up yet in this client. See the repository README.';
    });
  }

  @override
  Widget build(BuildContext context) {
    final venueName = _venue?.displayName ?? 'Aquaria';
    final currency = _venue?.currency ?? 'THB';
    final wide = MediaQuery.sizeOf(context).width >= 900;

    final steps = Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _StepCard(
          step: 1,
          title: 'Choose your visit date',
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SizedBox(
                height: 76,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: _upcomingDates.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 8),
                  itemBuilder: (context, index) {
                    final date = _upcomingDates[index];
                    final parsed = DateTime.parse(date);
                    final selected = date == _visitDate;
                    return _DateChip(
                      date: parsed,
                      selected: selected,
                      onTap: () => _selectDate(date),
                    );
                  },
                ),
              ),
              const SizedBox(height: 10),
              Text(
                _visitDate == null
                    ? "Choose a date to see today's prices and availability."
                    : 'Selected $_visitDate.',
                style: const TextStyle(fontSize: 13, color: AquariaColors.mutedForeground),
              ),
            ],
          ),
        ),
        if (_visitDate != null) ...[
          const SizedBox(height: 16),
          _StepCard(
            step: 2,
            title: 'Choose your tickets',
            child: _products.isEmpty
                ? const Padding(
                    padding: EdgeInsets.symmetric(vertical: 20),
                    child: Center(
                      child: Text(
                        'No tickets are on sale for this date.',
                        style: TextStyle(color: AquariaColors.mutedForeground),
                      ),
                    ),
                  )
                : Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      for (final product in _products) ...[
                        Text(
                          product.displayName,
                          style: const TextStyle(fontWeight: FontWeight.w600),
                        ),
                        const SizedBox(height: 4),
                        for (final type in product.ticketTypes)
                          TicketTypeRow(
                            ticketType: type,
                            quantity: _quantities[type.id] ?? 0,
                            max: type.maxQuantity ?? product.maxPerBooking,
                            onChanged: (next) => _setQuantity(type.id, next),
                          ),
                        const SizedBox(height: 12),
                      ],
                    ],
                  ),
          ),
        ],
      ],
    );

    final summary = OrderSummary(
      venueName: venueName,
      locality: _venue?.address ?? '',
      visitDate: _visitDate,
      lines: _lines,
      charges: _charges,
      currency: currency,
      onContinue: _onContinue,
      busy: _busy,
    );

    return Scaffold(
      body: CustomScrollView(
        slivers: [
          SliverAppBar(
            pinned: true,
            backgroundColor: AquariaColors.card.withValues(alpha: 0.85),
            surfaceTintColor: Colors.transparent,
            title: Text(
              venueName,
              style: const TextStyle(
                fontFamily: 'Georgia',
                fontWeight: FontWeight.w600,
                color: AquariaColors.foreground,
              ),
            ),
            leading: const Icon(Icons.waves, color: AquariaColors.primary),
          ),
          SliverToBoxAdapter(child: _Hero(venueName: venueName)),
          SliverPadding(
            padding: const EdgeInsets.all(16),
            sliver: SliverToBoxAdapter(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (_error != null)
                    Container(
                      margin: const EdgeInsets.only(bottom: 16),
                      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                      decoration: BoxDecoration(
                        color: AquariaColors.card,
                        border: Border.all(color: AquariaColors.danger),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Text(
                        _error!,
                        style: const TextStyle(color: AquariaColors.danger, fontSize: 13.5),
                      ),
                    ),
                  if (wide)
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Expanded(flex: 14, child: steps),
                        const SizedBox(width: 24),
                        Expanded(flex: 10, child: summary),
                      ],
                    )
                  else ...[
                    steps,
                    const SizedBox(height: 20),
                    summary,
                  ],
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _Hero extends StatelessWidget {
  const _Hero({required this.venueName});

  final String venueName;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(20, 36, 20, 40),
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.centerLeft,
          end: Alignment.centerRight,
          colors: [AquariaColors.primaryDeep, AquariaColors.primary],
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 5),
            decoration: BoxDecoration(
              color: AquariaColors.accent,
              borderRadius: BorderRadius.circular(999),
            ),
            child: const Text(
              'Online booking',
              style: TextStyle(
                fontSize: 11.5,
                fontWeight: FontWeight.bold,
                color: AquariaColors.accentForeground,
              ),
            ),
          ),
          const SizedBox(height: 16),
          Text(
            'Book your visit to $venueName',
            style: const TextStyle(
              fontFamily: 'Georgia',
              fontSize: 30,
              height: 1.15,
              fontWeight: FontWeight.w600,
              color: AquariaColors.primaryForeground,
            ),
          ),
          const SizedBox(height: 12),
          const Text(
            'Pick your date, choose your tickets and pay securely. Your QR e-ticket '
            'arrives by email — walk straight to the gate.',
            style: TextStyle(fontSize: 14, color: Color(0xDBF7FBFC), height: 1.45),
          ),
        ],
      ),
    );
  }
}

class _StepCard extends StatelessWidget {
  const _StepCard({required this.step, required this.title, required this.child});

  final int step;
  final String title;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Container(
                  width: 26,
                  height: 26,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AquariaColors.primary,
                    shape: BoxShape.circle,
                  ),
                  child: Text(
                    '$step',
                    style: const TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.bold,
                      color: AquariaColors.primaryForeground,
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(child: Text(title, style: Theme.of(context).textTheme.titleLarge)),
              ],
            ),
            const SizedBox(height: 14),
            child,
          ],
        ),
      ),
    );
  }
}

class _DateChip extends StatelessWidget {
  const _DateChip({required this.date, required this.selected, required this.onTap});

  final DateTime date;
  final bool selected;
  final VoidCallback onTap;

  static const _weekdays = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
  static const _months = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(10),
      child: Container(
        width: 66,
        padding: const EdgeInsets.symmetric(vertical: 8),
        decoration: BoxDecoration(
          color: selected ? AquariaColors.primary : AquariaColors.card,
          border: Border.all(color: selected ? AquariaColors.primary : AquariaColors.border),
          borderRadius: BorderRadius.circular(10),
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(
              _weekdays[date.weekday - 1],
              style: TextStyle(
                fontSize: 10,
                color: selected ? const Color(0xCCF7FBFC) : AquariaColors.mutedForeground,
              ),
            ),
            Text(
              '${date.day}',
              style: TextStyle(
                fontSize: 17,
                fontWeight: FontWeight.bold,
                color: selected ? AquariaColors.primaryForeground : AquariaColors.foreground,
              ),
            ),
            Text(
              _months[date.month - 1],
              style: TextStyle(
                fontSize: 10,
                color: selected ? const Color(0xCCF7FBFC) : AquariaColors.mutedForeground,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
