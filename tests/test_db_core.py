"""
Tests for shared/db.py — member management, name resolution, and snapshots.

All tests run against an in-memory SQLite database (see conftest.py) so they
are fully isolated from the real alliance.db.
"""

import sqlite3
import sys
from pathlib import Path
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.db import (
    _to_clean_name,
    _fuzzy_confidence,
    add_member,
    rename_member,
    set_member_status,
    resolve_member,
    add_player_snapshot,
    get_player_stats,
    get_latest_stats,
    military_rank_from_merit,
    _add_to_review_queue,
)


# ── _to_clean_name ────────────────────────────────────────────


class TestToCleanName:
    def test_ascii_lowercase(self):
        assert _to_clean_name("Tom") == "tom"

    def test_strips_non_ascii(self):
        # Decorative Unicode prefix/suffix common in game names
        assert _to_clean_name("꧁༺Tom༻꧂") == "tom"

    def test_strips_chinese_characters(self):
        # Chinese name — only ASCII digits survive
        assert _to_clean_name("指挥官123266007") == "123266007"

    def test_strips_accented_latin(self):
        # Non-ASCII accented chars are dropped (not transliterated)
        assert _to_clean_name("José García") == "jos garca"

    def test_collapses_whitespace(self):
        assert _to_clean_name("  Hello   World  ") == "hello world"

    def test_empty_string(self):
        assert _to_clean_name("") == ""

    def test_all_non_ascii(self):
        assert _to_clean_name("꧁༺༻꧂") == ""

    def test_mixed_ascii_and_unicode(self):
        assert _to_clean_name("SUN써니") == "sun"

    def test_numbers_preserved(self):
        assert _to_clean_name("Player123") == "player123"

    def test_special_ascii_preserved(self):
        # @, underscore, hyphen are all ASCII — should survive
        assert _to_clean_name("MINH@DUC") == "minh@duc"


# ── _fuzzy_confidence ─────────────────────────────────────────


class TestFuzzyConfidence:
    def test_identical_strings(self):
        assert _fuzzy_confidence("Alice", "Alice") == 1.0

    def test_case_insensitive(self):
        assert _fuzzy_confidence("alice", "ALICE") == 1.0

    def test_completely_different(self):
        score = _fuzzy_confidence("abc", "xyz")
        assert score == 0.0

    def test_partial_overlap(self):
        score = _fuzzy_confidence("Alice", "Alic3")
        assert 0.7 < score < 1.0

    def test_single_char_difference(self):
        score = _fuzzy_confidence("DragonKing", "DragonKlng")
        assert score > 0.85

    def test_symmetry(self):
        assert _fuzzy_confidence("foo", "bar") == _fuzzy_confidence("bar", "foo")


# ── military_rank_from_merit ──────────────────────────────────


class TestMilitaryRankFromMerit:
    def test_none_merit(self):
        assert military_rank_from_merit(None) is None

    def test_below_all_thresholds(self):
        assert military_rank_from_merit(0) is None

    def test_exact_private_threshold(self):
        assert military_rank_from_merit(300) == "Private"

    def test_private_first_class(self):
        assert military_rank_from_merit(1300) == "Private First Class"

    def test_general(self):
        assert military_rank_from_merit(822300) == "General"

    def test_between_ranks(self):
        # 5000 is between Sergeant (4300) and Master Sergeant (11300)
        assert military_rank_from_merit(5000) == "Sergeant"

    def test_very_high_merit_stays_general(self):
        # Above General threshold — None-threshold ranks are skipped
        assert military_rank_from_merit(999_999_999) == "General"


# ── add_member ────────────────────────────────────────────────


class TestAddMember:
    def test_returns_member_id(self, db_conn):
        mid = add_member("TestPlayer", conn=db_conn)
        assert isinstance(mid, int)
        assert mid > 0

    def test_member_row_created(self, db_conn):
        mid = add_member("TestPlayer", conn=db_conn)
        row = db_conn.execute("SELECT * FROM members WHERE id = ?", (mid,)).fetchone()
        assert row["name"] == "TestPlayer"
        assert row["status"] == "active"

    def test_clean_name_stored(self, db_conn):
        mid = add_member("꧁TestPlayer꧂", conn=db_conn)
        row = db_conn.execute("SELECT clean_name FROM members WHERE id = ?", (mid,)).fetchone()
        assert row["clean_name"] == "testplayer"

    def test_member_names_entry_created(self, db_conn):
        mid = add_member("TestPlayer", conn=db_conn)
        row = db_conn.execute(
            "SELECT * FROM member_names WHERE member_id = ? AND is_current = 1", (mid,)
        ).fetchone()
        assert row is not None
        assert row["name"] == "TestPlayer"

    def test_alliance_history_entry_created(self, db_conn):
        mid = add_member("TestPlayer", conn=db_conn)
        row = db_conn.execute(
            "SELECT * FROM alliance_history WHERE member_id = ? AND event = 'joined'", (mid,)
        ).fetchone()
        assert row is not None

    def test_custom_joined_date(self, db_conn):
        mid = add_member("TestPlayer", joined_date="2024-01-15", conn=db_conn)
        row = db_conn.execute("SELECT joined_date FROM members WHERE id = ?", (mid,)).fetchone()
        assert row["joined_date"] == "2024-01-15"

    def test_multiple_members_get_distinct_ids(self, db_conn):
        id1 = add_member("Alpha", conn=db_conn)
        id2 = add_member("Beta", conn=db_conn)
        assert id1 != id2


# ── rename_member ─────────────────────────────────────────────


class TestRenameMember:
    def test_display_name_updated(self, db_conn):
        mid = add_member("OldName", conn=db_conn)
        rename_member(mid, "NewName", conn=db_conn)
        row = db_conn.execute("SELECT name FROM members WHERE id = ?", (mid,)).fetchone()
        assert row["name"] == "NewName"

    def test_old_name_retired_in_member_names(self, db_conn):
        mid = add_member("OldName", conn=db_conn)
        rename_member(mid, "NewName", conn=db_conn)
        old = db_conn.execute(
            "SELECT is_current FROM member_names WHERE member_id = ? AND name = 'OldName'", (mid,)
        ).fetchone()
        assert old["is_current"] == 0

    def test_new_name_is_current(self, db_conn):
        mid = add_member("OldName", conn=db_conn)
        rename_member(mid, "NewName", conn=db_conn)
        new = db_conn.execute(
            "SELECT is_current FROM member_names WHERE member_id = ? AND name = 'NewName'", (mid,)
        ).fetchone()
        assert new["is_current"] == 1

    def test_history_entry_logged(self, db_conn):
        mid = add_member("OldName", conn=db_conn)
        rename_member(mid, "NewName", conn=db_conn)
        history = db_conn.execute(
            "SELECT detail FROM alliance_history WHERE member_id = ? AND event = 'name_change'",
            (mid,)
        ).fetchone()
        assert history is not None
        assert "OldName" in history["detail"]
        assert "NewName" in history["detail"]

    def test_only_one_current_name(self, db_conn):
        mid = add_member("Name1", conn=db_conn)
        rename_member(mid, "Name2", conn=db_conn)
        rename_member(mid, "Name3", conn=db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS cnt FROM member_names WHERE member_id = ? AND is_current = 1",
            (mid,)
        ).fetchone()["cnt"]
        assert count == 1


# ── set_member_status ─────────────────────────────────────────


class TestSetMemberStatus:
    def test_status_updated(self, db_conn):
        mid = add_member("Player", conn=db_conn)
        set_member_status(mid, "inactive", conn=db_conn)
        row = db_conn.execute("SELECT status FROM members WHERE id = ?", (mid,)).fetchone()
        assert row["status"] == "inactive"

    def test_history_logged_for_inactive(self, db_conn):
        mid = add_member("Player", conn=db_conn)
        set_member_status(mid, "inactive", conn=db_conn)
        row = db_conn.execute(
            "SELECT event FROM alliance_history WHERE member_id = ? AND event = 'inactive'",
            (mid,)
        ).fetchone()
        assert row is not None

    def test_history_logged_for_left(self, db_conn):
        mid = add_member("Player", conn=db_conn)
        set_member_status(mid, "left", conn=db_conn)
        row = db_conn.execute(
            "SELECT event FROM alliance_history WHERE member_id = ? AND event = 'left'",
            (mid,)
        ).fetchone()
        assert row is not None

    def test_reactivation_logged_as_rejoined(self, db_conn):
        mid = add_member("Player", conn=db_conn)
        set_member_status(mid, "inactive", conn=db_conn)
        set_member_status(mid, "active", conn=db_conn)
        row = db_conn.execute(
            "SELECT event FROM alliance_history WHERE member_id = ? AND event = 'rejoined'",
            (mid,)
        ).fetchone()
        assert row is not None


# ── resolve_member ────────────────────────────────────────────


class TestResolveMember:
    def test_exact_current_name_match(self, db_conn):
        mid = add_member("Warlord", conn=db_conn)
        result = resolve_member("Warlord", db_conn)
        assert result is not None
        assert result["id"] == mid

    def test_exact_clean_name_match(self, db_conn):
        # Member has Unicode decorations; OCR produces the ASCII core
        mid = add_member("꧁Warlord꧂", conn=db_conn)
        result = resolve_member("Warlord", db_conn)
        assert result is not None
        assert result["id"] == mid

    def test_historical_name_queued_for_review(self, db_conn):
        mid = add_member("OldName", conn=db_conn)
        rename_member(mid, "NewName", conn=db_conn)
        result = resolve_member("OldName", db_conn)
        # Should return None and add to review queue
        assert result is None
        queue = db_conn.execute(
            "SELECT * FROM review_queue WHERE scraped_name = 'OldName' AND resolved = 0"
        ).fetchone()
        assert queue is not None
        assert queue["reason"] == "historical_name"

    def test_high_confidence_fuzzy_auto_accepted(self, db_conn):
        # Single-character OCR typo in a 13-char name scores ~0.923, above the
        # 0.92 auto-accept threshold.  "DragonKingdom" → "DragonKlngdom" (i→l).
        mid = add_member("DragonKingdom", conn=db_conn)
        result = resolve_member("DragonKlngdom", db_conn)
        assert result is not None
        assert result["id"] == mid

    def test_low_confidence_fuzzy_queued_for_review(self, db_conn):
        add_member("SomePlayer", conn=db_conn)
        # "SomPlaer" is close-ish but below the 0.92 auto-accept threshold
        result = resolve_member("SomPlaer", db_conn)
        assert result is None
        queue = db_conn.execute(
            "SELECT * FROM review_queue WHERE scraped_name = 'SomPlaer' AND resolved = 0"
        ).fetchone()
        assert queue is not None
        assert queue["reason"] == "fuzzy_match"

    def test_unknown_name_queued_as_unknown(self, db_conn):
        result = resolve_member("TotallyUnknown", db_conn)
        assert result is None
        queue = db_conn.execute(
            "SELECT * FROM review_queue WHERE scraped_name = 'TotallyUnknown' AND resolved = 0"
        ).fetchone()
        assert queue is not None
        assert queue["reason"] == "unknown_name"

    def test_duplicate_unknown_not_queued_twice(self, db_conn):
        resolve_member("GhostPlayer", db_conn)
        resolve_member("GhostPlayer", db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS cnt FROM review_queue WHERE scraped_name = 'GhostPlayer'"
        ).fetchone()["cnt"]
        assert count == 1


# ── add_player_snapshot / get_player_stats ────────────────────


class TestPlayerSnapshots:
    def test_snapshot_stored(self, db_conn):
        mid = add_member("Snapper", conn=db_conn)
        add_player_snapshot(mid, battle_power=1_000_000, captured_at="2025-01-01", conn=db_conn)
        stats = get_player_stats(mid, conn=db_conn)
        assert len(stats) == 1
        assert stats[0]["battle_power"] == 1_000_000

    def test_members_table_updated(self, db_conn):
        mid = add_member("Snapper", conn=db_conn)
        add_player_snapshot(mid, battle_power=999_999, captured_at="2025-01-01", conn=db_conn)
        row = db_conn.execute("SELECT battle_power FROM members WHERE id = ?", (mid,)).fetchone()
        assert row["battle_power"] == 999_999

    def test_idempotent_same_day_same_source(self, db_conn):
        mid = add_member("Snapper", conn=db_conn)
        add_player_snapshot(mid, battle_power=100, captured_at="2025-01-01", conn=db_conn)
        add_player_snapshot(mid, battle_power=200, captured_at="2025-01-01", conn=db_conn)
        # INSERT OR REPLACE should result in only one row
        stats = get_player_stats(mid, conn=db_conn)
        assert len(stats) == 1
        assert stats[0]["battle_power"] == 200

    def test_multiple_days_all_stored(self, db_conn):
        mid = add_member("Snapper", conn=db_conn)
        add_player_snapshot(mid, battle_power=100, captured_at="2025-01-01", conn=db_conn)
        add_player_snapshot(mid, battle_power=200, captured_at="2025-01-02", conn=db_conn)
        stats = get_player_stats(mid, conn=db_conn)
        assert len(stats) == 2

    def test_get_player_stats_ordered_newest_first(self, db_conn):
        mid = add_member("Snapper", conn=db_conn)
        add_player_snapshot(mid, battle_power=100, captured_at="2025-01-01", conn=db_conn)
        add_player_snapshot(mid, battle_power=200, captured_at="2025-01-02", conn=db_conn)
        stats = get_player_stats(mid, conn=db_conn)
        assert stats[0]["captured_at"] == "2025-01-02"

    def test_null_stats_not_overwrite_members_table(self, db_conn):
        mid = add_member("Snapper", conn=db_conn)
        add_player_snapshot(mid, battle_power=500, captured_at="2025-01-01", conn=db_conn)
        # Second snapshot has no battle_power — members.battle_power should stay 500
        add_player_snapshot(mid, kills=100, captured_at="2025-01-02", conn=db_conn)
        row = db_conn.execute("SELECT battle_power FROM members WHERE id = ?", (mid,)).fetchone()
        assert row["battle_power"] == 500

    def test_get_latest_stats_returns_most_recent(self, db_conn):
        mid = add_member("Snapper", conn=db_conn)
        add_player_snapshot(mid, battle_power=100, captured_at="2025-01-01", conn=db_conn)
        add_player_snapshot(mid, battle_power=200, captured_at="2025-01-03", conn=db_conn)
        latest = get_latest_stats(member_id=mid, conn=db_conn)
        assert len(latest) == 1
        assert latest[0]["battle_power"] == 200

    def test_get_latest_stats_all_members(self, db_conn):
        id1 = add_member("Alpha", conn=db_conn)
        id2 = add_member("Beta", conn=db_conn)
        add_player_snapshot(id1, battle_power=100, captured_at="2025-01-01", conn=db_conn)
        add_player_snapshot(id2, battle_power=200, captured_at="2025-01-01", conn=db_conn)
        latest = get_latest_stats(conn=db_conn)
        assert len(latest) == 2


# ── review queue ──────────────────────────────────────────────


class TestReviewQueue:
    def test_add_to_review_queue(self, db_conn):
        _add_to_review_queue(db_conn, "GhostName", "unknown_name", None, 0.0)
        row = db_conn.execute(
            "SELECT * FROM review_queue WHERE scraped_name = 'GhostName'"
        ).fetchone()
        assert row is not None
        assert row["reason"] == "unknown_name"
        assert row["resolved"] == 0

    def test_no_duplicate_entries(self, db_conn):
        _add_to_review_queue(db_conn, "GhostName", "unknown_name", None, 0.0)
        _add_to_review_queue(db_conn, "GhostName", "unknown_name", None, 0.0)
        count = db_conn.execute(
            "SELECT COUNT(*) AS cnt FROM review_queue WHERE scraped_name = 'GhostName'"
        ).fetchone()["cnt"]
        assert count == 1

    def test_suggested_member_id_stored(self, db_conn):
        mid = add_member("RealPlayer", conn=db_conn)
        _add_to_review_queue(db_conn, "Re4lPlayer", "fuzzy_match", mid, 0.88)
        row = db_conn.execute(
            "SELECT suggested_member_id, match_confidence FROM review_queue WHERE scraped_name = 'Re4lPlayer'"
        ).fetchone()
        assert row["suggested_member_id"] == mid
        assert abs(row["match_confidence"] - 0.88) < 0.001
