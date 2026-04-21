"""
aoo_roster.py — Interactive alliance roster builder.

Workflow:
  1. You open a member's profile in-game and click the "Copy" button.
  2. Press Enter in this script's terminal.
  3. The script reads the name from the clipboard, captures the game window,
     OCRs City Level / Battle Power / Individual Reputation / Position,
     and appends a row to the CSV.
  4. Navigate to the next profile and repeat.

Usage:
    ./venv/bin/python aoo_roster.py              # interactive capture
    ./venv/bin/python aoo_roster.py --show       # print current CSV
"""

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

from PIL import Image, ImageEnhance
import pytesseract

# ─── Shared infrastructure from event scraper ────────────────────────────────

from aoo_scraper import (
    WINDOW_TITLE,
    _session_env,
    _get_window_id_wmctrl,
    _get_inner_window_geom,
    _grab_window,
    capture_window,
    _read_clipboard,
    _ocr,
    _clean_name,
    _px,
)

# ─── Constants ───────────────────────────────────────────────────────────────

# ─── Shared DB layer ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import db

CSV_PATH = "aoo_roster.csv"
CSV_HEADERS = ["Date", "Member Name", "City Level", "Battle Power",
               "Individual Reputation", "Kills", "Losses", "Position"]

# Profile page OCR regions (ratio-based, tuned against 690×1034 window).
# City Level line: "City Level Lv.41" — right of avatar, below name.
CITY_X1, CITY_Y1, CITY_W, CITY_H = 0.15, 0.145, 0.40, 0.030

# Stats box: Battle Power / Power Ranking / Individual Reputation / Position /
# Kills / Losses — 3×2 grid below the medal row.
STATS_X1, STATS_Y1, STATS_W, STATS_H = 0.05, 0.350, 0.90, 0.170

# Position map: normalise OCR'd position text to rank codes.
POSITION_MAP = {
    "alliance leader": "R5",
    "deputy": "R4",
    "cabinet": "R3",
    "elite": "R2",
    "member": "R1",
}


# ─── Profile OCR ─────────────────────────────────────────────────────────────


def _strip_alliance_tag(raw_name: str) -> str:
    """Remove alliance tag prefix like '(MOM) ' or 'MOM) '."""
    stripped = re.sub(r'^\([^)]{1,8}\)\s*', '', raw_name)
    if stripped == raw_name:
        stripped = re.sub(r'^[A-Z0-9]{1,8}\)\s*', '', raw_name)
    return stripped.strip()


def _parse_city_level(text: str) -> str:
    """Extract city level number from text like 'City Level Lv.41'."""
    m = re.search(r'Lv\.?\s*(\d+)', text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r'\b(\d{1,2})\b', text)
    return m.group(1) if m else ""


def _parse_number(text: str) -> str:
    """Extract a comma-separated number, normalise OCR artefacts."""
    normalised = text.replace(".", ",").replace("O", "0").replace("o", "0")
    m = re.search(r'[\d,]{2,}', normalised)
    if m:
        return m.group(0).replace(",", "")
    return ""


def _parse_stats(text: str) -> dict:
    """Parse the stats box OCR text into individual fields.

    Expected layout (3×2 grid):
        Battle Power   82,625,964    Power Ranking      3
        Individual Reputation 84,010 Position  Alliance Leader
        Kills        264,484,300     Losses     10,953,132
    """
    result = {"battle_power": "", "individual_reputation": "",
              "kills": "", "losses": "", "position": ""}

    # Battle Power — number after "Battle Power"
    m = re.search(
        r'Battle\s*Power\s+([\d,.\s]+)',
        text, re.IGNORECASE,
    )
    if m:
        result["battle_power"] = _parse_number(m.group(1))

    # Individual Reputation — number after "Reputation"
    m = re.search(
        r'Reputation\s+([\d,.\s]+)',
        text, re.IGNORECASE,
    )
    if m:
        result["individual_reputation"] = _parse_number(m.group(1))

    # Position — text after "Position"
    m = re.search(
        r'Position\s+(.+?)(?:\n|Kills|$)',
        text, re.IGNORECASE,
    )
    if m:
        pos_raw = m.group(1).strip()
        pos_raw = re.sub(r'[|}\])\s]+$', '', pos_raw).strip()
        pos_key = pos_raw.lower()
        result["position"] = POSITION_MAP.get(pos_key, pos_raw)

    # Kills — number after "Kills"
    m = re.search(
        r'Kills\s+([\d,.\s]+)',
        text, re.IGNORECASE,
    )
    if m:
        result["kills"] = _parse_number(m.group(1))

    # Losses — number after "Losses"
    m = re.search(
        r'Losses\.?\s+([\d,.\s]+)',
        text, re.IGNORECASE,
    )
    if m:
        result["losses"] = _parse_number(m.group(1))

    return result


def extract_profile(img: Image.Image, debug: bool = False) -> dict:
    """OCR a Commander Info profile page and return all fields."""
    # City level — try a few y-offsets since layout varies slightly.
    city_level = ""
    for dy in [0, -0.015, 0.015]:
        crop = _px(img, CITY_X1, CITY_Y1 + dy, CITY_W, CITY_H)
        text = _ocr(crop, psm=7)
        if debug:
            crop.save(f"debug_city_dy{dy:+.3f}.png")
            print(f"      [debug] city OCR (dy={dy:+.3f}): {text!r}")
        city_level = _parse_city_level(text)
        if city_level:
            break

    # Stats box
    stats_crop = _px(img, STATS_X1, STATS_Y1, STATS_W, STATS_H)
    stats_text = _ocr(stats_crop, psm=6)
    if debug:
        stats_crop.save("debug_stats.png")
        print(f"      [debug] stats OCR:\n{stats_text}")
    stats = _parse_stats(stats_text)

    return {
        "city_level":            city_level,
        "battle_power":          stats["battle_power"],
        "individual_reputation": stats["individual_reputation"],
        "kills":                 stats["kills"],
        "losses":                stats["losses"],
        "position":              stats["position"],
        "_stats_text":           stats_text,
    }


# ─── CSV I/O ─────────────────────────────────────────────────────────────────


def load_roster(path: str = CSV_PATH) -> list[dict]:
    """Load existing roster rows from CSV."""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_roster(rows: list[dict], path: str = CSV_PATH) -> None:
    """Write the full roster CSV (overwrites)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for r in rows:
            writer.writerow({h: r.get(h, "") for h in CSV_HEADERS})


def append_row(row: dict, path: str = CSV_PATH) -> int:
    """Append a single row to the CSV. Creates the file if needed.
    Returns the new row count."""
    p = Path(path)
    write_header = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow({h: row.get(h, "") for h in CSV_HEADERS})
    return len(load_roster(path))


# ─── DB write ───────────────────────────────────────────────────────────────


def _parse_int(val) -> int | None:
    """Parse an OCR'd stat value to int, stripping commas and artifacts."""
    if val is None:
        return None
    s = str(val).replace(",", "").replace(" ", "").strip()
    # Strip non-digit chars from ends (OCR artifacts)
    s = re.sub(r'^[^\d]+', '', s)
    s = re.sub(r'[^\d]+$', '', s)
    try:
        return int(s) if s else None
    except ValueError:
        return None


def write_to_db(rows: list[dict]) -> int:
    """Write roster rows to the database via shared/db.py.

    For each row, resolves the member by name (creating new members as needed),
    then stores a player stats snapshot (power, kills, city level, etc.).
    """
    db.init_db()
    conn = db.get_connection()
    count = 0
    try:
        for r in rows:
            name = r.get("Member Name", "").strip()
            if not name:
                continue
            member = db.resolve_member(name, conn)
            if member is None:
                member_id = db.add_member(name, joined_date=r.get("Date"), conn=conn)
            else:
                member_id = member["id"]
                # Auto-reactivate inactive members when they appear in a capture
                if member.get("status") == "inactive":
                    db.set_member_status(member_id, "active",
                                         noted_by="roster_ocr", conn=conn)
                    print(f"    Reactivated: {name}")

            # Store the stats snapshot
            city = r.get("City Level", "")
            city_match = re.search(r'(\d+)', str(city)) if city else None
            db.add_player_snapshot(
                member_id=member_id,
                source="roster_ocr",
                city_level=int(city_match.group(1)) if city_match else None,
                battle_power=_parse_int(r.get("Battle Power")),
                kills=_parse_int(r.get("Kills")),
                losses=_parse_int(r.get("Losses")),
                reputation=_parse_int(r.get("Individual Reputation")),
                position=r.get("Position"),
                captured_at=r.get("Date"),
                conn=conn,
            )
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


# ─── Finalize ────────────────────────────────────────────────────────────────


def _finalize_roster(seen_names: set[str]):
    """Mark active members not captured in this session as inactive."""
    conn = db.get_connection()
    try:
        active = conn.execute(
            "SELECT id, name FROM members WHERE status = 'active'"
        ).fetchall()

        unseen = [
            r for r in active
            if r["name"].strip().lower() not in seen_names
        ]

        if not unseen:
            print("    All active members were captured — nothing to finalize.")
            return

        print(f"\n    {len(unseen)} active member(s) NOT captured this session:")
        for r in sorted(unseen, key=lambda x: x["name"].lower()):
            print(f"      - {r['name']}")

        print()
        try:
            answer = input(f"    Mark these {len(unseen)} member(s) as inactive? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n    Cancelled.")
            return

        if answer != "y":
            print("    Skipped.")
            return

        for r in unseen:
            db.set_member_status(r["id"], "inactive",
                                 noted_by="roster_ocr", conn=conn)
        print(f"    {len(unseen)} member(s) marked inactive.")

    finally:
        conn.close()


# ─── Interactive loop ─────────────────────────────────────────────────────────


def show_roster(csv_mode: bool = False):
    """Print the current roster."""
    if csv_mode:
        rows = load_roster()
        if not rows:
            print("No roster data yet.")
            return
        print(f"\n{'#':>3s}  {'Date':<10s}  {'Name':<24s} {'Lv':>3s} "
              f"{'Battle Power':>14s} {'Reputation':>12s} "
              f"{'Kills':>14s} {'Losses':>14s} {'Position':<8s}")
        print("-" * 110)
        for i, r in enumerate(rows, 1):
            print(f"{i:3d}  {r.get('Date',''):<10s}  "
                  f"{r.get('Member Name',''):<24s} "
                  f"{r.get('City Level',''):>3s} "
                  f"{r.get('Battle Power',''):>14s} "
                  f"{r.get('Individual Reputation',''):>12s} "
                  f"{r.get('Kills',''):>14s} "
                  f"{r.get('Losses',''):>14s} "
                  f"{r.get('Position',''):<8s}")
        print(f"\nTotal: {len(rows)} rows")
    else:
        stats = db.get_latest_stats()
        if not stats:
            print("No roster data yet.")
            return
        print(f"\n{'#':>3s}  {'Name':<24s} {'Lv':>6s} "
              f"{'Power':>14s} {'Reputation':>12s} "
              f"{'Kills':>14s} {'Losses':>14s} {'Pos':<5s} {'Date':<11s}")
        print("-" * 110)
        for i, s in enumerate(stats, 1):
            power = f"{s['battle_power']:,}" if s['battle_power'] else "-"
            city = str(s['city_level']) if s['city_level'] else "-"
            rep = f"{s['reputation']:,}" if s['reputation'] else "-"
            kills = f"{s['kills']:,}" if s['kills'] else "-"
            losses = f"{s['losses']:,}" if s['losses'] else "-"
            print(f"{i:3d}  {s['name']:<24s} {city:>6s} "
                  f"{power:>14s} {rep:>12s} "
                  f"{kills:>14s} {losses:>14s} {s['position'] or '-':<5s} {s['captured_at']:<11s}")
        print(f"\nTotal: {len(stats)} players")


def interactive(debug: bool = False, csv_mode: bool = False):
    """Main interactive capture loop."""
    mode_label = "CSV" if csv_mode else "DB"
    print("=" * 60)
    print(f"  AoO Roster Builder  —  Interactive Mode  [{mode_label}]")
    print("=" * 60)
    print()
    print("  1. Open a member's profile in-game")
    print("  2. Click the 'Copy' button on their profile")
    print("  3. Press Enter here to capture")
    print()
    print("  Commands:  (enter at the prompt)")
    print("    Enter     — capture current profile")
    print("    s         — show roster so far")
    print("    d         — delete last entry")
    print("    f         — finalize (mark unseen members inactive)")
    print("    q         — quit")
    print()

    # Detect the game window once.
    try:
        img, geom = capture_window()
        w, h = img.size
        print(f"  Game window: {w}x{h}")
    except Exception as e:
        print(f"  [warn] Could not detect game window: {e}")
        print("  Make sure the game is running.")
        return

    env = _session_env()
    today = date.today().isoformat()

    # Duplicate check is per-date: same name can appear on different weeks.
    if csv_mode:
        roster = load_roster()
        seen_names = {
            r["Member Name"].strip().lower()
            for r in roster if r.get("Date") == today
        }
        total_count = len(roster)
    else:
        db.init_db()
        conn = db.get_connection()
        today_rows = conn.execute(
            "SELECT m.name FROM player_snapshots ps "
            "JOIN members m ON m.id = ps.member_id "
            "WHERE ps.captured_at = ? AND ps.source = 'roster_ocr'",
            (today,)
        ).fetchall()
        conn.close()
        seen_names = {r["name"].strip().lower() for r in today_rows}
        total_count = len(db.get_latest_stats())

    today_count = len(seen_names)

    if today_count:
        print(f"  Resuming — {today_count} members captured today ({today})")
    elif total_count:
        print(f"  {total_count} players in {'CSV' if csv_mode else 'DB'} from prior sessions")
    print(f"  Collection date: {today}")
    print()

    while True:
        try:
            cmd = input(f"  [{today_count + 1}] Press Enter to capture (s/d/f/q): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if cmd == "q":
            break
        elif cmd == "s":
            show_roster(csv_mode=csv_mode)
            print()
            continue
        elif cmd == "d":
            if csv_mode:
                roster = load_roster()
                if roster:
                    removed = roster.pop()
                    save_roster(roster)
                    name = removed.get("Member Name", "?")
                    seen_names.discard(name.strip().lower())
                    today_count = sum(1 for r in roster if r.get("Date") == today)
                    print(f"    Deleted: {name}")
                else:
                    print("    Nothing to delete.")
            else:
                print("    Delete not supported in DB mode.")
            print()
            continue
        elif cmd == "f":
            if csv_mode:
                print("    Finalize not supported in CSV mode.")
            else:
                _finalize_roster(seen_names)
            print()
            continue
        elif cmd != "":
            print(f"    Unknown command: {cmd!r}")
            continue

        # ── Capture ──
        # 1. Read name from clipboard (user already clicked Copy).
        clipboard = _read_clipboard(env=env)
        if not clipboard:
            print("    Clipboard is empty — did you click the Copy button?")
            continue

        # Sanity check: player names are short, single-line strings
        if len(clipboard) > 60 or "\n" in clipboard:
            print(f"    Clipboard doesn't look like a player name ({len(clipboard)} chars).")
            print(f"    Make sure you clicked the Copy button on their profile.")
            continue

        name = _strip_alliance_tag(clipboard)
        if not name:
            name = clipboard  # fallback to raw if stripping removed everything

        name_key = name.strip().lower()
        if name_key in seen_names:
            print(f"    '{name}' already captured today — skipping.")
            print(f"    (Press Enter again to recapture, or 'd' to delete last)")
            continue

        # 2. Capture the game window and OCR the profile fields.
        try:
            profile_img = _grab_window(geom)
        except Exception as e:
            print(f"    Capture failed: {e}")
            continue

        data = extract_profile(profile_img, debug=debug)

        row = {
            "Date":                   today,
            "Member Name":            name,
            "City Level":             data["city_level"],
            "Battle Power":           data["battle_power"],
            "Individual Reputation":  data["individual_reputation"],
            "Kills":                  data["kills"],
            "Losses":                 data["losses"],
            "Position":               data["position"],
        }

        # 3. Show what we got and append.
        pos = row["Position"] or "?"
        lv = row["City Level"] or "?"
        bp = row["Battle Power"] or "?"
        rep = row["Individual Reputation"] or "?"
        kills = row["Kills"] or "?"
        losses = row["Losses"] or "?"

        if csv_mode:
            append_row(row)
        else:
            write_to_db([row])

        seen_names.add(name_key)
        today_count += 1

        dest = CSV_PATH if csv_mode else "DB"
        print(f"    [{today_count:3d}] {name:<24s}  Lv.{lv:<3s}  "
              f"BP={bp:<12s}  Rep={rep:<8s}  "
              f"K={kills:<12s}  L={losses:<12s}  {pos}  → {dest}")

    # ── Done ──
    if csv_mode:
        final_today = sum(1 for r in load_roster() if r.get("Date") == today)
        total = len(load_roster())
        print(f"\nDone — {final_today} members captured today, {total} total rows in {CSV_PATH}")
    else:
        print(f"\nDone — {today_count} members captured today")


# ─── Entry point ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="AoO Roster Builder")
    parser.add_argument("--show", action="store_true", help="Print current CSV roster")
    parser.add_argument("--csv", action="store_true", help="Write to CSV instead of DB")
    parser.add_argument("--debug", action="store_true", help="Save debug OCR crops")
    parser.add_argument("--finalize", action="store_true",
                        help="Mark active members not captured today as inactive")
    args = parser.parse_args()

    if args.show:
        show_roster()
        return

    if not args.csv:
        db.init_db()

    if args.finalize:
        if args.csv:
            print("Finalize not supported in CSV mode.")
            return
        # Build seen_names from today's snapshots
        today = date.today().isoformat()
        conn = db.get_connection()
        today_rows = conn.execute(
            "SELECT m.name FROM player_snapshots ps "
            "JOIN members m ON m.id = ps.member_id "
            "WHERE ps.captured_at = ? AND ps.source = 'roster_ocr'",
            (today,)
        ).fetchall()
        conn.close()
        seen = {r["name"].strip().lower() for r in today_rows}
        if not seen:
            print(f"No members captured today ({today}). Run a roster session first.")
            return
        print(f"  {len(seen)} members captured today ({today})")
        _finalize_roster(seen)
        return

    interactive(debug=args.debug, csv_mode=args.csv)


if __name__ == "__main__":
    main()
