# Aquaria — Flutter customer booking app

Flutter 3 / Dart 3, Material 3. Dependencies are deliberately minimal: `http` and
`intl`. Money, tax, availability and every rule live in the backend.

> **Not built on the development machine.** The Flutter and Dart SDKs were not
> installed, so this source has never been through `flutter pub get`, `flutter
> analyze` or a build. It is coherent and complete but unexercised — expect to fix
> small issues on first run.

## Run

```bash
flutter pub get
flutter analyze          # worth running first

flutter run \
  --dart-define=BACKEND_ORIGIN=http://127.0.0.1:8000 \
  --dart-define=VENUE_CODE=aqp
```

Origin and venue come from `--dart-define`, so one build can target dev, staging or
production without a code change.

**Android emulator:** the host is `http://10.0.2.2:8000`, not `127.0.0.1`. For
plain-HTTP development you will also need a network-security config or
`usesCleartextTraffic`, since Android blocks cleartext by default. Use HTTPS
anywhere real.

## Structure

```
lib/
├── main.dart                     entry point, dart-define config
├── theme.dart                    design tokens, 44dp minimum tap target
├── money.dart                    formatting only, never arithmetic
├── api/client.dart               typed client + models, single error unwrap
├── screens/booking_screen.dart   the booking flow
└── widgets/
    ├── segment_icon.dart         small inline icon per ticket type
    ├── ticket_type_row.dart      single-line row: icon, name, price, stepper
    └── order_summary.dart        gradient ticket-head summary
```

## Rules this client follows

- **It never computes money.** `chargePreview` asks the server for the breakdown;
  the app formats integer minor units and nothing else.
- **It never assumes two decimal places.** JPY has none.
- **A missing price means unavailable, not free.** `unitPriceMinor == null` renders
  as "Unavailable" and the stepper stays disabled.
- **Tap targets are at least 44dp.**
- **Server errors are shown as sent**, preferring the specific per-field message
  over the generic one.

## Not wired up yet

Checkout — PDPA consent, capacity hold, payment — is deliberately absent, matching
the Next client. Those paths are being ported to Django with their tests first. See
the repository README for the port order.

Platform folders (`android/`, `ios/`, `web/`) are not committed; run
`flutter create . --platforms=android,ios,web` in this directory to generate them.
