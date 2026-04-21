-- ============================================================
-- Age of Origins Alliance Tracker — SQLite Schema
-- ============================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;  -- Better concurrent read performance

-- ── Members ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,            -- Current display name
    clean_name      TEXT,                     -- ASCII-only version (for OCR matching)
    joined_date     DATE,
    status          TEXT DEFAULT 'active',    -- active | inactive | left | kicked | alt
    notes           TEXT,
    -- Stats (most recent values, updated by roster/rankings capture)
    city_level      INTEGER,                 -- roster_ocr
    position        TEXT,                    -- R1-R5 alliance position (roster_ocr)
    battle_power    INTEGER,                 -- roster_ocr
    reputation      INTEGER,                 -- Individual Reputation (roster_ocr)
    kills           INTEGER,                 -- roster_ocr
    losses          INTEGER,                 -- roster_ocr
    officer_power   INTEGER,                 -- rankings only
    titan_power     INTEGER,                 -- rankings only
    warplane_power  INTEGER,                 -- rankings only
    island_lux      INTEGER,                 -- Island Luxoriousness (rankings only)
    antique_power   INTEGER,                 -- rankings only
    merit           INTEGER,                 -- rankings only
    commander_level INTEGER,                 -- rankings only
    stats_updated_at DATETIME,               -- last time any stat was updated
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Name History ─────────────────────────────────────────────
-- Every name a member has ever used
CREATE TABLE IF NOT EXISTS member_names (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    name        TEXT NOT NULL,
    clean_name  TEXT,                   -- ASCII-only version (for OCR matching)
    from_date   DATE NOT NULL,
    to_date     DATE,                   -- NULL = current name
    is_current  BOOLEAN DEFAULT 1,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_member_names_name
    ON member_names(name);             -- All names (current + historical) must be unique

CREATE INDEX IF NOT EXISTS idx_member_names_clean_name
    ON member_names(clean_name);       -- For OCR name resolution

-- ── Alliance History ─────────────────────────────────────────
-- Full audit trail of membership events
CREATE TABLE IF NOT EXISTS alliance_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES members(id),
    event       TEXT NOT NULL,          -- joined | left | kicked | rejoined | name_change | rank_change
    detail      TEXT,                   -- e.g. "OldName → NewName" or "R4 → R3"
    occurred_at DATE NOT NULL,
    noted_by    TEXT DEFAULT 'system'   -- system | ocr | manual
);

-- ── Events ───────────────────────────────────────────────────
-- One row per event occurrence (e.g. Elite War on 2025-04-10)
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,          -- Elite War | Void War | Battle Frenzy | etc.
    occurred_at DATE NOT NULL,
    notes       TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_type_date
    ON events(event_type, occurred_at);

-- ── Event Results ────────────────────────────────────────────
-- One row per member per event — the raw time-series data
CREATE TABLE IF NOT EXISTS event_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL REFERENCES members(id),
    event_id        INTEGER NOT NULL REFERENCES events(id),
    score           INTEGER,
    participated    BOOLEAN DEFAULT 1,
    percentile      REAL,               -- Calculated at ingest time (rank / participants)
    pct_vs_mean     REAL,               -- (score - mean) / mean * 100
    recorded_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    source          TEXT DEFAULT 'manual', -- manual | ocr
    UNIQUE(member_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_event_results_member
    ON event_results(member_id);
CREATE INDEX IF NOT EXISTS idx_event_results_event
    ON event_results(event_id);

-- ── Member Summary (Pre-aggregated) ──────────────────────────
-- Refreshed after each event ingestion — what the bot reads
CREATE TABLE IF NOT EXISTS member_summary (
    member_id           INTEGER NOT NULL REFERENCES members(id),
    period              TEXT NOT NULL,  -- alltime | 30d | 90d
    events_participated INTEGER DEFAULT 0,
    events_available    INTEGER DEFAULT 0,
    attendance_rate     REAL DEFAULT 0,
    avg_score           REAL,
    avg_percentile      REAL,
    avg_pct_vs_mean     REAL,
    trend               TEXT,           -- up | down | stable | insufficient_data
    last_active         DATE,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (member_id, period)
);

-- ── Member Event Type Stats (Pre-aggregated) ─────────────────
-- Per-event-type breakdown per member — also what the bot reads
CREATE TABLE IF NOT EXISTS member_event_stats (
    member_id           INTEGER NOT NULL REFERENCES members(id),
    event_type          TEXT NOT NULL,
    events_participated INTEGER DEFAULT 0,
    events_available    INTEGER DEFAULT 0,
    participation_rate  REAL DEFAULT 0,
    avg_score           REAL,
    avg_percentile      REAL,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (member_id, event_type)
);

-- ── Alliance Summary (Pre-aggregated) ────────────────────────
-- Alliance-wide metrics snapshot — refreshed after each event
CREATE TABLE IF NOT EXISTS alliance_summary (
    id                      INTEGER PRIMARY KEY CHECK (id = 1), -- singleton row
    total_members           INTEGER DEFAULT 0,
    active_members          INTEGER DEFAULT 0,
    overall_attendance_rate REAL DEFAULT 0,
    best_event_type         TEXT,
    worst_event_type        TEXT,
    most_active_member      TEXT,
    least_active_member     TEXT,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Review Queue ─────────────────────────────────────────────
-- Ambiguous OCR matches needing manual resolution
CREATE TABLE IF NOT EXISTS review_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scraped_name        TEXT NOT NULL,
    reason              TEXT NOT NULL,  -- unknown_name | historical_name | fuzzy_match
    suggested_member_id INTEGER REFERENCES members(id),
    match_confidence    REAL,           -- 0.0 to 1.0
    raw_context         TEXT,           -- JSON blob of surrounding OCR data
    resolved            BOOLEAN DEFAULT 0,
    resolution          TEXT,           -- new_member | name_change | rejoin | duplicate | ignore
    resolved_member_id  INTEGER REFERENCES members(id),
    resolved_at         DATETIME,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ── Player Snapshots ────────────────────────────────────────
-- Time-series player stats — one row per member per day per source
CREATE TABLE IF NOT EXISTS player_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id       INTEGER NOT NULL REFERENCES members(id),
    captured_at     DATE NOT NULL,
    source          TEXT NOT NULL DEFAULT 'roster_ocr',  -- roster_ocr | rankings
    city_level      INTEGER,
    battle_power    INTEGER,
    kills           INTEGER,
    losses          INTEGER,
    reputation      INTEGER,            -- Individual Reputation
    position        TEXT,               -- R1-R5 alliance position
    officer_power   INTEGER,
    titan_power     INTEGER,
    warplane_power  INTEGER,
    island_lux      INTEGER,            -- Island Luxoriousness
    antique_power   INTEGER,
    merit           INTEGER,
    commander_level INTEGER,
    UNIQUE(member_id, captured_at, source)
);

CREATE INDEX IF NOT EXISTS idx_player_snapshots_member
    ON player_snapshots(member_id, captured_at);

-- ── Ranking Entries ─────────────────────────────────────────
-- Nation-wide top-100 leaderboard captures (includes non-alliance players)
CREATE TABLE IF NOT EXISTS ranking_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id     INTEGER REFERENCES members(id),  -- NULL if unknown player
    player_name   TEXT NOT NULL,
    category      TEXT NOT NULL,     -- battle_power | kills | city_level | reputation
    rank_position INTEGER NOT NULL,  -- 1-100
    value         INTEGER,
    captured_at   DATE NOT NULL,
    UNIQUE(category, rank_position, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_ranking_entries_member
    ON ranking_entries(member_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_ranking_entries_category
    ON ranking_entries(category, captured_at);
