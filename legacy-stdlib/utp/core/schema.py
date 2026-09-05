"""Database schema.

Three invariants are enforced *here*, in the data layer, rather than only in
service code — because R46.6 requires that no API path, bulk operation, import
or administrative tool can bypass them:

1. **No oversell.** ``sessions`` carries a CHECK constraint so a confirmed count
   can never exceed capacity, and ``seat_reservations`` / ``seat_holds`` carry
   partial unique indexes so two confirmations for the same seat in the same
   session are impossible (R10.5, R57.9). Standing/GA zone capacity flows through
   the same session counter, so R58.12 is covered by the same constraint.
2. **No erasure of financial, access, consent or audit history.** DELETE
   triggers raise on the protected tables, and audit/consent rows also reject
   UPDATE (R45.3, R46.1).
3. **Tenant ownership.** Every business table carries ``tenant_id``. The
   repository layer refuses to build a query without one (R1.1).

Session and ShowSession share one physical table, discriminated by ``kind``.
They remain independently addressable concepts in the service layer
(``sessions.py`` and ``shows.py``), but sharing storage is what guarantees that a
show reservation and a timed-entry ticket contend for capacity through the *same*
authoritative mechanism, exactly as R25.8 requires.
"""

from __future__ import annotations

SCHEMA_VERSION = 10

# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

TABLES: tuple[str, ...] = (
    # ---------------------------- platform meta ---------------------------- #
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version     INTEGER PRIMARY KEY,
        applied_at  TEXT NOT NULL
    )
    """,
    # ------------------------------ tenancy -------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id               TEXT PRIMARY KEY,
        code             TEXT NOT NULL UNIQUE,
        name             TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'ACTIVE',
        default_language TEXT NOT NULL DEFAULT 'en',
        languages_json   TEXT NOT NULL DEFAULT '["en"]',
        settings_json    TEXT NOT NULL DEFAULT '{}',
        created_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS organizations (
        id           TEXT PRIMARY KEY,
        tenant_id    TEXT NOT NULL REFERENCES tenants(id),
        code         TEXT NOT NULL,
        name         TEXT NOT NULL,
        legal_name   TEXT,
        tax_id       TEXT,
        address      TEXT,
        country      TEXT NOT NULL DEFAULT 'TH',
        status       TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at   TEXT NOT NULL,
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brands (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        organization_id TEXT NOT NULL REFERENCES organizations(id),
        code            TEXT NOT NULL,
        name            TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'ACTIVE',
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venue_types (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT,
        code          TEXT NOT NULL,
        name          TEXT NOT NULL,
        terminology_json TEXT NOT NULL DEFAULT '{}',
        template_json TEXT NOT NULL DEFAULT '{}',
        status        TEXT NOT NULL DEFAULT 'ACTIVE',
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venues (
        id                 TEXT PRIMARY KEY,
        tenant_id          TEXT NOT NULL REFERENCES tenants(id),
        organization_id    TEXT NOT NULL REFERENCES organizations(id),
        brand_id           TEXT REFERENCES brands(id),
        venue_type_id      TEXT NOT NULL REFERENCES venue_types(id),
        code               TEXT NOT NULL,
        short_code         TEXT NOT NULL,
        name_json          TEXT NOT NULL,
        timezone           TEXT NOT NULL,
        currency           TEXT NOT NULL,
        rounding_mode      TEXT NOT NULL DEFAULT 'NONE',
        tax_model          TEXT NOT NULL DEFAULT 'INCLUSIVE',
        tax_rate_bp        INTEGER NOT NULL DEFAULT 0,
        tax_registration   TEXT,
        day_boundary_hour  INTEGER NOT NULL DEFAULT 0,
        address_json       TEXT NOT NULL DEFAULT '{}',
        contact_json       TEXT NOT NULL DEFAULT '{}',
        logo_url           TEXT,
        operating_hours_json TEXT NOT NULL DEFAULT '{}',
        status             TEXT NOT NULL DEFAULT 'ACTIVE',
        customer_visible   INTEGER NOT NULL DEFAULT 1,
        created_at         TEXT NOT NULL,
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS areas (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        venue_id        TEXT NOT NULL REFERENCES venues(id),
        parent_id       TEXT REFERENCES areas(id),
        code            TEXT NOT NULL,
        kind            TEXT NOT NULL DEFAULT 'ZONE',
        name_json       TEXT NOT NULL,
        description_json TEXT NOT NULL DEFAULT '{}',
        directions_json TEXT NOT NULL DEFAULT '{}',
        image_url       TEXT,
        icon            TEXT,
        floor           TEXT,
        map_ref         TEXT,
        display_order   INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'ACTIVE',
        UNIQUE (tenant_id, venue_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS access_points (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        venue_id    TEXT NOT NULL REFERENCES venues(id),
        area_id     TEXT REFERENCES areas(id),
        code        TEXT NOT NULL,
        name_json   TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'GATE',
        direction   TEXT NOT NULL DEFAULT 'IN',
        status      TEXT NOT NULL DEFAULT 'ACTIVE',
        UNIQUE (tenant_id, venue_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        venue_id          TEXT NOT NULL REFERENCES venues(id),
        access_point_id   TEXT REFERENCES access_points(id),
        code              TEXT NOT NULL,
        name              TEXT NOT NULL,
        kind              TEXT NOT NULL,
        channel           TEXT NOT NULL,
        secret_hash       TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'ACTIVE',
        config_json       TEXT NOT NULL DEFAULT '{}',
        last_seen_at      TEXT,
        cache_generated_at TEXT,
        health_json       TEXT NOT NULL DEFAULT '{}',
        created_at        TEXT NOT NULL,
        UNIQUE (tenant_id, code)
    )
    """,
    # --------------------------- configuration ----------------------------- #
    """
    CREATE TABLE IF NOT EXISTS config_values (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        scope_type     TEXT NOT NULL,
        scope_id       TEXT,
        key            TEXT NOT NULL,
        value_json     TEXT NOT NULL,
        version        INTEGER NOT NULL DEFAULT 1,
        actor_id       TEXT,
        created_at     TEXT NOT NULL,
        superseded_at  TEXT
    )
    """,
    # ---------------- business / venue settings (add_features) -------------- #
    # VAT and service charge as effective-dated, versioned records. A change is a
    # new row, never an in-place edit, so a booking made yesterday keeps yesterday's
    # rate and a rate scheduled for a future effective_from does not affect today
    # (settings spec §2, §33). The resolver picks the row with the latest
    # effective_from that is not after the transaction date.
    """
    CREATE TABLE IF NOT EXISTS charge_settings (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        scope_type     TEXT NOT NULL,          -- ORGANIZATION | VENUE
        scope_id       TEXT NOT NULL,
        charge_kind    TEXT NOT NULL,          -- VAT | SERVICE_CHARGE
        enabled        INTEGER NOT NULL DEFAULT 0,
        rate_bp        INTEGER NOT NULL DEFAULT 0,
        mode           TEXT NOT NULL DEFAULT 'EXCLUSIVE',  -- INCLUSIVE | EXCLUSIVE
        display_name   TEXT,
        tax_registration TEXT,
        effective_from TEXT NOT NULL,          -- venue-local date the row takes effect
        version        INTEGER NOT NULL DEFAULT 1,
        actor_id       TEXT,
        created_at     TEXT NOT NULL,
        superseded_at  TEXT,
        CHECK (charge_kind IN ('VAT', 'SERVICE_CHARGE')),
        CHECK (mode IN ('INCLUSIVE', 'EXCLUSIVE')),
        CHECK (rate_bp >= 0)
    )
    """,
    # Customer-facing payment types (update spec §20-§25, §49-§50). A payment type is
    # the *presentation and availability* of a way to pay — PromptPay, Alipay, Credit
    # Card — mapped onto an underlying settlement ``method`` (CARD/QR_BANK_TRANSFER/
    # EWALLET/CASH). Admin manages branding, order and per-channel availability; the
    # booking UI never hard-codes the list. Provider credentials live in the secret
    # store, not here — only a non-secret ``provider_config_ref`` is stored.
    """
    CREATE TABLE IF NOT EXISTS payment_types (
        id                 TEXT PRIMARY KEY,
        tenant_id          TEXT NOT NULL REFERENCES tenants(id),
        scope_type         TEXT NOT NULL DEFAULT 'VENUE',   -- ORGANIZATION | VENUE
        scope_id           TEXT NOT NULL,
        code               TEXT NOT NULL,                   -- internal, stable
        method             TEXT NOT NULL,                   -- settlement primitive
        display_name_json  TEXT NOT NULL,                   -- customer-facing, per language
        description_json   TEXT,
        icon               TEXT,
        provider           TEXT,
        provider_config_ref TEXT,
        supported_currencies_json TEXT,                     -- null = all
        web_enabled        INTEGER NOT NULL DEFAULT 1,
        kiosk_enabled      INTEGER NOT NULL DEFAULT 1,
        counter_enabled    INTEGER NOT NULL DEFAULT 1,
        display_order      INTEGER NOT NULL DEFAULT 0,
        status             TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | DISABLED | ARCHIVED
        created_at         TEXT NOT NULL,
        updated_at         TEXT,
        actor_id           TEXT,
        UNIQUE (tenant_id, scope_type, scope_id, code)
    )
    """,
    # Manually configured exchange rates (settings spec §16-§22). One base amount of
    # from_currency equals ``rate`` units of to_currency. rate_text holds the exact
    # decimal string so no float ever round-trips through the database.
    """
    CREATE TABLE IF NOT EXISTS exchange_rates (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        scope_type     TEXT NOT NULL DEFAULT 'ORGANIZATION',  -- ORGANIZATION | VENUE
        scope_id       TEXT NOT NULL,
        from_currency  TEXT NOT NULL,
        to_currency    TEXT NOT NULL,
        rate_text      TEXT NOT NULL,          -- exact decimal, e.g. '33.100000'
        effective_from TEXT NOT NULL,
        effective_until TEXT,
        status         TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | ENDED
        source         TEXT NOT NULL DEFAULT 'MANUAL',
        actor_id       TEXT,
        updated_actor_id TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT,
        CHECK (from_currency <> to_currency),
        CHECK (status IN ('ACTIVE', 'ENDED'))
    )
    """,
    # ------------------------------ catalog -------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS customer_segments (
        id                 TEXT PRIMARY KEY,
        tenant_id          TEXT NOT NULL REFERENCES tenants(id),
        code               TEXT NOT NULL,
        name_json          TEXT NOT NULL,
        description_json   TEXT NOT NULL DEFAULT '{}',
        qualification_json TEXT NOT NULL DEFAULT '{}',
        proof_required     INTEGER NOT NULL DEFAULT 0,
        proof_json         TEXT NOT NULL DEFAULT '{}',
        display_order      INTEGER NOT NULL DEFAULT 0,
        status             TEXT NOT NULL DEFAULT 'ACTIVE',
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experiences (
        id               TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL REFERENCES tenants(id),
        venue_id         TEXT NOT NULL REFERENCES venues(id),
        area_id          TEXT REFERENCES areas(id),
        code             TEXT NOT NULL,
        kind             TEXT NOT NULL DEFAULT 'EXPERIENCE',
        name_json        TEXT NOT NULL,
        short_name_json  TEXT NOT NULL DEFAULT '{}',
        description_json TEXT NOT NULL DEFAULT '{}',
        instructions_json TEXT NOT NULL DEFAULT '{}',
        cancellation_message_json TEXT NOT NULL DEFAULT '{}',
        category         TEXT,
        audience         TEXT,
        languages_json   TEXT NOT NULL DEFAULT '[]',
        cover_image_url  TEXT,
        images_json      TEXT NOT NULL DEFAULT '[]',
        icon             TEXT,
        default_duration_minutes INTEGER,
        meeting_point_area_id TEXT REFERENCES areas(id),
        reservation_mode TEXT NOT NULL DEFAULT 'NONE',
        eligibility_json TEXT NOT NULL DEFAULT '{}',
        display_priority INTEGER NOT NULL DEFAULT 0,
        customer_visible INTEGER NOT NULL DEFAULT 1,
        status           TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at       TEXT NOT NULL,
        UNIQUE (tenant_id, venue_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        id                  TEXT PRIMARY KEY,
        tenant_id           TEXT NOT NULL REFERENCES tenants(id),
        venue_id            TEXT NOT NULL REFERENCES venues(id),
        experience_id       TEXT REFERENCES experiences(id),
        code                TEXT NOT NULL,
        name_json           TEXT NOT NULL,
        description_json    TEXT NOT NULL DEFAULT '{}',
        admission_model     TEXT NOT NULL,
        session_requirement TEXT NOT NULL DEFAULT 'NOT_USED',
        seat_requirement    TEXT NOT NULL DEFAULT 'NOT_USED',
        seat_flow_model     TEXT NOT NULL DEFAULT 'FLOW_A',
        capacity_controlled INTEGER NOT NULL DEFAULT 0,
        min_per_booking     INTEGER NOT NULL DEFAULT 1,
        max_per_booking     INTEGER,
        channels_json       TEXT NOT NULL DEFAULT '[]',
        available_from      TEXT,
        available_until     TEXT,
        display_order       INTEGER NOT NULL DEFAULT 0,
        customer_visible    INTEGER NOT NULL DEFAULT 1,
        status              TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at          TEXT NOT NULL,
        UNIQUE (tenant_id, venue_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_components (
        id               TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL REFERENCES tenants(id),
        parent_product_id TEXT NOT NULL REFERENCES products(id),
        child_product_id TEXT NOT NULL REFERENCES products(id),
        relation         TEXT NOT NULL,
        quantity         INTEGER NOT NULL DEFAULT 1,
        min_quantity     INTEGER NOT NULL DEFAULT 0,
        max_quantity     INTEGER,
        eligibility_json TEXT NOT NULL DEFAULT '{}',
        display_order    INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, parent_product_id, child_product_id, relation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ticket_types (
        id                  TEXT PRIMARY KEY,
        tenant_id           TEXT NOT NULL REFERENCES tenants(id),
        product_id          TEXT NOT NULL REFERENCES products(id),
        segment_id          TEXT NOT NULL REFERENCES customer_segments(id),
        code                TEXT NOT NULL,
        name_json           TEXT NOT NULL,
        admission_model     TEXT NOT NULL,
        tax_treatment       TEXT NOT NULL DEFAULT 'STANDARD',
        validity_json       TEXT NOT NULL DEFAULT '{}',
        entry_allowance     INTEGER NOT NULL DEFAULT 1,
        reentry_window_minutes INTEGER,
        min_quantity        INTEGER NOT NULL DEFAULT 0,
        max_quantity        INTEGER,
        channels_json       TEXT NOT NULL DEFAULT '[]',
        eligibility_json    TEXT NOT NULL DEFAULT '{}',
        seat_eligibility_json TEXT NOT NULL DEFAULT '{}',
        transferable        INTEGER NOT NULL DEFAULT 0,
        consumes_capacity   INTEGER NOT NULL DEFAULT 1,
        is_complimentary    INTEGER NOT NULL DEFAULT 0,
        display_order       INTEGER NOT NULL DEFAULT 0,
        status              TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at          TEXT NOT NULL,
        UNIQUE (tenant_id, code)
    )
    """,
    # ------------------------------ pricing -------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS price_rules (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        ticket_type_id TEXT NOT NULL REFERENCES ticket_types(id),
        code           TEXT,
        currency       TEXT NOT NULL,
        amount_minor   INTEGER NOT NULL CHECK (amount_minor >= 0),
        priority       INTEGER NOT NULL DEFAULT 0,
        date_from      TEXT,
        date_to        TEXT,
        weekdays_json  TEXT,
        session_id     TEXT,
        channel        TEXT,
        partner_id     TEXT,
        segment_id     TEXT,
        qty_min        INTEGER,
        qty_max        INTEGER,
        status         TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at     TEXT NOT NULL
    )
    """,
    # ------------------- booking rules / operating calendar ---------------- #
    """
    CREATE TABLE IF NOT EXISTS booking_rules (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL REFERENCES tenants(id),
        scope_type    TEXT NOT NULL,
        scope_id      TEXT,
        channel       TEXT,
        settings_json TEXT NOT NULL DEFAULT '{}',
        status        TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operating_calendar (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        venue_id    TEXT NOT NULL REFERENCES venues(id),
        date        TEXT NOT NULL,
        kind        TEXT NOT NULL,
        config_json TEXT NOT NULL DEFAULT '{}',
        note        TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE (tenant_id, venue_id, date, kind)
    )
    """,
    # -------------------------- sessions & capacity ------------------------ #
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id                 TEXT PRIMARY KEY,
        tenant_id          TEXT NOT NULL REFERENCES tenants(id),
        venue_id           TEXT NOT NULL REFERENCES venues(id),
        kind               TEXT NOT NULL DEFAULT 'PRODUCT',
        product_id         TEXT REFERENCES products(id),
        experience_id      TEXT REFERENCES experiences(id),
        area_id            TEXT REFERENCES areas(id),
        date               TEXT NOT NULL,
        start_time         TEXT NOT NULL,
        end_time           TEXT NOT NULL,
        delayed_start_time TEXT,
        capacity           INTEGER,
        confirmed          INTEGER NOT NULL DEFAULT 0,
        capacity_overridden INTEGER NOT NULL DEFAULT 0,
        status             TEXT NOT NULL DEFAULT 'SCHEDULED',
        publication_state  TEXT NOT NULL DEFAULT 'PUBLISHED',
        booking_cutoff_minutes INTEGER,
        grace_minutes      INTEGER NOT NULL DEFAULT 0,
        reservation_mode   TEXT NOT NULL DEFAULT 'REQUIRED',
        booking_required   INTEGER NOT NULL DEFAULT 1,
        check_in_required  INTEGER NOT NULL DEFAULT 0,
        waiting_list_enabled INTEGER NOT NULL DEFAULT 0,
        customer_visible   INTEGER NOT NULL DEFAULT 1,
        seat_layout_version_id TEXT,
        source             TEXT NOT NULL DEFAULT 'MANUAL',
        pattern_id         TEXT,
        override_id        TEXT,
        notes              TEXT,
        cancel_reason      TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT,
        CHECK (confirmed >= 0),
        -- Confirmed consumption may never exceed capacity. The single exception is
        -- an explicit, audited capacity reduction below existing bookings, which
        -- R8.9 requires to be possible for a principal holding OVERRIDE_CAPACITY
        -- and which must not cancel those bookings. The override is recorded on the
        -- row so reporting can see it; new sales are still blocked because every
        -- increment is guarded by ``confirmed + delta <= capacity``.
        CHECK (capacity IS NULL OR confirmed <= capacity OR capacity_overridden = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_allocations (
        id                    TEXT PRIMARY KEY,
        tenant_id             TEXT NOT NULL REFERENCES tenants(id),
        session_id            TEXT NOT NULL REFERENCES sessions(id),
        alloc_type            TEXT NOT NULL,
        alloc_key             TEXT NOT NULL,
        quantity              INTEGER,
        percent_bp            INTEGER,
        confirmed             INTEGER NOT NULL DEFAULT 0,
        overflow_allowed      INTEGER NOT NULL DEFAULT 0,
        release_minutes_before INTEGER,
        released_at           TEXT,
        UNIQUE (tenant_id, session_id, alloc_type, alloc_key),
        CHECK (confirmed >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS holds (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        session_id     TEXT REFERENCES sessions(id),
        zone_id        TEXT,
        cart_id        TEXT NOT NULL,
        channel        TEXT NOT NULL,
        alloc_type     TEXT,
        alloc_key      TEXT,
        quantity       INTEGER NOT NULL CHECK (quantity > 0),
        state          TEXT NOT NULL DEFAULT 'ACTIVE',
        expires_at     TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        released_at    TEXT,
        confirmed_at   TEXT,
        correlation_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_patterns (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        venue_id          TEXT NOT NULL REFERENCES venues(id),
        experience_id     TEXT NOT NULL REFERENCES experiences(id),
        product_id        TEXT REFERENCES products(id),
        area_id           TEXT REFERENCES areas(id),
        kind              TEXT NOT NULL DEFAULT 'SHOW',
        start_time        TEXT NOT NULL,
        duration_minutes  INTEGER NOT NULL,
        capacity          INTEGER,
        reservation_mode  TEXT NOT NULL DEFAULT 'NONE',
        check_in_required INTEGER NOT NULL DEFAULT 0,
        recurrence_json   TEXT NOT NULL,
        valid_from        TEXT NOT NULL,
        valid_until       TEXT,
        publication_state TEXT NOT NULL DEFAULT 'DRAFT',
        materialized_until TEXT,
        status            TEXT NOT NULL DEFAULT 'ACTIVE',
        ended_at          TEXT,
        created_at        TEXT NOT NULL,
        created_by        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedule_overrides (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL REFERENCES tenants(id),
        venue_id      TEXT NOT NULL REFERENCES venues(id),
        experience_id TEXT REFERENCES experiences(id),
        date          TEXT NOT NULL,
        mode          TEXT NOT NULL,
        payload_json  TEXT NOT NULL DEFAULT '{}',
        actor_id      TEXT,
        created_at    TEXT NOT NULL,
        removed_at    TEXT,
        removed_by    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS waiting_list (
        id               TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL REFERENCES tenants(id),
        session_id       TEXT NOT NULL REFERENCES sessions(id),
        customer_id      TEXT,
        contact_hash     TEXT NOT NULL,
        quantity         INTEGER NOT NULL DEFAULT 1,
        position         INTEGER NOT NULL,
        state            TEXT NOT NULL DEFAULT 'WAITING',
        offered_at       TEXT,
        offer_expires_at TEXT,
        resolved_at      TEXT,
        created_at       TEXT NOT NULL
    )
    """,
    # ------------------------------ seating -------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS seat_layouts (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        venue_id    TEXT NOT NULL REFERENCES venues(id),
        code        TEXT NOT NULL,
        name        TEXT NOT NULL,
        is_template INTEGER NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'ACTIVE',
        archived_at TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE (tenant_id, venue_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_layout_versions (
        id           TEXT PRIMARY KEY,
        tenant_id    TEXT NOT NULL REFERENCES tenants(id),
        layout_id    TEXT NOT NULL REFERENCES seat_layouts(id),
        version_no   INTEGER NOT NULL,
        state        TEXT NOT NULL DEFAULT 'DRAFT',
        canvas_json  TEXT NOT NULL DEFAULT '{}',
        created_at   TEXT NOT NULL,
        created_by   TEXT,
        published_at TEXT,
        published_by TEXT,
        archived_at  TEXT,
        UNIQUE (tenant_id, layout_id, version_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS layout_element_types (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT,
        code            TEXT NOT NULL,
        category        TEXT NOT NULL,
        name            TEXT NOT NULL,
        icon            TEXT,
        default_width   INTEGER NOT NULL DEFAULT 32,
        default_height  INTEGER NOT NULL DEFAULT 32,
        sellable        INTEGER NOT NULL DEFAULT 0,
        appearance_json TEXT NOT NULL DEFAULT '{}',
        status          TEXT NOT NULL DEFAULT 'ACTIVE',
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS layout_elements (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        layout_version_id TEXT NOT NULL REFERENCES seat_layout_versions(id),
        element_type_id   TEXT NOT NULL REFERENCES layout_element_types(id),
        x                 INTEGER NOT NULL,
        y                 INTEGER NOT NULL,
        width             INTEGER NOT NULL,
        height            INTEGER NOT NULL,
        rotation          INTEGER NOT NULL DEFAULT 0,
        z_index           INTEGER NOT NULL DEFAULT 0,
        label             TEXT,
        appearance_json   TEXT NOT NULL DEFAULT '{}',
        locked            INTEGER NOT NULL DEFAULT 0,
        hidden            INTEGER NOT NULL DEFAULT 0,
        group_key         TEXT,
        is_orientation_reference INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_price_categories (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL REFERENCES tenants(id),
        code          TEXT NOT NULL,
        name          TEXT NOT NULL,
        display_order INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_types (
        id                  TEXT PRIMARY KEY,
        tenant_id           TEXT NOT NULL REFERENCES tenants(id),
        code                TEXT NOT NULL,
        name                TEXT NOT NULL,
        display_name_json   TEXT NOT NULL DEFAULT '{}',
        description_json    TEXT NOT NULL DEFAULT '{}',
        colour              TEXT NOT NULL DEFAULT '#1F7A8C',
        shape               TEXT NOT NULL DEFAULT 'ROUNDED_SQUARE',
        icon                TEXT,
        price_category_id   TEXT REFERENCES seat_price_categories(id),
        default_price_minor INTEGER,
        display_priority    INTEGER NOT NULL DEFAULT 0,
        sellable            INTEGER NOT NULL DEFAULT 1,
        accessible          INTEGER NOT NULL DEFAULT 0,
        companion           INTEGER NOT NULL DEFAULT 0,
        max_occupancy       INTEGER NOT NULL DEFAULT 1,
        status              TEXT NOT NULL DEFAULT 'ACTIVE',
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_zones (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        layout_version_id TEXT NOT NULL REFERENCES seat_layout_versions(id),
        code              TEXT NOT NULL,
        name_json         TEXT NOT NULL,
        description_json  TEXT NOT NULL DEFAULT '{}',
        colour            TEXT NOT NULL DEFAULT '#2E8BA6',
        zone_kind         TEXT NOT NULL DEFAULT 'ASSIGNED',
        price_category_id TEXT REFERENCES seat_price_categories(id),
        capacity          INTEGER,
        display_order     INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, layout_version_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seats (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        layout_version_id TEXT NOT NULL REFERENCES seat_layout_versions(id),
        zone_id           TEXT REFERENCES seat_zones(id),
        seat_type_id      TEXT NOT NULL REFERENCES seat_types(id),
        element_type_id   TEXT REFERENCES layout_element_types(id),
        code              TEXT NOT NULL,
        label             TEXT,
        section           TEXT,
        row_label         TEXT,
        seat_number       TEXT,
        capacity          INTEGER NOT NULL DEFAULT 1,
        price_category_id TEXT REFERENCES seat_price_categories(id),
        price_override_minor INTEGER,
        x                 INTEGER NOT NULL DEFAULT 0,
        y                 INTEGER NOT NULL DEFAULT 0,
        rotation          INTEGER NOT NULL DEFAULT 0,
        accessible        INTEGER NOT NULL DEFAULT 0,
        companion         INTEGER NOT NULL DEFAULT 0,
        table_parent_id   TEXT REFERENCES seats(id),
        table_sale_mode   TEXT,
        view_note_json    TEXT NOT NULL DEFAULT '{}',
        display_priority  INTEGER NOT NULL DEFAULT 0,
        status            TEXT NOT NULL DEFAULT 'AVAILABLE',
        UNIQUE (tenant_id, layout_version_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_holds (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        session_id  TEXT NOT NULL REFERENCES sessions(id),
        seat_id     TEXT NOT NULL REFERENCES seats(id),
        cart_id     TEXT NOT NULL,
        channel     TEXT NOT NULL,
        state       TEXT NOT NULL DEFAULT 'ACTIVE',
        expires_at  TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        released_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_reservations (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        session_id      TEXT NOT NULL REFERENCES sessions(id),
        seat_id         TEXT NOT NULL REFERENCES seats(id),
        booking_id      TEXT REFERENCES bookings(id),
        booking_item_id TEXT,
        ticket_id       TEXT,
        state           TEXT NOT NULL DEFAULT 'CONFIRMED',
        price_minor     INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        released_at     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_blocks (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        seat_id     TEXT NOT NULL REFERENCES seats(id),
        session_id  TEXT REFERENCES sessions(id),
        scope       TEXT NOT NULL DEFAULT 'SESSION',
        reason_code TEXT NOT NULL,
        reason      TEXT NOT NULL,
        start_at    TEXT,
        end_at      TEXT,
        state       TEXT NOT NULL DEFAULT 'ACTIVE',
        actor_id    TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        lifted_at   TEXT,
        lifted_by   TEXT
    )
    """,
    # -------------------------- customers & consent ------------------------ #
    """
    CREATE TABLE IF NOT EXISTS customers (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        email_hash     TEXT NOT NULL,
        language       TEXT NOT NULL DEFAULT 'en',
        is_minor       INTEGER NOT NULL DEFAULT 0,
        marketing_opt_in INTEGER NOT NULL DEFAULT 0,
        analytics_opt_in INTEGER NOT NULL DEFAULT 0,
        partner_share_opt_in INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT NOT NULL,
        updated_at     TEXT,
        anonymized_at  TEXT,
        legal_hold     INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, email_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_pii (
        customer_id TEXT PRIMARY KEY REFERENCES customers(id),
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        email       TEXT,
        full_name   TEXT,
        phone       TEXT,
        extra_json  TEXT NOT NULL DEFAULT '{}',
        updated_at  TEXT NOT NULL
    )
    """,
    # --------------------------- loyalty / members ------------------------- #
    """
    CREATE TABLE IF NOT EXISTS members (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        email_hash     TEXT NOT NULL,
        customer_id    TEXT REFERENCES customers(id),
        tier           TEXT NOT NULL DEFAULT 'STANDARD',
        -- Point balance is the authoritative live figure; the ledger is the audit
        -- trail. The balance is never allowed below zero (CHECK), and it is only
        -- ever moved by a conditional UPDATE so two concurrent redemptions cannot
        -- both spend the same points (add_features §69).
        points_balance INTEGER NOT NULL DEFAULT 0,
        status         TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at     TEXT NOT NULL,
        updated_at     TEXT,
        UNIQUE (tenant_id, email_hash),
        CHECK (points_balance >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS point_ledger (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        member_id      TEXT NOT NULL REFERENCES members(id),
        entry_type     TEXT NOT NULL,          -- EARN | REDEEM | ADJUST | RESTORE
        points         INTEGER NOT NULL,       -- signed: +earn, -redeem
        -- Snapshot of the conversion used, so a later rate change never re-values a
        -- historical redemption (add_features §33). Stored as an exact string.
        rate_text      TEXT,
        value_minor    INTEGER NOT NULL DEFAULT 0,
        currency       TEXT,
        booking_id     TEXT,
        reason         TEXT,
        state          TEXT NOT NULL DEFAULT 'POSTED',
        created_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_notice_versions (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        version        TEXT NOT NULL,
        consent_text_version TEXT NOT NULL,
        language       TEXT NOT NULL,
        controller_json TEXT NOT NULL,
        purposes_json  TEXT NOT NULL,
        retention_json TEXT NOT NULL,
        recipients_json TEXT NOT NULL,
        cross_border_json TEXT NOT NULL DEFAULT '{}',
        rights_json    TEXT NOT NULL,
        dpo_contact    TEXT NOT NULL,
        notice_url     TEXT NOT NULL,
        items_json     TEXT NOT NULL,
        published_at   TEXT NOT NULL,
        UNIQUE (tenant_id, version, language)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consent_records (
        id                   TEXT PRIMARY KEY,
        tenant_id            TEXT NOT NULL REFERENCES tenants(id),
        venue_id             TEXT,
        channel              TEXT NOT NULL,
        device_id            TEXT,
        booking_id           TEXT,
        customer_id          TEXT,
        contact_hash         TEXT NOT NULL,
        items_json           TEXT NOT NULL,
        notice_version       TEXT NOT NULL,
        consent_text_version TEXT NOT NULL,
        language             TEXT NOT NULL,
        created_at_utc       TEXT NOT NULL,
        created_at_local     TEXT NOT NULL,
        ip_address           TEXT,
        user_agent           TEXT,
        staff_actor_id       TEXT,
        guardian_attestation TEXT,
        authority_attestation TEXT,
        partner_attestation_json TEXT,
        capture_method       TEXT NOT NULL DEFAULT 'DIALOG'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consent_withdrawals (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        consent_record_id TEXT NOT NULL REFERENCES consent_records(id),
        customer_id       TEXT,
        item_code         TEXT NOT NULL,
        withdrawn_at      TEXT NOT NULL,
        effective_by      TEXT NOT NULL,
        channel           TEXT NOT NULL,
        actor_id          TEXT,
        acknowledged      INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dsar_requests (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL REFERENCES tenants(id),
        customer_id   TEXT,
        contact_hash  TEXT NOT NULL,
        kind          TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'RECEIVED',
        received_at   TEXT NOT NULL,
        due_at        TEXT NOT NULL,
        completed_at  TEXT,
        outcome       TEXT,
        justification TEXT,
        actor_id      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS breach_incidents (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        detected_at    TEXT NOT NULL,
        reported_at    TEXT,
        due_at         TEXT NOT NULL,
        scope          TEXT NOT NULL,
        data_categories_json TEXT NOT NULL,
        affected_count INTEGER NOT NULL DEFAULT 0,
        remediation    TEXT,
        status         TEXT NOT NULL DEFAULT 'OPEN',
        actor_id       TEXT
    )
    """,
    # ------------------------ bookings / money / tickets ------------------- #
    """
    CREATE TABLE IF NOT EXISTS bookings (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        organization_id   TEXT NOT NULL REFERENCES organizations(id),
        venue_id          TEXT NOT NULL REFERENCES venues(id),
        booking_number    TEXT NOT NULL,
        customer_id       TEXT REFERENCES customers(id),
        channel           TEXT NOT NULL,
        partner_id        TEXT,
        device_id         TEXT,
        staff_actor_id    TEXT,
        status            TEXT NOT NULL DEFAULT 'PENDING',
        currency          TEXT NOT NULL,
        gross_minor       INTEGER NOT NULL DEFAULT 0,
        discount_minor    INTEGER NOT NULL DEFAULT 0,
        service_charge_minor INTEGER NOT NULL DEFAULT 0,
        tax_minor         INTEGER NOT NULL DEFAULT 0,
        net_minor         INTEGER NOT NULL DEFAULT 0,
        -- Stored value / payment-instrument coupons applied to this order
        -- (add_features §16, §68). net_minor stays the revenue total; the amount
        -- actually collected via the payment method is net_minor - settlement_minor.
        -- settlements_json snapshots which instruments settled the bill (§56).
        settlement_minor  INTEGER NOT NULL DEFAULT 0,
        settlements_json  TEXT,
        -- Free gifts / rewards granted with this order (add_features §11-§12).
        -- Snapshotted so the reward on a historical order is not affected by later
        -- promotion changes (§56).
        gifts_json        TEXT,
        refunded_minor    INTEGER NOT NULL DEFAULT 0,
        -- Historical configuration snapshot (settings spec §33). These record the
        -- exact tax/charge/currency settings used at confirmation so that changing
        -- the venue's current settings can never move a completed order.
        charge_snapshot_json TEXT,
        transaction_currency TEXT,
        base_currency        TEXT,
        exchange_rate_text   TEXT,
        base_currency_minor  INTEGER,
        language          TEXT NOT NULL DEFAULT 'en',
        visit_date        TEXT,
        session_id        TEXT,
        consent_record_id TEXT,
        cart_id           TEXT,
        correlation_id    TEXT,
        notes             TEXT,
        created_at        TEXT NOT NULL,
        confirmed_at      TEXT,
        cancelled_at      TEXT,
        cancel_reason     TEXT,
        late_confirmation INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, booking_number),
        CHECK (refunded_minor >= 0),
        CHECK (refunded_minor <= net_minor)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS booking_items (
        id               TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL REFERENCES tenants(id),
        booking_id       TEXT NOT NULL REFERENCES bookings(id),
        product_id       TEXT NOT NULL REFERENCES products(id),
        ticket_type_id   TEXT NOT NULL REFERENCES ticket_types(id),
        segment_id       TEXT NOT NULL REFERENCES customer_segments(id),
        session_id       TEXT,
        seat_id          TEXT,
        zone_id          TEXT,
        quantity         INTEGER NOT NULL CHECK (quantity > 0),
        unit_price_minor INTEGER NOT NULL DEFAULT 0,
        gross_minor      INTEGER NOT NULL DEFAULT 0,
        discount_minor   INTEGER NOT NULL DEFAULT 0,
        tax_minor        INTEGER NOT NULL DEFAULT 0,
        net_minor        INTEGER NOT NULL DEFAULT 0,
        price_rule_id    TEXT,
        promotions_json  TEXT NOT NULL DEFAULT '[]',
        price_unit       TEXT NOT NULL DEFAULT 'PER_PERSON',
        state            TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        id                  TEXT PRIMARY KEY,
        tenant_id           TEXT NOT NULL REFERENCES tenants(id),
        booking_id          TEXT REFERENCES bookings(id),
        method              TEXT NOT NULL,
        provider            TEXT NOT NULL,
        provider_ref        TEXT,
        amount_minor        INTEGER NOT NULL,
        tendered_minor      INTEGER,
        change_minor        INTEGER,
        currency            TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'INITIATED',
        idempotency_key     TEXT NOT NULL,
        channel             TEXT NOT NULL,
        device_id           TEXT,
        actor_id            TEXT,
        shift_id            TEXT,
        reconciliation_state TEXT,
        failure_code        TEXT,
        created_at          TEXT NOT NULL,
        authorized_at       TEXT,
        captured_at         TEXT,
        failed_at           TEXT,
        UNIQUE (tenant_id, idempotency_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_events (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        payment_id        TEXT REFERENCES payments(id),
        provider          TEXT NOT NULL,
        provider_event_id TEXT NOT NULL,
        kind              TEXT NOT NULL,
        signature_valid   INTEGER NOT NULL,
        payload_hash      TEXT NOT NULL,
        received_at       TEXT NOT NULL,
        processed_at      TEXT,
        outcome           TEXT,
        UNIQUE (tenant_id, provider, provider_event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refunds (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL REFERENCES tenants(id),
        booking_id    TEXT NOT NULL REFERENCES bookings(id),
        payment_id    TEXT REFERENCES payments(id),
        kind          TEXT NOT NULL DEFAULT 'REFUND',
        amount_minor  INTEGER NOT NULL CHECK (amount_minor >= 0),
        fee_minor     INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL DEFAULT 'PENDING',
        reason        TEXT NOT NULL,
        tickets_json  TEXT NOT NULL DEFAULT '[]',
        actor_id      TEXT NOT NULL,
        approver_id   TEXT,
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_error    TEXT,
        created_at    TEXT NOT NULL,
        completed_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id               TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL REFERENCES tenants(id),
        venue_id         TEXT NOT NULL REFERENCES venues(id),
        booking_id       TEXT NOT NULL REFERENCES bookings(id),
        booking_item_id  TEXT NOT NULL REFERENCES booking_items(id),
        ticket_number    TEXT NOT NULL,
        qr_token         TEXT NOT NULL,
        qr_signature     TEXT NOT NULL,
        state            TEXT NOT NULL DEFAULT 'ISSUED',
        product_id       TEXT NOT NULL,
        ticket_type_id   TEXT NOT NULL,
        segment_id       TEXT NOT NULL,
        session_id       TEXT,
        seat_id          TEXT,
        visit_date       TEXT,
        valid_from       TEXT,
        valid_until      TEXT,
        -- Validity snapshot (settings spec §14, §33). The IANA timezone and policy
        -- used to compute valid_from/valid_until are stored on the ticket so that
        -- changing the venue's timezone or validity rule can never silently alter a
        -- ticket already issued. Gate validation reads valid_until directly.
        validity_timezone TEXT,
        validity_type    TEXT,
        validity_policy_json TEXT,
        entry_allowance  INTEGER NOT NULL DEFAULT 1,
        entries_used     INTEGER NOT NULL DEFAULT 0,
        reentry_window_minutes INTEGER,
        last_entry_at    TEXT,
        first_entry_at   TEXT,
        proof_required   INTEGER NOT NULL DEFAULT 0,
        blocked_reason   TEXT,
        issued_at        TEXT NOT NULL,
        superseded_at    TEXT,
        reissue_count    INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, ticket_number),
        UNIQUE (qr_token),
        CHECK (entries_used >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_events (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        venue_id        TEXT NOT NULL,
        access_point_id TEXT,
        device_id       TEXT,
        ticket_id       TEXT,
        booking_id      TEXT,
        qr_token_hash   TEXT,
        decision        TEXT NOT NULL,
        reason          TEXT,
        at_utc          TEXT NOT NULL,
        at_local        TEXT NOT NULL,
        operator_id     TEXT,
        offline_captured INTEGER NOT NULL DEFAULT 0,
        synced_at       TEXT,
        conflict_flag   INTEGER NOT NULL DEFAULT 0,
        override_actor_id TEXT,
        override_reason TEXT,
        correlation_id  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shift_sessions (
        id                  TEXT PRIMARY KEY,
        tenant_id           TEXT NOT NULL REFERENCES tenants(id),
        venue_id            TEXT NOT NULL REFERENCES venues(id),
        counter_code        TEXT NOT NULL,
        staff_id            TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'OPEN',
        opening_float_minor INTEGER NOT NULL DEFAULT 0,
        expected_minor      INTEGER NOT NULL DEFAULT 0,
        counted_minor       INTEGER,
        variance_minor      INTEGER,
        opened_at           TEXT NOT NULL,
        closed_at           TEXT,
        approved_by         TEXT,
        approval_reason     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_sequences (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        organization_id TEXT NOT NULL,
        doc_type        TEXT NOT NULL,
        prefix          TEXT NOT NULL DEFAULT '',
        next_no         INTEGER NOT NULL DEFAULT 1,
        UNIQUE (tenant_id, organization_id, doc_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS receipts (
        id           TEXT PRIMARY KEY,
        tenant_id    TEXT NOT NULL REFERENCES tenants(id),
        booking_id   TEXT NOT NULL REFERENCES bookings(id),
        number       TEXT NOT NULL,
        issued_at    TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        reprint_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tax_invoices (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        organization_id   TEXT NOT NULL,
        booking_id        TEXT NOT NULL REFERENCES bookings(id),
        number            TEXT NOT NULL,
        sequence_no       INTEGER NOT NULL,
        doc_type          TEXT NOT NULL DEFAULT 'TAX_INVOICE',
        issued_at         TEXT NOT NULL,
        customer_tax_json TEXT NOT NULL,
        lines_json        TEXT NOT NULL,
        tax_base_minor    INTEGER NOT NULL,
        tax_minor         INTEGER NOT NULL,
        total_minor       INTEGER NOT NULL,
        status            TEXT NOT NULL DEFAULT 'ISSUED',
        credit_note_of    TEXT,
        actor_id          TEXT,
        UNIQUE (tenant_id, organization_id, doc_type, sequence_no),
        UNIQUE (tenant_id, number)
    )
    """,
    # ----------------------------- promotions ------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS promotions (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        code              TEXT,
        internal_code     TEXT NOT NULL,
        name_json         TEXT NOT NULL,
        mechanic          TEXT NOT NULL,
        config_json       TEXT NOT NULL DEFAULT '{}',
        rules_json        TEXT NOT NULL DEFAULT '{}',
        priority          INTEGER NOT NULL DEFAULT 0,
        stackable         INTEGER NOT NULL DEFAULT 0,
        exclusions_json   TEXT NOT NULL DEFAULT '[]',
        usage_limit       INTEGER,
        usage_count       INTEGER NOT NULL DEFAULT 0,
        per_customer_limit INTEGER,
        per_code_limit    INTEGER,
        budget_minor      INTEGER,
        budget_used_minor INTEGER NOT NULL DEFAULT 0,
        restoring         INTEGER NOT NULL DEFAULT 1,
        -- How a redeemed value is treated for accounting (add_features §16):
        -- DISCOUNT reduces sales revenue; STORED_VALUE / PAYMENT is a payment
        -- instrument and is NOT a sales discount; LIABILITY draws down a voucher
        -- liability; COMPLIMENTARY is a marketing expense. Default keeps existing
        -- promotions behaving exactly as before.
        accounting_treatment TEXT NOT NULL DEFAULT 'DISCOUNT',
        status            TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at        TEXT NOT NULL,
        UNIQUE (tenant_id, internal_code),
        CHECK (usage_count >= 0),
        CHECK (usage_limit IS NULL OR usage_count <= usage_limit),
        CHECK (budget_minor IS NULL OR budget_used_minor <= budget_minor)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promotion_redemptions (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        promotion_id    TEXT NOT NULL REFERENCES promotions(id),
        booking_id      TEXT NOT NULL,
        booking_item_id TEXT,
        customer_key    TEXT,
        amount_minor    INTEGER NOT NULL DEFAULT 0,
        sequence        INTEGER NOT NULL DEFAULT 0,
        state           TEXT NOT NULL DEFAULT 'APPLIED',
        created_at      TEXT NOT NULL,
        restored_at     TEXT
    )
    """,
    # -------------------------- staff / roles / audit ---------------------- #
    """
    CREATE TABLE IF NOT EXISTS staff (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        organization_id TEXT NOT NULL REFERENCES organizations(id),
        first_name      TEXT NOT NULL,
        last_name       TEXT NOT NULL,
        display_name    TEXT NOT NULL,
        email           TEXT NOT NULL,
        phone           TEXT,
        employee_id     TEXT,
        status          TEXT NOT NULL DEFAULT 'INVITED',
        mfa_enrolled    INTEGER NOT NULL DEFAULT 0,
        mfa_required    INTEGER NOT NULL DEFAULT 0,
        credential_hash TEXT,
        invite_token_hash TEXT,
        invite_expires_at TEXT,
        failed_logins   INTEGER NOT NULL DEFAULT 0,
        locked_until    TEXT,
        last_login_at   TEXT,
        last_login_channel TEXT,
        last_login_ip   TEXT,
        perm_epoch      INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL,
        created_by      TEXT,
        updated_at      TEXT,
        updated_by      TEXT,
        deactivated_at  TEXT,
        UNIQUE (tenant_id, email)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS roles (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        organization_id TEXT,
        code            TEXT NOT NULL,
        name            TEXT NOT NULL,
        description     TEXT,
        authority_level INTEGER NOT NULL DEFAULT 10,
        template_code   TEXT,
        status          TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at      TEXT NOT NULL,
        created_by      TEXT,
        UNIQUE (tenant_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS role_permissions (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        role_id        TEXT NOT NULL REFERENCES roles(id),
        permission_key TEXT NOT NULL,
        granted        INTEGER NOT NULL DEFAULT 0,
        UNIQUE (tenant_id, role_id, permission_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS role_assignments (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL REFERENCES tenants(id),
        staff_id        TEXT NOT NULL REFERENCES staff(id),
        role_id         TEXT NOT NULL REFERENCES roles(id),
        scope_type      TEXT NOT NULL,
        scope_id        TEXT,
        operating_point TEXT,
        status          TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at      TEXT NOT NULL,
        created_by      TEXT,
        revoked_at      TEXT,
        UNIQUE (tenant_id, staff_id, role_id, scope_type, scope_id, operating_point)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        staff_id    TEXT NOT NULL REFERENCES staff(id),
        token_hash  TEXT NOT NULL UNIQUE,
        channel     TEXT NOT NULL,
        ip_address  TEXT,
        perm_epoch  INTEGER NOT NULL DEFAULT 1,
        issued_at   TEXT NOT NULL,
        idle_expires_at TEXT NOT NULL,
        absolute_expires_at TEXT NOT NULL,
        revoked_at  TEXT,
        last_seen_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id              TEXT PRIMARY KEY,
        tenant_id       TEXT NOT NULL,
        organization_id TEXT,
        venue_id        TEXT,
        actor_id        TEXT,
        actor_role      TEXT,
        action          TEXT NOT NULL,
        target_type     TEXT,
        target_id       TEXT,
        previous_json   TEXT,
        new_json        TEXT,
        reason          TEXT,
        at_utc          TEXT NOT NULL,
        at_local        TEXT,
        channel         TEXT,
        device_id       TEXT,
        ip_address      TEXT,
        correlation_id  TEXT,
        severity        TEXT NOT NULL DEFAULT 'INFO'
    )
    """,
    # ----------------------------- partners -------------------------------- #
    """
    CREATE TABLE IF NOT EXISTS partners (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL REFERENCES tenants(id),
        code              TEXT NOT NULL,
        name              TEXT NOT NULL,
        contract_json     TEXT NOT NULL DEFAULT '{}',
        rate_config_json  TEXT NOT NULL DEFAULT '{}',
        settlement_model  TEXT NOT NULL DEFAULT 'PREPAID',
        credit_limit_minor INTEGER NOT NULL DEFAULT 0,
        credit_used_minor INTEGER NOT NULL DEFAULT 0,
        commission_bp     INTEGER NOT NULL DEFAULT 0,
        api_key_hash      TEXT,
        ip_allowlist_json TEXT NOT NULL DEFAULT '[]',
        venues_json       TEXT NOT NULL DEFAULT '[]',
        status            TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at        TEXT NOT NULL,
        UNIQUE (tenant_id, code),
        CHECK (credit_used_minor >= 0)
    )
    """,
    # --------------------------- notifications ----------------------------- #
    """
    CREATE TABLE IF NOT EXISTS notification_templates (
        id             TEXT PRIMARY KEY,
        tenant_id      TEXT NOT NULL REFERENCES tenants(id),
        venue_id       TEXT,
        event_type     TEXT NOT NULL,
        language       TEXT NOT NULL,
        version        INTEGER NOT NULL DEFAULT 1,
        subject        TEXT NOT NULL,
        header         TEXT NOT NULL DEFAULT '',
        body           TEXT NOT NULL,
        footer         TEXT NOT NULL DEFAULT '',
        state          TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at     TEXT NOT NULL,
        created_by     TEXT,
        UNIQUE (tenant_id, venue_id, event_type, language, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_messages (
        id                  TEXT PRIMARY KEY,
        tenant_id           TEXT NOT NULL REFERENCES tenants(id),
        booking_id          TEXT,
        event_type          TEXT NOT NULL,
        recipient           TEXT NOT NULL,
        recipient_hash      TEXT NOT NULL,
        channel             TEXT NOT NULL DEFAULT 'EMAIL',
        template_id         TEXT,
        template_version    INTEGER,
        language            TEXT NOT NULL,
        subject             TEXT,
        rendered_body       TEXT,
        -- Optional HTML alternative. The plain-text body remains authoritative and
        -- is always populated, so a client that cannot render HTML still receives a
        -- complete message; this carries the designed e-ticket where it can.
        rendered_html       TEXT,
        -- Inline images the HTML references by Content-ID, as {cid: base64}. Held on
        -- the row rather than regenerated at send time so a retry, a resend or a
        -- delivery inspected months later reproduces exactly the message that was
        -- composed, and so the cid references can never drift from the attachments.
        inline_images_json  TEXT,
        status              TEXT NOT NULL DEFAULT 'QUEUED',
        queued_at           TEXT NOT NULL,
        sent_at             TEXT,
        provider_message_id TEXT,
        failure_reason      TEXT,
        retry_count         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at     TEXT,
        is_test             INTEGER NOT NULL DEFAULT 0,
        dedupe_key          TEXT,
        correlation_id      TEXT,
        actor_id            TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_suppressions (
        id         TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL REFERENCES tenants(id),
        address    TEXT NOT NULL,
        reason     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        cleared_at TEXT,
        UNIQUE (tenant_id, address)
    )
    """,
    # ------------------------------ ops ------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS exceptions_log (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL REFERENCES tenants(id),
        venue_id    TEXT,
        kind        TEXT NOT NULL,
        severity    TEXT NOT NULL DEFAULT 'WARNING',
        entity_type TEXT,
        entity_id   TEXT,
        detail_json TEXT NOT NULL DEFAULT '{}',
        state       TEXT NOT NULL DEFAULT 'OPEN',
        created_at  TEXT NOT NULL,
        resolved_at TEXT,
        resolved_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS visit_plan_entries (
        id         TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL REFERENCES tenants(id),
        booking_id TEXT NOT NULL REFERENCES bookings(id),
        session_id TEXT NOT NULL REFERENCES sessions(id),
        kind       TEXT NOT NULL DEFAULT 'ITINERARY',
        created_at TEXT NOT NULL,
        removed_at TEXT,
        UNIQUE (tenant_id, booking_id, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rate_limit_counters (
        id           TEXT PRIMARY KEY,
        bucket       TEXT NOT NULL,
        window_start TEXT NOT NULL,
        count        INTEGER NOT NULL DEFAULT 0,
        UNIQUE (bucket, window_start)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS verification_challenges (
        id           TEXT PRIMARY KEY,
        tenant_id    TEXT NOT NULL REFERENCES tenants(id),
        booking_id   TEXT,
        purpose      TEXT NOT NULL,
        contact_hash TEXT NOT NULL,
        code_hash    TEXT NOT NULL,
        issued_at    TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        consumed_at  TEXT,
        attempts     INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS offline_caches (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL REFERENCES tenants(id),
        device_id     TEXT NOT NULL REFERENCES devices(id),
        generated_at  TEXT NOT NULL,
        max_age_minutes INTEGER NOT NULL,
        payload_json  TEXT NOT NULL,
        signature     TEXT NOT NULL,
        entry_count   INTEGER NOT NULL DEFAULT 0,
        erased_at     TEXT
    )
    """,
    # A staff member's saved report filters (reports spec §36). Per staff, not per
    # tenant: "my daily view" is a personal working preference, and one manager
    # renaming it must not change what another sees. Physically deletable — a
    # saved filter set has no financial, access or consent significance (R46.5).
    """
    CREATE TABLE IF NOT EXISTS report_views (
        id           TEXT PRIMARY KEY,
        tenant_id    TEXT NOT NULL REFERENCES tenants(id),
        staff_id     TEXT NOT NULL REFERENCES staff(id),
        report_key   TEXT NOT NULL,
        name         TEXT NOT NULL,
        filters_json TEXT NOT NULL DEFAULT '{}',
        is_default   INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT NOT NULL
    )
    """,
)

# --------------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------------- #

INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_config_lookup ON config_values(tenant_id, key, scope_type, scope_id, superseded_at)",
    "CREATE INDEX IF NOT EXISTS ix_charge_settings_lookup "
    "ON charge_settings(tenant_id, scope_type, scope_id, charge_kind, superseded_at)",
    "CREATE INDEX IF NOT EXISTS ix_payment_types_lookup "
    "ON payment_types(tenant_id, scope_type, scope_id, status, display_order)",
    "CREATE INDEX IF NOT EXISTS ix_exchange_rates_lookup "
    "ON exchange_rates(tenant_id, scope_type, scope_id, from_currency, to_currency, status)",
    # R22 of the settings spec — no two ACTIVE rows for the same pair share an
    # effective_from, which would make the applicable rate ambiguous. Ending a rate
    # (status='ENDED') or giving it a different effective_from is how you supersede.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_exchange_rate_active "
    "ON exchange_rates(tenant_id, scope_type, scope_id, from_currency, to_currency, effective_from) "
    "WHERE status = 'ACTIVE'",
    "CREATE INDEX IF NOT EXISTS ix_areas_venue ON areas(tenant_id, venue_id, parent_id)",
    "CREATE INDEX IF NOT EXISTS ix_products_venue ON products(tenant_id, venue_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_ticket_types_product ON ticket_types(tenant_id, product_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_price_rules_lookup ON price_rules(tenant_id, ticket_type_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_booking_rules_scope ON booking_rules(tenant_id, scope_type, scope_id)",
    "CREATE INDEX IF NOT EXISTS ix_calendar_lookup ON operating_calendar(tenant_id, venue_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_lookup ON sessions(tenant_id, venue_id, date, kind)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_product ON sessions(tenant_id, product_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_experience ON sessions(tenant_id, experience_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_pattern ON sessions(tenant_id, pattern_id, date)",
    "CREATE INDEX IF NOT EXISTS ix_holds_active ON holds(tenant_id, session_id, state, expires_at)",
    "CREATE INDEX IF NOT EXISTS ix_holds_cart ON holds(tenant_id, cart_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_bookings_lookup ON bookings(tenant_id, venue_id, status, visit_date)",
    "CREATE INDEX IF NOT EXISTS ix_bookings_customer ON bookings(tenant_id, customer_id)",
    "CREATE INDEX IF NOT EXISTS ix_booking_items_booking ON booking_items(tenant_id, booking_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_booking ON tickets(tenant_id, booking_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_session ON tickets(tenant_id, session_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_scan_ticket ON scan_events(tenant_id, ticket_id, at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_scan_venue ON scan_events(tenant_id, venue_id, at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_audit_lookup ON audit_events(tenant_id, at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_events(tenant_id, actor_id, at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_audit_correlation ON audit_events(correlation_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_events(tenant_id, target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS ix_role_perm_role ON role_permissions(tenant_id, role_id)",
    "CREATE INDEX IF NOT EXISTS ix_role_assign_staff ON role_assignments(tenant_id, staff_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_notif_queue ON notification_messages(tenant_id, status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS ix_notif_booking ON notification_messages(tenant_id, booking_id, event_type)",
    "CREATE INDEX IF NOT EXISTS ix_seats_version ON seats(tenant_id, layout_version_id, zone_id)",
    "CREATE INDEX IF NOT EXISTS ix_seat_res_session ON seat_reservations(tenant_id, session_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_seat_hold_session ON seat_holds(tenant_id, session_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_seat_blocks_seat ON seat_blocks(tenant_id, seat_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_consent_customer ON consent_records(tenant_id, contact_hash)",
    "CREATE INDEX IF NOT EXISTS ix_promo_code ON promotions(tenant_id, code, status)",
    "CREATE INDEX IF NOT EXISTS ix_promo_redemption ON promotion_redemptions(tenant_id, promotion_id, state)",
    "CREATE INDEX IF NOT EXISTS ix_waiting_list_session ON waiting_list(tenant_id, session_id, state, position)",
    "CREATE INDEX IF NOT EXISTS ix_exceptions_open ON exceptions_log(tenant_id, state, kind)",
    "CREATE INDEX IF NOT EXISTS ix_payments_booking ON payments(tenant_id, booking_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_overrides_lookup ON schedule_overrides(tenant_id, venue_id, date, removed_at)",
    # --- Integrity-critical partial unique indexes ------------------------- #
    # One confirmed reservation per seat per session. This is the data-layer
    # guarantee behind R57.9; no service bug can violate it.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_seat_reservation_confirmed "
    "ON seat_reservations(session_id, seat_id) WHERE state = 'CONFIRMED'",
    # One active hold per seat per session, across every channel (R57.4).
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_seat_hold_active "
    "ON seat_holds(session_id, seat_id) WHERE state = 'ACTIVE'",
    # A tax invoice number is never reused (R72.3).
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tax_invoice_number ON tax_invoices(tenant_id, number)",
)

# --------------------------------------------------------------------------- #
# Immutability triggers
# --------------------------------------------------------------------------- #

#: Tables whose rows may never be physically deleted (R46.1).
PROTECTED_TABLES: tuple[str, ...] = (
    "audit_events",
    "consent_records",
    "consent_withdrawals",
    "scan_events",
    "payment_events",
    "payments",
    "refunds",
    "tickets",
    "tax_invoices",
    "receipts",
    "bookings",
    "booking_items",
    "shift_sessions",
    "seat_reservations",
    "seat_blocks",
    "promotion_redemptions",
    "privacy_notice_versions",
    "breach_incidents",
    # Financial configuration history: a superseded VAT rate or an ended exchange
    # rate is evidence for reconciling past transactions and must not be erased
    # (settings spec §32). Both are still UPDATE-able to stamp supersession.
    "charge_settings",
    "exchange_rates",
    # Loyalty point movements are a financial audit trail (add_features §33): a
    # redemption row and its snapshotted rate must never be erased.
    "point_ledger",
)

#: Tables that are strictly append-only: no UPDATE either (R45.3, R12.12).
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "audit_events",
    "consent_records",
    "privacy_notice_versions",
)


def _delete_guard(table: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
    BEFORE DELETE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'protected_record: {table} rows are retained for audit and cannot be deleted');
    END
    """


def _update_guard(table: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
    BEFORE UPDATE ON {table}
    BEGIN
        SELECT RAISE(ABORT, 'append_only_record: {table} rows cannot be modified');
    END
    """


#: Scan decisions are immutable, but offline synchronisation must still be able
#: to stamp ``synced_at`` and raise ``conflict_flag`` (R32.8). The trigger
#: therefore guards the decision fields specifically rather than the whole row.
_SCAN_DECISION_GUARD = """
CREATE TRIGGER IF NOT EXISTS trg_scan_events_decision_immutable
BEFORE UPDATE ON scan_events
WHEN NEW.decision <> OLD.decision
  OR IFNULL(NEW.reason, '') <> IFNULL(OLD.reason, '')
  OR NEW.at_utc <> OLD.at_utc
  OR IFNULL(NEW.ticket_id, '') <> IFNULL(OLD.ticket_id, '')
BEGIN
    SELECT RAISE(ABORT, 'append_only_record: a recorded scan decision cannot be altered');
END
"""

#: A published layout version is immutable; structural change requires a new
#: version (R53.2, R53.3).
_LAYOUT_VERSION_GUARD = """
CREATE TRIGGER IF NOT EXISTS trg_layout_version_published_immutable
BEFORE UPDATE ON seat_layout_versions
WHEN OLD.state = 'PUBLISHED' AND NEW.canvas_json <> OLD.canvas_json
BEGIN
    SELECT RAISE(ABORT, 'immutable_layout_version: create a new version to change a published layout');
END
"""

#: Seats belonging to a published layout version cannot be re-identified, which
#: is what keeps a confirmed reservation attached to a real seat (R61.1, R61.2).
_SEAT_CODE_GUARD = """
CREATE TRIGGER IF NOT EXISTS trg_seat_code_stable
BEFORE UPDATE ON seats
WHEN NEW.code <> OLD.code
 AND EXISTS (
     SELECT 1 FROM seat_layout_versions v
     WHERE v.id = OLD.layout_version_id AND v.state = 'PUBLISHED'
 )
BEGIN
    SELECT RAISE(ABORT, 'immutable_seat_identity: seat codes are stable in a published layout version');
END
"""


def triggers() -> tuple[str, ...]:
    statements: list[str] = [_delete_guard(t) for t in PROTECTED_TABLES]
    statements += [_update_guard(t) for t in APPEND_ONLY_TABLES]
    statements.append(_SCAN_DECISION_GUARD)
    statements.append(_LAYOUT_VERSION_GUARD)
    statements.append(_SEAT_CODE_GUARD)
    return tuple(statements)


def all_statements() -> tuple[str, ...]:
    return TABLES + INDEXES + triggers()


__all__ = [
    "APPEND_ONLY_TABLES",
    "INDEXES",
    "PROTECTED_TABLES",
    "SCHEMA_VERSION",
    "TABLES",
    "all_statements",
    "triggers",
]
