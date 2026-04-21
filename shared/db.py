"""
db.py — SQLite database layer for AoO Alliance Stats Bot.

Handles:
  - DB initialisation
  - Member resolution (name → stable ID, with name-change detection)
  - Event + result ingestion
  - Aggregation refresh
  - Bot query interface (replaces sheets.py)
"""

import sqlite3
import os
import json
import logging
from datetime import date, datetime, timedelta
from difflib import get_close_matches
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("DB_PATH", str(_PROJECT_ROOT / "alliance.db"))
SCHEMA_PATH = Path(__file__).parent / "db_schema.sql"

log = logging.getLogger(__name__)

# Merit thresholds for military ranks (merit >= threshold → rank).
# Ranks beyond General are not yet confirmed — thresholds set to None.
MILITARY_RANKS = [
    (300,    "Private"),
    (1300,   "Private First Class"),
    (4300,   "Sergeant"),
    (11300,  "Master Sergeant"),
    (25300,  "Warrant Officer"),
    (51300,  "Second Lieutenant"),
    (91300,  "First Lieutenant"),
    (147300, "Captain"),
    (222300, "Major"),
    (322300, "Lieutenant Colonel"),
    (452300, "Colonel"),
    (617300, "Brigadier"),
    (822300, "General"),
    (None,   "General of the Army"),
    (None,   "Marshal"),
    (None,   "Grand Marshal"),
    (None,   "Field Marshal"),
]


def military_rank_from_merit(merit: int | None) -> str | None:
    """Return the military rank title for a given merit value."""
    if merit is None:
        return None
    result = None
    for threshold, title in MILITARY_RANKS:
        if threshold is not None and merit >= threshold:
            result = title
        else:
            break
    return result

EVENT_TYPES = [
    "Elite War",
    "Void War",
    "Battle Frenzy",
    "Polar Invasion",
    "Wasteland Showdown",
    "Ironblood Battlefield",
    "Chaosland",
    "Global Conquest",
    "Triangle War",
    "Duel of Dominance",
]

# ── Connection ────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    schema = SCHEMA_PATH.read_text()
    with get_connection() as conn:
        conn.executescript(schema)
    log.info(f"Database initialised at {DB_PATH}")


def _to_clean_name(name: str) -> str:
    """Strip all non-ASCII characters, collapse whitespace, lowercase.

    Examples:
        '꧁༺Tom༻꧂'      → 'tom'
        '指挥官123266007' → '123266007'
        'José García'    → 'jos garca'
    """
    import re
    cleaned = re.sub(r'[^\x00-\x7f]', '', name)   # strip non-ASCII
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()  # collapse whitespace
    return cleaned.lower()


def import_name_aliases(alias_path: str):
    """Import name_aliases.txt entries into the member_names table.

    File format: ``ocr_name==roster_name`` (one per line).
    For each alias, finds or creates the member matching roster_name,
    then adds ocr_name as a historical name for that member.
    Safe to re-run — skips names that already exist in member_names.
    """
    from pathlib import Path as _Path
    p = _Path(alias_path)
    if not p.exists():
        log.info(f"No alias file at {alias_path}")
        return

    conn = get_connection()
    imported = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "==" not in line:
                continue
            ocr_side, roster_side = line.split("==", 1)
            ocr_side = ocr_side.strip()
            roster_side = roster_side.strip()
            if not ocr_side or not roster_side:
                continue

            # Check if this alias already exists
            existing = conn.execute(
                "SELECT id FROM member_names WHERE name = ?", (ocr_side,)
            ).fetchone()
            if existing:
                continue

            # Find the member by their roster (current) name
            member = conn.execute("""
                SELECT m.id FROM members m
                JOIN member_names mn ON mn.member_id = m.id
                WHERE mn.name = ? AND mn.is_current = 1
            """, (roster_side,)).fetchone()

            if member is None:
                # Create the member with the roster name, then add the alias
                member_id = add_member(roster_side, conn=conn)
            else:
                member_id = member["id"]

            # Add the OCR name as a historical (non-current) alias
            today = date.today().isoformat()
            clean = _to_clean_name(ocr_side)
            conn.execute("""
                INSERT OR IGNORE INTO member_names (member_id, name, clean_name, from_date, is_current)
                VALUES (?, ?, ?, ?, 0)
            """, (member_id, ocr_side, clean, today))
            imported += 1

        conn.commit()
        log.info(f"Imported {imported} name alias(es) from {alias_path}")
    finally:
        conn.close()
    return imported


# ── Member Resolution ─────────────────────────────────────────

def resolve_member(name: str, conn: sqlite3.Connection) -> dict | None:
    """
    Given a scraped/input name, return the member record.
    Returns None and adds to review_queue if the name is ambiguous.

    Resolution order:
      1. Exact match on current name
      2. Exact match on historical name  → flag for review (could be rejoin)
      3. Fuzzy match (≥0.85 confidence) → auto-accept if confident, else flag
      4. Unknown                         → flag as possible new member
    """
    clean = _to_clean_name(name)

    # 1. Exact current name match (original or clean)
    row = conn.execute("""
        SELECT m.* FROM members m
        JOIN member_names mn ON mn.member_id = m.id
        WHERE (mn.name = ? OR mn.clean_name = ?) AND mn.is_current = 1
    """, (name, clean)).fetchone()
    if row:
        return dict(row)

    # 2. Exact historical name match (original or clean)
    row = conn.execute("""
        SELECT m.* FROM members m
        JOIN member_names mn ON mn.member_id = m.id
        WHERE (mn.name = ? OR mn.clean_name = ?) AND mn.is_current = 0
    """, (name, clean)).fetchone()
    if row:
        _add_to_review_queue(conn, name, "historical_name", row["id"], 0.95)
        return None  # Wait for manual resolution

    # 3. Fuzzy match against all current names
    all_current = conn.execute("""
        SELECT mn.name, mn.clean_name, mn.member_id FROM member_names mn
        WHERE mn.is_current = 1
    """).fetchall()
    current_names = [r["name"] for r in all_current]
    matches = get_close_matches(name, current_names, n=1, cutoff=0.85)

    if matches:
        matched_name = matches[0]
        member_id = next(r["member_id"] for r in all_current if r["name"] == matched_name)
        confidence = _fuzzy_confidence(name, matched_name)

        if confidence >= 0.92:
            # High confidence — probably an OCR typo, auto-accept
            log.info(f"Fuzzy auto-match: '{name}' → '{matched_name}' ({confidence:.2f})")
            row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
            return dict(row)
        else:
            # Lower confidence — flag for review
            _add_to_review_queue(conn, name, "fuzzy_match", member_id, confidence)
            return None

    # 4. Completely unknown
    _add_to_review_queue(conn, name, "unknown_name", None, 0.0)
    return None


def _fuzzy_confidence(a: str, b: str) -> float:
    """Simple character-overlap confidence score."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _add_to_review_queue(conn, scraped_name: str, reason: str,
                          suggested_member_id: int | None, confidence: float):
    # Don't add duplicates for the same unresolved name
    existing = conn.execute("""
        SELECT id FROM review_queue
        WHERE scraped_name = ? AND resolved = 0
    """, (scraped_name,)).fetchone()
    if existing:
        return
    conn.execute("""
        INSERT INTO review_queue (scraped_name, reason, suggested_member_id, match_confidence)
        VALUES (?, ?, ?, ?)
    """, (scraped_name, reason, suggested_member_id, confidence))
    log.warning(f"Review queue: '{scraped_name}' ({reason}, confidence={confidence:.2f})")


# ── Member Management ─────────────────────────────────────────

def add_member(name: str, joined_date: str = None,
               conn: sqlite3.Connection = None) -> int:
    """Add a new member. Returns the new member_id."""
    close = conn is None
    if close:
        conn = get_connection()
    try:
        today = joined_date or date.today().isoformat()
        clean = _to_clean_name(name)
        cur = conn.execute(
            "INSERT INTO members (name, clean_name, joined_date, status) VALUES (?, ?, ?, 'active')",
            (name, clean, today)
        )
        member_id = cur.lastrowid
        conn.execute("""
            INSERT INTO member_names (member_id, name, clean_name, from_date, is_current)
            VALUES (?, ?, ?, ?, 1)
        """, (member_id, name, clean, today))
        conn.execute("""
            INSERT INTO alliance_history (member_id, event, occurred_at, noted_by)
            VALUES (?, 'joined', ?, 'system')
        """, (member_id, today))
        conn.commit()
        log.info(f"Added member: {name} (id={member_id})")
        return member_id
    finally:
        if close:
            conn.close()


def rename_member(member_id: int, new_name: str, conn: sqlite3.Connection = None):
    """Record a name change for an existing member."""
    close = conn is None
    if close:
        conn = get_connection()
    try:
        today = date.today().isoformat()
        old_name_row = conn.execute("""
            SELECT name FROM member_names WHERE member_id = ? AND is_current = 1
        """, (member_id,)).fetchone()
        old_name = old_name_row["name"] if old_name_row else "unknown"

        # Retire old name
        conn.execute("""
            UPDATE member_names SET is_current = 0, to_date = ?
            WHERE member_id = ? AND is_current = 1
        """, (today, member_id))

        # Add new name
        clean = _to_clean_name(new_name)
        conn.execute("""
            INSERT INTO member_names (member_id, name, clean_name, from_date, is_current)
            VALUES (?, ?, ?, ?, 1)
        """, (member_id, new_name, clean, today))

        # Update display name
        conn.execute("UPDATE members SET name = ?, clean_name = ? WHERE id = ?",
                     (new_name, clean, member_id))

        # Log the change
        conn.execute("""
            INSERT INTO alliance_history (member_id, event, detail, occurred_at, noted_by)
            VALUES (?, 'name_change', ?, ?, 'manual')
        """, (member_id, f"{old_name} → {new_name}", today))

        conn.commit()
        log.info(f"Renamed member id={member_id}: {old_name} → {new_name}")
    finally:
        if close:
            conn.close()


def set_member_status(member_id: int, status: str, noted_by: str = "manual",
                       conn: sqlite3.Connection = None):
    """Update member status (active/left/kicked/inactive). Logs to alliance_history."""
    close = conn is None
    if close:
        conn = get_connection()
    try:
        conn.execute("UPDATE members SET status = ? WHERE id = ?", (status, member_id))
        event_map = {
            "left": "left", "kicked": "kicked",
            "active": "rejoined", "inactive": "inactive"
        }
        event = event_map.get(status, status)
        conn.execute("""
            INSERT INTO alliance_history (member_id, event, occurred_at, noted_by)
            VALUES (?, ?, ?, ?)
        """, (member_id, event, date.today().isoformat(), noted_by))
        conn.commit()
    finally:
        if close:
            conn.close()


# ── Player Snapshots ─────────────────────────────────────────

def add_player_snapshot(member_id: int, source: str = "roster_ocr",
                        captured_at: str = None,
                        conn: sqlite3.Connection = None,
                        **stats):
    """Record a point-in-time stats snapshot for a member.

    Idempotent per (member_id, captured_at, source) — re-running the same
    capture on the same day with the same source updates the existing row.

    Also updates the corresponding columns on the members table with any
    non-NULL values provided.

    Accepted stats kwargs:
        city_level, battle_power, kills, losses, reputation, position,
        officer_power, titan_power, warplane_power, island_lux,
        antique_power, merit, commander_level
    """
    SNAPSHOT_COLS = [
        "city_level", "battle_power", "kills", "losses", "reputation",
        "position", "officer_power", "titan_power", "warplane_power",
        "island_lux", "antique_power", "merit", "commander_level",
    ]

    close = conn is None
    if close:
        conn = get_connection()
    try:
        captured_at = captured_at or date.today().isoformat()

        # Build snapshot insert
        col_names = ["member_id", "captured_at", "source"]
        values = [member_id, captured_at, source]
        for col in SNAPSHOT_COLS:
            col_names.append(col)
            values.append(stats.get(col))

        placeholders = ", ".join(["?"] * len(values))
        cols_sql = ", ".join(col_names)
        conn.execute(f"""
            INSERT OR REPLACE INTO player_snapshots ({cols_sql})
            VALUES ({placeholders})
        """, values)

        # Update members table with non-NULL values
        updates = {col: stats[col] for col in SNAPSHOT_COLS
                   if col in stats and stats[col] is not None}
        if updates:
            set_clause = ", ".join(f"{col} = ?" for col in updates)
            conn.execute(
                f"UPDATE members SET {set_clause}, stats_updated_at = CURRENT_TIMESTAMP "
                f"WHERE id = ?",
                [*updates.values(), member_id],
            )

        conn.commit()
    finally:
        if close:
            conn.close()


def add_ranking_entry(player_name: str, category: str, rank_position: int,
                      value: int = None, captured_at: str = None,
                      conn: sqlite3.Connection = None):
    """Record a single ranking leaderboard entry.

    Attempts to resolve the player name to a known member. If resolved,
    also cross-populates player_snapshots with the stat value.
    Unknown players are stored with member_id=NULL (no review queue noise).
    """
    close = conn is None
    if close:
        conn = get_connection()
    try:
        captured_at = captured_at or date.today().isoformat()

        # Try to resolve without flooding review queue
        member_id = None
        row = conn.execute("""
            SELECT m.id FROM members m
            JOIN member_names mn ON mn.member_id = m.id
            WHERE mn.name = ? AND mn.is_current = 1
        """, (player_name,)).fetchone()
        if row:
            member_id = row["id"]

        conn.execute("""
            INSERT OR REPLACE INTO ranking_entries
                (member_id, player_name, category, rank_position, value, captured_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (member_id, player_name, category, rank_position, value, captured_at))

        # Cross-populate player_snapshots for known members
        if member_id and value is not None:
            category_to_field = {
                "battle_power": "battle_power",
                "kills": "kills",
                "city_level": "city_level",
                "reputation": "reputation",
                "officer": "officer_power",
                "titan": "titan_power",
                "warplane": "warplane_power",
                "island": "island_lux",
                "antique": "antique_power",
                "merit": "merit",
                "commander_level": "commander_level",
            }
            field = category_to_field.get(category)
            if field:
                add_player_snapshot(
                    member_id=member_id, source="rankings",
                    captured_at=captured_at, conn=conn,
                    **{field: value},
                )

        conn.commit()
    finally:
        if close:
            conn.close()


def get_player_stats(member_id: int, limit: int = 30,
                     conn: sqlite3.Connection = None) -> list[dict]:
    """Return snapshot history for a member, newest first."""
    close = conn is None
    if close:
        conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM player_snapshots
            WHERE member_id = ?
            ORDER BY captured_at DESC
            LIMIT ?
        """, (member_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


def get_latest_stats(member_id: int = None,
                     conn: sqlite3.Connection = None) -> list[dict]:
    """Return the most recent snapshot per member.

    If member_id is given, returns a single-element list (or empty).
    Otherwise returns latest stats for all members.
    """
    close = conn is None
    if close:
        conn = get_connection()
    try:
        where = "WHERE ps.member_id = ?" if member_id else ""
        params = (member_id,) if member_id else ()
        rows = conn.execute(f"""
            SELECT m.name, ps.*
            FROM player_snapshots ps
            JOIN members m ON m.id = ps.member_id
            INNER JOIN (
                SELECT member_id, MAX(captured_at) AS max_date
                FROM player_snapshots
                GROUP BY member_id
            ) latest ON ps.member_id = latest.member_id
                    AND ps.captured_at = latest.max_date
            {where}
            ORDER BY m.name
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()


# ── Event Ingestion ───────────────────────────────────────────

def add_event(event_type: str, occurred_at: str, conn: sqlite3.Connection = None) -> int:
    """Register a new event occurrence. Returns event_id."""
    close = conn is None
    if close:
        conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO events (event_type, occurred_at) VALUES (?, ?)",
            (event_type, occurred_at)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if close:
            conn.close()


def add_event_result(member_id: int, event_id: int, score: int,
                      source: str = "manual",
                      conn: sqlite3.Connection = None):
    """Record a single member's result for an event (participants only)."""
    close = conn is None
    if close:
        conn = get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO event_results
                (member_id, event_id, score, participated, source)
            VALUES (?, ?, ?, 1, ?)
        """, (member_id, event_id, score, source))
        conn.commit()
    finally:
        if close:
            conn.close()


def finalise_event(event_id: int):
    """
    After all results are inserted, calculate percentile and pct_vs_mean
    for every participant in the event, then refresh aggregations.
    """
    with get_connection() as conn:
        results = conn.execute("""
            SELECT id, member_id, score FROM event_results
            WHERE event_id = ?
            ORDER BY score DESC
        """, (event_id,)).fetchall()

        if not results:
            return

        scores = [r["score"] for r in results if r["score"] is not None]
        if not scores:
            return

        mean_score = sum(scores) / len(scores)
        n = len(results)

        for rank, row in enumerate(results, start=1):
            if row["score"] is None:
                continue
            percentile = round((n - rank) / (n - 1) * 100, 1) if n > 1 else 100.0
            pct_vs_mean = round((row["score"] - mean_score) / mean_score * 100, 1) \
                if mean_score else 0.0
            conn.execute("""
                UPDATE event_results
                SET percentile = ?, pct_vs_mean = ?
                WHERE id = ?
            """, (percentile, pct_vs_mean, row["id"]))

        conn.commit()

    refresh_aggregations()


# ── Aggregation ───────────────────────────────────────────────

def refresh_aggregations():
    """Rebuild all summary tables from raw event_results. Call after each event."""
    with get_connection() as conn:
        _refresh_member_summaries(conn)
        _refresh_member_event_stats(conn)
        _refresh_alliance_summary(conn)
        conn.commit()
    log.info("Aggregations refreshed")


def _refresh_member_summaries(conn):
    """Rebuild member_summary for alltime, 30d, and 90d periods.

    Attendance is calculated as:
      events_participated / events_available

    Where events_available = total events that occurred since the member's
    first recorded event result (or within the period window, whichever is later).
    Only actual participation records exist in event_results (no absent rows).
    """
    periods = {
        "alltime": None,
        "30d": 30,
        "90d": 90,
    }
    members = conn.execute("SELECT id, joined_date FROM members").fetchall()

    for member in members:
        mid = member["id"]

        # Find the member's earliest event result — this is when they became trackable
        first_event = conn.execute("""
            SELECT MIN(e.occurred_at) AS first_date
            FROM event_results er
            JOIN events e ON e.id = er.event_id
            WHERE er.member_id = ?
        """, (mid,)).fetchone()
        member_start = first_event["first_date"] if first_event and first_event["first_date"] else None

        for period_name, days in periods.items():
            if member_start is None:
                # Member has never participated — write zeros
                conn.execute("""
                    INSERT OR REPLACE INTO member_summary
                        (member_id, period, events_participated, events_available,
                         attendance_rate, avg_score, avg_percentile, avg_pct_vs_mean,
                         trend, last_active, updated_at)
                    VALUES (?, ?, 0, 0, 0, NULL, NULL, NULL, 'insufficient_data', NULL, CURRENT_TIMESTAMP)
                """, (mid, period_name))
                continue

            # Determine the effective start date for this period
            if days:
                cutoff = (date.today() - timedelta(days=days)).isoformat()
                effective_start = max(member_start, cutoff)
            else:
                effective_start = member_start

            # Count events participated in
            row = conn.execute("""
                SELECT
                    COUNT(*) AS participated,
                    AVG(er.score) AS avg_score,
                    AVG(er.percentile) AS avg_pct,
                    AVG(er.pct_vs_mean) AS avg_pct_vs_mean,
                    MAX(e.occurred_at) AS last_active
                FROM event_results er
                JOIN events e ON e.id = er.event_id
                WHERE er.member_id = ? AND e.occurred_at >= ?
            """, (mid, effective_start)).fetchone()

            participated = row["participated"] or 0

            # Count total events since effective start date
            avail_row = conn.execute("""
                SELECT COUNT(*) AS available
                FROM events
                WHERE occurred_at >= ?
            """, (effective_start,)).fetchone()
            available = avail_row["available"] or 0

            attendance = round(participated / available * 100, 1) if available else 0

            trend = _calculate_trend(conn, mid)

            conn.execute("""
                INSERT OR REPLACE INTO member_summary
                    (member_id, period, events_participated, events_available,
                     attendance_rate, avg_score, avg_percentile, avg_pct_vs_mean,
                     trend, last_active, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (mid, period_name, participated, available, attendance,
                  row["avg_score"], row["avg_pct"], row["avg_pct_vs_mean"],
                  trend, row["last_active"]))


def _calculate_trend(conn, member_id: int) -> str:
    """Compare avg percentile in the last 45 days vs 45-90 days ago."""
    cutoff_recent = (date.today() - timedelta(days=45)).isoformat()
    cutoff_older = (date.today() - timedelta(days=90)).isoformat()

    recent = conn.execute("""
        SELECT AVG(er.percentile) AS avg_pct, COUNT(*) AS cnt
        FROM event_results er
        JOIN events e ON e.id = er.event_id
        WHERE er.member_id = ?
          AND e.occurred_at >= ?
    """, (member_id, cutoff_recent)).fetchone()

    older = conn.execute("""
        SELECT AVG(er.percentile) AS avg_pct, COUNT(*) AS cnt
        FROM event_results er
        JOIN events e ON e.id = er.event_id
        WHERE er.member_id = ?
          AND e.occurred_at >= ? AND e.occurred_at < ?
    """, (member_id, cutoff_older, cutoff_recent)).fetchone()

    if not recent or not older or (older["cnt"] or 0) == 0 or (recent["cnt"] or 0) == 0:
        return "insufficient_data"

    diff = (recent["avg_pct"] or 0) - (older["avg_pct"] or 0)
    if diff >= 5:
        return "up"
    elif diff <= -5:
        return "down"
    return "stable"


def _refresh_member_event_stats(conn):
    """Rebuild per-event-type stats for every member.

    events_available = count of this event type since the member's first
    recorded result for that type.
    """
    conn.execute("DELETE FROM member_event_stats")

    # Get each member's participation per event type
    rows = conn.execute("""
        SELECT
            er.member_id,
            e.event_type,
            COUNT(*) AS participated,
            AVG(er.score) AS avg_score,
            AVG(er.percentile) AS avg_percentile,
            MIN(e.occurred_at) AS first_date
        FROM event_results er
        JOIN events e ON e.id = er.event_id
        GROUP BY er.member_id, e.event_type
    """).fetchall()

    for r in rows:
        # Count how many times this event type has occurred since the member's first result
        avail = conn.execute("""
            SELECT COUNT(*) AS available
            FROM events
            WHERE event_type = ? AND occurred_at >= ?
        """, (r["event_type"], r["first_date"])).fetchone()
        available = avail["available"] or 0
        participated = r["participated"]
        rate = round(participated * 100.0 / available, 1) if available else 0

        conn.execute("""
            INSERT INTO member_event_stats
                (member_id, event_type, events_participated, events_available,
                 participation_rate, avg_score, avg_percentile, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (r["member_id"], r["event_type"], participated, available,
              rate, r["avg_score"], r["avg_percentile"]))


def _refresh_alliance_summary(conn):
    """Rebuild the singleton alliance summary row."""
    totals = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
        FROM members
    """).fetchone()

    overall = conn.execute("""
        SELECT
            ROUND(AVG(CASE WHEN ms.period = '30d' THEN ms.attendance_rate END), 1) AS rate
        FROM member_summary ms
        JOIN members m ON m.id = ms.member_id
        WHERE m.status = 'active'
    """).fetchone()

    best_event = conn.execute("""
        SELECT event_type, ROUND(AVG(participation_rate), 1) AS avg_rate
        FROM member_event_stats
        GROUP BY event_type ORDER BY avg_rate DESC LIMIT 1
    """).fetchone()

    worst_event = conn.execute("""
        SELECT event_type, ROUND(AVG(participation_rate), 1) AS avg_rate
        FROM member_event_stats
        GROUP BY event_type ORDER BY avg_rate ASC LIMIT 1
    """).fetchone()

    most_active = conn.execute("""
        SELECT m.name FROM members m
        JOIN member_summary ms ON ms.member_id = m.id
        WHERE ms.period = '30d' AND m.status = 'active'
        ORDER BY ms.attendance_rate DESC LIMIT 1
    """).fetchone()

    least_active = conn.execute("""
        SELECT m.name FROM members m
        JOIN member_summary ms ON ms.member_id = m.id
        WHERE ms.period = '30d' AND m.status = 'active'
        ORDER BY ms.attendance_rate ASC LIMIT 1
    """).fetchone()

    conn.execute("""
        INSERT OR REPLACE INTO alliance_summary
            (id, total_members, active_members, overall_attendance_rate,
             best_event_type, worst_event_type, most_active_member,
             least_active_member, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        totals["total"], totals["active"],
        overall["rate"] if overall else None,
        best_event["event_type"] if best_event else None,
        worst_event["event_type"] if worst_event else None,
        most_active["name"] if most_active else None,
        least_active["name"] if least_active else None,
    ))


# ── Bot Query Interface ───────────────────────────────────────
# Replaces sheets.py — provides pre-formatted text for Claude

def get_bot_context(question: str = "") -> dict[str, str]:
    """
    Returns pre-aggregated data as text blocks for the bot.
    Selects which tables to include based on question keywords.
    """
    q = question.lower()
    needs_detail = any(k in q for k in [
        "history", "all time", "specific", "event log", "when did", "last time"
    ])

    with get_connection() as conn:
        context = {}
        context["Alliance Summary"] = _fmt_alliance_summary(conn)
        context["Member Summary (30d)"] = _fmt_member_summary(conn, "30d")
        context["Member Event Breakdown"] = _fmt_event_stats(conn)

        if needs_detail:
            context["Recent Event Results (last 60 days)"] = _fmt_recent_results(conn, days=60)

        pending = _fmt_review_queue(conn)
        if pending:
            context["⚠️ Pending Review Items"] = pending

    return context


def _fmt_alliance_summary(conn) -> str:
    row = conn.execute("SELECT * FROM alliance_summary WHERE id = 1").fetchone()
    if not row:
        return "(No alliance summary available yet)"
    return (
        f"Total Members: {row['total_members']} | Active: {row['active_members']}\n"
        f"Overall 30d Attendance: {row['overall_attendance_rate']}%\n"
        f"Best Event: {row['best_event_type']} | Worst Event: {row['worst_event_type']}\n"
        f"Most Active (30d): {row['most_active_member']} | "
        f"Least Active (30d): {row['least_active_member']}\n"
        f"Last Updated: {row['updated_at']}"
    )


def _fmt_member_summary(conn, period: str = "30d") -> str:
    rows = conn.execute("""
        SELECT m.name, ms.events_participated, ms.events_available,
               ms.attendance_rate, ms.avg_percentile, ms.avg_pct_vs_mean,
               ms.trend, ms.last_active
        FROM member_summary ms
        JOIN members m ON m.id = ms.member_id
        WHERE ms.period = ? AND m.status = 'active'
        ORDER BY ms.attendance_rate DESC
    """, (period,)).fetchall()

    if not rows:
        return f"(No member summary data for period '{period}')"

    header = "Name\tAttended\tAvailable\tAttendance%\tAvg Percentile\tVs Mean%\tTrend\tLast Active"
    lines = [header]
    for r in rows:
        lines.append(
            f"{r['name']}\t{r['events_participated']}\t{r['events_available']}\t"
            f"{r['attendance_rate']}%\t{r['avg_percentile']}\t{r['avg_pct_vs_mean']}\t"
            f"{r['trend']}\t{r['last_active']}"
        )
    return "\n".join(lines)


def _fmt_event_stats(conn) -> str:
    rows = conn.execute("""
        SELECT m.name, mes.event_type, mes.events_participated,
               mes.events_available, mes.participation_rate, mes.avg_percentile
        FROM member_event_stats mes
        JOIN members m ON m.id = mes.member_id
        WHERE m.status = 'active'
        ORDER BY m.name, mes.event_type
    """).fetchall()

    if not rows:
        return "(No event stats available yet)"

    header = "Name\tEvent Type\tParticipated\tAvailable\tRate%\tAvg Percentile"
    lines = [header]
    for r in rows:
        lines.append(
            f"{r['name']}\t{r['event_type']}\t{r['events_participated']}\t"
            f"{r['events_available']}\t{r['participation_rate']}%\t{r['avg_percentile']}"
        )
    return "\n".join(lines)


def _fmt_recent_results(conn, days: int = 60) -> str:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT m.name, e.event_type, e.occurred_at, er.score,
               er.percentile, er.pct_vs_mean
        FROM event_results er
        JOIN members m ON m.id = er.member_id
        JOIN events e ON e.id = er.event_id
        WHERE e.occurred_at >= ?
        ORDER BY e.occurred_at DESC, er.percentile DESC
    """, (cutoff,)).fetchall()

    if not rows:
        return f"(No results in the last {days} days)"

    header = "Name\tEvent\tDate\tScore\tPercentile\tVs Mean%"
    lines = [header]
    for r in rows:
        lines.append(
            f"{r['name']}\t{r['event_type']}\t{r['occurred_at']}\t"
            f"{r['score']}\t{r['percentile']}\t{r['pct_vs_mean']}"
        )
    return "\n".join(lines)


def _fmt_review_queue(conn) -> str:
    rows = conn.execute("""
        SELECT rq.scraped_name, rq.reason, rq.match_confidence,
               m.name AS suggested_name
        FROM review_queue rq
        LEFT JOIN members m ON m.id = rq.suggested_member_id
        WHERE rq.resolved = 0
        ORDER BY rq.created_at
    """).fetchall()

    if not rows:
        return ""

    lines = [f"{len(rows)} item(s) need review:"]
    for r in rows:
        suggestion = f" (possible match: {r['suggested_name']}, {r['match_confidence']:.0%})" \
                     if r["suggested_name"] else ""
        lines.append(f"  • '{r['scraped_name']}' — {r['reason']}{suggestion}")
    return "\n".join(lines)


# ── Review Queue Resolution ───────────────────────────────────

def resolve_review_item(review_id: int, resolution: str,
                         member_id: int = None, new_name: str = None):
    """
    Resolve a review queue item.

    resolution options:
      'new_member'  — scraped_name is a brand new player
      'name_change' — existing member_id changed their name to scraped_name
      'rejoin'      — member_id has rejoined the alliance
      'duplicate'   — duplicate entry, ignore
      'ignore'      — not a real member, skip
    """
    with get_connection() as conn:
        item = conn.execute(
            "SELECT * FROM review_queue WHERE id = ?", (review_id,)
        ).fetchone()
        if not item:
            raise ValueError(f"Review item {review_id} not found")

        scraped_name = item["scraped_name"]

        if resolution == "new_member":
            new_id = add_member(scraped_name, conn=conn)
            member_id = new_id

        elif resolution == "name_change" and member_id:
            rename_member(member_id, scraped_name, conn=conn)

        elif resolution == "rejoin" and member_id:
            set_member_status(member_id, "active", noted_by="manual", conn=conn)

        conn.execute("""
            UPDATE review_queue
            SET resolved = 1, resolution = ?, resolved_member_id = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (resolution, member_id, review_id))
        conn.commit()
        log.info(f"Review item {review_id} resolved as '{resolution}'")
