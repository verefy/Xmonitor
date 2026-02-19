"""
Google Alerts Enrichment Pipeline — Orchestrator & CLI entry point.

Reads unenriched rows from a Google Sheet, fetches full article text,
tags with source tier / company names / financial data, derives priority,
and writes columns J-O back to the sheet.

Usage:
    python enrich_alerts.py                  # full run
    python enrich_alerts.py --dry-run        # preview without writing
    python enrich_alerts.py --send-digest    # enrich + send email digest
"""

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# .env loading — works locally (python-dotenv) and in CI (native env vars)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    _script_dir = Path(__file__).parent
    _local_env = _script_dir / ".env"
    _root_env = _script_dir.parent / ".env"
    if _local_env.exists():
        load_dotenv(_local_env)
        print(f"Loaded env from {_local_env}")
    elif _root_env.exists():
        load_dotenv(_root_env)
        print(f"Loaded env from {_root_env}")
except ImportError:
    pass  # python-dotenv not installed (e.g. in CI) — env vars set natively

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Local imports (after env loading so env vars are available)
# ---------------------------------------------------------------------------
from digest import send_digest  # noqa: E402
from fetcher import fetch_batch  # noqa: E402
from sheets_client import (  # noqa: E402
    build_sheets_service,
    ensure_headers,
    read_unenriched_rows,
    write_enrichment_batch,
)
from tagger import (  # noqa: E402
    classify_source_tier,
    derive_priority,
    detect_financial_impact,
    extract_company,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Google Alerts Enrichment Pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview enrichment without writing to sheet or sending email",
    )
    parser.add_argument(
        "--send-digest",
        action="store_true",
        help="Send email digest after enrichment",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.dry_run:
        log.info("=== DRY RUN — no writes will be made ===\n")

    # 1. Build Sheets service
    log.info("Connecting to Google Sheets...")
    service = build_sheets_service()

    # 2. Ensure enrichment headers exist
    ensure_headers(service)

    # 3. Read unenriched rows
    rows = read_unenriched_rows(service)
    log.info(f"\nFound {len(rows)} unenriched rows")

    if not rows:
        log.info("Nothing to process — exiting cleanly")
        return

    # 4. Fetch article text
    log.info("\nFetching article text...")
    urls = [r["url"] for r in rows]
    texts = fetch_batch(urls)

    # 5. Tag each row
    log.info("\nTagging rows...")
    updates = []
    enriched_rows = []  # for digest

    for row, full_text in zip(rows, texts):
        source = row.get("source", "")
        headline = row.get("headline", "")
        # Combine snippet + full text for tagging (snippet is always available)
        tag_text = f"{headline} {row.get('snippet', '')} {full_text}"

        source_tier = classify_source_tier(source)
        named_company = extract_company(tag_text, headline=headline)
        has_financial = detect_financial_impact(tag_text)
        priority = derive_priority(source_tier, named_company, has_financial)

        update = {
            "row_number": row["row_number"],
            "enriched": "TRUE",
            "full_text": full_text,
            "source_tier": source_tier,
            "named_company": named_company,
            "has_financial_data": str(has_financial).upper(),
            "priority": priority,
        }
        updates.append(update)

        # Build enriched row dict for digest
        enriched_rows.append({
            **row,
            "full_text": full_text,
            "source_tier": source_tier,
            "named_company": named_company,
            "has_financial_data": has_financial,
            "priority": priority,
        })

        log.info(
            f"  Row {row['row_number']}: {source_tier} | "
            f"company={named_company or '(none)'} | "
            f"financial={has_financial} | "
            f"priority={priority}"
        )

    # 6. Write to sheet
    if not args.dry_run:
        log.info(f"\nWriting enrichment data for {len(updates)} rows...")
        write_enrichment_batch(service, updates)
    else:
        log.info(f"\n[DRY RUN] Would write {len(updates)} rows to sheet")

    # 7. Send digest
    if args.send_digest and not args.dry_run:
        log.info("\nSending digest email...")
        send_digest(enriched_rows)
    elif args.send_digest and args.dry_run:
        high = sum(1 for r in enriched_rows if r["priority"] == "high")
        medium = sum(1 for r in enriched_rows if r["priority"] == "medium")
        log.info(f"\n[DRY RUN] Would send digest: {high} high / {medium} medium priority")

    # 8. Summary
    high = sum(1 for u in updates if u["priority"] == "high")
    medium = sum(1 for u in updates if u["priority"] == "medium")
    low = sum(1 for u in updates if u["priority"] == "low")
    log.info(f"\n=== Done: {len(updates)} rows enriched — {high} high / {medium} medium / {low} low ===")


if __name__ == "__main__":
    main()
