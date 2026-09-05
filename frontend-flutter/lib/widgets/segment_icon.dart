import 'package:flutter/material.dart';

import '../theme.dart';

/// Small inline icon shown in front of each ticket type.
///
/// Matched to the segment code, with a neutral person mark for anything
/// unrecognised, so a venue that invents a new segment still renders sensibly.
/// Decorative reinforcement only: the ticket type's name is always beside it as
/// text, so meaning never depends on the glyph.
class SegmentIcon extends StatelessWidget {
  const SegmentIcon({super.key, required this.segment, this.size = 22});

  final String segment;
  final double size;

  IconData get _icon {
    final code = segment.toUpperCase();
    if (code.contains('ADULT')) return Icons.person_outline;
    if (code.contains('CHILD') || code.contains('KID')) return Icons.child_care_outlined;
    if (code.contains('SENIOR') || code.contains('ELDER')) return Icons.elderly_outlined;
    if (code.contains('STUDENT')) return Icons.school_outlined;
    if (code.contains('FAMILY')) return Icons.family_restroom_outlined;
    if (code.contains('GROUP')) return Icons.groups_outlined;
    return Icons.person_outline;
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: size + 6,
      height: size + 6,
      child: Center(
        child: Icon(_icon, size: size, color: AquariaColors.primaryDeep),
      ),
    );
  }
}
