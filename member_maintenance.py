"""
member_maintenance.py — Interactive alliance member maintenance.

Functions:
  - Rename a member (updates current name, retires old name to history)
  - Add an alias to a member (for OCR matching, without changing current name)
  - Merge duplicate members (move all data to one, keep variant name as alias)

Usage:
    ./venv/bin/python member_maintenance.py
"""

import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared import db


def search_members(conn, query: str) -> list[dict]:
    """Search members by name (partial, case-insensitive)."""
    rows = conn.execute("""
        SELECT DISTINCT m.id, m.name, m.status
        FROM members m
        LEFT JOIN member_names mn ON mn.member_id = m.id
        WHERE m.name LIKE ? OR mn.name LIKE ? OR mn.clean_name LIKE ?
        ORDER BY m.name
    """, (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()
    return [dict(r) for r in rows]


def show_member_detail(conn, member_id: int):
    """Display member info and all known names."""
    member = conn.execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    if not member:
        print(f"  Member id={member_id} not found.")
        return

    print(f"\n  ID:     {member['id']}")
    print(f"  Name:   {member['name']}")
    print(f"  Status: {member['status']}")

    names = conn.execute("""
        SELECT name, clean_name, is_current, from_date, to_date
        FROM member_names WHERE member_id = ?
        ORDER BY is_current DESC, from_date DESC
    """, (member_id,)).fetchall()

    if names:
        print("  Names:")
        for n in names:
            current = " (current)" if n["is_current"] else ""
            dates = f"from {n['from_date']}"
            if n["to_date"]:
                dates += f" to {n['to_date']}"
            print(f"    - {n['name']} [{n['clean_name']}] {dates}{current}")
    print()


def pick_member(conn, prompt_text: str = "Search for member") -> int | None:
    """Interactive member search and selection. Returns member_id or None."""
    while True:
        query = input(f"  {prompt_text} (name or partial, 'b' to go back): ").strip()
        if query.lower() == 'b':
            return None
        if not query:
            continue

        results = search_members(conn, query)
        if not results:
            print("  No members found.\n")
            continue

        if len(results) == 1:
            m = results[0]
            print(f"  Found: [{m['id']}] {m['name']} ({m['status']})")
            confirm = input("  Use this member? (Y/n): ").strip().lower()
            if confirm in ('', 'y', 'yes'):
                return m['id']
            continue

        print(f"  Found {len(results)} members:")
        for i, m in enumerate(results, 1):
            print(f"    {i}. [{m['id']}] {m['name']} ({m['status']})")

        choice = input("  Select number (or Enter to search again): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(results):
            return results[int(choice) - 1]['id']


def do_rename(conn):
    """Rename a member — changes their current display name."""
    print("\n=== Rename Member ===\n")

    member_id = pick_member(conn)
    if member_id is None:
        return

    show_member_detail(conn, member_id)

    new_name = input("  Enter new name: ").strip()
    if not new_name:
        print("  Cancelled — no name entered.\n")
        return

    # Check if name is already taken
    existing = conn.execute(
        "SELECT id FROM member_names WHERE name = ?", (new_name,)
    ).fetchone()
    if existing:
        print(f"  Error: The name '{new_name}' is already in use.\n")
        return

    confirm = input(f"  Rename to '{new_name}'? (y/N): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("  Cancelled.\n")
        return

    db.rename_member(member_id, new_name, conn=conn)
    print(f"  Done — member renamed to '{new_name}'.\n")
    show_member_detail(conn, member_id)


def do_add_alias(conn):
    """Add an alias (alternative name) for OCR matching."""
    print("\n=== Add Alias ===\n")

    member_id = pick_member(conn)
    if member_id is None:
        return

    show_member_detail(conn, member_id)

    alias = input("  Enter alias to add: ").strip()
    if not alias:
        print("  Cancelled — no alias entered.\n")
        return

    # Check if alias is already taken
    existing = conn.execute(
        "SELECT mn.member_id, m.name FROM member_names mn "
        "JOIN members m ON m.id = mn.member_id "
        "WHERE mn.name = ?", (alias,)
    ).fetchone()
    if existing:
        print(f"  Error: '{alias}' is already assigned to "
              f"[{existing['member_id']}] {existing['name']}.\n")
        return

    clean = db._to_clean_name(alias)
    today = date.today().isoformat()

    confirm = input(f"  Add alias '{alias}' (clean: '{clean}')? (y/N): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("  Cancelled.\n")
        return

    conn.execute("""
        INSERT INTO member_names (member_id, name, clean_name, from_date, is_current)
        VALUES (?, ?, ?, ?, 0)
    """, (member_id, alias, clean, today))
    conn.commit()

    print(f"  Done — alias '{alias}' added.\n")
    show_member_detail(conn, member_id)


def do_remove_alias(conn):
    """Remove an alias (non-current name) from a member."""
    print("\n=== Remove Alias ===\n")

    member_id = pick_member(conn)
    if member_id is None:
        return

    show_member_detail(conn, member_id)

    # Get non-current names (aliases) for this member
    aliases = conn.execute("""
        SELECT id, name, clean_name, from_date
        FROM member_names
        WHERE member_id = ? AND is_current = 0
        ORDER BY from_date
    """, (member_id,)).fetchall()

    if not aliases:
        print("  This member has no aliases to remove.\n")
        return

    print("  Aliases:")
    for i, a in enumerate(aliases, 1):
        print(f"    {i}. {a['name']} [{a['clean_name']}] from {a['from_date']}")

    choice = input("\n  Select alias number to remove (or Enter to cancel): ").strip()
    if not choice.isdigit() or not (1 <= int(choice) <= len(aliases)):
        print("  Cancelled.\n")
        return

    alias = aliases[int(choice) - 1]
    confirm = input(f"  Remove alias '{alias['name']}'? (y/N): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("  Cancelled.\n")
        return

    conn.execute("DELETE FROM member_names WHERE id = ?", (alias['id'],))
    conn.commit()
    print(f"  Done — alias '{alias['name']}' removed.\n")
    show_member_detail(conn, member_id)


def do_merge(conn):
    """Merge a duplicate member into the original, keeping all data."""
    print("\n=== Merge Duplicate Members ===\n")

    print("  First, select the member to KEEP (the original).")
    keep_id = pick_member(conn, "Search for member to KEEP")
    if keep_id is None:
        return
    show_member_detail(conn, keep_id)

    print("  Now, select the DUPLICATE to merge into the kept member.")
    dup_id = pick_member(conn, "Search for DUPLICATE to merge away")
    if dup_id is None:
        return

    if dup_id == keep_id:
        print("  Error: Can't merge a member into itself.\n")
        return

    show_member_detail(conn, dup_id)

    # Show what will be moved
    counts = {}
    for table, col in [
        ("event_results", "member_id"),
        ("player_snapshots", "member_id"),
        ("ranking_entries", "member_id"),
        ("alliance_history", "member_id"),
    ]:
        row = conn.execute(
            f"SELECT count(*) as c FROM {table} WHERE {col} = ?", (dup_id,)
        ).fetchone()
        counts[table] = row["c"]

    dup_names = conn.execute(
        "SELECT name FROM member_names WHERE member_id = ?", (dup_id,)
    ).fetchall()
    dup_name_list = [r["name"] for r in dup_names]

    keep_name = conn.execute(
        "SELECT name FROM members WHERE id = ?", (keep_id,)
    ).fetchone()["name"]
    dup_name = conn.execute(
        "SELECT name FROM members WHERE id = ?", (dup_id,)
    ).fetchone()["name"]

    print(f"  Will merge [{dup_id}] {dup_name} → [{keep_id}] {keep_name}:")
    print(f"    Event results:    {counts['event_results']}")
    print(f"    Player snapshots: {counts['player_snapshots']}")
    print(f"    Ranking entries:  {counts['ranking_entries']}")
    print(f"    History records:  {counts['alliance_history']}")
    print(f"    Names to add as aliases: {dup_name_list}")

    confirm = input("\n  Proceed with merge? (y/N): ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("  Cancelled.\n")
        return

    today = date.today().isoformat()

    # Move event_results — skip any that would violate UNIQUE(member_id, event_id)
    conn.execute("""
        UPDATE OR IGNORE event_results SET member_id = ? WHERE member_id = ?
    """, (keep_id, dup_id))
    conn.execute("DELETE FROM event_results WHERE member_id = ?", (dup_id,))

    # Move player_snapshots — skip any that would violate UNIQUE(member_id, captured_at, source)
    conn.execute("""
        UPDATE OR IGNORE player_snapshots SET member_id = ? WHERE member_id = ?
    """, (keep_id, dup_id))
    conn.execute("DELETE FROM player_snapshots WHERE member_id = ?", (dup_id,))

    # Move ranking_entries
    conn.execute("""
        UPDATE ranking_entries SET member_id = ? WHERE member_id = ?
    """, (keep_id, dup_id))

    # Move alliance_history
    conn.execute("""
        UPDATE alliance_history SET member_id = ? WHERE member_id = ?
    """, (keep_id, dup_id))

    # Move member_summary
    conn.execute("DELETE FROM member_summary WHERE member_id = ?", (dup_id,))

    # Move member_event_stats
    conn.execute("DELETE FROM member_event_stats WHERE member_id = ?", (dup_id,))

    # Add duplicate's names as aliases on the kept member (skip if already exists)
    for name_row in dup_names:
        existing = conn.execute(
            "SELECT id FROM member_names WHERE name = ? AND member_id = ?",
            (name_row["name"], keep_id)
        ).fetchone()
        if not existing:
            clean = db._to_clean_name(name_row["name"])
            conn.execute("""
                INSERT OR IGNORE INTO member_names
                    (member_id, name, clean_name, from_date, is_current)
                VALUES (?, ?, ?, ?, 0)
            """, (keep_id, name_row["name"], clean, today))

    # Re-point any review_queue FK references from the duplicate to the kept member
    conn.execute("""
        UPDATE review_queue SET suggested_member_id = ?
        WHERE suggested_member_id = ?
    """, (keep_id, dup_id))
    conn.execute("""
        UPDATE review_queue SET resolved_member_id = ?
        WHERE resolved_member_id = ?
    """, (keep_id, dup_id))

    # Remove duplicate's name entries and the member record
    conn.execute("DELETE FROM member_names WHERE member_id = ?", (dup_id,))
    conn.execute("DELETE FROM members WHERE id = ?", (dup_id,))

    # Resolve any review queue items for the duplicate's names
    for name_row in dup_names:
        conn.execute("""
            UPDATE review_queue
            SET resolved = 1, resolution = 'duplicate',
                resolved_member_id = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE scraped_name = ? AND resolved = 0
        """, (keep_id, name_row["name"]))

    # Log the merge
    conn.execute("""
        INSERT INTO alliance_history (member_id, event, detail, occurred_at, noted_by)
        VALUES (?, 'merge', ?, ?, 'manual')
    """, (keep_id, f"Merged [{dup_id}] {dup_name} → [{keep_id}] {keep_name}", today))

    conn.commit()
    print(f"\n  Done — [{dup_id}] {dup_name} merged into [{keep_id}] {keep_name}.")
    print(f"  Variant name(s) added as aliases for future OCR matching.\n")
    show_member_detail(conn, keep_id)


def do_review_queue(conn):
    """Show and resolve pending review queue items."""
    print("\n=== Review Queue ===\n")

    rows = conn.execute("""
        SELECT rq.id, rq.scraped_name, rq.reason, rq.match_confidence,
               rq.suggested_member_id, m.name AS suggested_name
        FROM review_queue rq
        LEFT JOIN members m ON m.id = rq.suggested_member_id
        WHERE rq.resolved = 0
        ORDER BY rq.created_at
    """).fetchall()
    # Strip null characters that OCR may introduce — they break input() prompts
    items = []
    for row in rows:
        item = dict(row)
        item["scraped_name"] = item["scraped_name"].replace("\x00", "")
        items.append(item)

    if not items:
        print("  No pending review items.\n")
        return

    print(f"  {len(items)} pending item(s):\n")
    for item in items:
        suggestion = ""
        if item["suggested_name"]:
            suggestion = f" → suggested: [{item['suggested_member_id']}] {item['suggested_name']} ({item['match_confidence']:.0%})"
        print(f"    [{item['id']}] '{item['scraped_name']}' — {item['reason']}{suggestion}")

    print()
    print("  For each item, choose:")
    print("    m = merge (this is a known member, add as alias)")
    print("    n = new member")
    print("    i = ignore")
    print("    s = skip")
    print()

    for item in items:
        print(f"  --- '{item['scraped_name']}' ({item['reason']}) ---")
        action = input("    Action (m/n/i/s): ").strip().lower()

        if action == 's':
            continue
        elif action == 'i':
            conn.execute("""
                UPDATE review_queue
                SET resolved = 1, resolution = 'ignore', resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (item["id"],))
            conn.commit()
            print("    Ignored.\n")
        elif action == 'n':
            db.resolve_review_item(item["id"], "new_member")
            print(f"    Created new member '{item['scraped_name']}'.\n")
        elif action == 'm':
            member_id = pick_member(conn, "Search for the real member")
            if member_id is None:
                continue
            show_member_detail(conn, member_id)
            confirm = input(f"    Add '{item['scraped_name']}' as alias for this member? (y/N): ").strip().lower()
            if confirm in ('y', 'yes'):
                # Add as alias
                clean = db._to_clean_name(item["scraped_name"])
                today = date.today().isoformat()
                conn.execute("""
                    INSERT OR IGNORE INTO member_names
                        (member_id, name, clean_name, from_date, is_current)
                    VALUES (?, ?, ?, ?, 0)
                """, (member_id, item["scraped_name"], clean, today))
                conn.execute("""
                    UPDATE review_queue
                    SET resolved = 1, resolution = 'duplicate',
                        resolved_member_id = ?, resolved_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (member_id, item["id"]))
                conn.commit()
                print(f"    Alias added and review item resolved.\n")


def main_menu():
    """Main interactive loop."""
    db.init_db()
    conn = db.get_connection()

    print("\n╔══════════════════════════════════╗")
    print("║   Alliance Member Maintenance    ║")
    print("╚══════════════════════════════════╝\n")

    try:
        while True:
            print("  1. Rename member")
            print("  2. Add alias")
            print("  3. Remove alias")
            print("  4. Merge duplicates")
            print("  5. Review queue")
            print("  6. Look up member")
            print("  7. Refresh aggregations")
            print("  q. Quit")
            choice = input("\n  Choice: ").strip().lower()

            if choice == '1':
                do_rename(conn)
            elif choice == '2':
                do_add_alias(conn)
            elif choice == '3':
                do_remove_alias(conn)
            elif choice == '4':
                do_merge(conn)
            elif choice == '5':
                do_review_queue(conn)
            elif choice == '6':
                member_id = pick_member(conn)
                if member_id:
                    show_member_detail(conn, member_id)
            elif choice == '7':
                print("\n  Refreshing aggregations...")
                db.refresh_aggregations()
                print("  Done.\n")
            elif choice in ('q', 'quit', 'exit'):
                print("  Bye.\n")
                break
            else:
                print("  Invalid choice.\n")
    except (KeyboardInterrupt, EOFError):
        print("\n  Bye.\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main_menu()
