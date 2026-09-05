import 'package:flutter/material.dart';

import '../api/client.dart';
import '../money.dart';
import '../theme.dart';

class SummaryLine {
  const SummaryLine({
    required this.code,
    required this.label,
    required this.quantity,
    required this.amountMinor,
  });

  final String code;
  final String label;
  final int quantity;
  final int amountMinor;
}

/// The order summary: a gradient "ticket head" above a white body.
///
/// Every figure comes from the server's charge breakdown, so what the guest reads
/// is exactly what will be charged.
class OrderSummary extends StatelessWidget {
  const OrderSummary({
    super.key,
    required this.venueName,
    required this.locality,
    required this.visitDate,
    required this.lines,
    required this.charges,
    required this.currency,
    required this.onContinue,
    this.busy = false,
  });

  final String venueName;
  final String locality;
  final String? visitDate;
  final List<SummaryLine> lines;
  final ChargeBreakdown? charges;
  final String currency;
  final VoidCallback onContinue;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final totalTickets = lines.fold<int>(0, (sum, l) => sum + l.quantity);

    return Card(
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _TicketHead(venueName: venueName, locality: locality),
          Padding(
            padding: const EdgeInsets.all(18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text('Your order', style: Theme.of(context).textTheme.titleLarge),
                    if (totalTickets > 0)
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
                        decoration: BoxDecoration(
                          color: AquariaColors.secondary,
                          borderRadius: BorderRadius.circular(999),
                        ),
                        child: Text(
                          '$totalTickets ${totalTickets == 1 ? 'ticket' : 'tickets'}',
                          style: const TextStyle(
                            fontSize: 11.5,
                            fontWeight: FontWeight.bold,
                            color: AquariaColors.primaryDeep,
                          ),
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 14),
                Row(
                  children: [
                    Expanded(child: _Tile(label: 'Date', value: visitDate ?? 'Not selected')),
                    const SizedBox(width: 10),
                    Expanded(child: _Tile(label: 'Venue', value: venueName)),
                  ],
                ),
                const SizedBox(height: 14),
                if (lines.isEmpty)
                  const Text(
                    'No tickets added yet.',
                    style: TextStyle(fontSize: 13.5, color: AquariaColors.mutedForeground),
                  )
                else
                  ...lines.map(
                    (line) => Padding(
                      padding: const EdgeInsets.only(bottom: 6),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          Expanded(
                            child: Text(
                              '${line.quantity} × ${line.label}',
                              style: const TextStyle(color: AquariaColors.mutedForeground),
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                          Text(
                            Money.format(line.amountMinor, currency: currency),
                            style: const TextStyle(
                              fontWeight: FontWeight.w600,
                              fontFeatures: [FontFeature.tabularFigures()],
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                if (charges != null) ...[
                  const Divider(height: 20),
                  _Row(label: 'Subtotal', value: Money.format(charges!.subtotalMinor, currency: currency)),
                  if (charges!.serviceChargeMinor != 0)
                    _Row(
                      label: 'Service charge${charges!.serviceChargeIncluded ? ' (included)' : ''}',
                      value: Money.format(charges!.serviceChargeMinor, currency: currency),
                    ),
                  if (charges!.vatMinor != 0)
                    _Row(
                      label: 'VAT${charges!.vatIncluded ? ' (included)' : ''}',
                      value: Money.format(charges!.vatMinor, currency: currency),
                    ),
                  // Never let displayed lines silently fail to sum to the total.
                  if (charges!.roundingAdjustmentMinor != 0)
                    _Row(
                      label: 'Rounding',
                      value:
                          '${charges!.roundingAdjustmentMinor < 0 ? '−' : '+'}${Money.format(charges!.roundingAdjustmentMinor.abs(), currency: currency)}',
                    ),
                ],
                const Divider(height: 20),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text('Total', style: TextStyle(fontWeight: FontWeight.w600)),
                        Text(
                          (charges?.vatIncluded ?? true) ? 'VAT included' : 'VAT added at payment',
                          style: const TextStyle(
                            fontSize: 11,
                            color: AquariaColors.mutedForeground,
                          ),
                        ),
                      ],
                    ),
                    Text(
                      Money.format(charges?.grandTotalMinor ?? 0, currency: currency),
                      style: const TextStyle(
                        fontFamily: 'Georgia',
                        fontSize: 27,
                        fontWeight: FontWeight.w600,
                        color: AquariaColors.primary,
                        fontFeatures: [FontFeature.tabularFigures()],
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 14),
                FilledButton(
                  onPressed: (busy || totalTickets == 0) ? null : onContinue,
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      Text(busy ? 'Working…' : 'Make a Payment'),
                      const Icon(Icons.arrow_forward, size: 18),
                    ],
                  ),
                ),
                const SizedBox(height: 8),
                const Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(Icons.lock_outline, size: 12, color: AquariaColors.mutedForeground),
                    SizedBox(width: 5),
                    Text(
                      'Secure checkout · QR e-ticket emailed instantly',
                      style: TextStyle(fontSize: 11, color: AquariaColors.mutedForeground),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _TicketHead extends StatelessWidget {
  const _TicketHead({required this.venueName, required this.locality});

  final String venueName;
  final String locality;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(22, 22, 22, 24),
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [AquariaColors.primary, AquariaColors.primary, AquariaColors.accent],
          stops: [0.0, 0.55, 1.6],
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'YOUR VISIT',
            style: TextStyle(
              fontSize: 10,
              fontWeight: FontWeight.bold,
              letterSpacing: 2.5,
              color: Color(0xCCF7FBFC),
            ),
          ),
          const SizedBox(height: 4),
          Text(
            venueName,
            style: const TextStyle(
              fontFamily: 'Georgia',
              fontSize: 26,
              fontWeight: FontWeight.w600,
              color: AquariaColors.primaryForeground,
            ),
          ),
          if (locality.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                locality,
                style: const TextStyle(fontSize: 12, color: Color(0xD9F7FBFC)),
              ),
            ),
        ],
      ),
    );
  }
}

class _Tile extends StatelessWidget {
  const _Tile({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: AquariaColors.secondary.withValues(alpha: 0.45),
        border: Border.all(color: AquariaColors.border),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: const TextStyle(fontSize: 11, color: AquariaColors.mutedForeground),
          ),
          const SizedBox(height: 2),
          Text(
            value,
            style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
            overflow: TextOverflow.ellipsis,
          ),
        ],
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(label, style: const TextStyle(color: AquariaColors.mutedForeground)),
          Text(
            value,
            style: const TextStyle(
              fontWeight: FontWeight.w600,
              fontFeatures: [FontFeature.tabularFigures()],
            ),
          ),
        ],
      ),
    );
  }
}
