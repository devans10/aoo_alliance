"""
Tests for the event ingestion pipeline in shared/db.py.

Covers: add_event, add_event_result, finalise_event, refresh_aggregations,
_refresh_member_summaries, _refresh_member_event_stats, _refresh_alliance_summary,
and _calculate_trend.

All tests use an in-memory database patched via monkeypatch so that
get_connection() returns our isolated fixture connection.
"""

import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shared.db as db_module
from shared.db import (
    add_member,
    add_event,
    add_event_result,
    finalise_event,
    refresh_aggregations,
    _refresh_member_summaries,
    _refresh_member_event_stats,
    _refresh_alliance_summary,
    _calculate_trend,
)


# ── Helpers ──────────────────────────────────────────────────

def _conn_factory(conn):
    """Return a context manager that yields an existing connection (no close)."""
    @contextmanager
    def _cm():
        yield conn
    return _cm


def _patch_get_connection(monkeypatch, conn):
    """
    Redirect db_module.get_connection() to return our in-memory connection.
    Also patches the context-manager form used by finalise_event /
    refresh_aggregations (``with get_connection() as conn``).
    """
    @contextmanager
    def _fake_ctx():
        yield conn

    # get_connection() used as plain call (add_event etc.)
    monkeypatch.setattr(db_module, "get_connection", lambda: conn)

    # The `with get_connection() as conn:` pattern used in finalise_event /
    # refresh_aggregations needs a context manager object.
    # Patch the module-level name so both call sites resolve to the same thing.
    class _FakeConn:
        """Proxy that behaves both as a connection and as a context manager."""
        def __enter__(self_inner):
            return conn
        def __exit__(self_inner, *a):
            conn.commit()
            return False
        def __getattr__(self_inner, name):
            return getattr(conn, name)

    monkeypatch.setattr(db_module, "get_connection", lambda: _FakeConn())


# ── add_event ─────────────────────────────────────────────────


class TestAddEvent:
    def test_returns_event_id(self, db_conn):
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        assert isinstance(eid, int) and eid > 0

    def test_event_row_created(self, db_conn):
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        row = db_conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
        assert row["event_type"] == "Elite War"
        assert row["occurred_at"] == "2025-04-01"

    def test_multiple_events_distinct_ids(self, db_conn):
        e1 = add_event("Elite War", "2025-04-01", conn=db_conn)
        e2 = add_event("Void War", "2025-04-02", conn=db_conn)
        assert e1 != e2


# ── add_event_result ──────────────────────────────────────────


class TestAddEventResult:
    def test_result_stored(self, db_conn):
        mid = add_member("Player1", conn=db_conn)
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        add_event_result(mid, eid, score=1_000_000, conn=db_conn)
        row = db_conn.execute(
            "SELECT * FROM event_results WHERE member_id = ? AND event_id = ?", (mid, eid)
        ).fetchone()
        assert row is not None
        assert row["score"] == 1_000_000
        assert row["participated"] == 1

    def test_upsert_replaces_existing(self, db_conn):
        mid = add_member("Player1", conn=db_conn)
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        add_event_result(mid, eid, score=100, conn=db_conn)
        add_event_result(mid, eid, score=200, conn=db_conn)
        count = db_conn.execute("SELECT COUNT(*) AS cnt FROM event_results").fetchone()["cnt"]
        assert count == 1
        score = db_conn.execute("SELECT score FROM event_results").fetchone()["score"]
        assert score == 200


# ── finalise_event ────────────────────────────────────────────


class TestFinaliseEvent:
    def _setup_event(self, db_conn, scores: list[int]) -> int:
        """Add members and results for one event, return event_id."""
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        for i, score in enumerate(scores):
            mid = add_member(f"Player{i}", conn=db_conn)
            add_event_result(mid, eid, score=score, conn=db_conn)
        return eid

    def test_percentile_top_player_is_100(self, db_conn, monkeypatch):
        _patch_get_connection(monkeypatch, db_conn)
        eid = self._setup_event(db_conn, [1000, 500, 100])
        finalise_event(eid)
        top = db_conn.execute(
            "SELECT percentile FROM event_results WHERE score = 1000"
        ).fetchone()
        assert top["percentile"] == 100.0

    def test_percentile_bottom_player_is_0(self, db_conn, monkeypatch):
        _patch_get_connection(monkeypatch, db_conn)
        eid = self._setup_event(db_conn, [1000, 500, 100])
        finalise_event(eid)
        bottom = db_conn.execute(
            "SELECT percentile FROM event_results WHERE score = 100"
        ).fetchone()
        assert bottom["percentile"] == 0.0

    def test_percentile_middle_player(self, db_conn, monkeypatch):
        _patch_get_connection(monkeypatch, db_conn)
        eid = self._setup_event(db_conn, [1000, 500, 100])
        finalise_event(eid)
        mid_row = db_conn.execute(
            "SELECT percentile FROM event_results WHERE score = 500"
        ).fetchone()
        assert mid_row["percentile"] == 50.0

    def test_pct_vs_mean_above_average(self, db_conn, monkeypatch):
        _patch_get_connection(monkeypatch, db_conn)
        eid = self._setup_event(db_conn, [900, 100])
        finalise_event(eid)
        # mean = 500; top scorer: (900-500)/500*100 = 80%
        top = db_conn.execute(
            "SELECT pct_vs_mean FROM event_results WHERE score = 900"
        ).fetchone()
        assert abs(top["pct_vs_mean"] - 80.0) < 0.5

    def test_pct_vs_mean_below_average(self, db_conn, monkeypatch):
        _patch_get_connection(monkeypatch, db_conn)
        eid = self._setup_event(db_conn, [900, 100])
        finalise_event(eid)
        # mean = 500; bottom scorer: (100-500)/500*100 = -80%
        bottom = db_conn.execute(
            "SELECT pct_vs_mean FROM event_results WHERE score = 100"
        ).fetchone()
        assert abs(bottom["pct_vs_mean"] - (-80.0)) < 0.5

    def test_single_participant_gets_100_percentile(self, db_conn, monkeypatch):
        _patch_get_connection(monkeypatch, db_conn)
        eid = self._setup_event(db_conn, [500])
        finalise_event(eid)
        row = db_conn.execute(
            "SELECT percentile, pct_vs_mean FROM event_results"
        ).fetchone()
        assert row["percentile"] == 100.0
        assert row["pct_vs_mean"] == 0.0

    def test_empty_event_does_not_crash(self, db_conn, monkeypatch):
        _patch_get_connection(monkeypatch, db_conn)
        eid = add_event("Void War", "2025-04-02", conn=db_conn)
        finalise_event(eid)  # no results — should be a no-op


# ── _refresh_member_summaries ─────────────────────────────────


class TestRefreshMemberSummaries:
    def test_zero_attendance_for_non_participant(self, db_conn):
        mid = add_member("Lurker", conn=db_conn)
        # Create an event but don't add a result for this member
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        other = add_member("Active", conn=db_conn)
        add_event_result(other, eid, score=500, conn=db_conn)

        _refresh_member_summaries(db_conn)
        row = db_conn.execute(
            "SELECT attendance_rate FROM member_summary WHERE member_id = ? AND period = 'alltime'",
            (mid,)
        ).fetchone()
        assert row is not None
        assert row["attendance_rate"] == 0

    def test_full_attendance_rate_for_participant(self, db_conn):
        mid = add_member("Dedicated", conn=db_conn)
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        add_event_result(mid, eid, score=1000, conn=db_conn)

        _refresh_member_summaries(db_conn)
        row = db_conn.execute(
            "SELECT attendance_rate FROM member_summary WHERE member_id = ? AND period = 'alltime'",
            (mid,)
        ).fetchone()
        assert row["attendance_rate"] == 100.0

    def test_partial_attendance_rate(self, db_conn):
        mid = add_member("Occasional", conn=db_conn)
        e1 = add_event("Elite War", "2025-03-01", conn=db_conn)
        e2 = add_event("Void War", "2025-03-15", conn=db_conn)
        add_event_result(mid, e1, score=500, conn=db_conn)
        # Member skipped e2

        _refresh_member_summaries(db_conn)
        row = db_conn.execute(
            "SELECT attendance_rate FROM member_summary WHERE member_id = ? AND period = 'alltime'",
            (mid,)
        ).fetchone()
        # Participated in 1 of 2 events since their first result
        assert row["attendance_rate"] == 50.0

    def test_all_periods_written(self, db_conn):
        mid = add_member("AllPeriods", conn=db_conn)
        _refresh_member_summaries(db_conn)
        rows = db_conn.execute(
            "SELECT period FROM member_summary WHERE member_id = ?", (mid,)
        ).fetchall()
        periods = {r["period"] for r in rows}
        assert periods == {"alltime", "30d", "90d"}


# ── _calculate_trend ──────────────────────────────────────────


class TestCalculateTrend:
    def _add_results_at(self, db_conn, member_id, event_type, date_str, percentile):
        """Insert an already-finalised event result with a known percentile."""
        eid = db_conn.execute(
            "INSERT INTO events (event_type, occurred_at) VALUES (?, ?) RETURNING id",
            (event_type, date_str)
        ).fetchone()["id"]
        db_conn.execute("""
            INSERT INTO event_results (member_id, event_id, score, participated, percentile)
            VALUES (?, ?, 1000, 1, ?)
        """, (member_id, eid, percentile))
        db_conn.commit()

    def test_insufficient_data_no_results(self, db_conn):
        mid = add_member("Ghost", conn=db_conn)
        trend = _calculate_trend(db_conn, mid)
        assert trend == "insufficient_data"

    def test_trend_up(self, db_conn):
        mid = add_member("Improver", conn=db_conn)
        # Old window (45-90 days ago): low percentiles
        self._add_results_at(db_conn, mid, "Elite War",
                              (date.today() - timedelta(days=60)).isoformat(), 20.0)
        # Recent window (last 45 days): high percentiles
        self._add_results_at(db_conn, mid, "Void War",
                              (date.today() - timedelta(days=10)).isoformat(), 80.0)
        trend = _calculate_trend(db_conn, mid)
        assert trend == "up"

    def test_trend_down(self, db_conn):
        mid = add_member("Decliner", conn=db_conn)
        self._add_results_at(db_conn, mid, "Elite War",
                              (date.today() - timedelta(days=60)).isoformat(), 80.0)
        self._add_results_at(db_conn, mid, "Void War",
                              (date.today() - timedelta(days=10)).isoformat(), 20.0)
        trend = _calculate_trend(db_conn, mid)
        assert trend == "down"

    def test_trend_stable(self, db_conn):
        mid = add_member("Consistent", conn=db_conn)
        self._add_results_at(db_conn, mid, "Elite War",
                              (date.today() - timedelta(days=60)).isoformat(), 50.0)
        self._add_results_at(db_conn, mid, "Void War",
                              (date.today() - timedelta(days=10)).isoformat(), 52.0)
        trend = _calculate_trend(db_conn, mid)
        assert trend == "stable"


# ── _refresh_member_event_stats ───────────────────────────────


class TestRefreshMemberEventStats:
    def test_stats_written_per_event_type(self, db_conn):
        mid = add_member("Player", conn=db_conn)
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        db_conn.execute("""
            INSERT INTO event_results (member_id, event_id, score, participated, percentile)
            VALUES (?, ?, 500, 1, 75.0)
        """, (mid, eid))
        db_conn.commit()

        _refresh_member_event_stats(db_conn)
        row = db_conn.execute("""
            SELECT * FROM member_event_stats WHERE member_id = ? AND event_type = 'Elite War'
        """, (mid,)).fetchone()
        assert row is not None
        assert row["events_participated"] == 1
        assert row["avg_percentile"] == 75.0

    def test_participation_rate_calculated(self, db_conn):
        mid = add_member("Player", conn=db_conn)
        e1 = add_event("Elite War", "2025-03-01", conn=db_conn)
        e2 = add_event("Elite War", "2025-04-01", conn=db_conn)
        db_conn.execute("""
            INSERT INTO event_results (member_id, event_id, score, participated, percentile)
            VALUES (?, ?, 500, 1, 50.0)
        """, (mid, e1))
        # Member skipped e2
        db_conn.commit()

        _refresh_member_event_stats(db_conn)
        row = db_conn.execute("""
            SELECT participation_rate FROM member_event_stats
            WHERE member_id = ? AND event_type = 'Elite War'
        """, (mid,)).fetchone()
        assert row["participation_rate"] == 50.0

    def test_stats_cleared_and_rebuilt(self, db_conn):
        mid = add_member("Player", conn=db_conn)
        eid = add_event("Elite War", "2025-04-01", conn=db_conn)
        db_conn.execute("""
            INSERT INTO event_results (member_id, event_id, score, participated, percentile)
            VALUES (?, ?, 500, 1, 50.0)
        """, (mid, eid))
        db_conn.commit()

        _refresh_member_event_stats(db_conn)
        _refresh_member_event_stats(db_conn)  # run twice — no duplicate rows
        count = db_conn.execute(
            "SELECT COUNT(*) AS cnt FROM member_event_stats WHERE member_id = ?", (mid,)
        ).fetchone()["cnt"]
        assert count == 1


# ── _refresh_alliance_summary ─────────────────────────────────


class TestRefreshAllianceSummary:
    def test_total_and_active_counts(self, db_conn):
        add_member("Active1", conn=db_conn)
        mid2 = add_member("Inactive1", conn=db_conn)
        db_conn.execute("UPDATE members SET status = 'inactive' WHERE id = ?", (mid2,))
        db_conn.commit()

        # Need member_summary to exist for attendance calc
        _refresh_member_summaries(db_conn)
        _refresh_alliance_summary(db_conn)

        row = db_conn.execute("SELECT * FROM alliance_summary WHERE id = 1").fetchone()
        assert row["total_members"] == 2
        assert row["active_members"] == 1

    def test_summary_row_upserts(self, db_conn):
        add_member("Alpha", conn=db_conn)
        _refresh_member_summaries(db_conn)
        _refresh_alliance_summary(db_conn)
        _refresh_alliance_summary(db_conn)
        count = db_conn.execute("SELECT COUNT(*) AS cnt FROM alliance_summary").fetchone()["cnt"]
        assert count == 1
