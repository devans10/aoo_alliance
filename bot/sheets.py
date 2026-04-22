"""
Google Sheets data fetcher for AoO Alliance Stats Bot.
Reads Member Summary, Event Log, Alliance Summary, and Roster tabs.
Uses a service account (read-only) — no OAuth flow required.
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

SPREADSHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# Knowledge base sheet config — can be a different spreadsheet from the alliance data.
# KNOWLEDGE_BASE_SHEET_ID defaults to GOOGLE_SHEET_ID if unset (i.e. a tab in the same sheet).
KB_SHEET_ID = os.environ.get("KNOWLEDGE_BASE_SHEET_ID") or SPREADSHEET_ID

# Scopes needed for read-only Sheets access
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Tab names to fetch — must match exactly what's in your Sheet
TABS = {
    "Roster": "Roster",
    "Event Log": "Event Log",
    "Member Summary": "Member Summary",
    "Alliance Summary": "Alliance Summary",
}

# How many rows to fetch from Event Log (most recent data is enough for most questions)
EVENT_LOG_MAX_ROWS = 500


def get_credentials() -> Credentials:
    """Load service account credentials from env or file."""
    # Support credentials as a JSON string in env (useful for cloud hosting)
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if creds_json:
        info = json.loads(creds_json)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    # Fall back to a local file path
    creds_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    return Credentials.from_service_account_file(creds_file, scopes=SCOPES)


def tab_to_text(records: list[list]) -> str:
    """Convert a list-of-lists (raw sheet values) to a TSV-style text block."""
    if not records:
        return "(empty)"
    lines = []
    for row in records:
        lines.append("\t".join(str(cell) for cell in row))
    return "\n".join(lines)


def get_knowledge_base() -> str:
    """Fetch all tabs from the knowledge base spreadsheet and return them as text.

    Each tab becomes a labelled section so Claude knows which topic area it
    belongs to.  New tabs are picked up automatically on the next bot restart.

    Controlled by KNOWLEDGE_BASE_SHEET_ID (defaults to GOOGLE_SHEET_ID).
    Returns an empty string on any error so the bot degrades gracefully.
    """
    if not KB_SHEET_ID:
        return ""
    creds = get_credentials()
    client = gspread.authorize(creds)
    try:
        spreadsheet = client.open_by_key(KB_SHEET_ID)
        sections: list[str] = []
        for worksheet in spreadsheet.worksheets():
            try:
                rows = worksheet.get_all_values()
                if not rows:
                    continue
                sections.append(f"--- {worksheet.title} ---\n{tab_to_text(rows)}")
            except Exception as e:
                print(f"⚠️  Skipping KB tab '{worksheet.title}': {e}")
        return "\n\n".join(sections)
    except Exception as e:
        print(f"⚠️  Error reading knowledge base: {e}")
        return ""


def get_all_sheet_data() -> dict[str, str]:
    """
    Fetch all relevant tabs from the Google Sheet.
    Returns a dict of {tab_name: text_content}.
    Event Log is capped to avoid overflowing Claude's context window.
    """
    creds = get_credentials()
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(SPREADSHEET_ID)

    result = {}

    for label, tab_name in TABS.items():
        try:
            worksheet = spreadsheet.worksheet(tab_name)

            if label == "Event Log":
                # Only pull header + most recent N rows to keep context manageable
                all_rows = worksheet.get_all_values()
                header = all_rows[:1]
                recent = all_rows[1:][-EVENT_LOG_MAX_ROWS:] if len(all_rows) > 1 else []
                rows = header + recent
            else:
                rows = worksheet.get_all_values()

            result[label] = tab_to_text(rows)

        except gspread.exceptions.WorksheetNotFound:
            result[label] = f"(Tab '{tab_name}' not found — check the tab name in sheets.py)"
        except Exception as e:
            result[label] = f"(Error reading tab '{tab_name}': {e})"

    return result
