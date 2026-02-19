"""
Google Sheets API client for reading unenriched rows and writing enrichment data.
Uses a service account configured via GOOGLE_SERVICE_ACCOUNT_JSON env var.
"""

import base64
import json
import logging
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ENRICHMENT_HEADERS = ["enriched", "full_text", "source_tier", "named_company", "has_financial_data", "priority"]
HEADER_RANGE = "J1:O1"


def build_sheets_service():
    """Build an authenticated Google Sheets API service.

    Reads GOOGLE_SERVICE_ACCOUNT_JSON env var — tries raw JSON first,
    falls back to base64 decoding (GitHub Actions friendly).
    """
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var is not set")

    # Try raw JSON first, then base64
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        try:
            decoded = base64.b64decode(raw)
            info = json.loads(decoded)
        except Exception as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is neither valid JSON nor valid base64-encoded JSON"
            ) from exc

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return service


def _get_spreadsheet_id() -> str:
    """Get the spreadsheet ID from env var."""
    sid = os.environ.get("SPREADSHEET_ID", "")
    if not sid:
        raise RuntimeError("SPREADSHEET_ID env var is not set")
    return sid


def ensure_headers(service) -> None:
    """Add enrichment headers (J1:O1) if they are not already present."""
    sid = _get_spreadsheet_id()
    result = service.spreadsheets().values().get(
        spreadsheetId=sid,
        range=HEADER_RANGE,
    ).execute()

    existing = result.get("values", [[]])[0] if result.get("values") else []

    if existing == ENRICHMENT_HEADERS:
        log.info("Enrichment headers already present")
        return

    service.spreadsheets().values().update(
        spreadsheetId=sid,
        range=HEADER_RANGE,
        valueInputOption="RAW",
        body={"values": [ENRICHMENT_HEADERS]},
    ).execute()
    log.info("Enrichment headers written to J1:O1")


def read_unenriched_rows(service) -> list[dict]:
    """Read all rows from the sheet, return those where column J (enriched) is empty.

    Each returned dict has keys: row_number, date, alert_query, headline,
    source, url, snippet, category, use_case, used.
    """
    sid = _get_spreadsheet_id()
    result = service.spreadsheets().values().get(
        spreadsheetId=sid,
        range="A:O",
    ).execute()

    all_rows = result.get("values", [])
    if len(all_rows) <= 1:
        return []  # only header or empty

    unenriched = []
    for i, row in enumerate(all_rows[1:], start=2):  # row 2 is first data row
        # Pad row to at least 10 columns (A-J)
        padded = row + [""] * (10 - len(row)) if len(row) < 10 else row

        # Column J is index 9 — skip if already enriched
        if padded[9].strip().upper() == "TRUE":
            continue

        unenriched.append({
            "row_number": i,
            "date": padded[0],
            "alert_query": padded[1],
            "headline": padded[2],
            "source": padded[3],
            "url": padded[4],
            "snippet": padded[5],
            "category": padded[6],
            "use_case": padded[7],
            "used": padded[8],
        })

    return unenriched


def write_enrichment_batch(service, updates: list[dict]) -> None:
    """Write enrichment data to columns J-O for a batch of rows.

    Each update dict must have: row_number, enriched, full_text,
    source_tier, named_company, has_financial_data, priority.
    """
    if not updates:
        return

    sid = _get_spreadsheet_id()
    data = []
    for u in updates:
        row_num = u["row_number"]
        data.append({
            "range": f"J{row_num}:O{row_num}",
            "values": [[
                u["enriched"],
                u["full_text"],
                u["source_tier"],
                u["named_company"],
                u["has_financial_data"],
                u["priority"],
            ]],
        })

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={
            "valueInputOption": "RAW",
            "data": data,
        },
    ).execute()

    log.info(f"Wrote enrichment data for {len(updates)} rows")
