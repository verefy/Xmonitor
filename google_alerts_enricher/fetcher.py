"""
Article text extraction using trafilatura.
Handles Google redirect URLs, domain skipping, and rate limiting.
"""

import logging
import time
from urllib.parse import parse_qs, urlparse

import trafilatura

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; VeRefyBot/1.0; +https://verefy.ai)"
FETCH_FAILED = "[FETCH_FAILED]"
MAX_TEXT_LENGTH = 2000
FETCH_DELAY = 2  # seconds between fetches

# Domains known to block scrapers or return garbage — skip without fetching
SKIP_DOMAINS: set[str] = set()


def extract_domain(url: str) -> str:
    """Extract the domain from a URL, stripping www. prefix."""
    try:
        hostname = urlparse(url).hostname or ""
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname.lower()
    except Exception:
        return ""


def _resolve_google_redirect(url: str) -> str:
    """If the URL is a Google redirect, extract the real destination URL."""
    parsed = urlparse(url)
    if parsed.hostname and "google.com" in parsed.hostname and parsed.path == "/url":
        params = parse_qs(parsed.query)
        real_urls = params.get("q") or params.get("url") or []
        if real_urls:
            return real_urls[0]
    return url


def fetch_article_text(url: str) -> str:
    """Fetch and extract article body text from a URL.

    Returns the first MAX_TEXT_LENGTH characters of extracted text,
    or FETCH_FAILED on any error.
    """
    url = _resolve_google_redirect(url)
    domain = extract_domain(url)

    if domain in SKIP_DOMAINS:
        log.info(f"  Skipping blocked domain: {domain}")
        return FETCH_FAILED

    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            log.warning(f"  No content downloaded: {url}")
            return FETCH_FAILED

        text = trafilatura.extract(downloaded)
        if not text:
            log.warning(f"  No text extracted: {url}")
            return FETCH_FAILED

        return text[:MAX_TEXT_LENGTH]

    except Exception as exc:
        log.warning(f"  Fetch error for {url}: {exc}")
        return FETCH_FAILED


def fetch_batch(urls: list[str]) -> list[str]:
    """Fetch article text for a list of URLs with rate limiting.

    Returns a list of extracted texts (or FETCH_FAILED) in the same order.
    """
    results = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(FETCH_DELAY)
        log.info(f"  [{i + 1}/{len(urls)}] Fetching {url}")
        results.append(fetch_article_text(url))
    return results
