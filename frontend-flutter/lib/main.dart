import 'package:flutter/material.dart';

import 'api/client.dart';
import 'screens/booking_screen.dart';
import 'theme.dart';

/// Entry point.
///
/// The backend origin and venue code come from `--dart-define` so one build can
/// point at dev, staging or production without a code change:
///
///   flutter run --dart-define=BACKEND_ORIGIN=http://10.0.2.2:8000 \
///               --dart-define=VENUE_CODE=aqp
void main() {
  const backendOrigin = String.fromEnvironment(
    'BACKEND_ORIGIN',
    defaultValue: 'http://127.0.0.1:8000',
  );
  const venueCode = String.fromEnvironment('VENUE_CODE', defaultValue: 'aqp');

  runApp(
    AquariaApp(
      client: ApiClient(baseUrl: backendOrigin, venueCode: venueCode),
    ),
  );
}

class AquariaApp extends StatelessWidget {
  const AquariaApp({super.key, required this.client});

  final ApiClient client;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Aquaria Booking',
      debugShowCheckedModeBanner: false,
      theme: AquariaTheme.light(),
      home: BookingScreen(client: client),
    );
  }
}
