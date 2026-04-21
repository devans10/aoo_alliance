"""
Tests for the scraper's name-cleaning and fuzzy-matching pipeline.

These functions live in ocr/aoo_scraper.py.  We import them directly —
no window capture or OCR code is exercised here.

Covers:
  _clean_name      — strip OCR noise from player name
  _clean_points    — extract numeric score from OCR output
  _strip_decorative — keep only ASCII alphanumerics/@_-
  _strip_accents   — decompose accented chars, drop combining marks
  _ocr_confuse_norm — collapse commonly-swapped OCR char pairs
  fuzzy_match      — 6-pass matching pipeline
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub out native deps that are unavailable in the test environment (mss requires
# a display, pytesseract requires the tesseract binary, etc.).  We only need the
# pure string-manipulation and fuzzy-matching functions from the scraper.
for _mod in ["mss", "mss.tools", "pytesseract", "pyperclip",
             "Xlib", "Xlib.display", "Xlib.ext", "Xlib.ext.randr"]:
    sys.modules.setdefault(_mod, MagicMock())

# Import only the pure functions — avoids triggering OCR / window capture imports
from ocr.aoo_scraper import (
    _clean_name,
    _clean_points,
    _strip_decorative,
    _strip_accents,
    _ocr_confuse_norm,
    fuzzy_match,
    _name_aliases,
)
import ocr.aoo_scraper as scraper_module


# ── _clean_name ───────────────────────────────────────────────


class TestCleanName:
    def test_plain_name_unchanged(self):
        assert _clean_name("DragonKing") == "DragonKing"

    def test_strips_all_numeric_lines(self):
        # A numeric line (power bleed-through) should be dropped
        raw = "DragonKing\n1,234,567"
        assert _clean_name(raw) == "DragonKing"

    def test_strips_leading_junk_chars(self):
        raw = "「DragonKing"
        result = _clean_name(raw)
        assert result.startswith("DragonKing")

    def test_strips_trailing_punctuation(self):
        assert _clean_name("DragonKing.") == "DragonKing"
        assert _clean_name("DragonKing,") == "DragonKing"
        assert _clean_name("DragonKing'") == "DragonKing"

    def test_strips_leading_quotes(self):
        assert _clean_name("'DragonKing") == "DragonKing"

    def test_collapses_internal_whitespace(self):
        assert _clean_name("Dragon  King") == "Dragon King"

    def test_empty_string(self):
        assert _clean_name("") == ""

    def test_all_numeric_produces_empty(self):
        assert _clean_name("1234567") == ""

    def test_name_with_at_sign(self):
        result = _clean_name("MINH@DUC")
        assert result == "MINH@DUC"

    def test_multiline_keeps_name_lines(self):
        raw = "SomePlayer\n\n987,654"
        assert _clean_name(raw) == "SomePlayer"


# ── _clean_points ─────────────────────────────────────────────


class TestCleanPoints:
    def test_comma_formatted_number(self):
        assert _clean_points("1,234,567") == "1234567"

    def test_plain_number(self):
        assert _clean_points("500000") == "500000"

    def test_ocr_substitution_O_to_0(self):
        assert _clean_points("1O0") == "100"

    def test_ocr_substitution_l_to_1(self):
        assert _clean_points("l23") == "123"

    def test_ocr_substitution_I_to_1(self):
        assert _clean_points("I23") == "123"

    def test_picks_longest_digit_run(self):
        # A stray single digit before the real score
        result = _clean_points("3 1,234,567")
        assert result == "1234567"

    def test_empty_string(self):
        assert _clean_points("") == ""

    def test_no_digits(self):
        assert _clean_points("abc") == ""

    def test_large_score(self):
        assert _clean_points("12,345,678,901") == "12345678901"


# ── _strip_decorative ─────────────────────────────────────────


class TestStripDecorative:
    def test_removes_ornamental_unicode(self):
        assert _strip_decorative("༒۝warlord۝༒") == "warlord"

    def test_removes_korean_suffix(self):
        assert _strip_decorative("SUN써니") == "SUN"

    def test_removes_diamond_separator(self):
        assert _strip_decorative("Prison◇Mike") == "PrisonMike"

    def test_removes_bullet_dots(self):
        assert _strip_decorative("°•°SHADOW°•°") == "SHADOW"

    def test_removes_cjk_suffix(self):
        assert _strip_decorative("ccchhiii千香") == "ccchhiii"

    def test_preserves_at_sign(self):
        assert _strip_decorative("MINH@DUC") == "MINH@DUC"

    def test_preserves_hyphen(self):
        assert _strip_decorative("Dark-Lord") == "Dark-Lord"

    def test_preserves_underscore(self):
        assert _strip_decorative("dark_lord") == "dark_lord"

    def test_pure_ascii_unchanged(self):
        assert _strip_decorative("HelloWorld123") == "HelloWorld123"

    def test_all_decorative_produces_empty(self):
        assert _strip_decorative("꧁༺༻꧂") == ""


# ── _strip_accents ────────────────────────────────────────────


class TestStripAccents:
    def test_drops_diaeresis(self):
        # Ï decomposes to I + combining diaeresis (U+0308), which gets stripped.
        # Ø is not a precomposed letter+combiner, so it is not affected.
        assert _strip_accents("RAÏZØ") == "RAIZØ"

    def test_drops_diaeresis_simple(self):
        assert _strip_accents("naïve") == "naive"

    def test_drops_vietnamese_combining_accents(self):
        # Ứ (U+1EE8) decomposes to U + combining hook + combining acute → U.
        # Đ (U+0110) is not a precomposed char, so it stays as Đ.
        result = _strip_accents("MINH@ĐỨC")
        assert result == "MINH@ĐUC"

    def test_plain_ascii_unchanged(self):
        assert _strip_accents("HelloWorld") == "HelloWorld"

    def test_accented_e(self):
        assert _strip_accents("José") == "Jose"

    def test_handles_empty(self):
        assert _strip_accents("") == ""


# ── _ocr_confuse_norm ─────────────────────────────────────────


class TestOcrConfuseNorm:
    def test_b_to_d(self):
        assert _ocr_confuse_norm("b") == "d"

    def test_B_to_D(self):
        assert _ocr_confuse_norm("B") == "D"

    def test_l_to_i(self):
        assert _ocr_confuse_norm("l") == "i"

    def test_1_to_i(self):
        assert _ocr_confuse_norm("1") == "i"

    def test_s_to_g(self):
        assert _ocr_confuse_norm("s") == "g"

    def test_0_to_o(self):
        assert _ocr_confuse_norm("0") == "o"

    def test_O_to_o(self):
        assert _ocr_confuse_norm("O") == "o"

    def test_full_word_normalisation(self):
        # "Bob" → "Dod" (b→d, o→o, b→d)
        assert _ocr_confuse_norm("Bob") == "Dod"

    def test_unaffected_chars_preserved(self):
        result = _ocr_confuse_norm("xyz")
        assert result == "xyz"


# ── fuzzy_match ───────────────────────────────────────────────


class TestFuzzyMatch:
    def setup_method(self):
        # Ensure no stale aliases bleed between tests
        scraper_module._name_aliases.clear()

    def test_exact_match_scores_100(self):
        roster = ["DragonKing", "ShadowWolf", "IronFist"]
        name, score = fuzzy_match("DragonKing", roster)
        assert name == "DragonKing"
        assert score == 100

    def test_single_char_typo_matched(self):
        roster = ["DragonKing"]
        name, score = fuzzy_match("DragonKlng", roster)
        assert name == "DragonKing"
        assert score >= 80

    def test_no_match_below_threshold(self):
        roster = ["AlphaWolf"]
        name, score = fuzzy_match("XXXXXXXXXX", roster)
        assert name is None
        assert score == 0

    def test_alias_override_returns_100(self):
        scraper_module._name_aliases["minh@duc"] = "MINH@ĐỨC"
        roster = ["MINH@ĐỨC", "OtherPlayer"]
        name, score = fuzzy_match("MINH@DUC", roster)
        assert name == "MINH@ĐỨC"
        assert score == 100

    def test_decorative_strip_pass_matches(self):
        # OCR produces ASCII core; roster has ornamental chars
        roster = ["༒۝warlord۝༒", "OtherPlayer"]
        name, score = fuzzy_match("warlord", roster)
        assert name == "༒۝warlord۝༒"
        assert score >= 80

    def test_accent_strip_pass_matches(self):
        # OCR drops diacritics; roster has accented name
        roster = ["RAÏZØ", "OtherPlayer"]
        name, score = fuzzy_match("RAIZO", roster)
        assert name == "RAÏZØ"
        assert score >= 80

    def test_empty_roster_returns_none(self):
        name, score = fuzzy_match("DragonKing", [])
        assert name is None
        assert score == 0

    def test_empty_ocr_name_returns_none(self):
        name, score = fuzzy_match("", ["DragonKing"])
        assert name is None
        assert score == 0

    def test_korean_suffix_stripped(self):
        # "SUN써니" in roster; OCR only produces "SUN"
        roster = ["SUN써니", "SHUN"]
        name, score = fuzzy_match("SUN", roster)
        # After decorative strip, "SUN써니" → "SUN" which is a perfect match
        assert name == "SUN써니"
        assert score == 100

    def test_returns_original_roster_name_not_stripped(self):
        # Even though matching happens on stripped form, the returned name is
        # the original roster entry (with decorative chars intact).
        roster = ["Prison◇Mike"]
        name, score = fuzzy_match("PrisonMike", roster)
        assert name == "Prison◇Mike"
