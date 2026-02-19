# Google Alerts Enricher

Step 3 of the Google Alerts pipeline: reads unenriched rows from a Google Sheet, fetches full article text, tags with source tier / company names / financial data, derives priority, and writes columns J-O back.

## Setup

### 1. Google Cloud Service Account

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Google Sheets API**
3. Create a **Service Account** (IAM & Admin → Service Accounts)
4. Create a JSON key for the service account and download it
5. Share your Google Sheet with the service account email (Editor access)

### 2. Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | Raw JSON or base64-encoded service account key |
| `SPREADSHEET_ID` | Yes | The ID from the Google Sheet URL |
| `RESEND_API_KEY` | No | Resend API key for email digest |
| `DIGEST_EMAIL_TO` | No | Recipient email for digest |
| `DIGEST_EMAIL_FROM` | No | Sender email (must be verified in Resend) |

For GitHub Actions, add these as repository secrets.

To base64-encode the service account JSON (recommended for CI):
```bash
base64 -w 0 service-account.json
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Full run — enrich + write to sheet
python enrich_alerts.py

# Preview without writing
python enrich_alerts.py --dry-run

# Enrich + send email digest
python enrich_alerts.py --send-digest
```

## Testing

```bash
python -m pytest google_alerts_enricher/tests/ -v
```

## Column Layout

Columns A-I are owned by Apps Script (read-only). Columns J-O are written by this script:

| Col | Header | Description |
|-----|--------|-------------|
| J | enriched | TRUE when processed |
| K | full_text | First 2000 chars of article body |
| L | source_tier | t1 / t2 / t3 |
| M | named_company | Company name or blank |
| N | has_financial_data | TRUE / FALSE |
| O | priority | high / medium / low |

## GitHub Actions

The workflow runs every Monday at 07:00 UTC with `--send-digest`. Can also be triggered manually via workflow_dispatch.
