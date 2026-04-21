"""
export_csv.py — Export alliance DB to CSV files for Google Sheets import.

Produces two CSV files in the exports/ directory:
  - roster.csv      — Current member roster with latest stats
  - event_log.csv   — All event results (one row per member per event)

Usage:
    ./venv/bin/python export_csv.py              # export both
    ./venv/bin/python export_csv.py --roster     # roster only
    ./venv/bin/python export_csv.py --events     # event log only
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared import db

EXPORT_DIR = Path(__file__).resolve().parent / "exports"


def export_roster(out_path: Path):
    """Export current roster with latest stats."""
    conn = db.get_connection()
    try:
        rows = conn.execute("""
            SELECT
                m.id,
                m.name,
                m.status,
                m.position,
                m.city_level       AS "City Level",
                m.battle_power     AS "Battle Power",
                m.reputation       AS "Reputation",
                m.kills            AS "Kills",
                m.losses           AS "Losses",
                m.officer_power    AS "Officer Power",
                m.titan_power      AS "Titan Power",
                m.warplane_power   AS "Warplane Power",
                m.island_lux       AS "Island Lux",
                m.antique_power    AS "Antique Power",
                m.merit            AS "Merit",
                m.commander_level  AS "Commander Level",
                m.joined_date      AS "Joined",
                m.stats_updated_at AS "Stats Updated",
                ms.events_participated AS "Events (30d)",
                ms.events_available    AS "Available (30d)",
                ms.attendance_rate     AS "Attendance % (30d)",
                ms.avg_percentile      AS "Avg Percentile (30d)",
                ms.trend               AS "Trend"
            FROM members m
            LEFT JOIN member_summary ms
                ON ms.member_id = m.id AND ms.period = '30d'
            ORDER BY
                CASE m.status
                    WHEN 'active' THEN 0
                    WHEN 'inactive' THEN 1
                    ELSE 2
                END,
                m.name COLLATE NOCASE
        """).fetchall()

        headers = [
            "ID", "Name", "Status", "Position",
            "City Level", "Battle Power", "Reputation",
            "Kills", "Losses",
            "Officer Power", "Titan Power", "Warplane Power",
            "Island Lux", "Antique Power", "Merit", "Commander Level",
            "Joined", "Stats Updated",
            "Events (30d)", "Available (30d)", "Attendance % (30d)",
            "Avg Percentile (30d)", "Trend",
        ]

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in rows:
                writer.writerow([
                    r["id"], r["name"], r["status"], r["position"],
                    r["City Level"], r["Battle Power"], r["Reputation"],
                    r["Kills"], r["Losses"],
                    r["Officer Power"], r["Titan Power"], r["Warplane Power"],
                    r["Island Lux"], r["Antique Power"], r["Merit"],
                    r["Commander Level"],
                    r["Joined"], r["Stats Updated"],
                    r["Events (30d)"], r["Available (30d)"],
                    r["Attendance % (30d)"], r["Avg Percentile (30d)"],
                    r["Trend"],
                ])

        print(f"  Roster: {len(rows)} members → {out_path}")
    finally:
        conn.close()


def export_event_log(out_path: Path):
    """Export all event results as a flat table."""
    conn = db.get_connection()
    try:
        rows = conn.execute("""
            SELECT
                m.name              AS "Member Name",
                e.event_type        AS "Event Type",
                e.occurred_at       AS "Event Date",
                er.score            AS "Score",
                er.percentile       AS "Percentile",
                er.pct_vs_mean      AS "% vs Mean",
                er.source           AS "Source"
            FROM event_results er
            JOIN members m ON m.id = er.member_id
            JOIN events e ON e.id = er.event_id
            ORDER BY e.occurred_at, e.event_type, er.score DESC
        """).fetchall()

        headers = [
            "Member Name", "Event Type", "Event Date",
            "Score", "Percentile", "% vs Mean", "Source",
        ]

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in rows:
                writer.writerow([
                    r["Member Name"],
                    r["Event Type"],
                    r["Event Date"],
                    r["Score"],
                    r["Percentile"],
                    r["% vs Mean"],
                    r["Source"],
                ])

        print(f"  Event Log: {len(rows)} results → {out_path}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Export alliance DB to CSV for Google Sheets")
    parser.add_argument("--roster", action="store_true", help="Export roster only")
    parser.add_argument("--events", action="store_true", help="Export event log only")
    parser.add_argument("--out-dir", type=str, default=str(EXPORT_DIR),
                        help=f"Output directory (default: {EXPORT_DIR})")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    export_both = not args.roster and not args.events

    db.init_db()

    print()
    if export_both or args.roster:
        export_roster(out_dir / "roster.csv")
    if export_both or args.events:
        export_event_log(out_dir / "event_log.csv")
    print()


if __name__ == "__main__":
    main()
