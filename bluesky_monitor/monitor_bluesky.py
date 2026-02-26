"""
Verefy Bluesky Monitor — Daily Digest Bot

Tracks curated Bluesky accounts and keyword searches via the public
AT Protocol API, filters for relevance and engagement, and sends a
grouped HTML digest email via Resend. Supports a two-tier schedule:
"hot" groups run 2x/day, all groups run 1x/day.

Usage:
    python monitor_bluesky.py --config config_bluesky.yaml             # Full run: all groups + email
    python monitor_bluesky.py --config config_bluesky.yaml --tier hot  # Hot groups only + email
    python monitor_bluesky.py --config config_bluesky.yaml --dry-run   # Save HTML locally
"""

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

try:
    from langdetect import detect as _langdetect_detect, LangDetectException
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False

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
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Post:
    id: str
    author_handle: str
    author_name: str
    author_label: str
    text: str
    url: str
    likes: int
    reposts: int
    replies: int
    created_at: str
    source: str        # "account" or "search:{query}"
    group_name: str    # which config group this belongs to

    @property
    def engagement_score(self) -> int:
        return self.likes + self.reposts * 2 + self.replies * 3


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Campaign support
# ---------------------------------------------------------------------------

def get_active_campaign_phases(campaigns: list, today: date = None) -> list[dict]:
    """Return group-compatible dicts for campaign phases active today."""
    if today is None:
        today = date.today()

    active: list[dict] = []
    for campaign in campaigns:
        campaign_name = campaign.get("name", "Unnamed Campaign")
        campaign_desc = campaign.get("description", "")
        for phase in campaign.get("phases", []):
            phase_name = phase.get("name", "Unnamed Phase")
            start = date.fromisoformat(phase["start"])
            end = date.fromisoformat(phase["end"])

            if not (start <= today <= end):
                continue

            accounts = phase.get("accounts", [])
            searches = phase.get("keyword_searches", [])
            if not accounts and not searches:
                log.warning(
                    f"Campaign '{campaign_name}' phase '{phase_name}' "
                    f"has no accounts or keyword_searches -- skipping"
                )
                continue

            active.append({
                "name": f"{campaign_name} -- {phase_name}",
                "description": campaign_desc,
                "tier": phase.get("tier", "hot"),
                "accounts": accounts,
                "keyword_searches": searches,
                "_campaign_name": campaign_name,
                "_phase_name": phase_name,
                "_frequency": phase.get("frequency", ""),
                "_manual_tasks": phase.get("manual_tasks", []),
                "_is_campaign": True,
            })

    return active


# ---------------------------------------------------------------------------
# Bluesky API
# ---------------------------------------------------------------------------

BSKY_PUBLIC = "https://public.api.bsky.app/xrpc"
BSKY_APPVIEW = "https://api.bsky.app/xrpc"
AUTHOR_FEED_URL = f"{BSKY_PUBLIC}/app.bsky.feed.getAuthorFeed"
# searchPosts returns 403 on public.api.bsky.app; use api.bsky.app instead
SEARCH_POSTS_URL = f"{BSKY_APPVIEW}/app.bsky.feed.searchPosts"
API_DELAY = 0.5  # seconds between calls
MAX_RETRIES = 3


def _api_get(url: str, params: dict) -> dict | None:
    """GET request with retry logic and rate-limit handling."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=30)

            if resp.status_code == 429:
                log.warning("    ! Rate limited (429) — waiting 30s")
                time.sleep(30)
                continue

            if resp.status_code == 400:
                error_msg = resp.text[:200]
                log.warning(f"    ! Bad request (400): {error_msg}")
                return None

            if resp.status_code == 404:
                log.warning(f"    ! Not found (404) for params {params}")
                return None

            if resp.status_code >= 500:
                delay = 2 ** (attempt + 1)
                log.warning(
                    f"    ! Server error ({resp.status_code}) — "
                    f"retry {attempt + 1}/{MAX_RETRIES} in {delay}s"
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.Timeout:
            delay = 2 ** (attempt + 1)
            log.warning(
                f"    ! Timeout — retry {attempt + 1}/{MAX_RETRIES} in {delay}s"
            )
            time.sleep(delay)
        except requests.exceptions.RequestException as exc:
            delay = 2 ** (attempt + 1)
            log.warning(
                f"    ! Request error: {exc} — "
                f"retry {attempt + 1}/{MAX_RETRIES} in {delay}s"
            )
            time.sleep(delay)

    log.warning("    ! All retries exhausted — skipping")
    return None


def fetch_author_posts(handle: str) -> list[dict]:
    """Fetch recent posts from an author via getAuthorFeed."""
    params = {
        "actor": handle,
        "limit": 30,
        "filter": "posts_no_replies",
    }
    data = _api_get(AUTHOR_FEED_URL, params)
    if data is None:
        return []

    posts = []
    for item in data.get("feed", []):
        # Skip reposts
        reason = item.get("reason")
        if reason and reason.get("$type") == "app.bsky.feed.defs#reasonRepost":
            continue
        post = item.get("post")
        if post:
            posts.append(post)
    return posts


def search_posts(query: str, since_iso: str) -> list[dict]:
    """Search Bluesky posts via searchPosts."""
    params = {
        "q": query,
        "limit": 25,
        "sort": "latest",
        "since": since_iso,
    }
    data = _api_get(SEARCH_POSTS_URL, params)
    if data is None:
        return []
    return data.get("posts", [])


# ---------------------------------------------------------------------------
# Post normalization
# ---------------------------------------------------------------------------

def _post_url(handle: str, uri: str) -> str:
    """Build bsky.app URL from handle and AT URI."""
    rkey = uri.split("/")[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def normalize_post(
    raw: dict,
    source: str,
    group_name: str,
    account_label: str = "",
) -> Post:
    """Convert a raw Bluesky post dict to our Post dataclass."""
    author = raw.get("author", {})
    handle = author.get("handle", "unknown")
    display_name = author.get("displayName", "") or handle
    record = raw.get("record", {})
    uri = raw.get("uri", "")

    return Post(
        id=uri,
        author_handle=handle,
        author_name=display_name,
        author_label=account_label or display_name,
        text=record.get("text", ""),
        url=_post_url(handle, uri),
        likes=int(raw.get("likeCount", 0)),
        reposts=int(raw.get("repostCount", 0)),
        replies=int(raw.get("replyCount", 0)),
        created_at=record.get("createdAt", ""),
        source=source,
        group_name=group_name,
    )


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_post_date(date_str: str) -> datetime:
    """Parse ISO 8601 datetime from Bluesky."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(date_str)
    except ValueError:
        pass
    raise ValueError(f"Cannot parse date: {date_str}")


def since_iso(lookback_hours: int) -> str:
    """Return ISO 8601 datetime string for the lookback window."""
    dt = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

_LANGDETECT_MIN_CHARS = 50  # langdetect is unreliable on very short text


def _detect_language(text: str) -> str | None:
    """Detect language of text using langdetect. Returns BCP-47 code or None."""
    if not _HAS_LANGDETECT:
        return None
    if len(text) < _LANGDETECT_MIN_CHARS:
        return None
    try:
        return _langdetect_detect(text)
    except Exception:
        return None


def is_relevant(post: Post, keywords: list[str]) -> bool:
    """Check if post text contains any relevance keyword (case-insensitive)."""
    text_lower = post.text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(config: dict, tier: str) -> dict[str, list[Post]]:
    """
    Execute the full fetch -> dedup -> filter pipeline.
    Returns {group_name: [Post, ...]} for groups with results.
    """
    settings = config.get("settings", {})
    if tier == "hot":
        lookback_hours = settings.get("lookback_hours_hot", 13)
    else:
        lookback_hours = settings.get("lookback_hours_daily", 26)
    min_likes_accounts = settings.get("min_likes_accounts", 3)
    min_likes_search = settings.get("min_likes_search", 5)
    relevance_keywords = config.get("relevance_keywords", [])
    all_groups = config.get("groups", [])
    campaigns = config.get("campaigns") or []
    active_phases = get_active_campaign_phases(campaigns)
    all_groups = all_groups + active_phases
    since = since_iso(lookback_hours)

    # Filter groups by tier
    if tier == "hot":
        groups = [g for g in all_groups if g.get("tier") == "hot"]
    else:
        groups = all_groups

    tier_label = "Hot" if tier == "hot" else "Full"
    log.info(f"{tier_label} tier run -- processing {len(groups)} groups "
             f"(lookback {lookback_hours}h, since {since})")

    seen_ids: set[str] = set()
    results: dict[str, list[Post]] = {}
    total_fetched = 0
    total_kept = 0
    api_errors = 0

    for group in groups:
        group_name = group.get("name", "Unnamed")
        accounts = group.get("accounts", [])
        searches = group.get("keyword_searches", [])
        group_languages = group.get("languages")       # e.g. ["en", "pt"] or None
        group_handle_exclude = group.get("handle_exclude")  # e.g. [".brid.gy"] or None
        log.info(
            f">>> Checking group: {group_name} "
            f"({len(accounts)} accounts, {len(searches)} searches)"
        )

        group_posts: list[Post] = []

        # --- Account posts ---
        for acct in accounts:
            handle = acct["username"]
            label = acct.get("label", handle)
            time.sleep(API_DELAY)
            raw_posts = fetch_author_posts(handle)
            if raw_posts is None:
                api_errors += 1
                continue

            fetched = 0
            kept = 0
            for raw in raw_posts:
                post = normalize_post(raw, "account", group_name, label)
                fetched += 1

                # Client-side date filter (getAuthorFeed has no since param)
                try:
                    post_dt = parse_post_date(post.created_at)
                    since_dt = parse_post_date(since)
                    if post_dt < since_dt:
                        continue
                except ValueError:
                    pass  # if date can't be parsed, include the post

                # Handle exclusion filter
                if group_handle_exclude:
                    handle_lower = post.author_handle.lower()
                    matched = next(
                        (p for p in group_handle_exclude if p.lower() in handle_lower),
                        None,
                    )
                    if matched:
                        log.debug(
                            f"    Filtered post by @{post.author_handle}: "
                            f"handle matches exclude pattern '{matched}'"
                        )
                        continue

                # Language filter
                if group_languages:
                    post_langs = raw.get("record", {}).get("langs") or []
                    if post_langs:
                        if not any(
                            lang in group_languages for lang in post_langs
                        ):
                            log.debug(
                                f"    Filtered post by @{post.author_handle}: "
                                f"language {post_langs} not in {group_languages}"
                            )
                            continue
                    else:
                        # No langs metadata — fall back to langdetect
                        detected = _detect_language(post.text)
                        if detected and detected not in group_languages:
                            log.debug(
                                f"    Filtered post by @{post.author_handle}: "
                                f"detected language '{detected}' not in "
                                f"{group_languages} (langs field was empty)"
                            )
                            continue

                if post.id in seen_ids:
                    continue
                if post.likes < min_likes_accounts:
                    continue
                if not is_relevant(post, relevance_keywords):
                    continue
                seen_ids.add(post.id)
                group_posts.append(post)
                kept += 1

            total_fetched += fetched
            total_kept += kept
            log.info(f"    -> @{handle}... {fetched} fetched, {kept} relevant")

        # --- Keyword searches ---
        for ks in searches:
            query_text = ks["query"]
            per_search_min = ks.get("min_likes", min_likes_search)
            time.sleep(API_DELAY)
            raw_posts = search_posts(query_text, since)
            if raw_posts is None:
                api_errors += 1
                continue

            fetched = 0
            kept = 0
            for raw in raw_posts:
                post = normalize_post(raw, f"search:{query_text}", group_name)
                fetched += 1

                # Handle exclusion filter
                if group_handle_exclude:
                    handle_lower = post.author_handle.lower()
                    matched = next(
                        (p for p in group_handle_exclude if p.lower() in handle_lower),
                        None,
                    )
                    if matched:
                        log.debug(
                            f"    Filtered post by @{post.author_handle}: "
                            f"handle matches exclude pattern '{matched}'"
                        )
                        continue

                # Language filter
                if group_languages:
                    post_langs = raw.get("record", {}).get("langs") or []
                    if post_langs:
                        if not any(
                            lang in group_languages for lang in post_langs
                        ):
                            log.debug(
                                f"    Filtered post by @{post.author_handle}: "
                                f"language {post_langs} not in {group_languages}"
                            )
                            continue
                    else:
                        # No langs metadata — fall back to langdetect
                        detected = _detect_language(post.text)
                        if detected and detected not in group_languages:
                            log.debug(
                                f"    Filtered post by @{post.author_handle}: "
                                f"detected language '{detected}' not in "
                                f"{group_languages} (langs field was empty)"
                            )
                            continue

                if post.id in seen_ids:
                    continue
                if post.likes < per_search_min:
                    continue
                seen_ids.add(post.id)
                group_posts.append(post)
                kept += 1

            total_fetched += fetched
            total_kept += kept
            log.info(
                f"    -> \"{query_text}\"... {fetched} found, {kept} new"
            )

        # Sort by engagement within group
        group_posts.sort(key=lambda p: p.engagement_score, reverse=True)
        if group_posts:
            results[group_name] = group_posts

    log.info(f"\nTotal: {total_fetched} fetched, {total_kept} kept, {api_errors} errors")
    return results


# ---------------------------------------------------------------------------
# HTML digest
# ---------------------------------------------------------------------------

def truncate(text: str, max_len: int = 280) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 1] + "\u2026"


def build_html(
    results: dict[str, list[Post]],
    config: dict,
    tier: str,
    max_per_group: int = 10,
) -> str:
    """Build the HTML digest email."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %b %d")
    all_groups = config.get("groups", [])

    # Merge active campaign phases
    campaigns = config.get("campaigns") or []
    active_phases = get_active_campaign_phases(campaigns)
    all_groups = all_groups + active_phases

    if tier == "hot":
        eligible_groups = [g for g in all_groups if g.get("tier") == "hot"]
    else:
        eligible_groups = all_groups

    group_descriptions = {g["name"]: g.get("description", "") for g in eligible_groups}

    # Stats
    total_accounts = sum(len(g.get("accounts", [])) for g in eligible_groups)
    total_searches = sum(len(g.get("keyword_searches", [])) for g in eligible_groups)
    total_posts = sum(len(posts) for posts in results.values())
    groups_with_results = len(results)

    is_hot = tier == "hot"
    title = "Verefy Bluesky Monitor"
    badge = "HOT RUN" if is_hot else "FULL RUN"
    accent = "#E8740C" if is_hot else "#0560FF"

    html_parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<style>",
        "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
        "color: #1a1a1a; background: #f5f5f5; margin: 0; padding: 20px; }",
        ".container { max-width: 640px; margin: 0 auto; background: #fff; "
        "border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }",
        f".header {{ background: {accent}; color: #fff; padding: 24px 28px; }}",
        ".header h1 { margin: 0 0 4px 0; font-size: 20px; font-weight: 600; }",
        ".header .badge { display: inline-block; background: rgba(255,255,255,0.2); "
        "font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px; "
        "margin-left: 8px; vertical-align: middle; letter-spacing: 0.5px; }",
        ".header p { margin: 4px 0 0 0; opacity: 0.85; font-size: 14px; }",
        ".body { padding: 20px 28px; }",
        f".group-header {{ font-size: 18px; font-weight: 600; color: #1a1a1a; "
        f"margin: 28px 0 4px 0; border-bottom: 2px solid {accent}; padding-bottom: 6px; }}",
        ".group-header:first-child { margin-top: 0; }",
        f".group-desc {{ background: #e8f0fe; color: #4a5568; font-size: 13px; "
        f"padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; line-height: 1.4; "
        f"border-left: 3px solid {accent}; }}",
        ".post { border: 1px solid #e8e8e8; border-radius: 8px; padding: 14px 16px; "
        "margin-bottom: 10px; }",
        f".post-author {{ font-weight: 600; color: {accent}; font-size: 14px; }}",
        ".post-label { color: #888; font-size: 12px; margin-left: 4px; }",
        ".post-text { margin: 6px 0; font-size: 14px; line-height: 1.45; color: #2d2d2d; }",
        ".post-meta { font-size: 12px; color: #888; display: flex; "
        "flex-wrap: wrap; gap: 10px; align-items: center; }",
        f".post-source {{ background: #e8f0fe; color: {accent}; font-size: 11px; "
        f"padding: 2px 8px; border-radius: 10px; }}",
        f".post-link {{ color: {accent}; text-decoration: none; font-size: 12px; font-weight: 500; }}",
        ".footer { background: #fafafa; padding: 16px 28px; font-size: 12px; "
        "color: #888; border-top: 1px solid #eee; text-align: center; }",
        ".quiet { text-align: center; padding: 40px 20px; color: #888; font-size: 15px; }",
        ".manual-tasks { background: #fff8e1; border: 1px solid #ffe082; border-radius: 6px; "
        "padding: 10px 14px; margin-bottom: 14px; font-size: 13px; line-height: 1.5; }",
        ".manual-tasks-header { font-weight: 600; color: #e65100; margin-bottom: 4px; }",
        ".campaign-empty { color: #888; font-size: 13px; font-style: italic; "
        "padding: 12px 0; }",
        ".no-posts { color: #888; font-size: 13px; font-style: italic; padding: 12px 0; }",
        "</style></head><body>",
        "<div class='container'>",
        "<div class='header'>",
        f"<h1>{_esc(title)}<span class='badge'>{badge}</span></h1>",
        f"<p>{date_str} &middot; {now.strftime('%H:%M')} UTC</p>",
        "</div>",
        "<div class='body'>",
    ]

    # Check if any group has content to show
    has_any_content = bool(results) or any(
        g.get("_is_campaign") for g in eligible_groups
    )

    if not has_any_content:
        html_parts.append(
            "<div class='quiet'>Quiet day &mdash; no matching posts found. "
            "Bot is running normally.</div>"
        )
    else:
        for group in eligible_groups:
            gname = group["name"]
            is_campaign = group.get("_is_campaign", False)
            posts = results.get(gname, [])[:max_per_group]

            # Regular groups: show header with "no posts" message
            # Campaign groups: always show (for manual task reminders)
            if not is_campaign and gname not in results:
                # Still show the group with a "no posts" note
                desc = group_descriptions.get(gname, "")
                html_parts.append(
                    f"<div class='group-header'>{_esc(gname)}</div>"
                )
                if desc:
                    html_parts.append(
                        f"<div class='group-desc'>{_esc(desc.strip())}</div>"
                    )
                html_parts.append(
                    "<div class='no-posts'>No matching posts in this period.</div>"
                )
                continue

            desc = group_descriptions.get(gname, "")

            # Header: campaign groups get a calendar prefix
            if is_campaign:
                campaign_name = group.get("_campaign_name", "")
                phase_name = group.get("_phase_name", "")
                header_text = f"\U0001f4c5 {campaign_name} -- {phase_name}"
                html_parts.append(
                    f"<div class='group-header'>{_esc(header_text)}</div>"
                )
            else:
                html_parts.append(
                    f"<div class='group-header'>{_esc(gname)}</div>"
                )

            if desc:
                html_parts.append(
                    f"<div class='group-desc'>{_esc(desc.strip())}</div>"
                )

            # Manual tasks reminder for campaigns
            if is_campaign:
                manual_tasks = group.get("_manual_tasks", [])
                if manual_tasks:
                    html_parts.append("<div class='manual-tasks'>")
                    html_parts.append(
                        "<div class='manual-tasks-header'>"
                        "&#9888;&#65039; MANUAL TASKS FOR THIS PHASE:</div>"
                    )
                    for mt in manual_tasks:
                        task_text = mt.get("task", "")
                        freq = mt.get("frequency", "")
                        bullet = f"[{freq}] {task_text}" if freq else task_text
                        html_parts.append(
                            f"<div>&bull; {_esc(bullet)}</div>"
                        )
                    html_parts.append("</div>")

            if not posts and is_campaign:
                html_parts.append(
                    "<div class='campaign-empty'>"
                    "No posts matched this campaign phase today.</div>"
                )
            elif not posts:
                html_parts.append(
                    "<div class='no-posts'>No matching posts in this period.</div>"
                )
            else:
                for p in posts:
                    source_tag = "account" if p.source == "account" else p.source
                    html_parts.append("<div class='post'>")
                    html_parts.append(
                        f"<span class='post-author'>{_esc(p.author_name)}</span> "
                        f"<span class='post-author' style='font-weight:400'>"
                        f"@{_esc(p.author_handle)}</span>"
                        f"<span class='post-label'>({_esc(p.author_label)})</span>"
                    )
                    html_parts.append(
                        f"<div class='post-text'>{_esc(truncate(p.text))}</div>"
                    )
                    html_parts.append("<div class='post-meta'>")
                    html_parts.append(
                        f"<span>&#9829; {p.likes} &middot; "
                        f"&#128257; {p.reposts} &middot; "
                        f"&#128172; {p.replies} &middot; "
                        f"Score: {p.engagement_score}</span>"
                    )
                    html_parts.append(
                        f"<span class='post-source'>{_esc(source_tag)}</span>"
                    )
                    html_parts.append(
                        f"<a class='post-link' href='{_esc(p.url)}'>"
                        f"View on Bluesky &rarr;</a>"
                    )
                    html_parts.append("</div></div>")

    html_parts.append("</div>")  # .body

    html_parts.append(
        f"<div class='footer'>"
        f"{total_posts} posts &middot; "
        f"{total_accounts} accounts monitored &middot; "
        f"{total_searches} keyword searches &middot; "
        f"{groups_with_results} groups with results<br>"
        f"Source: Bluesky (AT Protocol) &middot; Public API &middot; "
        f"No authentication required"
        f"</div>"
    )
    html_parts.append("</div></body></html>")

    return "\n".join(html_parts)


def _esc(text: str) -> str:
    """Basic HTML escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ---------------------------------------------------------------------------
# Email via Resend
# ---------------------------------------------------------------------------

def send_email(html: str, config: dict, tier: str, total_posts: int) -> None:
    """Send digest email via Resend API."""
    resend_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_key:
        log.error("RESEND_API_KEY not set -- skipping email send")
        sys.exit(1)

    settings = config.get("settings", {})
    email_to = os.environ.get("EMAIL_TO", settings.get("email_to", ""))
    email_from = os.environ.get("EMAIL_FROM", settings.get("email_from", ""))

    if not email_to or not email_from:
        log.error("EMAIL_TO or EMAIL_FROM not configured")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    groups_count = len([v for v in [total_posts] if v > 0]) if total_posts else 0

    if total_posts == 0:
        subject = f"[Bluesky] Verefy Monitor — {date_str} — quiet day"
    else:
        subject = (
            f"[Bluesky] Verefy Monitor — {date_str} — {total_posts} posts"
        )

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
        log.info(f"Email sent successfully: {subject}")
    except Exception as exc:
        log.error(f"Failed to send email: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verefy Bluesky Monitor")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--tier",
        choices=["all", "hot"],
        default="all",
        help="Which tier to run: 'all' (all groups) or 'hot' (hot groups only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch posts and save HTML locally instead of emailing",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log.info("=" * 50)
    log.info("Verefy Bluesky Monitor -- starting")
    log.info(f"  TIER: {args.tier}")
    log.info(f"  CONFIG: {args.config}")
    if args.dry_run:
        log.info("  MODE: dry-run (no email)")
    log.info("=" * 50)

    config = load_config(args.config)
    results = run_pipeline(config, args.tier)
    html = build_html(results, config, args.tier)
    total_posts = sum(len(posts) for posts in results.values())

    if args.dry_run:
        out_path = Path(__file__).parent / "digest_preview.html"
        out_path.write_text(html, encoding="utf-8")
        log.info(f"\nHTML preview saved to: {out_path}")
    else:
        send_email(html, config, args.tier, total_posts)

    if results:
        for gname, posts in results.items():
            log.info(f"\n  {gname}: {len(posts)} posts")
    else:
        log.info("\nNo matching posts today.")

    log.info("\nDone.")


if __name__ == "__main__":
    main()
