"""
aoo_rankings.py — Interactive nation rankings leaderboard scraper.

Captures the top-100 rankings from the AoO game's leaderboard screens.
Each leaderboard category (Battle Power, Kills, etc.) shows ranked players
with their stat values.

Workflow:
  1. Open a rankings leaderboard in-game.
  2. Press Enter to capture the current page.
  3. Scroll down and press Enter again for the next page.
  4. Type 'q' to finish and write results to the database.

Usage:
    python aoo_rankings.py              # interactive capture (DB mode)
    python aoo_rankings.py --csv        # write to CSV instead of DB
    python aoo_rankings.py --debug      # save OCR debug crops
    python aoo_rankings.py --category battle_power  # override auto-detect
"""

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

from PIL import Image
import pytesseract

# ─── Shared infrastructure from event scraper ────────────────────────────────

from aoo_scraper import (
    WINDOW_TITLE,
    _session_env,
    _get_window_id_wmctrl,
    _get_inner_window_geom,
    _grab_window,
    capture_window,
    _ocr,
    _clean_name,
    _px,
)

# ─── Shared DB layer ────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import db

# ─── Constants ───────────────────────────────────────────────────────────────

CSV_PATH = "aoo_rankings.csv"
CSV_HEADERS = ["Date", "Category", "Rank", "Player Name", "Value"]

# Rankings categories — map OCR'd header text to normalised keys.
# Headers read "Commander <X> Ranking" in-game.
CATEGORY_MAP = {
    "power":      "battle_power",
    "kill":       "kills",
    "city":       "city_level",
    "officer":    "officer",
    "titan":      "titan",
    "warplane":   "warplane",
    "island":     "island",
    "antique":    "antique",
}

# OCR regions (ratio-based) — tuned from Ranking_Screenshot.png.
#
# The rankings page layout (tuned from Ranking_Screenshot.png, 690×1047):
#   - Header "Commander <X> Ranking" near the top
#   - "My Rank: N" subheader
#   - Column headers: "Rank" and category-specific label
#   - ~8 rows per page, top 3 have special styling (medals, larger avatars)
#   - Each row: rank#/medal, avatar, "(TAG) Name / Alliance: ...", value

# Category header text region (below "Age of Origins" title bar)
HDR_X1, HDR_Y1, HDR_W, HDR_H = 0.10, 0.06, 0.80, 0.05

# Name column: after avatar, before value.
# Some avatars have wide frames that bleed into the text, so we try
# NAME_X1_NARROW first, falling back to NAME_X1_WIDE if no (TAG) found.
NAME_X1_NARROW = 0.32
NAME_X1_WIDE   = 0.36    # aggressive avatar skip for decorated frames
NAME_X2        = 0.62
# Value column: right side
VALUE_X1, VALUE_X2 = 0.62, 0.95

# Row y-boundaries as ratios — first page (top 3 rows are taller due to medals)
ROW_Y_PAGE1 = [
    (0.186, 0.291),  # rank 1
    (0.291, 0.382),  # rank 2
    (0.382, 0.468),  # rank 3
    (0.468, 0.549),  # rank 4
    (0.549, 0.630),  # rank 5
    (0.630, 0.716),  # rank 6
    (0.716, 0.797),  # rank 7
    (0.797, 0.903),  # rank 8
]

# For scrolled pages (uniform row height, no medals)
UNIFORM_ROW_Y1 = 0.186   # below column headers
UNIFORM_ROW_H  = 0.088
UNIFORM_ROWS   = 8       # 9th row is an ad banner


# ─── OCR helpers ─────────────────────────────────────────────────────────────


def _parse_number(text: str) -> int | None:
    """Extract a number from OCR text, stripping commas and artifacts."""
    normalised = text.replace(".", ",").replace("O", "0").replace("o", "0")
    m = re.search(r'[\d,]{2,}', normalised)
    if m:
        digits = m.group(0).replace(",", "")
        try:
            return int(digits)
        except ValueError:
            return None
    # Try plain digits
    m = re.search(r'\d+', normalised)
    if m:
        try:
            return int(m.group(0))
        except ValueError:
            return None
    return None


def _parse_rank(text: str) -> int | None:
    """Extract a rank number (1-100) from OCR text."""
    m = re.search(r'\d+', text)
    if m:
        val = int(m.group(0))
        if 1 <= val <= 100:
            return val
    return None


def detect_category(img: Image.Image, debug: bool = False) -> str | None:
    """OCR the header region to detect which ranking category is displayed.

    Expected format: "Commander <X> Ranking"
    """
    crop = _px(img, HDR_X1, HDR_Y1, HDR_W, HDR_H)
    # Use raw crop with psm=6 — _preprocess contrast is too aggressive,
    # and psm=7 misreads "Power" as "Roney" on live captures.
    text = pytesseract.image_to_string(crop, config="--psm 6").strip()
    if debug:
        crop.save("debug_rankings_header.png")
        print(f"  [debug] header OCR: {text!r}")

    text_lower = text.lower().strip()
    for key, category in CATEGORY_MAP.items():
        if key in text_lower:
            return category
    return None


def _clean_ranking_name(text: str) -> str:
    """Clean a player name from rankings OCR.

    Rankings show "(TAG) Name" on line 1 and "Alliance: ..." on line 2.
    Strip the alliance tag and the second line.
    """
    # Take first line only (ignore "Alliance: ..." line)
    name = text.split("\n")[0].strip()
    # Remove OCR noise before the alliance tag
    m = re.search(r'\(.*?\)\s*(.*)', name)
    if m:
        name = m.group(1).strip()
    else:
        # No tag found — strip leading OCR noise (non-letter chars)
        name = re.sub(r'^[^a-zA-Z\u0080-\uffff]+', '', name)
    # Fall back to shared cleaner for remaining OCR artifacts
    return _clean_name(name)


def extract_rows(img: Image.Image, page: int = 1,
                 start_rank: int = 1, debug: bool = False) -> list[dict]:
    """Extract ranked player rows from a rankings page.

    Page 1 uses ROW_Y_PAGE1 positions (top 3 have medals).
    Pages 2+ use uniform row spacing.
    ``start_rank`` is the rank of the first row on this page.

    Returns list of {'rank': int, 'name': str, 'value': int|None}.
    """
    w, h = img.size
    rows = []

    if page == 1:
        row_ys = ROW_Y_PAGE1
    else:
        row_ys = [
            (UNIFORM_ROW_Y1 + i * UNIFORM_ROW_H,
             UNIFORM_ROW_Y1 + (i + 1) * UNIFORM_ROW_H)
            for i in range(UNIFORM_ROWS)
        ]

    for i, (ry1, ry2) in enumerate(row_ys):
        y1, y2 = int(ry1 * h), int(ry2 * h)
        row_h = y2 - y1
        if row_h < 10:
            continue

        # Name: two-pass OCR to handle wide avatar frames.
        # Pass 1 with narrow crop; if no alliance tag found, retry wider.
        name_crop = img.crop((int(NAME_X1_NARROW * w), y1, int(NAME_X2 * w), y2))
        name_text = pytesseract.image_to_string(name_crop, config="--psm 6").strip()
        if not re.search(r'\([A-Za-z]{2,}\)', name_text.split("\n")[0]):
            wide_crop = img.crop((int(NAME_X1_WIDE * w), y1, int(NAME_X2 * w), y2))
            wide_text = pytesseract.image_to_string(wide_crop, config="--psm 6").strip()
            if re.search(r'\([A-Za-z]{2,}\)', wide_text.split("\n")[0]):
                name_text = wide_text

        # Value: full row height, psm=6 — take first line only
        value_crop = img.crop((int(VALUE_X1 * w), y1, int(VALUE_X2 * w), y2))
        value_text = pytesseract.image_to_string(
            value_crop, config="--psm 6"
        ).strip().split("\n")[0]

        if debug:
            name_crop.save(f"debug_rankings_r{i}_name.png")
            value_crop.save(f"debug_rankings_r{i}_value.png")
            print(f"  [debug] row {i}: name={name_text!r} value={value_text!r}")

        name = _clean_ranking_name(name_text) if name_text else ""
        value = _parse_number(value_text)
        rank = start_rank + i

        if rank <= 100 and name:
            rows.append({"rank": rank, "name": name, "value": value})

    return rows


# ─── Output ──────────────────────────────────────────────────────────────────


def write_to_db(category: str, all_rows: list[dict]) -> int:
    """Write ranking entries to the database."""
    db.init_db()
    conn = db.get_connection()
    count = 0
    today = date.today().isoformat()
    try:
        for r in all_rows:
            db.add_ranking_entry(
                player_name=r["name"],
                category=category,
                rank_position=r["rank"],
                value=r["value"],
                captured_at=today,
                conn=conn,
            )
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def write_to_csv(category: str, all_rows: list[dict], path: str = CSV_PATH) -> int:
    """Append ranking entries to CSV."""
    p = Path(path)
    write_header = not p.exists() or p.stat().st_size == 0
    today = date.today().isoformat()
    with p.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        for r in all_rows:
            writer.writerow({
                "Date": today,
                "Category": category,
                "Rank": r["rank"],
                "Player Name": r["name"],
                "Value": r["value"] or "",
            })
    return len(all_rows)


# ─── Interactive loop ────────────────────────────────────────────────────────


def interactive(category_override: str = None, csv_mode: bool = False,
                debug: bool = False, file: str = None):
    """Main interactive capture loop.

    If ``file`` is given, process that single image instead of capturing
    interactively (useful for OCR tuning).
    """
    all_rows: dict[int, dict] = {}  # rank → row (dedup by rank)
    category = category_override
    page = 0

    next_rank = 1  # rank of the first row on the next page

    # Single-file mode for testing/tuning
    if file:
        img = Image.open(file)
        if not category:
            category = detect_category(img, debug=debug)
            if category:
                print(f"  Detected category: {category}")
            else:
                print("  Could not detect category from header.")
                return
        rows = extract_rows(img, page=1, start_rank=1, debug=debug)
        for r in rows:
            all_rows[r["rank"]] = r
        page = 1
        next_rank = max(r["rank"] for r in rows) + 1 if rows else 1
        print(f"  File: {file}")
        print(f"  Rows extracted: {len(rows)}")
    else:
        print("\n  Rankings Scraper — capture leaderboard pages")
        print("  Press Enter to capture a page, 'q' to finish.\n")

    if not file:
        while True:
            prompt = f"  [page {page + 1}, rank {next_rank}+] Enter=capture, q=finish, r=set rank: "
            cmd = input(prompt).strip().lower()
            if cmd == "q":
                break
            if cmd == "r":
                try:
                    next_rank = int(input("    Starting rank for this page: ").strip())
                except (ValueError, EOFError, KeyboardInterrupt):
                    print("    Invalid, keeping current.")
                continue

            # Capture game window
            try:
                img, _geom = capture_window()
            except Exception as e:
                print(f"  Error capturing window: {e}")
                continue

            if img is None:
                print("  Could not capture the game window.")
                continue

            if debug:
                img.save(f"debug_rankings_page{page + 1}.png")
                print(f"  [debug] saved capture: debug_rankings_page{page + 1}.png ({img.size[0]}×{img.size[1]})")

            # Detect category on first page (or if not overridden)
            if not category:
                category = detect_category(img, debug=debug)
                if category:
                    print(f"  Detected category: {category}")
                else:
                    print("  Could not detect category from header.")
                    cat_input = input("  Enter category manually (e.g., battle_power, kills): ").strip()
                    if cat_input:
                        category = cat_input
                    else:
                        print("  Skipping page.")
                        continue

            # Extract rows
            rows = extract_rows(img, page=page + 1, start_rank=next_rank,
                                debug=debug)
            new_count = 0
            for r in rows:
                if r["rank"] not in all_rows:
                    all_rows[r["rank"]] = r
                    new_count += 1

            if rows:
                next_rank = max(r["rank"] for r in rows) + 1

            page += 1
            print(f"  Page {page}: {len(rows)} rows read, {new_count} new "
                  f"(total: {len(all_rows)} unique)")

    if not all_rows:
        print("\n  No data captured.")
        return

    # Sort by rank
    sorted_rows = sorted(all_rows.values(), key=lambda r: r["rank"])

    # Preview
    print(f"\n  Category: {category}")
    print(f"  Total entries: {len(sorted_rows)}")
    print(f"  Rank range: {sorted_rows[0]['rank']}–{sorted_rows[-1]['rank']}")
    print()
    for r in sorted_rows[:5]:
        val_str = f"{r['value']:,}" if r['value'] else "?"
        print(f"    #{r['rank']:>3d}  {r['name']:<30s}  {val_str}")
    if len(sorted_rows) > 5:
        print(f"    ... ({len(sorted_rows) - 5} more)")

    # Write
    if csv_mode:
        count = write_to_csv(category, sorted_rows)
        print(f"\n  Wrote {count} entries to {CSV_PATH}")
    else:
        count = write_to_db(category, sorted_rows)
        print(f"\n  Wrote {count} entries to database")


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Capture AoO nation rankings leaderboard via OCR"
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="Write to CSV instead of DB",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Save OCR debug crops as PNG files",
    )
    parser.add_argument(
        "--category",
        help="Override category auto-detection (e.g., battle_power, kills, city_level)",
    )
    parser.add_argument(
        "--file",
        help="Process a screenshot file instead of capturing interactively (for OCR tuning)",
    )
    args = parser.parse_args()

    if not args.csv:
        db.init_db()

    interactive(
        category_override=args.category,
        csv_mode=args.csv,
        debug=args.debug,
        file=args.file,
    )


if __name__ == "__main__":
    main()
