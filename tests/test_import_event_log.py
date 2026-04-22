"""
Tests for ocr/import_event_log.py — date parsing, score parsing,
event type normalisation, and name resolution.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ocr.import_event_log import (
    normalise_date,
    normalise_event_type,
    parse_score,
    group_events,
    resolve_names,
)
from shared.db import add_member


# ── normalise_date ────────────────────────────────────────────


class TestNormaliseDate:
    def test_iso_format_passthrough(self):
        assert normalise_date("2025-04-15") == "2025-04-15"

    def test_iso_with_time_suffix(self):
        assert normalise_date("2025-04-15 14:30") == "2025-04-15"

    def test_us_slash_format(self):
        assert normalise_date("4/15/2025") == "2025-04-15"

    def test_us_slash_format_zero_padded(self):
        assert normalise_date("04/15/2025") == "2025-04-15"

    def test_two_digit_year(self):
        assert normalise_date("4/15/25") == "2025-04-15"

    def test_empty_string_returns_none(self):
        assert normalise_date("") is None

    def test_whitespace_only_returns_none(self):
        assert normalise_date("   ") is None

    def test_strips_ocr_junk_prefix(self):
        # OCR sometimes adds garbage chars before the date
        assert normalise_date("||2025-04-15 10:00") == "2025-04-15"

    def test_three_digit_year_restored(self):
        # "020-04-12" → "2020-04-12" (leading digit dropped by OCR)
        assert normalise_date("020-04-12") == "2020-04-12"

    def test_invalid_date_returns_none(self):
        assert normalise_date("99/99/9999") is None

    def test_garbage_string_returns_none(self):
        assert normalise_date("not a date at all") is None


# ── normalise_event_type ──────────────────────────────────────


class TestNormaliseEventType:
    def test_standard_event_passthrough(self):
        assert normalise_event_type("Elite War") == "Elite War"

    def test_fixes_doubled_void_war(self):
        assert normalise_event_type("Void War War") == "Void War"

    def test_fixes_doubled_battle_frenzy(self):
        assert normalise_event_type("Battle Battle Frenzy") == "Battle Frenzy"

    def test_empty_returns_none(self):
        assert normalise_event_type("") is None

    def test_whitespace_only_returns_none(self):
        assert normalise_event_type("   ") is None

    def test_unknown_type_passes_through(self):
        # Unknown event types are forwarded as-is (caller decides)
        assert normalise_event_type("New Unknown Event") == "New Unknown Event"

    def test_strips_surrounding_whitespace(self):
        assert normalise_event_type("  Elite War  ") == "Elite War"


# ── parse_score ───────────────────────────────────────────────


class TestParseScore:
    def test_comma_formatted_large_score(self):
        assert parse_score("1,385,409,188") == 1_385_409_188

    def test_plain_integer(self):
        assert parse_score("500000") == 500_000

    def test_quoted_score(self):
        # CSV files sometimes wrap values in extra quotes
        assert parse_score('"1,000,000"') == 1_000_000

    def test_zero_score(self):
        assert parse_score("0") == 0

    def test_empty_string_returns_zero(self):
        assert parse_score("") == 0

    def test_non_numeric_returns_zero(self):
        assert parse_score("N/A") == 0

    def test_strips_surrounding_whitespace(self):
        assert parse_score("  500,000  ") == 500_000


# ── group_events ──────────────────────────────────────────────


class TestGroupEvents:
    def _make_rows(self, specs):
        """Build rows in the format returned by load_csv (lowercase keys)."""
        return [
            {
                "event_type": et,
                "event_date": dt,
                "name": mn,
                "score": int(sc),
                "attendance": "",
            }
            for et, dt, mn, sc in specs
        ]

    def test_groups_by_event_and_date(self):
        rows = self._make_rows([
            ("Elite War", "2025-04-01", "Alpha", "1000"),
            ("Elite War", "2025-04-01", "Beta", "2000"),
            ("Void War",  "2025-04-02", "Alpha", "500"),
        ])
        groups = group_events(rows)
        assert len(groups) == 2
        assert ("Elite War", "2025-04-01") in groups
        assert ("Void War",  "2025-04-02") in groups

    def test_members_per_event_correct(self):
        rows = self._make_rows([
            ("Elite War", "2025-04-01", "Alpha", "1000"),
            ("Elite War", "2025-04-01", "Beta",  "2000"),
        ])
        groups = group_events(rows)
        assert len(groups[("Elite War", "2025-04-01")]) == 2

    def test_single_member_single_event(self):
        rows = self._make_rows([("Elite War", "2025-04-01", "Alpha", "1000")])
        groups = group_events(rows)
        assert len(groups) == 1


# ── resolve_names ─────────────────────────────────────────────


class TestResolveNames:
    def _make_rows(self, names):
        return [{"name": n, "score": 1000,
                 "event_type": "Elite War", "event_date": "2025-04-01",
                 "attendance": ""}
                for n in names]

    def test_exact_name_resolved(self, db_conn):
        mid = add_member("ExactPlayer", conn=db_conn)
        rows = self._make_rows(["ExactPlayer"])
        resolved = resolve_names(rows, db_conn)
        assert resolved["ExactPlayer"] == mid

    def test_unknown_name_resolves_to_none(self, db_conn):
        rows = self._make_rows(["TotallyUnknown"])
        resolved = resolve_names(rows, db_conn)
        assert resolved["TotallyUnknown"] is None

    def test_alias_resolves_to_member(self, db_conn):
        from ocr.import_event_log import NAME_ALIASES
        mid = add_member("MINH@ĐỨC", conn=db_conn)
        # NAME_ALIASES maps "MINH@DUC" → "MINH@ĐỨC"
        rows = self._make_rows(["MINH@DUC"])
        resolved = resolve_names(rows, db_conn)
        assert resolved["MINH@DUC"] == mid

    def test_each_name_resolved_once(self, db_conn):
        mid = add_member("Alpha", conn=db_conn)
        rows = self._make_rows(["Alpha", "Alpha", "Beta"])
        resolved = resolve_names(rows, db_conn)
        # Alpha should be in the dict once, Beta as None
        assert resolved["Alpha"] == mid
        assert resolved["Beta"] is None
        assert len(resolved) == 2
