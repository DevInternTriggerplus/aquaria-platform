import 'package:flutter/material.dart';

import '../api/client.dart';
import '../money.dart';
import '../theme.dart';
import 'segment_icon.dart';

/// One ticket type as a single-line row: small segment icon, name and price, then
/// the quantity stepper.
///
/// A type with no resolved price is shown as unavailable rather than free.
class TicketTypeRow extends StatelessWidget {
  const TicketTypeRow({
    super.key,
    required this.ticketType,
    required this.quantity,
    required this.max,
    required this.onChanged,
  });

  final TicketType ticketType;
  final int quantity;
  final int max;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) {
    final label = ticketType.displayName;
    final description = pick(ticketType.description);
    final sellable = ticketType.sellable;

    return Container(
      decoration: const BoxDecoration(
        border: Border(top: BorderSide(color: AquariaColors.border)),
      ),
      padding: const EdgeInsets.symmetric(vertical: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          SegmentIcon(segment: ticketType.segmentCode.isNotEmpty
              ? ticketType.segmentCode
              : ticketType.code),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        label,
                        style: const TextStyle(fontWeight: FontWeight.w600),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                    const SizedBox(width: 8),
                    Text(
                      sellable
                          ? Money.format(ticketType.unitPriceMinor, currency: ticketType.currency)
                          : 'Unavailable',
                      style: const TextStyle(
                        fontWeight: FontWeight.w600,
                        fontFeatures: [FontFeature.tabularFigures()],
                      ),
                    ),
                  ],
                ),
                if (description.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      description,
                      style: const TextStyle(
                        fontSize: 12.5,
                        color: AquariaColors.mutedForeground,
                      ),
                    ),
                  ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          _StepperButton(
            icon: Icons.remove,
            tooltip: 'Remove one $label',
            onPressed: (!sellable || quantity == 0) ? null : () => onChanged(quantity - 1),
          ),
          SizedBox(
            width: 32,
            child: Text(
              '$quantity',
              textAlign: TextAlign.center,
              style: const TextStyle(
                fontWeight: FontWeight.bold,
                fontFeatures: [FontFeature.tabularFigures()],
              ),
            ),
          ),
          _StepperButton(
            icon: Icons.add,
            tooltip: 'Add one $label',
            onPressed: (!sellable || quantity >= max) ? null : () => onChanged(quantity + 1),
          ),
        ],
      ),
    );
  }
}

class _StepperButton extends StatelessWidget {
  const _StepperButton({required this.icon, required this.tooltip, this.onPressed});

  final IconData icon;
  final String tooltip;
  final VoidCallback? onPressed;

  @override
  Widget build(BuildContext context) {
    // 44dp minimum, so the control is comfortably tappable.
    return SizedBox(
      width: AquariaTheme.minTapTarget,
      height: AquariaTheme.minTapTarget,
      child: IconButton(
        onPressed: onPressed,
        tooltip: tooltip,
        icon: Icon(icon, size: 18),
        style: IconButton.styleFrom(
          foregroundColor: AquariaColors.primaryDeep,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(8),
            side: const BorderSide(color: AquariaColors.primary),
          ),
        ),
      ),
    );
  }
}
