import 'package:flutter/material.dart';

/// Design tokens shared with the Next and legacy web clients so all three read as
/// one product.
///
/// The palette is the ocean set: a deep peacock primary with a warm amber accent.
/// The accent never carries meaning alone — every status also has a label — because
/// colour on its own is not an accessible cue.
class AquariaColors {
  const AquariaColors._();

  // oklch(0.44 0.115 213) and friends, converted to sRGB.
  static const primary = Color(0xFF1C6B84);
  static const primaryDeep = Color(0xFF17384C);
  static const primaryForeground = Color(0xFFF7FBFC);
  static const secondary = Color(0xFFE2EFF2);
  static const background = Color(0xFFF7FBFC);
  static const card = Color(0xFFFFFFFF);
  static const foreground = Color(0xFF17242E);
  static const mutedForeground = Color(0xFF6B7A85);
  static const border = Color(0xFFDBE7EB);
  static const accent = Color(0xFFE8A34C);
  static const accentForeground = Color(0xFF2A2015);
  static const success = Color(0xFF2E9E7E);
  static const warning = Color(0xFFE0A33A);
  static const danger = Color(0xFFB0453A);
}

class AquariaTheme {
  const AquariaTheme._();

  /// Minimum tappable size. 44dp is the floor for touch targets.
  static const double minTapTarget = 44;
  static const double cardRadius = 14;

  static ThemeData light() {
    final base = ThemeData.light(useMaterial3: true);
    return base.copyWith(
      scaffoldBackgroundColor: AquariaColors.background,
      colorScheme: base.colorScheme.copyWith(
        primary: AquariaColors.primary,
        onPrimary: AquariaColors.primaryForeground,
        secondary: AquariaColors.accent,
        onSecondary: AquariaColors.accentForeground,
        surface: AquariaColors.card,
        onSurface: AquariaColors.foreground,
        error: AquariaColors.danger,
      ),
      textTheme: base.textTheme.copyWith(
        // Serif for display headings, mirroring the web clients' pairing.
        displaySmall: base.textTheme.displaySmall?.copyWith(
          fontFamily: 'Georgia',
          fontWeight: FontWeight.w600,
          color: AquariaColors.foreground,
        ),
        headlineMedium: base.textTheme.headlineMedium?.copyWith(
          fontFamily: 'Georgia',
          fontWeight: FontWeight.w600,
        ),
        titleLarge: base.textTheme.titleLarge?.copyWith(
          fontFamily: 'Georgia',
          fontWeight: FontWeight.w600,
        ),
      ),
      cardTheme: CardThemeData(
        color: AquariaColors.card,
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(cardRadius),
          side: const BorderSide(color: AquariaColors.border),
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size.fromHeight(52),
          backgroundColor: AquariaColors.primary,
          foregroundColor: AquariaColors.primaryForeground,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
          textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
        ),
      ),
      dividerTheme: const DividerThemeData(color: AquariaColors.border, space: 1, thickness: 1),
    );
  }
}
