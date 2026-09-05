# Product

This is the **Universal Ticketing, Booking & Access Management Platform** — a
multi-tenant system that sells, reserves, delivers, validates and reconciles admission
across every channel a venue operates: online booking, self-service kiosk, counter POS,
partner/agent, entrance gate scanners and back office.

The requirements are the authoritative spec: `.kiro/specs/universal-ticketing-platform/requirements.md`
(77 requirements, R1–R77), plus the injected `additional_features` rule for the
Business/Venue Settings module (VAT, service charge, timezone, QR validity, currency,
exchange rates).

## Non-negotiable design principles

These come straight from the spec and constrain every change:

1. **Configuration over code.** A new venue type must be launchable by an administrator
   with no code change or deployment. Business behaviour lives in tenant-scoped
   configuration records, never in compiled constants.
2. **Generic domain language.** Entities are `Tenant`, `Organization`, `Venue`,
   `Experience`, `Show`, `Session`, `Product`, `TicketType`, `CustomerSegment`,
   `Booking`, `Ticket`, `AccessRight`, `AccessPoint`, `Device`. Names like
   `AquariumShow` or `AquariumTicket` are **prohibited** — there must be no
   aquarium-specific table, enum value, field or code path.
3. **Default deny.** Any permission not explicitly granted is denied.
4. **Never oversell.** Capacity is authoritative and enforced under concurrency.
5. **Financial and audit records are never physically deleted.** DELETE maps to
   Cancel / Void / Archive / Deactivate for protected records.
6. **Fast and obvious for the operator; effortless for the customer.**

## First deployment: Aquaria Phuket

Aquaria Phuket (an aquarium in Phuket, Thailand) is the first production tenant. It is
implemented **entirely as configuration data** in `seed.py` — no aquarium-specific code.
Key facts that show up in seed data and tests: currency THB, timezone `Asia/Bangkok`,
7% VAT inclusive, operating hours 10:30–19:00, nine zones, segments Adult/Child/Senior,
international vs Thai-resident pricing (THB 1,251 vs 621 adult online). Business context
is in the workspace-root `deep-research-report.md`.

## Language and audience

- Customer-facing flows support at minimum Thai and English.
- Thailand PDPA consent is mandatory before personal data is submitted, in every channel.
- Money, tax and audit correctness are treated as the highest-priority properties.
