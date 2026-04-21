"""
aoo_summary.py — Refresh and display member performance summaries.

In DB mode (default): calls db.refresh_aggregations() and prints a summary
from the member_summary table.

In CSV mode (--csv): runs the original CSV-based computation and writes
aoo_member_summary.csv.

Usage:
    python aoo_summary.py              # DB mode
    python aoo_summary.py --csv        # CSV mode (legacy)
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import db


# ─── DB mode ────────────────────────────────────────────────────────────────


def run_db_summary(show_stats: bool = False):
    """Refresh aggregations and print a summary from the DB."""
    db.init_db()
    db.refresh_aggregations()

    conn = db.get_connection()
    try:
        # Event performance summary
        rows = conn.execute("""
            SELECT m.name, ms.events_participated, ms.events_available,
                   ms.attendance_rate, ms.avg_percentile, ms.avg_pct_vs_mean,
                   ms.trend, ms.last_active
            FROM member_summary ms
            JOIN members m ON m.id = ms.member_id
            WHERE ms.period = 'alltime' AND m.status = 'active'
            ORDER BY ms.avg_percentile DESC
        """).fetchall()

        if rows and not show_stats:
            print(f"\n{'#':>3s}  {'Name':<24s} {'Attended':>8s} {'Available':>9s} "
                  f"{'Att%':>5s} {'AvgPctl':>7s} {'VsMean%':>7s} {'Trend':<8s} {'Last Active':<11s}")
            print("-" * 95)
            for i, r in enumerate(rows, 1):
                pctl = f"{r['avg_percentile']:.1f}" if r['avg_percentile'] is not None else "-"
                vs_mean = f"{r['avg_pct_vs_mean']:.1f}" if r['avg_pct_vs_mean'] is not None else "-"
                print(f"{i:3d}  {r['name']:<24s} {r['events_participated']:>8d} "
                      f"{r['events_available']:>9d} {r['attendance_rate']:>5.1f} "
                      f"{pctl:>7s} {vs_mean:>7s} {r['trend'] or '-':<8s} "
                      f"{r['last_active'] or '-':<11s}")
            print(f"\nTotal: {len(rows)} active members")

        # Player stats summary
        stats = db.get_latest_stats(conn=conn)
        if stats:
            _print_player_stats(stats, conn)
        elif show_stats:
            print("No player stats available yet. Run roster capture or rankings scraper first.")

        if rows and not show_stats:
            print("✓ Aggregations refreshed")
    finally:
        conn.close()


def _print_player_stats(stats: list[dict], conn):
    """Print a formatted player stats table with growth deltas."""
    from datetime import timedelta

    print(f"\n  Player Stats (latest snapshots)")
    print(f"  {'Name':<24s} {'Power':>14s} {'CityLv':>6s} {'Kills':>14s} "
          f"{'Losses':>14s} {'Rep':>12s} {'Rank':<5s} {'Source':<10s} {'Date':<11s}")
    print("  " + "-" * 115)

    for s in stats:
        power = f"{s['power']:,}" if s['power'] else "-"
        city = str(s['city_level']) if s['city_level'] else "-"
        kills = f"{s['kills']:,}" if s['kills'] else "-"
        losses = f"{s['losses']:,}" if s['losses'] else "-"
        rep = f"{s['reputation']:,}" if s['reputation'] else "-"
        rank = s['rank'] or "-"
        print(f"  {s['name']:<24s} {power:>14s} {city:>6s} {kills:>14s} "
              f"{losses:>14s} {rep:>12s} {rank:<5s} {s['source']:<10s} {s['captured_at']:<11s}")

    # Growth summary (7d and 30d) for members with multiple snapshots
    cutoff_7d = (date.today() - timedelta(days=7)).isoformat()
    cutoff_30d = (date.today() - timedelta(days=30)).isoformat()

    growth_rows = conn.execute("""
        SELECT m.name,
               MAX(CASE WHEN ps.captured_at = latest.max_date THEN ps.power END) AS current_power,
               MAX(CASE WHEN ps.captured_at <= ? THEN ps.power END) AS power_7d_ago,
               MAX(CASE WHEN ps.captured_at <= ? THEN ps.power END) AS power_30d_ago
        FROM player_snapshots ps
        JOIN members m ON m.id = ps.member_id
        JOIN (SELECT member_id, MAX(captured_at) AS max_date FROM player_snapshots GROUP BY member_id) latest
            ON ps.member_id = latest.member_id
        WHERE m.status = 'active'
        GROUP BY ps.member_id
        HAVING power_7d_ago IS NOT NULL OR power_30d_ago IS NOT NULL
        ORDER BY current_power DESC
    """, (cutoff_7d, cutoff_30d)).fetchall()

    if growth_rows:
        has_growth = any(r['power_7d_ago'] or r['power_30d_ago'] for r in growth_rows)
        if has_growth:
            print(f"\n  Power Growth")
            print(f"  {'Name':<24s} {'Current':>14s} {'7d Δ':>12s} {'30d Δ':>12s}")
            print("  " + "-" * 65)
            for r in growth_rows:
                current = r['current_power']
                if not current:
                    continue
                d7 = f"{current - r['power_7d_ago']:+,}" if r['power_7d_ago'] else "-"
                d30 = f"{current - r['power_30d_ago']:+,}" if r['power_30d_ago'] else "-"
                print(f"  {r['name']:<24s} {current:>14,} {d7:>12s} {d30:>12s}")

    print(f"\n  Total: {len(stats)} players with stats")


# ─── CSV mode (original logic) ──────────────────────────────────────────────


def run_csv_summary():
    """Original CSV-based summary generation."""
    import numpy as np

    EVENT_LOG = "aoo_event_log.csv"
    ROSTER_CSV = "aoo_roster.csv"
    OUTPUT_CSV = "aoo_member_summary.csv"

    def parse_score(raw: str) -> float | None:
        raw = raw.strip().strip('"')
        if not raw:
            return None
        return float(raw.replace(",", ""))

    def parse_date(raw: str) -> datetime | None:
        raw = raw.strip()
        if not raw:
            return None
        for fmt in ("%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def load_event_log(path: str = EVENT_LOG) -> list[dict]:
        rows = []
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                name = r.get("Member Name", "").strip()
                event_type = r.get("Event Type", "").strip()
                if not name or not event_type:
                    continue
                rows.append(r)
        return rows

    def load_roster(path: str = ROSTER_CSV) -> dict[str, dict]:
        if not Path(path).exists():
            return {}
        roster = {}
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                name = r.get("Member Name", "").strip()
                if name:
                    roster[name] = r
        return roster

    event_rows = load_event_log()
    roster = load_roster()

    events = {}
    for r in event_rows:
        etype = r["Event Type"].strip()
        dt = parse_date(r["Event Date"])
        if dt is None:
            continue
        key = (etype, dt.strftime("%Y-%m-%d"))
        events.setdefault(key, []).append(r)

    event_keys = sorted(events.keys(), key=lambda k: k[1])
    total_events = len(event_keys)

    event_stats = {}
    for ekey, rows in events.items():
        scores = {}
        for r in rows:
            name = r["Member Name"].strip()
            attendance = r.get("Attendance", "").strip()
            if attendance == "Present":
                s = parse_score(r.get("Battle Score", ""))
                scores[name] = s

        score_vals = [v for v in scores.values() if v is not None and v > 0]
        mean_score = np.mean(score_vals) if score_vals else 0
        sorted_scores = sorted(score_vals)
        n = len(sorted_scores)

        stats = {}
        for name, s in scores.items():
            pct_of_mean = (s / mean_score * 100) if (s is not None and mean_score > 0) else None
            if s is not None and n > 1:
                rank_below = sum(1 for v in sorted_scores if v < s)
                percentile = rank_below / (n - 1) * 100
            else:
                percentile = None
            stats[name] = {"score": s, "pct_of_mean": pct_of_mean, "percentile": percentile}
        event_stats[ekey] = {"mean": mean_score, "members": stats}

    all_members = set()
    member_attendance = defaultdict(lambda: {"present": 0, "absent": 0})
    for r in event_rows:
        name = r["Member Name"].strip()
        dt = parse_date(r["Event Date"])
        if dt is None:
            continue
        all_members.add(name)
        attendance = r.get("Attendance", "").strip()
        if attendance == "Present":
            member_attendance[name]["present"] += 1
        elif attendance == "Absent":
            member_attendance[name]["absent"] += 1

    summary_rows = []
    for name in sorted(all_members):
        att = member_attendance[name]
        events_listed = att["present"] + att["absent"]
        events_attended = att["present"]
        attendance_pct = (events_attended / events_listed * 100) if events_listed else 0

        pct_of_means = []
        percentiles = []
        by_event_type = defaultdict(list)
        event_scores_chronological = []

        for ekey in event_keys:
            estats = event_stats[ekey]
            if name in estats["members"]:
                m = estats["members"][name]
                if m["pct_of_mean"] is not None:
                    pct_of_means.append(m["pct_of_mean"])
                    by_event_type[ekey[0]].append(m["pct_of_mean"])
                    event_scores_chronological.append((ekey[1], m["pct_of_mean"]))
                if m["percentile"] is not None:
                    percentiles.append(m["percentile"])

        avg_pct_of_mean = np.mean(pct_of_means) if pct_of_means else None
        avg_percentile = np.mean(percentiles) if percentiles else None

        best_event = worst_event = ""
        if by_event_type:
            type_avgs = {et: np.mean(vals) for et, vals in by_event_type.items()}
            best_event = max(type_avgs, key=type_avgs.get)
            worst_event = min(type_avgs, key=type_avgs.get)
            if best_event == worst_event:
                worst_event = ""

        trend = None
        if len(event_scores_chronological) >= 3:
            y = np.array([v for _, v in event_scores_chronological])
            x = np.arange(len(y), dtype=float)
            slope = np.polyfit(x, y, 1)[0]
            trend = slope

        consistency_cv = None
        if len(pct_of_means) >= 2:
            std = np.std(pct_of_means, ddof=1)
            mean_val = np.mean(pct_of_means)
            if mean_val > 0:
                consistency_cv = std / mean_val * 100

        rdata = roster.get(name, {})
        if not rdata:
            name_lower = name.lower()
            for rname, rd in roster.items():
                if rname.strip().lower().startswith(name_lower[:8]):
                    rdata = rd
                    break

        row = {
            "Member Name": name,
            "Position": rdata.get("Position", ""),
            "City Level": rdata.get("City Level", ""),
            "Battle Power": rdata.get("Battle Power", ""),
            "Events Listed": events_listed,
            "Events Attended": events_attended,
            "Attendance %": f"{attendance_pct:.0f}",
            "Avg % of Mean Score": f"{avg_pct_of_mean:.1f}" if avg_pct_of_mean is not None else "",
            "Avg Percentile": f"{avg_percentile:.1f}" if avg_percentile is not None else "",
            "Best Event Type": best_event,
            "Worst Event Type": worst_event,
            "Trend (slope)": f"{trend:+.1f}" if trend is not None else "",
            "Consistency (CV%)": f"{consistency_cv:.1f}" if consistency_cv is not None else "",
        }
        summary_rows.append(row)

    summary_rows.sort(
        key=lambda r: float(r["Avg Percentile"]) if r["Avg Percentile"] else -1,
        reverse=True,
    )

    headers = list(summary_rows[0].keys()) if summary_rows else []
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"Wrote {len(summary_rows)} members to {OUTPUT_CSV}")
    print(f"  Events: {total_events} distinct events across {len(set(k[0] for k in event_keys))} types")
    print(f"  Date range: {event_keys[0][1]} to {event_keys[-1][1]}")

    print(f"\nTop 10 by Avg Percentile:")
    print(f"  {'Name':<24s} {'Att%':>4s} {'Avg%Mean':>8s} {'AvgPctl':>7s} {'Trend':>6s} {'CV%':>5s}")
    print(f"  {'-'*60}")
    for r in summary_rows[:10]:
        print(f"  {r['Member Name']:<24s} {r['Attendance %']:>4s} "
              f"{r['Avg % of Mean Score']:>8s} {r['Avg Percentile']:>7s} "
              f"{r['Trend (slope)']:>6s} {r['Consistency (CV%)']:>5s}")


# ─── Entry point ────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="AoO Member Summary")
    parser.add_argument("--csv", action="store_true",
                        help="Generate summary CSV from event log (legacy mode)")
    parser.add_argument("--stats", action="store_true",
                        help="Show player stats only (power, kills, etc.)")
    args = parser.parse_args()

    if args.csv:
        run_csv_summary()
    else:
        run_db_summary(show_stats=args.stats)


if __name__ == "__main__":
    main()
