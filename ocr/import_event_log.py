"""
import_event_log.py — Import historical event data from aoo_event_log.csv into the database.

Reads the CSV, normalises event names and dates, resolves member names,
and inserts events + results into the DB.

Usage:
    python import_event_log.py                          # preview, prompt before writing
    python import_event_log.py --apply                  # apply without prompting
    python import_event_log.py --dry-run                # preview only
    python import_event_log.py --file path/to/file.csv  # custom CSV path
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher, get_close_matches
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import db

DEFAULT_CSV = str(Path(__file__).resolve().parent.parent / "aoo_event_log.csv")

# ── Normalisation ────────────────────────────────────────────────────────────

# Fix doubled words from OCR: "Void War War" → "Void War", "Battle Battle Frenzy" → "Battle Frenzy"
EVENT_TYPE_MAP = {
    "Void War War":         "Void War",
    "Battle Battle Frenzy": "Battle Frenzy",
}

# Manual name aliases for CSV names that don't match DB names automatically.
# CSV name → DB current name
NAME_ALIASES = {
    "MINH@DUC":    "MINH@ĐỨC",
    "ccchhii":     "ccchhiii千香",
    "UnderStorm":  "༒۝warlord۝༒",
    "DISASTER":    "°•°SHADOW°•°",
    "Surveyor":    "Prison◇Mike",
}


def _load_aliases_from_file():
    """Load additional aliases from name_aliases.txt into NAME_ALIASES."""
    aliases_path = Path(__file__).resolve().parent / "name_aliases.txt"
    if not aliases_path.exists():
        return
    for line in aliases_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            ocr_name, _, roster_name = line.partition("==")
            NAME_ALIASES[ocr_name.strip()] = roster_name.strip()


_load_aliases_from_file()


def normalise_event_type(raw: str) -> str | None:
    """Normalise an event type name. Returns None if empty/invalid."""
    raw = raw.strip()
    if not raw:
        return None
    return EVENT_TYPE_MAP.get(raw, raw)


def normalise_date(raw: str) -> str | None:
    """Parse various date formats to YYYY-MM-DD. Returns None if empty/invalid.

    Handles:
      - YYYY-MM-DD HH:MM (scraper output, possibly with OCR junk prefix)
      - MM/DD/YYYY or M/D/YYYY (manual/historical)
    """
    raw = raw.strip()
    if not raw:
        return None

    # Strip leading OCR junk: keep only digits, dashes, colons, slashes, spaces
    cleaned = re.sub(r"^[^0-9]+", "", raw)

    # OCR sometimes drops the leading digit of the year (e.g. "020-04-12" from "2020-04-12")
    # Restore it if we see a 3-digit year prefix
    if re.match(r"\d{3}-\d{2}-\d{2}", cleaned) and not re.match(r"\d{4}", cleaned):
        cleaned = "2" + cleaned

    # Try YYYY-MM-DD (with optional time suffix)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", cleaned)
    if m:
        try:
            datetime.strptime(m.group(1), "%Y-%m-%d")
            return m.group(1)
        except ValueError:
            pass

    # Try MM/DD/YYYY or M/D/YYYY
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def parse_score(raw: str) -> int:
    """Parse a comma-formatted score like '1,385,409,188' to int."""
    cleaned = raw.strip().strip('"').replace(",", "")
    if not cleaned:
        return 0
    try:
        return int(cleaned)
    except ValueError:
        return 0


# ── Import logic ─────────────────────────────────────────────────────────────


def load_csv(path: str) -> list[dict]:
    """Load and filter the event log CSV, skipping empty rows."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    valid = []
    for r in rows:
        name = r.get("Member Name", "").strip()
        event_type = normalise_event_type(r.get("Event Type", ""))
        event_date = normalise_date(r.get("Event Date", ""))
        if not name or not event_type or not event_date:
            continue
        valid.append({
            "name": name,
            "event_type": event_type,
            "event_date": event_date,
            "score": parse_score(r.get("Battle Score", "")),
            "attendance": r.get("Attendance", "").strip(),
        })

    return valid


def group_events(rows: list[dict]) -> dict[tuple, list[dict]]:
    """Group rows by (event_type, event_date) → list of member results."""
    events = defaultdict(list)
    for r in rows:
        key = (r["event_type"], r["event_date"])
        events[key].append(r)
    return dict(sorted(events.items(), key=lambda x: (x[0][1], x[0][0])))


def resolve_names(rows: list[dict], conn) -> dict[str, int | None]:
    """Resolve all unique member names to member IDs.

    Uses exact match first, then case-insensitive, then high-confidence fuzzy.
    Returns {name: member_id or None}.
    """
    unique_names = sorted(set(r["name"] for r in rows))

    # All current names in DB
    db_names = conn.execute(
        "SELECT mn.name, mn.member_id FROM member_names mn WHERE mn.is_current = 1"
    ).fetchall()
    name_to_id = {r["name"]: r["member_id"] for r in db_names}
    name_list = list(name_to_id.keys())

    # Case-insensitive lookup
    name_lower_to_id = {r["name"].lower(): r["member_id"] for r in db_names}

    resolved = {}
    for name in unique_names:
        # 0. Explicit alias
        if name in NAME_ALIASES:
            alias_target = NAME_ALIASES[name]
            if alias_target in name_to_id:
                resolved[name] = name_to_id[alias_target]
                continue

        # 1. Exact match
        if name in name_to_id:
            resolved[name] = name_to_id[name]
            continue

        # 2. Case-insensitive match
        if name.lower() in name_lower_to_id:
            resolved[name] = name_lower_to_id[name.lower()]
            continue

        # 3. Fuzzy match (≥0.85)
        matches = get_close_matches(name, name_list, n=1, cutoff=0.80)
        if matches:
            conf = SequenceMatcher(None, name.lower(), matches[0].lower()).ratio()
            if conf >= 0.85:
                resolved[name] = name_to_id[matches[0]]
                continue

        resolved[name] = None

    return resolved


ALIASES_FILE = str(Path(__file__).resolve().parent / "name_aliases.txt")


def review_unmatched(resolved: dict[str, int | None], rows: list[dict], conn) -> dict[str, int | None]:
    """Interactive review for unmatched names.

    For each unresolved name, shows fuzzy candidates and offers:
      m) match to a DB member (by number)
      n) create as new member
      a) add as alias in name_aliases.txt → matched member
      s) skip this name
      q) quit review (skip remaining)
    """
    unmatched = sorted(n for n, mid in resolved.items() if mid is None)
    if not unmatched:
        return resolved

    # Gather scores per name for context
    name_scores = defaultdict(list)
    for r in rows:
        if r["name"] in resolved and resolved[r["name"]] is None:
            name_scores[r["name"]].append(r["score"])

    # All DB names for fuzzy matching
    db_rows = conn.execute(
        "SELECT mn.name, mn.member_id, m.status "
        "FROM member_names mn JOIN members m ON m.id = mn.member_id"
    ).fetchall()
    all_db_names = [r["name"] for r in db_rows]
    name_to_id = {r["name"]: r["member_id"] for r in db_rows}
    name_to_status = {r["name"]: r["status"] for r in db_rows}

    print(f"\n  ── Review {len(unmatched)} unmatched name(s) ──")
    print(f"  Commands: (m)atch, (n)ew member, (a)lias, (s)kip, (q)uit\n")

    for i, name in enumerate(unmatched, 1):
        scores = name_scores[name]
        avg_score = sum(scores) // len(scores) if scores else 0
        print(f"  [{i}/{len(unmatched)}] \"{name}\"  (avg score: {avg_score:,})")

        # Show top fuzzy matches
        candidates = get_close_matches(name, all_db_names, n=5, cutoff=0.40)
        if candidates:
            print(f"    Fuzzy matches:")
            for j, cand in enumerate(candidates, 1):
                conf = SequenceMatcher(None, name.lower(), cand.lower()).ratio()
                status = name_to_status.get(cand, "?")
                mid = name_to_id[cand]
                print(f"      {j}. {cand}  (id={mid}, {status}, {conf:.0%})")
        else:
            print(f"    No fuzzy matches found.")

        while True:
            try:
                choice = input(f"    Action [m/n/a/s/q]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n    Quitting review.")
                return resolved

            if choice == "q":
                print("    Skipping remaining unmatched.")
                return resolved

            if choice == "s":
                print(f"    Skipped.")
                break

            if choice == "n":
                member_id = db.add_member(name, conn=conn)
                resolved[name] = member_id
                # Add to local lookup for subsequent matches
                all_db_names.append(name)
                name_to_id[name] = member_id
                name_to_status[name] = "active"
                print(f"    Created member \"{name}\" (id={member_id})")
                break

            if choice == "m":
                if not candidates:
                    print(f"    No candidates to match. Use (n) to create or (s) to skip.")
                    continue
                try:
                    pick = input(f"    Match to which # (1-{len(candidates)})? ").strip()
                    idx = int(pick) - 1
                    if 0 <= idx < len(candidates):
                        matched_name = candidates[idx]
                        mid = name_to_id[matched_name]
                        resolved[name] = mid
                        print(f"    Matched \"{name}\" → \"{matched_name}\" (id={mid})")
                        break
                    else:
                        print(f"    Invalid number.")
                except (ValueError, EOFError, KeyboardInterrupt):
                    print(f"    Invalid input.")
                    continue

            if choice == "a":
                if not candidates:
                    print(f"    No candidates. Use (n) to create or (s) to skip.")
                    continue
                try:
                    pick = input(f"    Alias to which # (1-{len(candidates)})? ").strip()
                    idx = int(pick) - 1
                    if 0 <= idx < len(candidates):
                        matched_name = candidates[idx]
                        mid = name_to_id[matched_name]
                        resolved[name] = mid
                        # Append to name_aliases.txt
                        with open(ALIASES_FILE, "a", encoding="utf-8") as af:
                            af.write(f"{name}=={matched_name}\n")
                        print(f"    Aliased \"{name}\" → \"{matched_name}\" (saved to name_aliases.txt)")
                        break
                    else:
                        print(f"    Invalid number.")
                except (ValueError, EOFError, KeyboardInterrupt):
                    print(f"    Invalid input.")
                    continue

            if choice not in ("m", "n", "a", "s", "q"):
                print(f"    Unknown command. Use m/n/a/s/q.")

        print()

    still_unmatched = sum(1 for mid in resolved.values() if mid is None)
    if still_unmatched:
        print(f"  {still_unmatched} name(s) still unmatched (will be skipped during import).")
    else:
        print(f"  All names resolved.")

    return resolved


def preview_import(events: dict[tuple, list], resolved: dict[str, int | None]):
    """Print a summary of what will be imported."""
    total_rows = sum(len(rows) for rows in events.values())
    unresolved = {n for n, mid in resolved.items() if mid is None}
    auto_matched = {n for n, mid in resolved.items() if mid is not None and n not in {
        r["name"] for r in next(iter(events.values()))  # just for type checking
    }}

    print(f"\n  Import Preview")
    print(f"  {'=' * 55}")
    print(f"\n  Events: {len(events)}")
    for (etype, edate), rows in events.items():
        present = sum(1 for r in rows if r["attendance"] == "Present")
        absent = len(rows) - present
        print(f"    {edate}  {etype:<25s}  {present:>3d} present, {absent:>3d} absent")

    print(f"\n  Members: {len(resolved)} unique names")
    print(f"    Resolved: {sum(1 for m in resolved.values() if m is not None)}")
    if unresolved:
        print(f"    Unresolved: {len(unresolved)}")
        for n in sorted(unresolved):
            print(f"      ? {n}")

    print(f"\n  Total rows to import: {total_rows}")


def apply_import(events: dict[tuple, list], resolved: dict[str, int | None], conn):
    """Insert events and results into the database."""
    events_created = 0
    results_inserted = 0
    members_created = 0  # created during review, tracked for reporting

    for (etype, edate), rows in events.items():
        # Check for existing event to avoid duplicates
        existing = conn.execute(
            "SELECT id FROM events WHERE event_type = ? AND occurred_at = ?",
            (etype, edate)
        ).fetchone()
        if existing:
            print(f"    Skip {edate} {etype} — already in DB (event #{existing['id']})")
            continue

        event_id = db.add_event(etype, edate, conn=conn)
        events_created += 1

        for r in rows:
            member_id = resolved.get(r["name"])
            if member_id is None:
                # Skip unresolved names (should have been handled in review)
                continue

            participated = r["attendance"] == "Present"
            score = r["score"] if participated else 0

            # Only record members who actually participated
            if not participated or score <= 0:
                continue

            db.add_event_result(
                member_id=member_id,
                event_id=event_id,
                score=score,
                source="csv_import",
                conn=conn,
            )
            results_inserted += 1

        # Finalise calculates percentiles and refreshes aggregations
        db.finalise_event(event_id)

    conn.commit()
    return events_created, results_inserted, members_created


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Import historical event data from CSV into the database"
    )
    parser.add_argument(
        "--file", default=DEFAULT_CSV,
        help=f"Path to event log CSV (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply without prompting",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only, don't write",
    )
    parser.add_argument(
        "--no-review", action="store_true",
        help="Skip interactive review of unmatched names",
    )
    args = parser.parse_args()

    if not Path(args.file).exists():
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    print("=" * 60)
    print("  Import Event Log → Database")
    print("=" * 60)

    # 1. Load CSV
    print(f"\n  Loading: {args.file}")
    rows = load_csv(args.file)
    print(f"  Valid rows: {len(rows)}")

    if not rows:
        print("  No valid data to import.")
        return

    # 2. Group into events
    events = group_events(rows)

    # 3. Resolve member names
    db.init_db()
    conn = db.get_connection()
    try:
        resolved = resolve_names(rows, conn)

        # 3b. Interactive review of unmatched names
        unmatched_count = sum(1 for mid in resolved.values() if mid is None)
        if unmatched_count > 0 and not args.dry_run and not args.no_review:
            resolved = review_unmatched(resolved, rows, conn)

        # 4. Preview
        preview_import(events, resolved)

        if args.dry_run:
            print("\n  [dry-run] No changes written.")
            return

        # 5. Confirm and apply
        if not args.apply:
            print()
            try:
                answer = input("  Apply this import? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Cancelled.")
                return
            if answer != "y":
                print("  Cancelled.")
                return

        print("\n  Importing...")
        events_created, results_inserted, members_created = apply_import(events, resolved, conn)
        print(f"\n  Done:")
        print(f"    Events created:   {events_created}")
        print(f"    Results inserted: {results_inserted}")
        print(f"    New members:      {members_created}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
