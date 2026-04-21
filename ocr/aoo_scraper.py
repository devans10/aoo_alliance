"""
aoo_scraper.py — Phases 1–3: Capture, Scroll, OCR, Fuzzy Match, CSV Output
Extracts event type, date, battlers count, and ALL member name/points from
the Age of Origins game window (auto-scrolling through the full list), then
fuzzy-matches names against the alliance roster and writes to CSV.

Usage:
    ./venv/bin/python aoo_scraper.py                          # live window, full scroll
    ./venv/bin/python aoo_scraper.py --no-scroll              # live window, first page only
    ./venv/bin/python aoo_scraper.py --dry-run                # skip CSV write
    ./venv/bin/python aoo_scraper.py --static                 # test: screenshot_elite_wars.png
    ./venv/bin/python aoo_scraper.py --static p1.png p2.png   # test: multi-page screenshots
"""

import argparse
import csv
import math
import os
import random
import re
import shutil
import sys
import unicodedata
import subprocess
import tempfile
import time
from pathlib import Path

import mss
import mss.tools
from PIL import Image, ImageEnhance
import pytesseract
from rapidfuzz import process as rf_process, fuzz

# ─── Constants ───────────────────────────────────────────────────────────────

WINDOW_TITLE   = "Age of Origins"
DEFAULT_STATIC = "screenshot_elite_wars.png"
CSV_PATH       = "aoo_event_log.csv"
ROSTER_PATH    = "roster.txt"

EVENT_TYPE_MAP = {
    "Elite Wars":            "Elite War",
    "Void War":              "Void War",
    "Battle Frenzy":         "Battle Frenzy",
    "Polar Invasion":        "Polar Invasion",
    "Wasteland Showdown":    "Wasteland Showdown",
    "Ironblood Battlefield": "Ironblood Battlefield",
    "Chaosland":             "Chaosland",
    "Global Conquest":       "Global Conquest",
    "Triangle War":          "Triangle War",
    "Duel of Dominance":     "Duel of Dominance",
}

# ─── Proportional region layout ──────────────────────────────────────────────
#
# All coordinates are expressed as fractions of (image_width, image_height)
# so they scale with different window sizes.
#
# Tuned against the 690×1047 reference screenshot.
#
#   (x_ratio, y_ratio, w_ratio, h_ratio)  — top-left origin, then extent
#
REGION = {
    "event_type": (0.350, 0.062, 0.300, 0.040),   # blue header band
    "date":       (0.350, 0.122, 0.330, 0.033),   # date/time bar
    "battlers":   (0.230, 0.190, 0.100, 0.030),   # leftmost stat value
}

# Member list geometry
MEMBER_LIST_Y_RATIO   = 0.256   # y where first row begins
ROW_HEIGHT_RATIO      = 0.085   # height of one member row  (≈89 px at 1047h)
ROWS_PER_PAGE         = 8

# Sub-column x ranges (ratios of image width)
NAME_X1_RATIO    = 0.265   # skip avatar (~first 27 % of width)
NAME_X2_RATIO    = 0.570
POINTS_X1_RATIO  = 0.640
POINTS_X2_RATIO  = 0.785

# Sub-row y ranges (fractions of one row height)
# Name uses a two-pass approach in extract_row(); see comments there.
POINTS_YR1 = 0.22   # points number is vertically centred in the row
POINTS_YR2 = 0.72

# ─── Phase 3: Scroll settings ────────────────────────────────────────────────
#
# SCROLL_CLICKS: pyautogui scroll "clicks" per step.  Negative = scroll down.
# Each click ≈ 1 list row.  We advance ~5 rows per step (≈ ROWS_PER_PAGE - 3)
# to guarantee a 3-row overlap between consecutive pages for dedup safety.
#
SCROLL_CLICKS        = -25   # 5 clicks ≈ 1 row → 25 clicks ≈ 5 rows (keeps 3-row overlap)
SCROLL_PAUSE         = 3.0   # seconds to wait after each scroll (animation)
MAX_SCROLL_STEPS     = 60    # hard safety limit (prevents infinite loops)
EMPTY_PAGES_TO_STOP  = 5     # consecutive all-duplicate pages → bottom reached

# Drag-scroll parameters (primary scroll method — more reliable than scroll wheel
# because AoO is a mobile game designed for touch/drag, and drag events use button-1
# which routes to the window under the cursor regardless of X11 focus state).
DRAG_START_Y_RATIO   = 0.70  # start drag from lower portion of member list
DRAG_END_Y_RATIO     = 0.30  # drag upward to upper portion (scrolls content down)
DRAG_X_RATIO         = 0.50  # horizontal centre of the list
DRAG_STEPS           = 15    # intermediate mousemove points for smooth drag
DRAG_STEP_DELAY      = 0.03  # seconds between each drag increment

# ─── Profile-click name retrieval ────────────────────────────────────────────
#
# Instead of OCR-ing the name from the screenshot, clicking the player's avatar
# opens the Commander Info (profile) page, which has a "Copy" button that puts
# the exact UTF-8 name on the clipboard.  The back arrow returns to the same
# scroll position in the list.
#
AVATAR_X_RATIO       = 0.165   # horizontal centre of the avatar column in the member list
PROFILE_BACK_X_RATIO = 0.089   # Back (←) arrow x  (image x≈62 → ratio=0.089)
PROFILE_BACK_Y_RATIO = 0.078   # Back (←) arrow y  (image y≈82 → ratio=0.078)
# Name region on the Commander Info profile page (dark text on light bg)
# Crop covers the player-name line, right of the avatar, above city/level info.
PROFILE_NAME_X1      = 0.32    # left edge of name crop (right of avatar)
PROFILE_NAME_WIDTH   = 0.40    # width of name crop
PROFILE_NAME_Y1      = 0.105   # top of player header row bright area
PROFILE_NAME_HEIGHT  = 0.040   # height of name line
PROFILE_LOAD_PAUSE   = 2.5     # seconds to wait for Commander Info page to appear
PROFILE_BACK_PAUSE   = 2.0     # seconds to wait for member list to restore after Back


# ─── Image preprocessing ─────────────────────────────────────────────────────

def _preprocess(crop: Image.Image, contrast: float = 3.0) -> Image.Image:
    """Grayscale + contrast boost.  AoO uses light text on dark bg."""
    gray = crop.convert("L")
    return ImageEnhance.Contrast(gray).enhance(contrast)


def _ocr(crop: Image.Image, psm: int = 7, whitelist: str = "") -> str:
    """OCR a single sub-image region."""
    pre  = _preprocess(crop)
    cfg  = f"--psm {psm}"
    if whitelist:
        cfg += f" -c tessedit_char_whitelist={whitelist}"
    return pytesseract.image_to_string(pre, config=cfg).strip()


# ─── Coordinate helpers ──────────────────────────────────────────────────────

def _px(img: Image.Image, rx: float, ry: float, rw: float, rh: float) -> Image.Image:
    """Crop using ratio-based coordinates."""
    w, h = img.size
    x1, y1 = int(rx * w), int(ry * h)
    x2, y2 = int((rx + rw) * w), int((ry + rh) * h)
    return img.crop((x1, y1, x2, y2))


def _row_crop(img: Image.Image, row_idx: int, x1r: float, x2r: float,
              yr1: float, yr2: float) -> Image.Image:
    """Return a sub-crop within a single member-list row."""
    w, h = img.size
    row_top = (MEMBER_LIST_Y_RATIO + row_idx * ROW_HEIGHT_RATIO) * h
    row_h   = ROW_HEIGHT_RATIO * h
    y1 = int(row_top + yr1 * row_h)
    y2 = int(row_top + yr2 * row_h)
    x1 = int(x1r * w)
    x2 = int(x2r * w)
    return img.crop((x1, y1, x2, y2))


def _row_crop_at(img: Image.Image, row_top_y: int, x1r: float, x2r: float,
                 yr1: float, yr2: float) -> Image.Image:
    """Crop a sub-region from a row given its detected absolute pixel top."""
    w, h = img.size
    row_h = int(ROW_HEIGHT_RATIO * h)
    y1 = max(0, row_top_y + int(yr1 * row_h))
    y2 = min(h, row_top_y + int(yr2 * row_h))
    x1 = int(x1r * w)
    x2 = int(x2r * w)
    return img.crop((x1, y1, x2, y2))


# ─── Row boundary auto-detection ─────────────────────────────────────────────

def _fixed_row_tops(h: int, n: int) -> list[int]:
    """Fallback: evenly-spaced row tops based on layout constants."""
    return [int((MEMBER_LIST_Y_RATIO + i * ROW_HEIGHT_RATIO) * h) for i in range(n)]


def _detect_list_row_tops(img: Image.Image, max_rows: int = ROWS_PER_PAGE + 2) -> list[int]:
    """
    Auto-detect y-pixel tops of member-list rows via periodic-grid peak matching.

    Scans the score column (POINTS_X1/X2_RATIO), finds brightness local maxima,
    then finds which vertical offset (0..row_height-1) places the most maxima near
    expected row positions spaced exactly ROW_HEIGHT_RATIO apart.

    Using peak *positions* (not absolute brightness) to select the offset makes the
    detection robust to both large scores (many bright pixels, multiple sub-peaks)
    and small scores (short numbers, weaker signal) because it only asks "is there
    ANY brightness peak near the expected position?" for each row.

    Falls back to fixed positions when fewer than 3 rows can be matched.
    """
    w, h     = img.size
    row_h    = int(ROW_HEIGHT_RATIO * h)
    list_top = int(MEMBER_LIST_Y_RATIO * h)

    x1      = int(POINTS_X1_RATIO * w)
    x2      = int(POINTS_X2_RATIO * w)
    strip_w = max(1, x2 - x1)

    scan_h   = row_h * (max_rows + 1)
    scan_bot = min(h, list_top + scan_h)
    actual_h = scan_bot - list_top

    strip = img.convert("L").crop((x1, list_top, x2, scan_bot))
    data  = strip.tobytes()

    if not data or actual_h <= 0:
        return _fixed_row_tops(h, max_rows)

    # Per-scanline average brightness across the score column
    profile = [
        sum(data[y * strip_w : (y + 1) * strip_w]) / strip_w
        for y in range(actual_h)
    ]

    # Light smoothing to merge intra-digit noise while preserving row-level peaks
    radius = 3
    n = len(profile)
    smoothed = profile[:]
    for i in range(radius, n - radius):
        smoothed[i] = sum(profile[i - radius : i + radius + 1]) / (2 * radius + 1)

    # Collect ALL local maxima (positions only; we match them against the grid)
    raw_peaks = [
        i for i in range(1, n - 1)
        if smoothed[i] >= smoothed[i - 1] and smoothed[i] > smoothed[i + 1]
    ]

    if not raw_peaks:
        print("  [row-detect] no brightness peaks — using fixed positions")
        return _fixed_row_tops(h, max_rows)

    # Expected score-number centre within a row
    pts_centre  = int((POINTS_YR1 + POINTS_YR2) / 2 * row_h)
    tolerance   = int(row_h * 0.22)   # ±22% of row height ≈ ±20px

    # Periodic grid match: for each offset, count how many expected row centres
    # have at least one raw peak within ±tolerance.
    # Weight each match by the brightness of the closest peak (prefer bright rows).
    best_offset = 0
    best_score  = -1.0
    peak_set    = set(raw_peaks)

    for offset in range(row_h):
        score = 0.0
        for i in range(max_rows + 1):
            centre = offset + i * row_h + pts_centre
            if centre >= actual_h:
                break
            # Closest peak within tolerance?
            best_bright = 0.0
            for dp in range(-tolerance, tolerance + 1):
                p = centre + dp
                if 0 <= p < n and p in peak_set:
                    best_bright = max(best_bright, smoothed[p])
            score += best_bright
        if score > best_score:
            best_score  = score
            best_offset = offset

    # Require at least 3 matched rows
    def _row_has_peak(i: int) -> bool:
        centre = best_offset + i * row_h + pts_centre
        if centre >= actual_h:
            return False
        return any(0 <= centre + dp < n and (centre + dp) in peak_set
                   for dp in range(-tolerance, tolerance + 1))

    matched = sum(1 for i in range(max_rows + 1) if _row_has_peak(i))
    if matched < 3:
        print(f"  [row-detect] only {matched} matched rows — using fixed positions")
        return _fixed_row_tops(h, max_rows)

    # Use float arithmetic to compute each row top, avoiding accumulated
    # integer rounding error from repeated `+ row_h` additions (which would
    # shift later rows 3-7 px high, degrading OCR for rows 4-8).
    row_tops = [
        int((MEMBER_LIST_Y_RATIO + i * ROW_HEIGHT_RATIO) * h) + best_offset
        for i in range(max_rows)
        if int((MEMBER_LIST_Y_RATIO + i * ROW_HEIGHT_RATIO) * h) + best_offset + row_h <= h
    ]

    if len(row_tops) < 3:
        return _fixed_row_tops(h, max_rows)

    print(f"  [row-detect] offset={best_offset}px  "
          f"offsets from list top: {[t - list_top for t in row_tops[:4]]} ...")
    return row_tops[:max_rows]


# ─── Text cleaning helpers ───────────────────────────────────────────────────

_GARBAGE_RE = re.compile(r"[^\w\s@°•◇\-_\.!/|]", re.UNICODE)
_DIGIT_RE   = re.compile(r"\d")
_POINT_RE   = re.compile(r"[\d,]+")


def _clean_name(raw: str) -> str:
    """Strip OCR noise from a player name."""
    # Remove lines that are entirely numeric (power values bleed through)
    lines = [ln for ln in raw.splitlines() if ln.strip() and not re.fullmatch(r"[\d,.\s]+", ln.strip())]
    name  = " ".join(lines).strip()
    # Remove leading junk characters (rank badges, stray quotes/symbols)
    name  = re.sub(r"^[^\w@°•◇_\-]+", "", name, flags=re.UNICODE)
    # Collapse whitespace
    name  = re.sub(r"\s+", " ", name).strip()
    # Strip trailing punctuation & quotes (OCR artefacts)
    name  = name.rstrip(".,;:!'\"'`")
    # Strip leading quotes too
    name  = name.lstrip("'\"'`")
    return name


def _clean_points(raw: str) -> str:
    """Extract the first plausible point value from OCR output."""
    # Normalise common OCR substitutions
    normalised = raw.replace("O", "0").replace("l", "1").replace("I", "1")
    # Find longest digit run (with optional commas)
    matches = _POINT_RE.findall(normalised)
    if not matches:
        return ""
    # Pick the longest match (the actual score, not stray single digits)
    best = max(matches, key=lambda m: len(m.replace(",", "")))
    return best.replace(",", "")   # strip commas → plain integer string


# ─── Phase 2: Roster & fuzzy matching ───────────────────────────────────────

FUZZY_THRESHOLD = 80   # minimum score (0-100) to accept a roster match
ALIAS_PATH      = "name_aliases.txt"  # optional OCR→roster overrides

CSV_HEADERS = ["Member Name", "Event Type", "Event Date",
               "Battle Score", "Attendance", "Notes"]


def load_roster(path: str = ROSTER_PATH) -> list[str]:
    """Load alliance member names.

    Tries the database first (all current member names + historical names).
    Falls back to roster.txt if the DB has no members.
    """
    # Try DB first (imported later in file, so use lazy access)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from shared import db as _roster_db
        _roster_db.init_db()
        conn = _roster_db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT name FROM member_names ORDER BY name"
        ).fetchall()
        conn.close()
        if rows:
            names = [r["name"] for r in rows]
            print(f"[roster] Loaded {len(names)} names from database")
            return names
    except Exception:
        pass

    # Fall back to roster.txt
    p = Path(path)
    if not p.exists():
        print(f"[warn] No roster source available — fuzzy matching disabled.")
        return []
    names = []
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 1)
        name  = parts[-1].strip()
        if name:
            names.append(name)
    print(f"[roster] Loaded {len(names)} members from {path}")
    return names


def load_name_aliases(path: str = ALIAS_PATH) -> dict[str, str]:
    """Load manual OCR→roster name overrides from name_aliases.txt.

    File format: one alias per line, ``ocr_name==roster_name``.
    Lines starting with # are comments.  Lookup is case-insensitive on
    the OCR side.

    Example::

        MoM)==마쭈 (blackcrow)
        bwh==Dwh
    """
    p = Path(path)
    if not p.exists():
        return {}
    aliases: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        ocr_side, roster_side = line.split("==", 1)
        ocr_side = ocr_side.strip()
        roster_side = roster_side.strip()
        if ocr_side and roster_side:
            aliases[ocr_side.lower()] = roster_side
    if aliases:
        print(f"[aliases] Loaded {len(aliases)} name aliases from {path}")
    return aliases


def _strip_decorative(s: str) -> str:
    """Strip decorative Unicode, keeping only ASCII letters, digits, and @_-.

    Game names often have ornamental characters that OCR will never produce:
      '༒۝warlord۝༒' → 'warlord'
      'SUN써니'        → 'SUN'
      'Prison◇Mike'   → 'PrisonMike'
      '°•°SHADOW°•°'  → 'SHADOW'
      'ccchhiii千香'   → 'ccchhiii'
    """
    return re.sub(r'[^A-Za-z0-9@_\-]', '', s)


def _strip_accents(s: str) -> str:
    """Decompose accented characters and drop the combining marks.
    'RAÏZØ' → 'RAIZO', 'MINH@ĐỨC' → 'MINH@DUC', etc.
    Used as a fuzzy-match fallback so OCR (which drops diacritics) can
    still match roster names that contain them.
    """
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


# OCR confusion map: characters that Tesseract commonly swaps in game fonts.
# Each entry maps a character to a canonical representative so that both
# the OCR name and the roster name collapse to the same string.
_OCR_CONFUSE_TABLE = str.maketrans({
    "b": "d", "B": "D",         # b↔D (rounded right half)
    "l": "i", "L": "I",         # l↔i↔1 (narrow vertical strokes)
    "1": "i",
    "s": "g", "S": "G",         # s↔g (game font ambiguity)
    "0": "o", "O": "o",         # 0↔O↔o
})

def _ocr_confuse_norm(s: str) -> str:
    """Collapse OCR-confusable characters to a shared canonical form."""
    return s.translate(_OCR_CONFUSE_TABLE)


_name_aliases: dict[str, str] = {}   # populated by main() at startup


def fuzzy_match(ocr_name: str, roster: list[str]) -> tuple[str | None, int]:
    """
    Fuzzy-match an OCR'd name against the roster.
    Returns (matched_roster_name, score) or (None, 0) if below threshold.

    Strategy (tried in order until a match is found):
      0. Exact alias lookup (name_aliases.txt overrides)
      1. Raw OCR name vs raw roster
      2. Underscore / "L " normalisation (handles OCR artefact)
      3. Decorative Unicode stripped (༒۝warlord۝༒ → warlord, SUN써니 → SUN)
      4. Accent-stripped OCR vs accent-stripped roster (handles Ï→I, Ø→O, etc.)
      5. OCR confusion normalisation (b↔D, l↔i, s↔g, 0↔O)
      6. Combined decorative-strip + accent-strip + OCR confusion
    """
    if not roster or not ocr_name:
        return None, 0

    # Pass 0: manual alias override (handles Korean chars, total OCR failures)
    alias = _name_aliases.get(ocr_name.lower())
    if alias:
        return alias, 100

    def _try(query: str, candidates: list[str] | None = None) -> tuple[str | None, int]:
        r = rf_process.extractOne(
            query, candidates if candidates is not None else roster,
            scorer=fuzz.ratio,
            score_cutoff=FUZZY_THRESHOLD,
        )
        if r is None:
            return None, 0
        # r[0] is the matched candidate — map back to original roster name
        matched_candidate = r[0]
        if candidates is not None:
            # candidates[i] corresponds to roster[i]; find original
            try:
                idx = candidates.index(matched_candidate)
                return roster[idx], int(r[1])
            except ValueError:
                return matched_candidate, int(r[1])
        return matched_candidate, int(r[1])

    matched, score = _try(ocr_name)

    # Pass 2: collapse "L " or spaces → underscore (OCR underscore artefact)
    if matched is None:
        normalised = re.sub(r"\bL\s+", "_", ocr_name)
        if normalised == ocr_name:
            normalised = ocr_name.replace(" ", "_")
        if normalised != ocr_name:
            matched, score = _try(normalised)

    # Pass 3: strip decorative Unicode from roster names.
    # Game names have ornamental chars (༒۝warlord۝༒, SUN써니, Prison◇Mike)
    # that OCR will never produce.  Compare OCR text (already ASCII-ish)
    # against the ASCII core of each roster name.
    # Always run this pass — it may find a better match than pass 1
    # (e.g. "SUN" matches "SHUN" at 85 in pass 1 but "SUN써니" → "SUN" at 100).
    ascii_roster = [_strip_decorative(n) for n in roster]
    dec_matched, dec_score = _try(ocr_name, ascii_roster)
    if dec_score > score:
        matched, score = dec_matched, dec_score

    # Pass 4: strip accents from both sides and retry
    if matched is None:
        stripped_query    = _strip_accents(ocr_name)
        stripped_roster   = [_strip_accents(n) for n in roster]
        matched, score = _try(stripped_query, stripped_roster)

    # Pass 5: OCR confusion normalisation — collapse commonly swapped
    # characters (b↔D, l↔i, s↔g, 0↔O) to a canonical form on both sides.
    if matched is None:
        conf_query  = _ocr_confuse_norm(ocr_name)
        conf_roster = [_ocr_confuse_norm(n) for n in roster]
        matched, score = _try(conf_query, conf_roster)

    # Pass 6: combined decorative-strip + accent-strip + OCR confusion
    if matched is None:
        combo_query  = _ocr_confuse_norm(_strip_accents(ocr_name))
        combo_roster = [_ocr_confuse_norm(_strip_accents(_strip_decorative(n))) for n in roster]
        matched, score = _try(combo_query, combo_roster)

    return matched, score


# ─── Phase 2: CSV I/O ────────────────────────────────────────────────────────

def ensure_csv(path: str = CSV_PATH) -> None:
    """Create the CSV with headers if it doesn't exist."""
    p = Path(path)
    if not p.exists():
        with p.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)
        print(f"[csv] Created {path} with headers.")


def load_existing_keys(path: str = CSV_PATH) -> set[tuple[str, str, str]]:
    """
    Read existing CSV and return a set of (member_name, event_type, event_date)
    tuples for duplicate detection.
    """
    p = Path(path)
    if not p.exists():
        return set()
    keys: set[tuple[str, str, str]] = set()
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (
                row.get("Member Name", "").strip(),
                row.get("Event Type", "").strip(),
                row.get("Event Date", "").strip(),
            )
            keys.add(key)
    return keys


def append_to_csv(
    path: str,
    hdr: dict,
    matched_rows: list[dict],
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Append matched rows to the CSV, skipping duplicates.
    Returns (rows_written, rows_skipped).
    """
    existing = load_existing_keys(path)
    written  = 0
    skipped  = 0

    mode = "a" if not dry_run else None
    rows_to_write = []

    for r in matched_rows:
        key = (r["member_name"], hdr["event_type_csv"], hdr["event_date"])
        if key in existing:
            skipped += 1
            continue
        existing.add(key)   # prevent within-run duplicates too
        rows_to_write.append([
            r["member_name"],
            hdr["event_type_csv"],
            hdr["event_date"],
            r["points"],
            "Absent" if r["points"] == "0" else "Present",
            r["notes"],
        ])
        written += 1

    if not dry_run and rows_to_write:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows_to_write)

    return written, skipped


# ─── DB write ───────────────────────────────────────────────────────────────

# Add project root to path so we can import shared.db
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import db as _db


def write_to_db(hdr: dict, matched_rows: list[dict]) -> tuple[int, int]:
    """Write event results to the database via shared/db.py.

    Creates the event, resolves members, and inserts results.
    Returns (rows_written, rows_skipped).
    """
    _db.init_db()
    conn = _db.get_connection()
    written = 0
    skipped = 0
    try:
        # Parse date — scraper uses "YYYY-MM-DD HH:MM" or raw text
        event_date = hdr["event_date"].split()[0] if hdr["event_date"] else ""
        event_id = _db.add_event(hdr["event_type_csv"], event_date, conn=conn)

        for r in matched_rows:
            name = r["member_name"]
            score = int(r["points"]) if r["points"] and r["points"].isdigit() else 0

            # Only record members who actually participated
            if score <= 0:
                skipped += 1
                continue

            member = _db.resolve_member(name, conn)
            if member is None:
                # New member — auto-create
                member_id = _db.add_member(name, conn=conn)
            else:
                member_id = member["id"]

            _db.add_event_result(
                member_id=member_id,
                event_id=event_id,
                score=score,
                source="ocr",
                conn=conn,
            )
            written += 1

        conn.commit()
        _db.finalise_event(event_id)
    finally:
        conn.close()

    return written, skipped


# ─── Window detection (Wayland-safe) ─────────────────────────────────────────

def _detect_window_wmctrl() -> tuple[int, int, int, int] | None:
    """Try wmctrl -lG to locate the game window.  Returns (x, y, w, h) or None."""
    try:
        out = subprocess.check_output(["wmctrl", "-lG"], text=True, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    for line in out.splitlines():
        if WINDOW_TITLE.lower() in line.lower():
            parts = line.split()
            # Format: WID DESK X Y W H HOST TITLE
            if len(parts) >= 7:
                try:
                    return int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
                except ValueError:
                    pass
    return None


def _get_window_id_wmctrl() -> str | None:
    """Return the hex X11 window ID (e.g. '0x04800006') for the game via wmctrl."""
    try:
        out = subprocess.check_output(
            ["wmctrl", "-lG"], text=True, stderr=subprocess.DEVNULL, timeout=5
        )
    except Exception:
        return None
    for line in out.splitlines():
        if WINDOW_TITLE.lower() in line.lower():
            parts = line.split()
            if parts:
                return parts[0]   # hex WID like "0x04800006"
    return None


def _detect_window_xwininfo() -> tuple[int, int, int, int] | None:
    """Try xwininfo to locate the game window.  Returns (x, y, w, h) or None."""
    try:
        out = subprocess.check_output(
            ["xwininfo", "-name", WINDOW_TITLE], text=True, stderr=subprocess.DEVNULL
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    geom = {}
    patterns = {
        "x":  r"Absolute upper-left X:\s+(-?\d+)",
        "y":  r"Absolute upper-left Y:\s+(-?\d+)",
        "w":  r"Width:\s+(\d+)",
        "h":  r"Height:\s+(\d+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, out)
        if m:
            geom[key] = int(m.group(1))
    if len(geom) == 4:
        return geom["x"], geom["y"], geom["w"], geom["h"]
    return None


def _find_xauthority() -> str | None:
    """
    Find the XWayland Xauthority cookie file.

    GNOME Wayland stores it at /run/user/<uid>/.mutter-Xwaylandauth.<random>
    which is never in the XAUTHORITY env var by default.
    """
    xa = os.environ.get("XAUTHORITY", "")
    if xa and os.path.exists(xa):
        return xa
    import glob as _glob
    uid = os.getuid()
    for pattern in [
        f"/run/user/{uid}/.mutter-Xwaylandauth.*",
        f"/run/user/{uid}/gdm/Xauthority",
        os.path.expanduser("~/.Xauthority"),
    ]:
        hits = _glob.glob(pattern)
        if hits:
            return hits[0]
    return None


def _session_env() -> dict:
    """
    Build an environment dict for subprocesses that need Wayland/D-Bus access.
    Adds XAUTHORITY if missing (GNOME Mutter places it at a random path).
    """
    env = os.environ.copy()
    if not env.get("XAUTHORITY"):
        xa = _find_xauthority()
        if xa:
            env["XAUTHORITY"] = xa
    return env


def _grab_mss(x: int, y: int, w: int, h: int) -> Image.Image:
    """Capture screen region via mss (works on X11; fails on GNOME Wayland)."""
    with mss.mss() as sct:
        monitor = {"left": x, "top": y, "width": w, "height": h}
        raw = sct.grab(monitor)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def _find_xlib_window(root, title: str):
    """Recursively search the X11 window tree for a window matching title."""
    try:
        name = root.get_wm_name()
        if name and title.lower() in str(name).lower():
            return root
    except Exception:
        pass
    try:
        for child in root.query_tree().children:
            result = _find_xlib_window(child, title)
            if result:
                return result
    except Exception:
        pass
    return None


def _grab_xlib(w: int, h: int) -> Image.Image:
    """
    Capture the game window directly via python-Xlib.

    Calls GetImage on the window itself (not root), which XWayland permits
    even when GNOME Wayland blocks root-window XGetImage.
    Sets XAUTHORITY from the Mutter XWayland cookie file if not already set.
    """
    xa = _find_xauthority()
    if xa:
        os.environ["XAUTHORITY"] = xa  # needed by python-Xlib's pure-Python auth

    from Xlib import display as xdisplay, X  # lazy import (needs DISPLAY)

    d    = xdisplay.Display()
    root = d.screen().root
    win  = _find_xlib_window(root, WINDOW_TITLE)
    if win is None:
        raise RuntimeError(f"Window '{WINDOW_TITLE}' not found in X11 tree")

    ximg = win.get_image(0, 0, w, h, X.ZPixmap, 0xFFFFFFFF)
    # XImage data arrives as BGRX; convert to RGB
    return Image.frombytes("RGB", (w, h), ximg.data, "raw", "BGRX")


def _screen_size_from_mss() -> tuple[int, int]:
    """Return (width, height) of the primary monitor using mss."""
    try:
        with mss.mss() as sct:
            m = sct.monitors[1]
            return m["width"], m["height"]
    except Exception:
        return 3840, 2160   # safe upper bound fallback


def _grab_gnome_shell(x: int, y: int, w: int, h: int) -> Image.Image:
    """
    Capture a screen region via org.gnome.Shell.Screenshot.ScreenshotArea.

    Clamps the region to the monitor bounds (GNOME rejects out-of-bounds coords)
    and crops the resulting image to the original requested size.
    """
    sw, sh = _screen_size_from_mss()
    # Clamp to screen bounds
    cx = max(0, min(x, sw - 1))
    cy = max(0, min(y, sh - 1))
    cw = min(w, sw - cx)
    ch = min(h, sh - cy)
    if cw <= 0 or ch <= 0:
        raise RuntimeError(f"window region ({x},{y},{w},{h}) entirely outside screen ({sw}×{sh})")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        result = subprocess.run(
            [
                "dbus-send", "--session", "--print-reply",
                "--dest=org.gnome.Shell",
                "/org/gnome/Shell/Screenshot",
                "org.gnome.Shell.Screenshot.ScreenshotArea",
                f"int32:{cx}", f"int32:{cy}", f"int32:{cw}", f"int32:{ch}",
                "boolean:false",
                f"string:{tmp}",
            ],
            capture_output=True, text=True, timeout=10,
            env=_session_env(),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        p = Path(tmp)
        if not p.exists() or p.stat().st_size == 0:
            raise RuntimeError("screenshot file not created or empty")
        img = Image.open(tmp).convert("RGB")
        # Pad to original size if we had to clamp (window partially off-screen)
        if img.size != (w, h):
            full = Image.new("RGB", (w, h), (0, 0, 0))
            full.paste(img, (0, 0))
            img = full
        return img
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── XDG portal helper script (runs under system python3 which has gi) ────────
_PORTAL_SCRIPT = r"""
import sys, os, gi, shutil, urllib.parse
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib

output_path = sys.argv[1]
loop = GLib.MainLoop()
result = {'uri': None}

def on_response(connection, sender, obj_path, iface, sig, params, user_data):
    code, results = params.unpack()
    if code == 0 and 'uri' in results:
        result['uri'] = results['uri']
    loop.quit()

bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
token = f'aoo_{os.getpid()}'
sender = bus.get_unique_name()[1:].replace('.', '_')
req_path = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'

sub = bus.signal_subscribe(
    None, 'org.freedesktop.portal.Request', 'Response',
    req_path, None, Gio.DBusSignalFlags.NONE, on_response, None,
)
try:
    bus.call_sync(
        'org.freedesktop.portal.Desktop',
        '/org/freedesktop/portal/desktop',
        'org.freedesktop.portal.Screenshot',
        'Screenshot',
        GLib.Variant('(sa{sv})', ('', {
            'handle_token': GLib.Variant('s', token),
            'interactive':  GLib.Variant('b', False),
        })),
        GLib.VariantType('(o)'),
        Gio.DBusCallFlags.NONE, 10000, None,
    )
except Exception as e:
    print(f'portal call failed: {e}', file=sys.stderr)
    sys.exit(2)

GLib.timeout_add_seconds(20, loop.quit)
loop.run()
bus.signal_unsubscribe(sub)

if not result['uri']:
    print('no URI from portal', file=sys.stderr)
    sys.exit(1)

src = urllib.parse.urlparse(result['uri']).path
shutil.copy2(src, output_path)
"""


def _grab_xdg_portal(x: int, y: int, w: int, h: int) -> Image.Image:
    """
    Full-screen screenshot via XDG Desktop Portal, then crop to window.
    Uses system python3 (has gi.repository on GNOME).
    On first use GNOME may show a brief screenshot notification.
    """
    syspy = shutil.which("python3") or "/usr/bin/python3"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        r = subprocess.run(
            [syspy, "-c", _PORTAL_SCRIPT, tmp],
            capture_output=True, text=True, timeout=25,
            env=_session_env(),
        )
        if r.returncode != 0:
            # Show up to 5 lines of the real error so it's visible in [capture] log
            err_lines = (r.stderr.strip() or f"exit {r.returncode}").splitlines()
            raise RuntimeError("\n".join(err_lines[:5]))
        full = Image.open(tmp).convert("RGB")
        return full.crop((x, y, x + w, y + h))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _grab_imagemagick(win_title: str, w: int, h: int) -> Image.Image:
    """
    Capture a named X11 window using ImageMagick's import utility.
    Uses libX11 (same auth path as mss) but calls XGetImage on the window
    directly rather than on root, which may succeed where root-capture fails.
    """
    import_bin = shutil.which("import") or "/usr/bin/import"
    if not Path(import_bin).exists():
        raise RuntimeError("ImageMagick 'import' not found")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        r = subprocess.run(
            [import_bin, "-window", win_title, tmp],
            capture_output=True, text=True, timeout=15,
            env=_session_env(),
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or f"exit {r.returncode}")
        img = Image.open(tmp).convert("RGB")
        # Sanity-check: reject a fully-black image (common sign of failed capture)
        bbox = img.getbbox()
        if bbox is None:
            raise RuntimeError("captured image is entirely black")
        return img
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _grab_grim(x: int, y: int, w: int, h: int) -> Image.Image:
    """
    Capture a screen region via grim (works on wlroots compositors; NOT GNOME).
    """
    grim = shutil.which("grim")
    if not grim:
        raise RuntimeError("grim not found in PATH")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name
    try:
        subprocess.check_call(
            [grim, "-g", f"{x},{y} {w}x{h}", tmp],
            stderr=subprocess.DEVNULL,
        )
        return Image.open(tmp).convert("RGB")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _grab_window(geom: tuple[int, int, int, int],
                 debug_path: str | None = None) -> Image.Image:
    """
    Capture the game window using the best available method.

    Tries in order:
      1. mss              — fast; works on plain X11
      2. python-Xlib      — direct window GetImage; bypasses GNOME root restriction
      3. GNOME Shell DBus — org.gnome.Shell.Screenshot.ScreenshotArea
      4. XDG portal       — org.freedesktop.portal.Screenshot via system python3+gi
      5. grim             — wlroots Wayland-native (not GNOME)
    """
    x, y, w, h = geom
    last_err   = "unknown"

    for label, fn, args in [
        ("mss",          _grab_mss,          (x, y, w, h)),
        ("xlib",         _grab_xlib,         (w, h)),
        ("imagemagick",  _grab_imagemagick,  (WINDOW_TITLE, w, h)),
        ("gnome-shell",  _grab_gnome_shell,  (x, y, w, h)),
        ("xdg-portal",   _grab_xdg_portal,   (x, y, w, h)),
        ("grim",         _grab_grim,         (x, y, w, h)),
    ]:
        try:
            img = fn(*args)
            print(f"[capture] {label} succeeded")
            if debug_path:
                img.save(debug_path)
                print(f"[capture] debug image saved to {debug_path}")
            return img
        except Exception as e:
            last_err = str(e)
            # Print full error (including portal tracebacks)
            for line in last_err.splitlines():
                print(f"[capture] {label}: {line}")
            last_err = last_err.splitlines()[0]

    raise RuntimeError(
        "All screen capture methods failed.\n"
        "You can still use --static with manually taken screenshots.\n"
        f"Last error: {last_err}"
    )


def capture_window(debug_path: str | None = None) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Detect game window, capture it, and return (image, geom)."""
    geom = _detect_window_wmctrl()
    if geom is None:
        geom = _detect_window_xwininfo()
    if geom is None:
        raise RuntimeError(
            f"Could not locate '{WINDOW_TITLE}' window via wmctrl or xwininfo.\n"
            "Is the game running and visible?  "
            "Try: --static to test against the reference screenshot."
        )
    x, y, w, h = geom
    print(f"[window] Detected '{WINDOW_TITLE}' at ({x},{y})  {w}×{h}")
    return _grab_window(geom, debug_path=debug_path), geom


# ─── Phase 3: Scroll automation ──────────────────────────────────────────────

def _scroll_target(geom: tuple[int, int, int, int]) -> tuple[int, int]:
    """Return absolute screen (x, y) for the scroll target inside the member list."""
    wx, wy, ww, wh = geom
    x = wx + int(ww * 0.50)
    # Aim at the vertical centre of the visible member list
    y = wy + int(wh * (MEMBER_LIST_Y_RATIO + ROWS_PER_PAGE / 2 * ROW_HEIGHT_RATIO))
    return x, y


def _scroll_down_drag(geom: tuple[int, int, int, int]) -> bool:
    """
    Scroll the member list down by simulating a touch-drag gesture.

    AoO is a mobile game — its list is designed for touch/drag scrolling, not
    mouse wheels.  Drag (button-1 mousedown → mousemove → mouseup) is routed
    to the window under the cursor regardless of X11 focus state, which avoids
    the focus-disruption problems that make scroll-wheel events unreliable after
    ImageMagick captures.

    Returns True if the drag was executed, False if xdotool is not available.
    """
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return False

    # Use inner window geometry for accurate coordinates.
    inner = _get_inner_window_geom()
    g = inner if inner else geom
    wx, wy, ww, wh = g

    env = _session_env()

    # Raise the window (windowactivate only — no windowfocus to avoid
    # resetting scroll position).
    win_id = _get_window_id_wmctrl()
    if win_id:
        subprocess.run(
            [xdotool, "windowactivate", "--sync", win_id],
            capture_output=True, env=env, timeout=5,
        )
    time.sleep(0.3)

    # Compute drag start/end in absolute screen coordinates.
    # Add small random jitter (±15px) so repeated drags don't look identical
    # to the game engine, which can cause it to ignore the gesture.
    jitter_x = random.randint(-15, 15)
    jitter_start_y = random.randint(-10, 10)
    # Vary end-Y more aggressively so the game can't "learn" to ignore
    # a repeating gesture (±30px gives the drag noticeably different length).
    jitter_end_y = random.randint(-30, 30)
    drag_x = wx + int(ww * DRAG_X_RATIO) + jitter_x
    drag_start_y = wy + int(wh * DRAG_START_Y_RATIO) + jitter_start_y
    drag_end_y   = wy + int(wh * DRAG_END_Y_RATIO) + jitter_end_y

    print(f"  [scroll] drag from ({drag_x},{drag_start_y}) to ({drag_x},{drag_end_y})")

    # Move to start position.
    subprocess.run(
        [xdotool, "mousemove", "--sync", str(drag_x), str(drag_start_y)],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(0.15)

    # Press and hold.
    subprocess.run(
        [xdotool, "mousedown", "1"],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(0.1)

    # Drag upward in smooth increments (scrolls content downward).
    for i in range(1, DRAG_STEPS + 1):
        intermediate_y = drag_start_y + int((drag_end_y - drag_start_y) * i / DRAG_STEPS)
        subprocess.run(
            [xdotool, "mousemove", "--sync", str(drag_x), str(intermediate_y)],
            capture_output=True, env=env, timeout=5,
        )
        time.sleep(DRAG_STEP_DELAY)

    # Release.
    time.sleep(0.05)
    subprocess.run(
        [xdotool, "mouseup", "1"],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(SCROLL_PAUSE)
    return True


def _tap_to_restore_scroll(geom: tuple[int, int, int, int]) -> None:
    """
    Tap (click) the centre of the list area to re-establish the game's touch
    context.  After many profile clicks + ImageMagick captures, the game can
    stop recognising drag gestures — a single tap on the list wakes it up.
    """
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return

    inner = _get_inner_window_geom()
    g = inner if inner else geom
    wx, wy, ww, wh = g
    env = _session_env()

    # Do a micro-drag (5px) in the list area to re-establish touch context
    # without triggering a click/tap (which would open a profile).
    tap_x = wx + int(ww * 0.50)
    tap_y = wy + int(wh * 0.50)

    win_id = _get_window_id_wmctrl()
    if win_id:
        subprocess.run(
            [xdotool, "windowactivate", "--sync", win_id],
            capture_output=True, env=env, timeout=5,
        )
    time.sleep(0.5)
    subprocess.run(
        [xdotool, "mousemove", "--sync", str(tap_x), str(tap_y)],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(0.1)
    subprocess.run([xdotool, "mousedown", "1"], capture_output=True, env=env, timeout=5)
    time.sleep(0.1)
    # Move 5px — enough for the game to register a drag, not a click.
    subprocess.run(
        [xdotool, "mousemove", "--sync", str(tap_x), str(tap_y - 5)],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(0.1)
    subprocess.run([xdotool, "mouseup", "1"], capture_output=True, env=env, timeout=5)
    time.sleep(0.5)


def _scroll_down_wheel(geom: tuple[int, int, int, int]) -> bool:
    """
    Scroll the member list down using scroll-wheel events (button 5).

    Fallback method — scroll-wheel events depend on X11 focus state and become
    unreliable after ImageMagick captures steal focus.  Returns True on success.
    """
    inner = _get_inner_window_geom()
    sx, sy = _scroll_target(inner if inner else geom)
    clicks = abs(SCROLL_CLICKS)
    btn = 5 if SCROLL_CLICKS < 0 else 4

    xdotool = shutil.which("xdotool")
    if xdotool:
        try:
            env = _session_env()
            win_id = _get_window_id_wmctrl()
            if win_id:
                subprocess.run(
                    [xdotool, "windowactivate", "--sync", win_id],
                    capture_output=True, env=env, timeout=5,
                )
                subprocess.run(
                    [xdotool, "windowfocus", "--sync", win_id],
                    capture_output=True, env=env, timeout=5,
                )
            time.sleep(0.5)
            r = subprocess.run(
                [xdotool, "mousemove", "--sync", str(sx), str(sy)],
                capture_output=True, env=env, timeout=5,
            )
            if r.returncode == 0:
                time.sleep(0.1)
                subprocess.run(
                    [xdotool, "click", "--repeat", str(clicks), "--delay", "50", str(btn)],
                    capture_output=True, env=env, timeout=30,
                )
                time.sleep(SCROLL_PAUSE)
                return True
        except Exception as e:
            print(f"  [scroll] wheel: {e}")
    return False


def _scroll_down(geom: tuple[int, int, int, int]) -> None:
    """
    Scroll the member list down.

    Tries methods in order:
      1. click-drag  — simulates a touch swipe; reliable after focus-stealing captures
      2. scroll-wheel — xdotool button-5 clicks; works when X11 focus is intact
    """
    if _scroll_down_drag(geom):
        return
    if _scroll_down_wheel(geom):
        return
    raise RuntimeError(
        "All scroll methods failed.\n"
        "  Install xdotool (dnf install xdotool) for XWayland scroll.\n"
        "  Use --no-scroll to capture only the first page."
    )


def _dismiss_accidental_profile(
    geom: tuple[int, int, int, int],
    img: Image.Image,
) -> Image.Image | None:
    """Detect if a profile page is showing and dismiss it.

    The click-drag scroll can occasionally register as a tap, opening a
    member's profile page.  We detect this via the same brightness check
    used in the profile-click path: the player-header row has a bright
    background (~160 avg brightness) vs the dark member list (~44).

    Returns a fresh member-list capture if a profile was dismissed,
    or None if no profile was detected (image is fine as-is).
    """
    check_crop = _px(img, 0.0, PROFILE_NAME_Y1,
                     1.0, PROFILE_NAME_HEIGHT)
    gray_bytes = check_crop.convert("L").tobytes()
    avg_bright = sum(gray_bytes) / max(len(gray_bytes), 1)
    if avg_bright < 100:
        return None  # no profile page — normal list view

    print(f"  [scroll] accidental profile page detected (brightness {avg_bright:.0f}) — dismissing …")

    xdotool = shutil.which("xdotool")
    if not xdotool:
        return None
    env = _session_env()
    inner = _get_inner_window_geom(env=env)
    wx, wy, ww, wh = inner if inner else geom

    win_id = _get_window_id_wmctrl()
    if win_id:
        subprocess.run(
            [xdotool, "windowactivate", "--sync", win_id],
            capture_output=True, env=env, timeout=5,
        )
    time.sleep(0.2)

    back_sx = wx + int(PROFILE_BACK_X_RATIO * ww)
    back_sy = wy + int(PROFILE_BACK_Y_RATIO * wh)
    subprocess.run(
        [xdotool, "mousemove", "--sync", str(back_sx), str(back_sy)],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(0.15)
    subprocess.run(
        [xdotool, "click", "1"],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(PROFILE_BACK_PAUSE)

    # Re-capture the member list.
    return _grab_window(geom)


def _add_unique(
    page_rows: list[dict],
    seen_points: set[str],
) -> tuple[list[dict], int]:
    """
    Return rows whose points value has not been seen before.
    Points are used as the dedup key: they're numeric, reliably OCR'd,
    and effectively unique per player per event.
    Mutates seen_points.
    Returns (new_rows, duplicate_count).
    """
    new   = []
    dupes = 0
    for r in page_rows:
        pts = r["points"]
        if not pts or not pts.isdigit() or pts == "0":
            # Malformed or zero-point rows: use name+raw as dedup key.
            # "0" cannot be used alone because many players can share a zero
            # battle score, causing every one after the first to look like a
            # duplicate and triggering a false "bottom reached" early stop.
            key = f"\x00{r['name']}|{r['points_raw']}"
        else:
            key = pts
        if key in seen_points:
            dupes += 1
        else:
            seen_points.add(key)
            new.append(r)
    return new, dupes


def _get_window_geom_xdotool(win_id: str, env=None) -> tuple[int, int, int, int] | None:
    """
    Return (x, y, w, h) from xdotool getwindowgeometry.

    wmctrl and xdotool can disagree on window position/size on XWayland /
    GNOME Wayland.  xdotool's values are what xdotool uses for mousemove,
    so we use them for all click coordinate calculations.

    Output of `xdotool getwindowgeometry <id>` looks like:
        Window 8390862
          Position: 700,8 (screen: 0)
          Geometry: 690x1047
    """
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return None
    if env is None:
        env = _session_env()
    try:
        r = subprocess.run(
            [xdotool, "getwindowgeometry", win_id],
            capture_output=True, text=True, env=env, timeout=5,
        )
        pos = geom = None
        for line in r.stdout.splitlines():
            m = re.search(r"Position:\s*(-?\d+),(-?\d+)", line)
            if m:
                pos = int(m.group(1)), int(m.group(2))
            m = re.search(r"Geometry:\s*(\d+)x(\d+)", line)
            if m:
                geom = int(m.group(1)), int(m.group(2))
        if pos and geom:
            return pos[0], pos[1], geom[0], geom[1]
        if pos:
            return pos[0], pos[1], None, None
    except Exception:
        pass
    return None


def _get_window_pos_xdotool(win_id: str, env=None) -> tuple[int, int] | None:
    """Return (x, y) position only — thin wrapper around _get_window_geom_xdotool."""
    result = _get_window_geom_xdotool(win_id, env=env)
    if result:
        return result[0], result[1]
    return None


def _get_inner_window_geom(title: str = WINDOW_TITLE, env=None) -> tuple[int, int, int, int] | None:
    """
    Return (x, y, w, h) for the *inner* game window by searching xdotool by
    window title.

    On XWayland/GNOME the wmctrl window ID is the outer WM frame (640×960 at
    (725,70)).  The actual rendered game window is a child window with the same
    title that xdotool search --name finds (690×1047 at (700,8)).  Clicks must
    use the inner window's coordinate origin and size so that ratio-based button
    positions land on the correct pixels.
    """
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return None
    if env is None:
        env = _session_env()
    try:
        # Get window IDs matching the title.
        search = subprocess.run(
            [xdotool, "search", "--onlyvisible", "--name", title],
            capture_output=True, text=True, env=env, timeout=5,
        )
        win_ids = search.stdout.strip().splitlines()
        # Try each ID; return the first one with a valid geometry.
        for wid in win_ids:
            result = _get_window_geom_xdotool(wid.strip(), env=env)
            if result and result[2]:
                return result
    except Exception:
        pass
    return None


def _click_at_screen(sx: int, sy: int, env=None) -> None:
    """Left-click at absolute screen coordinates via xdotool."""
    xdotool = shutil.which("xdotool")
    if not xdotool:
        raise RuntimeError("xdotool not found — cannot perform profile click")
    if env is None:
        env = _session_env()
    subprocess.run(
        [xdotool, "mousemove", "--sync", str(sx), str(sy)],
        capture_output=True, env=env, timeout=5,
    )
    time.sleep(0.15)
    subprocess.run(
        [xdotool, "click", "1"],
        capture_output=True, env=env, timeout=5,
    )


def _click_ratio(geom: tuple[int, int, int, int], rx: float, ry: float, env=None) -> None:
    """Left-click at a position given as window-relative ratios."""
    wx, wy, ww, wh = geom
    _click_at_screen(wx + int(rx * ww), wy + int(ry * wh), env=env)


def _read_clipboard(env=None) -> str:
    """Return clipboard text via xclip or xsel (whichever is installed)."""
    if env is None:
        env = _session_env()
    for cmd in (
        ["xclip", "-o", "-selection", "clipboard"],
        ["xsel",  "--clipboard", "--output"],
    ):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=5, env=env,
                )
                if r.returncode == 0:
                    return r.stdout.strip()
            except Exception:
                pass
    return ""


def _get_name_via_profile(
    geom: tuple[int, int, int, int],
    row_top_y: int,
    img_h: int | None = None,
    win_id: str | None = None,
    debug_profile_path: str | None = None,
) -> str:
    """
    Open the Commander Info page for the player in a given row, capture the
    page image, OCR the player name from it, then press Back to return to the
    member list at the same scroll position.

    row_top_y is in image-pixel space (img_h tall).  It is scaled to
    screen coordinate space (wh tall) before computing click coordinates.
    xdotool's reported geometry is used for ww/wh/wx/wy so that all ratios
    and mousemove targets are in the same coordinate space.

    Returns the name string, or "" on failure.
    """
    env = _session_env()
    _wmctrl_wx, _wmctrl_wy, ww, wh = geom

    xdotool = shutil.which("xdotool")

    # The wmctrl window ID is the outer WM frame (640×960 at ~(725,70)).
    # The actual rendered game window is an inner child window that xdotool
    # finds by title (690×1047 at ~(700,8)).  All ratio-based click positions
    # must use the inner window's origin and size.
    inner = _get_inner_window_geom(env=env)
    if inner:
        wx, wy, ww, wh = inner
        print(f"      [profile] inner window geom: ({wx},{wy}) {ww}×{wh}")
    else:
        # Fallback: try xdotool geometry on wmctrl win_id, then wmctrl values.
        xdt_geom = _get_window_geom_xdotool(win_id, env=env) if win_id else None
        if xdt_geom and xdt_geom[2]:
            wx, wy, ww, wh = xdt_geom
        elif xdt_geom:
            wx, wy = xdt_geom[0], xdt_geom[1]
        else:
            wx, wy = _wmctrl_wx, _wmctrl_wy

    row_h_screen = int(ROW_HEIGHT_RATIO * wh)

    # Scale row_top_y from image-pixel space to screen coordinate space.
    # When xdotool reports the same size as the image (physical pixels = image pixels)
    # the scale factor is 1.0 and no scaling is needed.
    if img_h and img_h != wh:
        scale = wh / img_h
        screen_row_top = int(row_top_y * scale)
    else:
        screen_row_top = row_top_y

    if xdotool and win_id:
        # windowactivate only — avoid windowfocus which can reset scroll position
        subprocess.run(
            [xdotool, "windowactivate", "--sync", win_id],
            capture_output=True, env=env, timeout=5,
        )
    time.sleep(0.3)

    def _move_and_report(sx: int, sy: int, label: str) -> None:
        """Move mouse to (sx, sy) and print where xdotool says it actually ended up."""
        if not xdotool:
            return
        subprocess.run(
            [xdotool, "mousemove", "--sync", str(sx), str(sy)],
            capture_output=True, env=env, timeout=5,
        )
        time.sleep(0.15)
        loc = subprocess.run(
            [xdotool, "getmouselocation", "--shell"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        # Output lines: X=1220  Y=245  SCREEN=0  WINDOW=...
        loc_vals = {k: v for k, v in
                    (line.split("=", 1) for line in loc.stdout.strip().splitlines() if "=" in line)}
        actual_x = loc_vals.get("X", "?")
        actual_y = loc_vals.get("Y", "?")
        print(f"      {label:8s} target ({sx}, {sy})  actual ({actual_x}, {actual_y})")
        time.sleep(0.05)

    # Click the avatar (left column of the row, vertically centred).
    avatar_sx = wx + int(AVATAR_X_RATIO * ww)
    avatar_sy = wy + screen_row_top + row_h_screen // 2
    _move_and_report(avatar_sx, avatar_sy, "avatar")
    subprocess.run([xdotool, "click", "1"], capture_output=True, env=env, timeout=5)
    time.sleep(PROFILE_LOAD_PAUSE)

    # Capture the Commander Info profile page and OCR the player name from it.
    # The name appears as dark text on a light background in the player header row.
    name = ""
    profile_detected = False
    try:
        profile_img = _grab_window(geom, debug_path=debug_profile_path)
        if debug_profile_path:
            print(f"      debug profile page saved → {debug_profile_path}")

        # Verify the profile page is actually showing: the player-header row has a
        # distinctly bright background (avg ~160) vs the dark member list (~44).
        # Crop the expected bright-row area (y≈10.5–15%, full width) and measure.
        check_crop = _px(profile_img, 0.0, PROFILE_NAME_Y1,
                         1.0, PROFILE_NAME_HEIGHT)
        gray_bytes = check_crop.convert("L").tobytes()
        avg_bright = sum(gray_bytes) / max(len(gray_bytes), 1)
        if avg_bright < 100:
            print(f"      [profile] page not detected (brightness {avg_bright:.0f}<100) — skipping OCR")
        else:
            profile_detected = True
            # Crop to the player-name region (right of avatar, top of bright content row).
            name_crop = _px(profile_img,
                            PROFILE_NAME_X1, PROFILE_NAME_Y1,
                            PROFILE_NAME_WIDTH, PROFILE_NAME_HEIGHT)
            name = _ocr(name_crop, psm=7)
            print(f"      [profile] OCR from page: {name!r}")
    except Exception as exc:
        print(f"      [profile] page capture/OCR failed: {exc}")

    if not profile_detected:
        # The avatar click didn't open a profile page (e.g. clicking your own avatar
        # has no effect in this view).  Do NOT click Back — doing so when the member
        # list is still showing navigates up to the Battle Contribution menu and
        # breaks all subsequent captures.
        print(f"      [profile] skipping Back (profile page was not shown)")
        return name

    # Re-raise the game window — ImageMagick capture can steal focus.
    # Use windowactivate only; windowfocus disrupts X11 input routing and
    # can reset the game's scroll position.
    if xdotool and win_id:
        subprocess.run(
            [xdotool, "windowactivate", "--sync", win_id],
            capture_output=True, env=env, timeout=5,
        )
    time.sleep(0.2)

    # Click the Back (←) arrow to return to the member list.
    back_sx = wx + int(PROFILE_BACK_X_RATIO * ww)
    back_sy = wy + int(PROFILE_BACK_Y_RATIO * wh)
    _move_and_report(back_sx, back_sy, "Back")
    subprocess.run([xdotool, "click", "1"], capture_output=True, env=env, timeout=5)
    time.sleep(PROFILE_BACK_PAUSE)

    return name


def collect_all_rows(
    first_img: Image.Image,
    geom: tuple[int, int, int, int] | None,
    battlers: int | None,
    no_scroll: bool = False,
    debug_capture: bool = False,
) -> list[dict]:
    """
    Collect all member rows across scroll pages.

    - first_img: already-captured image of page 1
    - geom: window geometry for live scrolling; None in static-multi-file mode
    - battlers: expected total; used to stop early
    - no_scroll: if True, return only first_img rows
    """
    all_rows:    list[dict] = []
    seen_points: set[str]   = set()
    valid_total: int        = 0   # rows with numeric battle scores

    def _valid(rows: list[dict]) -> int:
        return sum(1 for r in rows if r["points"].isdigit())

    # Fetch and cache the window ID once; reused for every profile click.
    win_id: str | None = _get_window_id_wmctrl() if geom is not None else None

    # Save a debug capture of the very first profile page opened so the user
    # can verify button positions; set to None after the first use.
    _debug_profile_done: list[bool] = [False]

    def _resolve_names_via_profile(new_rows: list[dict]) -> None:
        """
        For each new row, open the Commander Info page, press Copy to get the
        exact UTF-8 name, then press Back.  Updates the row dict in-place.
        Only called during live (non-static) runs when geom is available.
        Rows whose profile name looks like garbage fall back to the OCR name.
        """
        if geom is None:
            return
        for r in new_rows:
            row_top = r.get("_row_top_y")
            if row_top is None:
                continue
            ocr_name = r["name"]
            dbg_path = None
            if not _debug_profile_done[0]:
                dbg_path = "debug_profile_page.png"
                _debug_profile_done[0] = True
            try:
                profile_name = _get_name_via_profile(
                    geom, row_top,
                    img_h=r.get("_img_h"),
                    win_id=win_id,
                    debug_profile_path=dbg_path,
                )
            except Exception as exc:
                print(f"    [profile] row {r['row']}: error — {exc} (keeping OCR name {ocr_name!r})")
                continue

            if not profile_name:
                print(f"    [profile] row {r['row']}: profile OCR empty (keeping OCR name {ocr_name!r})")
                continue

            # Sanity check: reject OCR results that look like banner text, garbage,
            # or numbers rather than a player name.
            digits_only  = re.sub(r"\D", "", profile_name)
            # Count Unicode letters (Lu, Ll, Lt, Lm, Lo) — excludes dashes, symbols.
            letter_count = sum(
                1 for c in profile_name
                if unicodedata.category(c).startswith("L")
            )
            letter_ratio = letter_count / max(len(profile_name), 1)
            bad = False
            reason = ""
            if len(profile_name) > 40:
                bad, reason = True, "too long"
            elif len(digits_only) > 6:
                bad, reason = True, "too many digits"
            elif letter_count < 2:
                bad, reason = True, "fewer than 2 letter chars"
            elif letter_ratio < 0.45:
                bad, reason = True, f"letter ratio {letter_ratio:.2f} < 0.45"
            if bad:
                preview = profile_name[:80] + ("…" if len(profile_name) > 80 else "")
                print(f"    [profile] row {r['row']}: profile OCR {preview!r} looks wrong "
                      f"({reason}) — keeping OCR name {ocr_name!r}")
                continue

            # Strip alliance tag prefix like "(MOM) " from the OCR result.
            # OCR sometimes misses the opening paren, giving "MOM) Name" instead.
            stripped = re.sub(r'^\([^)]{1,8}\)\s*', '', profile_name)   # "(MOM) " → ""
            if stripped == profile_name:
                stripped = re.sub(r'^[A-Z0-9]{1,8}\)\s*', '', profile_name)  # "MOM) " → ""
            if stripped and stripped != profile_name:
                print(f"      [profile] stripped alliance tag: {profile_name!r} → {stripped!r}")
                profile_name = stripped

            r["name_raw"] = profile_name
            r["name"]     = _clean_name(profile_name)
            if r["name"] and "name empty" in r["malformed"]:
                r["malformed"].remove("name empty")
            # If the profile click succeeded but points are empty/malformed,
            # this is a zero-score player (battle score shown as "--" or blank
            # in the game UI).  Set points to "0" so the row counts as valid.
            if not r["points"] or not r["points"].isdigit():
                r["points"] = "0"
                r["malformed"] = [m for m in r["malformed"] if "points malformed" not in m]
            changed = f"{ocr_name!r} → {profile_name!r}" if profile_name != ocr_name else f"{profile_name!r} (unchanged)"
            print(f"    [profile] row {r['row']}: {changed}")

    # Track profile names we've already seen to dedup zero-score rows.
    # Score-based dedup (in _add_unique) works well for scored rows but fails
    # for zero-score rows where both the name and score OCR are unreliable.
    # After profile clicks correct the names, we dedup again by profile name.
    seen_profile_names: set[str] = set()

    def _dedup_by_profile_name(rows: list[dict]) -> list[dict]:
        """Remove rows whose profile-corrected name was already captured."""
        kept = []
        for r in rows:
            pname = r["name"].strip().lower()
            if not pname:
                kept.append(r)  # can't dedup without a name
                continue
            if pname in seen_profile_names:
                continue  # duplicate — drop
            seen_profile_names.add(pname)
            kept.append(r)
        return kept

    # ── Page 1 ──
    page1 = extract_rows(first_img, max_rows=ROWS_PER_PAGE)
    new, dupes = _add_unique(page1, seen_points)
    print(f"  Page  1: {len(page1):2d} rows OCR'd  →  {len(new):2d} new  ({dupes} dupes)")
    if geom is not None:
        _resolve_names_via_profile(new)
    new = _dedup_by_profile_name(new)
    all_rows.extend(new)
    valid_total += _valid(new)

    if no_scroll or geom is None:
        return all_rows

    # ── Scroll pages ──
    # empty_streak counts consecutive pages where no NEW unique rows were found
    # (after both score-based and profile-name-based dedup).  This covers both
    # scored rows and zero-score rows whose names were resolved via profile click.
    empty_streak = 0

    for step in range(1, MAX_SCROLL_STEPS + 1):
        if battlers and len(seen_profile_names) >= battlers:
            print(f"  Collected {len(seen_profile_names)} unique players — matches battlers count. Done.")
            break

        # Preemptively restore touch context every 5 pages — this prevents
        # the game's touch state from drifting stuck after many profile clicks.
        if step > 1 and step % 5 == 0:
            print(f"  [scroll] preemptive touch-context restore (page {step+1})")
            _tap_to_restore_scroll(geom)

        _scroll_down(geom)
        dbg = f"debug_page{step+1:02d}.png" if debug_capture else None
        img  = _grab_window(geom, debug_path=dbg)

        # Guard: if the drag accidentally opened a profile page, dismiss it
        # and re-capture the member list before processing.
        fixed_img = _dismiss_accidental_profile(geom, img)
        if fixed_img is not None:
            img = fixed_img

        page = extract_rows(img, max_rows=ROWS_PER_PAGE)
        new, dupes = _add_unique(page, seen_points)

        # If this page is entirely duplicate, retry the scroll with escalating
        # touch-context restoration.  After many profile clicks, the game can
        # stop responding to drag gestures.  One retry is often not enough.
        retry = 0
        while len(new) == 0 and dupes > 0 and retry < 3:
            retry += 1
            print(f"  Page {step+1:2d}: all {dupes} rows duplicate — "
                  f"retrying scroll (attempt {retry}/3) …")
            _tap_to_restore_scroll(geom)
            # On second+ retry, tap twice and wait longer for the game to settle.
            if retry >= 2:
                time.sleep(1.0)
                _tap_to_restore_scroll(geom)
                time.sleep(1.0)
            _scroll_down(geom)
            img  = _grab_window(geom, debug_path=None)
            fixed_img = _dismiss_accidental_profile(geom, img)
            if fixed_img is not None:
                img = fixed_img
            page = extract_rows(img, max_rows=ROWS_PER_PAGE)
            new, dupes = _add_unique(page, seen_points)

        print(f"  Page {step+1:2d}: {len(page):2d} rows OCR'd  →  {len(new):2d} new  ({dupes} dupes)")
        _resolve_names_via_profile(new)

        # Second dedup pass: remove rows whose profile-corrected name is a duplicate.
        # This catches zero-score rows that slipped through score-based dedup because
        # their garbage OCR names/scores differed between overlapping pages.
        before_dedup = len(new)
        new = _dedup_by_profile_name(new)
        name_dupes = before_dedup - len(new)
        if name_dupes > 0:
            print(f"         {name_dupes} profile-name duplicates removed, {len(new)} truly new")

        all_rows.extend(new)
        valid_total += _valid(new)

        if len(new) == 0:
            empty_streak += 1
            if empty_streak >= EMPTY_PAGES_TO_STOP:
                print(f"  {EMPTY_PAGES_TO_STOP} consecutive pages with no new players — bottom reached.")
                break
        else:
            empty_streak = 0

        if step >= MAX_SCROLL_STEPS:
            print(f"  Safety limit ({MAX_SCROLL_STEPS} pages) reached.")

    return all_rows


# ─── OCR extraction ──────────────────────────────────────────────────────────

def extract_header(img: Image.Image) -> dict:
    """Extract event type, date, and battlers count from the header region."""

    # ── Event type ──
    et_crop  = _px(img, *REGION["event_type"])
    et_raw   = _ocr(et_crop, psm=7)
    # Match against known event names (fuzzy: find longest common substring)
    event_type_csv = None
    event_display  = None
    for display, csv_name in EVENT_TYPE_MAP.items():
        # Accept if all words of the display name appear in the OCR text
        words = display.lower().split()
        if all(w in et_raw.lower() for w in words):
            event_display  = display
            event_type_csv = csv_name
            break
    if event_type_csv is None:
        # Fall back: use cleaned raw text
        event_type_csv = et_raw.strip() or "UNKNOWN"
        event_display  = event_type_csv

    # ── Date ──
    date_crop = _px(img, *REGION["date"])
    date_raw  = _ocr(date_crop, psm=7)
    # Extract YYYY-MM-DD HH:MM pattern
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", date_raw)
    event_date = f"{m.group(1)} {m.group(2)}" if m else date_raw.strip()

    # ── Battlers count ──
    b_crop   = _px(img, *REGION["battlers"])
    b_raw    = _ocr(b_crop, psm=7, whitelist="0123456789")
    battlers_str = re.sub(r"\D", "", b_raw)
    battlers = int(battlers_str) if battlers_str.isdigit() else None

    return {
        "event_display":  event_display,
        "event_type_csv": event_type_csv,
        "event_date":     event_date,
        "battlers":       battlers,
    }


def extract_row(img: Image.Image, row_idx: int, row_top_y: int | None = None) -> dict:
    """Extract name and points from a single member-list row."""
    if row_top_y is not None:
        # Two-pass name OCR: pass 1 (centred, yr=0.15-0.65) works for most players;
        # pass 2 (top, yr=0.00-0.52) catches top-ranked players whose name sits higher.
        name_crop_p1 = _row_crop_at(img, row_top_y, NAME_X1_RATIO, NAME_X2_RATIO, 0.15, 0.65)
        name_crop_p2 = _row_crop_at(img, row_top_y, NAME_X1_RATIO, NAME_X2_RATIO, 0.00, 0.52)
        points_crop  = _row_crop_at(img, row_top_y, POINTS_X1_RATIO, POINTS_X2_RATIO, POINTS_YR1, POINTS_YR2)
    else:
        name_crop_p1 = _row_crop(img, row_idx, NAME_X1_RATIO, NAME_X2_RATIO, 0.15, 0.65)
        name_crop_p2 = _row_crop(img, row_idx, NAME_X1_RATIO, NAME_X2_RATIO, 0.00, 0.52)
        points_crop  = _row_crop(img, row_idx, POINTS_X1_RATIO, POINTS_X2_RATIO, POINTS_YR1, POINTS_YR2)

    name_r1 = _ocr(name_crop_p1, psm=7)
    name_r2 = _ocr(name_crop_p2, psm=7)
    # Prefer pass 1 (centred) unless it yields ≤2 chars, in which case try pass 2.
    name_raw = name_r1 if len(name_r1) > 2 else (name_r2 if len(name_r2) > len(name_r1) else name_r1)
    points_raw = _ocr(points_crop, psm=7, whitelist="0123456789,")

    name   = _clean_name(name_raw)
    points = _clean_points(points_raw)

    malformed = []
    if not name:
        malformed.append("name empty")
    if not points or not points.isdigit():
        malformed.append(f"points malformed ({points_raw!r})")

    return {
        "row":       row_idx + 1,
        "name_raw":  name_raw,
        "name":      name,
        "points_raw": points_raw,
        "points":    points,
        "malformed": malformed,
    }


def extract_rows(img: Image.Image, max_rows: int = ROWS_PER_PAGE) -> list[dict]:
    """Extract all visible member rows using auto-detected row positions."""
    img_h = img.size[1]
    row_tops = _detect_list_row_tops(img, max_rows=max_rows + 2)
    rows = []
    for i, top in enumerate(row_tops[:max_rows]):
        r = extract_row(img, i, row_top_y=top)
        r["_row_top_y"] = top    # image-space pixel y (img_h tall)
        r["_img_h"]     = img_h  # image height; needed to scale to screen-space for clicks
        rows.append(r)
    return rows


# ─── Pipeline ────────────────────────────────────────────────────────────────

def _match_rows(ocr_rows: list[dict], roster: list[str]) -> list[dict]:
    """Fuzzy-match a list of OCR rows against the roster. Returns matched_rows list."""
    matched: list[dict] = []
    for r in ocr_rows:
        ocr_name        = r["name"]
        roster_name, sc = fuzzy_match(ocr_name, roster)
        member_name     = roster_name if roster_name else ocr_name
        notes_parts     = []
        if not roster_name:
            notes_parts.append("UNMATCHED")
        if r["malformed"]:
            notes_parts.append("OCR_WARN: " + ", ".join(r["malformed"]))
        matched.append({
            "member_name":  member_name,
            "points":       r["points"],
            "notes":        "; ".join(notes_parts),
            "_ocr_name":    ocr_name,
            "_score":       sc,
            "_row":         r["row"],
        })
    return matched


def run_pipeline(
    first_img:  Image.Image,
    roster:     list[str],
    geom:       tuple[int, int, int, int] | None = None,
    extra_imgs: list[Image.Image] | None = None,
    source_label: str  = "",
    dry_run:    bool   = False,
    no_scroll:  bool   = False,
    debug_capture: bool = False,
    csv_mode:   bool   = False,
) -> None:
    """Full Phase 1–3 pipeline."""

    print(f"\n{'='*60}")
    print(f"  AoO Scraper  —  {source_label or 'live window'}")
    print(f"  Image: {first_img.size[0]}×{first_img.size[1]}")
    print(f"{'='*60}\n")

    # ── Phase 1: header OCR from first image ─────────────────────────────────
    hdr = extract_header(first_img)
    print(f"Event Type : {hdr['event_type_csv']!r}")
    print(f"Event Date : {hdr['event_date']!r}")
    print(f"Battlers   : {hdr['battlers']}")
    print()

    # ── Phase 3: collect rows (scroll or multi-file) ──────────────────────────
    print("--- Scroll / Capture ---")
    if extra_imgs:
        # Static multi-file: user supplied one image per page
        all_rows:    list[dict] = []
        seen_points: set[str]   = set()
        for page_num, pimg in enumerate([first_img] + extra_imgs, start=1):
            page = extract_rows(pimg, max_rows=ROWS_PER_PAGE)
            new, dupes = _add_unique(page, seen_points)
            all_rows.extend(new)
            print(f"  Page {page_num:2d}: {len(page):2d} rows OCR'd  →  {len(new):2d} new  ({dupes} dupes)")
    else:
        all_rows = collect_all_rows(
            first_img, geom,
            battlers=hdr["battlers"],
            no_scroll=no_scroll,
            debug_capture=debug_capture,
        )

    valid_rows = [r for r in all_rows if r["points"].isdigit()]
    print(f"\n  Total unique rows collected: {len(all_rows)}  ({len(valid_rows)} with valid scores)")
    if hdr["battlers"] and len(valid_rows) < hdr["battlers"]:
        print(f"  ⚠ Expected {hdr['battlers']} battlers but only captured {len(valid_rows)} with valid scores.")
        print("    Scroll may not have reached the bottom — try re-running.")

    # ── Phase 1 (cont.): print all OCR rows ──────────────────────────────────
    print()
    print("--- OCR Rows ---")
    for r in all_rows:
        pts  = f"{int(r['points']):,}" if r["points"].isdigit() else r["points"] or "(empty)"
        warn = f"  ⚠ {', '.join(r['malformed'])}" if r["malformed"] else ""
        print(f"  Row {r['row']:2d}: {r['name']!r:<24} {pts}{warn}")

    # ── Phase 2: fuzzy match ──────────────────────────────────────────────────
    print()
    print("--- Roster Matching ---")
    matched_rows = _match_rows(all_rows, roster)
    match_count   = sum(1 for r in matched_rows if not r["notes"].startswith("UNMATCHED"))
    nomatch_count = len(matched_rows) - match_count

    for r in matched_rows:
        label = (f"→ {r['member_name']:<24} score={r['_score']}"
                 if r["_score"] > 0 else
                 f"→ (UNMATCHED, kept as {r['_ocr_name']!r})")
        print(f"  Row {r['_row']:2d}: {r['_ocr_name']!r:<24} {label}")

    # ── Phase 2: write results ─────────────────────────────────────────────────
    print()
    if dry_run:
        print("[dry-run] Skipping write.")
    elif csv_mode:
        written, skipped = append_to_csv(CSV_PATH, hdr, matched_rows, dry_run=False)
        print(f"✓ {written} rows appended to {CSV_PATH}")
        if skipped:
            print(f"  {skipped} rows skipped (already in CSV)")
    else:
        written, skipped = write_to_db(hdr, matched_rows)
        print(f"✓ {written} rows written to DB")
        if skipped:
            print(f"  {skipped} rows skipped")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print(f"✓ Captured {len(valid_rows)} valid-score rows of {hdr['battlers'] or '?'} battlers  ({len(all_rows)} total incl. malformed)")
    print(f"✓ {match_count} matched to roster  |  {nomatch_count} unmatched")
    if nomatch_count and not dry_run:
        print("  Unmatched rows have Notes='UNMATCHED' — review manually.")


def _open_static(path: str) -> Image.Image:
    img = Image.open(path).convert("RGBA")
    bg  = Image.new("RGB", img.size, (0, 0, 0))
    bg.paste(img, mask=img.split()[3])
    return bg


def main():
    parser = argparse.ArgumentParser(description="AoO Event Scraper")
    parser.add_argument("--dry-run", action="store_true", help="Skip writing results")
    parser.add_argument("--no-scroll", action="store_true", help="First page only, no scrolling")
    parser.add_argument("--static", nargs="*", default=None, help="Test against screenshot files")
    parser.add_argument("--debug-capture", action="store_true", help="Save debug capture image")
    parser.add_argument("--csv", action="store_true", help="Write to CSV instead of DB")
    args = parser.parse_args()

    static       = args.static is not None
    debug_path   = "debug_capture.png" if args.debug_capture else None

    static_files: list[str] = []
    if static:
        static_files = args.static if args.static else [DEFAULT_STATIC]

    roster = load_roster(ROSTER_PATH)
    global _name_aliases
    _name_aliases = load_name_aliases(ALIAS_PATH)

    if args.csv:
        ensure_csv(CSV_PATH)
    else:
        _db.init_db()
        _db.import_name_aliases(ALIAS_PATH)

    if static:
        for sf in static_files:
            if not Path(sf).exists():
                print(f"[error] Static file not found: {sf}")
                sys.exit(1)
        imgs = [_open_static(sf) for sf in static_files]
        label = ", ".join(static_files)
        run_pipeline(
            first_img=imgs[0],
            roster=roster,
            geom=None,
            extra_imgs=imgs[1:] if len(imgs) > 1 else None,
            source_label=label,
            dry_run=args.dry_run,
            no_scroll=True,
            csv_mode=args.csv,
        )
    else:
        img, geom = capture_window(debug_path=debug_path)
        run_pipeline(
            first_img=img,
            roster=roster,
            geom=geom,
            extra_imgs=None,
            source_label="live window",
            dry_run=args.dry_run,
            no_scroll=args.no_scroll,
            debug_capture=args.debug_capture,
            csv_mode=args.csv,
        )


if __name__ == "__main__":
    main()
