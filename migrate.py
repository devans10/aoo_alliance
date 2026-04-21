"""
migrate.py — One-time migration from Google Sheets → SQLite

Reads your existing four tabs (Roster, Event Log, Member Summary, Alliance Summary)
and populates the SQLite database.

Run once:
    python migrate.py

Safe to re-run — uses INSERT OR IGNORE / upsert patterns.
"""

import sys
import json
import logging
from datetime import date
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
import os

load_dotenv()

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from db import (
    init_db, get_connection, add_member, rename_member,
    add_event, add_event_result, finalise_event, refresh_aggregations,
    resolve_member, EVENT_TYPES
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SPREADSHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ── Column name mapping ───────────────────────────────────────
# Adjust these to match your actual Sheet column headers exactly.

ROSTER_COLS = {
    "name": "Player Name",       # Required
    "joined": "Join Date",       # Optional — leave blank string if not present
    "rank": "Rank",              # Optional
}

EVENT_LOG_COLS = {
    "member": "Player Name",     # Required
    "event_type": "Event Type",  # Required — must match EVENT_TYPES list
    "date": "Event Date",        # Required — YYYY-MM-DD or MM/DD/YYYY
    "score": "Score",            # Required
    "participated": "Participated",  # Optional — assumed True if column missing
}


# ── Google Sheets helpers ─────────────────────────────────────

def get_sheet_client():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)


def tab_as_dicts(spreadsheet, tab_name: str) -> list[dict]:
    """Return a tab as a list of row dicts keyed by header."""
    try:
        ws = spreadsheet.worksheet(tab_name)
        records = ws.get_all_records(empty2zero=False, head=1)
        log.info(f"  Loaded '{tab_name}': {len(records)} rows")
        return records
    except gspread.exceptions.WorksheetNotFound:
        log.warning(f"  Tab '{tab_name}' not found — skipping")
        return []


def normalise_date(raw: str) -> str | None:
    """Try to parse a date string into YYYY-MM-DD."""
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    log.warning(f"  Could not parse date: '{raw}' — storing as-is")
    return raw


# ── Migration steps ───────────────────────────────────────────

def migrate_roster(roster_rows: list[dict], conn) -> dict[str, int]:
    """
    Insert all members from the Roster tab.
    Returns a name→member_id mapping.
    """
    log.info("Migrating roster...")
    name_col = ROSTER_COLS["name"]
    joined_col = ROSTER_COLS.get("joined", "")

    name_to_id = {}
    skipped = 0

    for row in roster_rows:
        name = str(row.get(name_col, "")).strip()
        if not name:
            skipped += 1
            continue

        joined = normalise_date(row.get(joined_col, "")) if joined_col else None

        # Check if already exists (safe re-run)
        existing = conn.execute("""
            SELECT member_id FROM member_names WHERE name = ? AND is_current = 1
        """, (name,)).fetchone()

        if existing:
            name_to_id[name] = existing["member_id"]
        else:
            mid = add_member(name, joined_date=joined, conn=conn)
            name_to_id[name] = mid
            log.info(f"  + Added member: {name}")

    log.info(f"  Roster done: {len(name_to_id)} members, {skipped} skipped")
    return name_to_id


def migrate_event_log(event_rows: list[dict], name_to_id: dict[str, int], conn):
    """
    Insert all event results from the Event Log tab.
    Groups rows by (event_type, date) to create event records.
    Then inserts individual results and calls finalise_event().
    """
    log.info("Migrating event log...")

    member_col = EVENT_LOG_COLS["member"]
    type_col = EVENT_LOG_COLS["event_type"]
    date_col = EVENT_LOG_COLS["date"]
    score_col = EVENT_LOG_COLS["score"]
    participated_col = EVENT_LOG_COLS.get("participated", "")

    # Group rows by (event_type, date) — each unique combo is one event instance
    events_map: dict[tuple, int] = {}  # (event_type, date) → event_id
    results_to_insert = []
    unknown_members = set()
    unknown_event_types = set()

    for i, row in enumerate(event_rows, start=2):  # start=2 for Sheet row numbers
        name = str(row.get(member_col, "")).strip()
        event_type = str(row.get(type_col, "")).strip()
        raw_date = str(row.get(date_col, "")).strip()
        raw_score = row.get(score_col, 0)
        participated = True

        if participated_col:
            pval = str(row.get(participated_col, "")).strip().lower()
            participated = pval not in ("0", "false", "no", "n", "")

        # Only record members who actually participated
        if not participated:
            continue

        if not name or not event_type or not raw_date:
            log.debug(f"  Row {i}: skipping empty row")
            continue

        # Validate event type
        if event_type not in EVENT_TYPES:
            if event_type not in unknown_event_types:
                log.warning(f"  Unknown event type '{event_type}' — check EVENT_LOG_COLS mapping")
                unknown_event_types.add(event_type)
            continue

        # Resolve member
        member_id = name_to_id.get(name)
        if not member_id:
            if name not in unknown_members:
                log.warning(f"  Member '{name}' not in roster — add to Roster tab first")
                unknown_members.add(name)
            continue

        event_date = normalise_date(raw_date)
        if not event_date:
            continue

        key = (event_type, event_date)
        if key not in events_map:
            event_id = add_event(event_type, event_date, conn=conn)
            events_map[key] = event_id
            log.info(f"  + Event: {event_type} on {event_date} (id={event_id})")
        else:
            event_id = events_map[key]

        try:
            score = int(float(raw_score)) if raw_score not in ("", None) else 0
        except (ValueError, TypeError):
            score = 0

        results_to_insert.append((member_id, event_id, score))

    # Bulk insert results
    conn.executemany("""
        INSERT OR IGNORE INTO event_results (member_id, event_id, score, participated, source)
        VALUES (?, ?, ?, 1, 'migration')
    """, results_to_insert)
    conn.commit()

    log.info(f"  Event log done: {len(events_map)} events, {len(results_to_insert)} results")

    # Finalise each event (calculate percentiles) — skip aggregation refresh until end
    for (event_type, event_date), event_id in events_map.items():
        with get_connection() as c:
            results = c.execute("""
                SELECT id, member_id, score FROM event_results
                WHERE event_id = ? AND participated = 1
                ORDER BY score DESC
            """, (event_id,)).fetchall()

            scores = [r["score"] for r in results if r["score"] is not None]
            if not scores:
                continue

            mean_score = sum(scores) / len(scores)
            n = len(results)

            for rank, row in enumerate(results, start=1):
                if row["score"] is None:
                    continue
                percentile = round((n - rank) / (n - 1) * 100, 1) if n > 1 else 100.0
                pct_vs_mean = round((row["score"] - mean_score) / mean_score * 100, 1) \
                    if mean_score else 0.0
                c.execute("""
                    UPDATE event_results SET percentile = ?, pct_vs_mean = ? WHERE id = ?
                """, (percentile, pct_vs_mean, row["id"]))
            c.commit()


# ── Main ──────────────────────────────────────────────────────

def run_migration():
    log.info("=" * 50)
    log.info("AoO Alliance Stats — Google Sheets → SQLite Migration")
    log.info("=" * 50)

    # 1. Initialise DB
    init_db()
    log.info(f"Database: {os.environ.get('DB_PATH', 'alliance.db')}")

    # 2. Connect to Sheets
    log.info("Connecting to Google Sheets...")
    client = get_sheet_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    log.info(f"Connected: '{spreadsheet.title}'")

    # 3. Load tabs
    roster_rows = tab_as_dicts(spreadsheet, "Roster")
    event_rows = tab_as_dicts(spreadsheet, "Event Log")

    if not roster_rows:
        log.error("Roster tab is empty or missing — cannot proceed")
        sys.exit(1)

    # 4. Migrate
    with get_connection() as conn:
        name_to_id = migrate_roster(roster_rows, conn)
        conn.commit()

    if event_rows:
        with get_connection() as conn:
            migrate_event_log(event_rows, name_to_id, conn)
    else:
        log.warning("Event Log tab is empty — skipping event migration")

    # 5. Rebuild all aggregations
    log.info("Refreshing aggregations...")
    refresh_aggregations()

    log.info("=" * 50)
    log.info("Migration complete!")
    log.info(f"  Members migrated : {len(name_to_id)}")
    log.info(f"  Database file    : {os.environ.get('DB_PATH', 'alliance.db')}")
    log.info("=" * 50)
    log.info("")
    log.info("Next steps:")
    log.info("  1. Check review_queue for any unresolved name issues:")
    log.info("     python -c \"from db import get_bot_context; print(get_bot_context())\"")
    log.info("  2. Update bot.py to use db.get_bot_context() instead of sheets.py")
    log.info("  3. Add DB_PATH=alliance.db to your .env file")


if __name__ == "__main__":
    run_migration()
