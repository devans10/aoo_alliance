# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

This project is a toolkit for the game "Age of Origins" (AoO) to track alliance members, capture player stats via OCR, and provide analytics through a Discord bot.

## Key Components

- `ocr/aoo_roster.py` — Interactive roster builder. Captures player profiles via clipboard + OCR, stores to SQLite DB. Supports finalizing sessions to mark unseen members inactive.
- `ocr/aoo_scraper.py` — Event score scraper (Elite War, etc.) via OCR.
- `ocr/aoo_rankings.py` — Leaderboard/rankings scraper.
- `shared/db.py` — SQLite database layer (member management, event ingestion, aggregation).
- `shared/db_schema.sql` — Database schema definition.
- `bot/bot_v2.py` — Discord bot for querying alliance stats.

## Member Identity

- Members are identified by auto-increment `id` in the `members` table (no game_id).
- Each member has `name` (original display name) and `clean_name` (ASCII-only, lowercased) for OCR matching.
- Members are never deleted — they are marked `inactive` when not seen during a finalized roster session, and auto-reactivated if they reappear.
- Name history is tracked in `member_names` (with both `name` and `clean_name`).

## Python Environment

- Create a venv at `./venv/` if it doesn't already exist: `python3 -m venv venv`
- Activate it for all pip installs: `source venv/bin/activate`
- Install all Python dependencies into the venv only
- Add `venv/` to `.gitignore`
- Always run scripts via `./venv/bin/python <script>` or with the venv activated
