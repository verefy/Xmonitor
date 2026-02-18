"""
Architect X/Twitter Monitor — Daily Digest Bot

Tracks curated accounts and keyword searches via twitterapi.io,
filters for relevance and engagement, and sends a grouped HTML
digest email via Resend. Supports a two-tier schedule: "hot" groups
run 2x/day, all groups run 1x/day.

Usage:
    python monitor.py                      # Full run: all groups + email
    python monitor.py --tier hot           # Hot groups only + email
    python monitor.py --tier full --dry-run  # All groups, save HTML locally
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
class Tweet:
    id: str
    author: str
    author_label: str
    text: str
    url: str
    likes: int
    retweets: int
    replies: int
    created_at: str
    source: str        # "account" or "keyword:{query}"
    group_name: str    # which config group this belongs to

    @property
    def engagement_score(self) -> int:
        return self.likes + self.retweets * 2 + self.replies * 3


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Campaign support
# ---------------------------------------------------------------------------

def get_active_campaign_phases(campaigns: list, today: date = None) -> list[dict]:
    """Return group-compatible dicts for campaign phases active today.

    Each returned dict has standard group fields (name, description, tier,
    accounts, keyword_searches) plus campaign metadata (_campaign_name,
    _phase_name, _frequency, _manual_tasks, _is_campaign).
    """
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
# Date helpers
# ---------------------------------------------------------------------------

def parse_tweet_date(date_str: str) -> datetime:
    """Parse various date formats returned by twitterapi.io."""
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
    try:
        return datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        pass
    raise ValueError(f"Cannot parse date: {date_str}")


def since_date_str(lookback_hours: int) -> str:
    """Return YYYY-MM-DD string for the Twitter since: operator."""
    dt = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Twitter API
# ---------------------------------------------------------------------------

SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
API_DELAY = 0.5  # seconds between calls


def search_tweets(query: str, api_key: str) -> list[dict]:
    """Run an advanced_search query and return raw tweet dicts."""
    headers = {"X-API-Key": api_key}
    params = {"query": query, "queryType": "Latest"}
    try:
        resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("tweets", [])
    except Exception as exc:
        log.warning(f"    ! API error for query '{query}': {exc}")
        return []


def raw_to_tweet(raw: dict, source: str, group_name: str, account_label: str = "") -> Tweet:
    """Convert a raw API tweet dict to our Tweet dataclass."""
    author_info = raw.get("author", {})
    username = author_info.get("userName", "unknown")
    label = account_label or author_info.get("name", username)
    return Tweet(
        id=str(raw.get("id", "")),
        author=username,
        author_label=label,
        text=raw.get("text", ""),
        url=raw.get("url", f"https://x.com/{username}/status/{raw.get('id', '')}"),
        likes=int(raw.get("likeCount", 0)),
        retweets=int(raw.get("retweetCount", 0)),
        replies=int(raw.get("replyCount", 0)),
        created_at=raw.get("createdAt", ""),
        source=source,
        group_name=group_name,
    )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def is_relevant(tweet: Tweet, keywords: list[str]) -> bool:
    """Check if tweet text contains any relevance keyword (case-insensitive)."""
    text_lower = tweet.text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def is_retweet(tweet: Tweet) -> bool:
    return tweet.text.startswith("RT @")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(config: dict, tier: str) -> dict[str, list[Tweet]]:
    """
    Execute the full fetch -> dedup -> filter pipeline.
    Returns {group_name: [Tweet, ...]} for groups with results.
    """
    api_key = os.environ.get("TWITTERAPI_KEY", "")
    if not api_key:
        log.error("TWITTERAPI_KEY environment variable is not set!")
        return {}

    settings = config.get("settings", {})
    if tier == "hot":
        lookback_hours = settings.get("lookback_hours_hot", 13)
    else:
        lookback_hours = settings.get("lookback_hours_daily", 26)
    min_likes_accounts = settings.get("min_likes_accounts", 5)
    min_likes_search = settings.get("min_likes_search", 20)
    relevance_keywords = config.get("relevance_keywords", [])
    all_groups = config.get("groups", [])
    campaigns = config.get("campaigns", [])
    active_phases = get_active_campaign_phases(campaigns)
    all_groups = all_groups + active_phases
    since = since_date_str(lookback_hours)

    # Filter groups by tier
    if tier == "hot":
        groups = [g for g in all_groups if g.get("tier") == "hot"]
    else:
        groups = all_groups

    tier_label = "Hot" if tier == "hot" else "Full"
    log.info(f"{tier_label} tier run -- processing {len(groups)} groups "
             f"(lookback {lookback_hours}h, since {since})")

    seen_ids: set[str] = set()
    results: dict[str, list[Tweet]] = {}
    total_fetched = 0
    total_kept = 0
    api_errors = 0

    for group in groups:
        group_name = group.get("name", "Unnamed")
        accounts = group.get("accounts", [])
        searches = group.get("keyword_searches", [])
        log.info(
            f">>> Checking group: {group_name} "
            f"({len(accounts)} accounts, {len(searches)} searches)"
        )

        group_tweets: list[Tweet] = []

        # --- Account posts ---
        for acct in accounts:
            username = acct["username"]
            label = acct.get("label", username)
            query = f"from:{username} since:{since}"
            time.sleep(API_DELAY)
            raw_tweets = search_tweets(query, api_key)
            if raw_tweets is None:
                api_errors += 1
                continue

            fetched = 0
            kept = 0
            for raw in raw_tweets:
                tweet = raw_to_tweet(raw, "account", group_name, label)
                fetched += 1
                if tweet.id in seen_ids:
                    continue
                if is_retweet(tweet):
                    continue
                if tweet.likes < min_likes_accounts:
                    continue
                if not is_relevant(tweet, relevance_keywords):
                    continue
                seen_ids.add(tweet.id)
                group_tweets.append(tweet)
                kept += 1

            total_fetched += fetched
            total_kept += kept
            log.info(f"    -> from:{username}... {fetched} fetched, {kept} relevant")

        # --- Keyword searches ---
        for ks in searches:
            query_text = ks["query"]
            per_search_min = ks.get("min_likes", min_likes_search)
            query = f"{query_text} since:{since}"
            time.sleep(API_DELAY)
            raw_tweets = search_tweets(query, api_key)
            if raw_tweets is None:
                api_errors += 1
                continue

            fetched = 0
            kept = 0
            for raw in raw_tweets:
                tweet = raw_to_tweet(raw, f"keyword:{query_text}", group_name)
                fetched += 1
                if tweet.id in seen_ids:
                    continue
                if is_retweet(tweet):
                    continue
                if tweet.likes < per_search_min:
                    continue
                seen_ids.add(tweet.id)
                group_tweets.append(tweet)
                kept += 1

            total_fetched += fetched
            total_kept += kept
            log.info(
                f"    -> \"{query_text}\"... {fetched} found, {kept} new"
            )

        # Sort by engagement within group
        group_tweets.sort(key=lambda t: t.engagement_score, reverse=True)
        if group_tweets:
            results[group_name] = group_tweets

    log.info(f"\nTotal: {total_fetched} fetched, {total_kept} kept, {api_errors} errors")
    return results


# ---------------------------------------------------------------------------
# HTML digest
# ---------------------------------------------------------------------------

def truncate(text: str, max_len: int = 280) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def build_html(
    results: dict[str, list[Tweet]],
    config: dict,
    tier: str,
    max_per_group: int = 10,
) -> str:
    """Build the HTML digest email."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %b %d")
    all_groups = config.get("groups", [])

    # Merge active campaign phases into eligible groups
    campaigns = config.get("campaigns", [])
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
    total_posts = sum(len(tweets) for tweets in results.values())
    groups_with_results = len(results)

    is_hot = tier == "hot"
    title = "Architect -- Hot Digest" if is_hot else "Architect -- Daily X Digest"
    accent = "#ff6b35" if is_hot else "#638dff"

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
        ".header p { margin: 0; opacity: 0.85; font-size: 14px; }",
        ".body { padding: 20px 28px; }",
        f".group-header {{ font-size: 18px; font-weight: 600; color: #1a1a1a; "
        f"margin: 28px 0 4px 0; border-bottom: 2px solid {accent}; padding-bottom: 6px; }}",
        ".group-header:first-child { margin-top: 0; }",
        ".group-desc { background: #f0f4ff; color: #4a5568; font-size: 13px; "
        "padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; line-height: 1.4; }",
        ".tweet { border: 1px solid #e8e8e8; border-radius: 8px; padding: 14px 16px; "
        "margin-bottom: 10px; }",
        f".tweet-author {{ font-weight: 600; color: {accent}; font-size: 14px; }}",
        ".tweet-label { color: #888; font-size: 12px; margin-left: 4px; }",
        ".tweet-text { margin: 6px 0; font-size: 14px; line-height: 1.45; color: #2d2d2d; }",
        ".tweet-meta { font-size: 12px; color: #888; display: flex; "
        "flex-wrap: wrap; gap: 10px; align-items: center; }",
        f".tweet-source {{ background: #f0f4ff; color: {accent}; font-size: 11px; "
        f"padding: 2px 8px; border-radius: 10px; }}",
        f".tweet-link {{ color: {accent}; text-decoration: none; font-size: 12px; font-weight: 500; }}",
        ".footer { background: #fafafa; padding: 16px 28px; font-size: 12px; "
        "color: #888; border-top: 1px solid #eee; text-align: center; }",
        ".quiet { text-align: center; padding: 40px 20px; color: #888; font-size: 15px; }",
        ".manual-tasks { background: #fff8e1; border: 1px solid #ffe082; border-radius: 6px; "
        "padding: 10px 14px; margin-bottom: 14px; font-size: 13px; line-height: 1.5; }",
        ".manual-tasks-header { font-weight: 600; color: #e65100; margin-bottom: 4px; }",
        ".campaign-empty { color: #888; font-size: 13px; font-style: italic; "
        "padding: 12px 0; }",
        "</style></head><body>",
        "<div class='container'>",
        "<div class='header'>",
        f"<h1>{_esc(title)}</h1>",
        f"<p>{date_str} &middot; Reply opportunities for @bus_architect</p>",
        "</div>",
        "<div class='body'>",
    ]

    # Check if any group (regular or campaign) has content to show
    has_any_content = bool(results) or any(
        g.get("_is_campaign") for g in eligible_groups
    )

    if not has_any_content:
        html_parts.append(
            "<div class='quiet'>Quiet day -- no matching posts found. "
            "Bot is running normally.</div>"
        )
    else:
        for group in eligible_groups:
            gname = group["name"]
            is_campaign = group.get("_is_campaign", False)
            tweets = results.get(gname, [])[:max_per_group]

            # Regular groups: skip if no results
            if not is_campaign and gname not in results:
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

            if not tweets and is_campaign:
                html_parts.append(
                    "<div class='campaign-empty'>"
                    "No posts matched this campaign phase today.</div>"
                )
            else:
                for tw in tweets:
                    source_tag = "account" if tw.source == "account" else tw.source
                    html_parts.append("<div class='tweet'>")
                    html_parts.append(
                        f"<span class='tweet-author'>@{_esc(tw.author)}</span>"
                        f"<span class='tweet-label'>({_esc(tw.author_label)})</span>"
                    )
                    html_parts.append(
                        f"<div class='tweet-text'>{_esc(truncate(tw.text))}</div>"
                    )
                    html_parts.append("<div class='tweet-meta'>")
                    html_parts.append(
                        f"<span>&#10084;&#65039; {tw.likes} "
                        f"&#128257; {tw.retweets} "
                        f"&#128172; {tw.replies}</span>"
                    )
                    html_parts.append(
                        f"<span class='tweet-source'>{_esc(source_tag)}</span>"
                    )
                    html_parts.append(
                        f"<a class='tweet-link' href='{_esc(tw.url)}'>View tweet</a>"
                    )
                    html_parts.append("</div></div>")

    html_parts.append("</div>")  # .body

    html_parts.append(
        f"<div class='footer'>"
        f"{total_posts} posts &middot; "
        f"{total_accounts} accounts monitored &middot; "
        f"{total_searches} keyword searches &middot; "
        f"{groups_with_results} groups with results"
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
        return

    settings = config.get("settings", {})
    email_to = os.environ.get("EMAIL_TO", settings.get("email_to", ""))
    email_from = os.environ.get("EMAIL_FROM", settings.get("email_from", ""))

    if not email_to or not email_from:
        log.error("EMAIL_TO or EMAIL_FROM not configured")
        return

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %b %d")

    if total_posts == 0:
        if tier == "hot":
            subject = f"Architect -- Hot Digest -- {date_str} (quiet day)"
        else:
            subject = f"Architect -- Daily X Digest -- {date_str} (quiet day)"
    else:
        if tier == "hot":
            subject = f"Architect -- Hot Digest -- {date_str} ({total_posts} posts)"
        else:
            subject = f"Architect -- Daily X Digest -- {date_str} ({total_posts} posts)"

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Architect X/Twitter Monitor")
    parser.add_argument(
        "--tier",
        choices=["full", "hot"],
        default="full",
        help="Which tier to run: 'full' (all groups) or 'hot' (hot groups only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch tweets and save HTML locally instead of emailing",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log.info("=" * 50)
    log.info("Architect X Monitor -- starting")
    log.info(f"  TIER: {args.tier}")
    if args.dry_run:
        log.info("  MODE: dry-run (no email)")
    log.info("=" * 50)

    config = load_config()
    results = run_pipeline(config, args.tier)
    html = build_html(results, config, args.tier)
    total_posts = sum(len(tweets) for tweets in results.values())

    if args.dry_run:
        out_path = Path(__file__).parent / "digest_preview.html"
        out_path.write_text(html, encoding="utf-8")
        log.info(f"\nHTML preview saved to: {out_path}")
    else:
        send_email(html, config, args.tier, total_posts)

    if results:
        for gname, tweets in results.items():
            log.info(f"\n  {gname}: {len(tweets)} posts")
    else:
        log.info("\nNo matching posts today.")

    log.info("\nDone.")


if __name__ == "__main__":
    main()
