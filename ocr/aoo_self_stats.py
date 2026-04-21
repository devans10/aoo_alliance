"""
aoo_self_stats.py — Enter your own player stats manually.

Since aoo_roster.py captures other players' profiles via clipboard + OCR,
your own stats need to be entered separately. This script prompts for each
stat field and saves a snapshot + updates the members table.

Usage:
    python aoo_self_stats.py                  # interactive entry for KillerDave
    python aoo_self_stats.py --name Treppix   # specify a different account
    python aoo_self_stats.py --show           # show current stats
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import db

DEFAULT_NAME = "KillerDave"

# Fields grouped by source, in prompt order.
# (field_name, display_label, is_int)
ROSTER_FIELDS = [
    ("city_level",    "City Level",              True),
    ("position",      "Position (R1-R5)",        False),
    ("battle_power",  "Battle Power",            True),
    ("reputation",    "Individual Reputation",   True),
    ("kills",         "Kills",                   True),
    ("losses",        "Losses",                  True),
]

RANKING_FIELDS = [
    ("officer_power",   "Officer Power",         True),
    ("titan_power",     "Titan Power",           True),
    ("warplane_power",  "Warplane Power",        True),
    ("island_lux",      "Island Luxoriousness",  True),
    ("antique_power",   "Antique Power",         True),
    ("merit",           "Merit",                 True),
    ("commander_level", "Commander Level",       True),
]


def _parse_input(raw: str, is_int: bool):
    """Parse user input. Returns None for blank/skip, value otherwise."""
    raw = raw.strip()
    if not raw or raw == "-":
        return None
    if is_int:
        cleaned = raw.replace(",", "").replace(" ", "")
        try:
            return int(cleaned)
        except ValueError:
            print(f"      Invalid number: {raw!r}")
            return None
    return raw


def _prompt_fields(fields: list, current: dict) -> dict:
    """Prompt for each field, showing current value. Returns non-None values."""
    values = {}
    for field_name, label, is_int in fields:
        cur = current.get(field_name)
        cur_display = f"{cur:,}" if isinstance(cur, int) else (cur or "-")
        try:
            raw = input(f"    {label:<25s} [{cur_display}]: ")
        except (EOFError, KeyboardInterrupt):
            print("\n    Cancelled.")
            return {}
        val = _parse_input(raw, is_int)
        if val is not None:
            values[field_name] = val
    return values


def show_stats(name: str):
    """Display current stats for a member."""
    conn = db.get_connection()
    row = conn.execute("""
        SELECT * FROM members
        WHERE name = ? OR clean_name = ?
    """, (name, db._to_clean_name(name))).fetchone()
    conn.close()

    if not row:
        print(f"  Member '{name}' not found in DB.")
        return

    row = dict(row)
    print(f"\n  Stats for {row['name']}  (id={row['id']}, status={row['status']})")
    if row.get("stats_updated_at"):
        print(f"  Last updated: {row['stats_updated_at']}")
    print()

    print("  Roster stats:")
    for field_name, label, _ in ROSTER_FIELDS:
        val = row.get(field_name)
        display = f"{val:,}" if isinstance(val, int) else (val or "-")
        print(f"    {label:<25s} {display}")

    print("\n  Ranking stats:")
    for field_name, label, _ in RANKING_FIELDS:
        val = row.get(field_name)
        display = f"{val:,}" if isinstance(val, int) else (val or "-")
        print(f"    {label:<25s} {display}")

    # Derived
    mil_rank = db.military_rank_from_merit(row.get("merit"))
    print(f"    {'Military Rank':<25s} {mil_rank or '-'}  (derived from merit)")
    print()


def enter_stats(name: str):
    """Interactive stat entry for a member."""
    db.init_db()
    conn = db.get_connection()

    # Resolve or create member
    member = db.resolve_member(name, conn)
    if member is None:
        print(f"  Member '{name}' not found. Creating...")
        member_id = db.add_member(name, conn=conn)
        member = dict(conn.execute("SELECT * FROM members WHERE id = ?",
                                   (member_id,)).fetchone())
    else:
        print(f"  Found: {member['name']} (id={member['id']})")

    member_id = member["id"]
    current = dict(member)
    today = date.today().isoformat()

    print(f"\n  Enter stats (press Enter to skip, '-' to clear)")
    print(f"  Date: {today}\n")

    # Prompt for roster fields
    print("  -- Roster stats --")
    roster_vals = _prompt_fields(ROSTER_FIELDS, current)

    print("\n  -- Ranking stats --")
    ranking_vals = _prompt_fields(RANKING_FIELDS, current)

    all_vals = {**roster_vals, **ranking_vals}
    if not all_vals:
        print("\n  No changes entered.")
        conn.close()
        return

    # Preview
    print(f"\n  Changes to save:")
    for k, v in all_vals.items():
        label = k.replace("_", " ").title()
        display = f"{v:,}" if isinstance(v, int) else v
        print(f"    {label:<25s} {display}")

    try:
        answer = input("\n  Save? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        conn.close()
        return

    if answer == "n":
        print("  Cancelled.")
        conn.close()
        return

    # Save as roster_ocr source (same as the roster script would)
    db.add_player_snapshot(
        member_id=member_id,
        source="roster_ocr",
        captured_at=today,
        conn=conn,
        **all_vals,
    )
    conn.close()

    print(f"\n  Saved {len(all_vals)} stat(s) for {name}.")


def main():
    parser = argparse.ArgumentParser(description="Enter your own AoO player stats")
    parser.add_argument("--name", default=DEFAULT_NAME,
                        help=f"Player name (default: {DEFAULT_NAME})")
    parser.add_argument("--show", action="store_true",
                        help="Show current stats instead of entering new ones")
    args = parser.parse_args()

    db.init_db()

    if args.show:
        show_stats(args.name)
    else:
        enter_stats(args.name)


if __name__ == "__main__":
    main()
