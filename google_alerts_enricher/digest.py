"""
Optional email digest for enriched Google Alerts.
Sends high + medium priority rows via Resend API.
Skips silently if RESEND_API_KEY is not configured.
"""

import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


def build_digest_html(rows: list[dict]) -> str:
    """Build an HTML digest table from high and medium priority enriched rows.

    Each row dict should have: headline, url, source, source_tier,
    named_company, has_financial_data, priority, snippet.
    """
    high = [r for r in rows if r.get("priority") == "high"]
    medium = [r for r in rows if r.get("priority") == "medium"]

    def _row_html(r: dict) -> str:
        company = r.get("named_company", "")
        financial = "Yes" if r.get("has_financial_data") else ""
        tier = r.get("source_tier", "")
        return (
            f'<tr>'
            f'<td style="padding:6px;border:1px solid #ddd">{r.get("priority","")}</td>'
            f'<td style="padding:6px;border:1px solid #ddd">'
            f'<a href="{r.get("url","")}">{r.get("headline","(no title)")}</a></td>'
            f'<td style="padding:6px;border:1px solid #ddd">{r.get("source","")} ({tier})</td>'
            f'<td style="padding:6px;border:1px solid #ddd">{company}</td>'
            f'<td style="padding:6px;border:1px solid #ddd">{financial}</td>'
            f'</tr>'
        )

    rows_html = "".join(_row_html(r) for r in high + medium)

    if not rows_html:
        return "<p>No high or medium priority alerts this period.</p>"

    return f"""
    <html><body>
    <h2>Verefy Alerts Digest</h2>
    <p>{len(high)} high / {len(medium)} medium priority alerts</p>
    <table style="border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px">
    <tr style="background:#f4f4f4">
        <th style="padding:8px;border:1px solid #ddd;text-align:left">Priority</th>
        <th style="padding:8px;border:1px solid #ddd;text-align:left">Headline</th>
        <th style="padding:8px;border:1px solid #ddd;text-align:left">Source</th>
        <th style="padding:8px;border:1px solid #ddd;text-align:left">Company</th>
        <th style="padding:8px;border:1px solid #ddd;text-align:left">Financial</th>
    </tr>
    {rows_html}
    </table>
    </body></html>
    """


def send_digest(rows: list[dict]) -> None:
    """Send email digest of high+medium priority rows via Resend API.

    Skips silently if RESEND_API_KEY is not set.
    """
    resend_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_key:
        log.info("RESEND_API_KEY not set -- skipping digest email")
        return

    email_to = os.environ.get("DIGEST_EMAIL_TO", "")
    email_from = os.environ.get("DIGEST_EMAIL_FROM", "")
    if not email_to or not email_from:
        log.error("DIGEST_EMAIL_TO or DIGEST_EMAIL_FROM not configured -- skipping digest")
        return

    high = [r for r in rows if r.get("priority") == "high"]
    medium = [r for r in rows if r.get("priority") == "medium"]

    if not high and not medium:
        log.info("No high/medium priority rows -- skipping digest email")
        return

    html = build_digest_html(rows)
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %b %d")
    subject = f"[Alerts] Verefy Weekly Digest — {date_str} — {len(high)} high / {len(medium)} medium"

    payload = {
        "from": email_from,
        "to": [email_to] if isinstance(email_to, str) else email_to,
        "subject": subject,
        "html": html,
    }

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        log.info(f"Digest email sent: {subject}")
    except Exception as exc:
        log.error(f"Failed to send digest email: {exc}")
