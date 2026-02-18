# Verefy X/Twitter Monitor

Daily digest bot that tracks curated X/Twitter accounts and keyword searches, filters for relevance and engagement, and sends a grouped HTML email via Resend.

## Tier System

Groups are tagged `tier: daily` or `tier: hot` in `config.yaml`:

- **Hot tier** (`--tier hot`): Runs 2x/day (08:00 + 20:00 UTC). Processes only `tier: hot` groups with a 13h lookback. For time-sensitive opportunities (competitors, self-mentions).
- **Full tier** (`--tier full`): Runs 1x/day (13:00 UTC). Processes ALL groups with a 26h lookback. Complete daily sweep.

## How It Works

1. Reads `config.yaml` for account lists, keyword searches, and settings
2. Filters groups by tier (hot or all)
3. Uses [twitterapi.io](https://twitterapi.io) `advanced_search` for all fetching
4. Deduplicates, filters by relevance keywords (accounts only) and engagement
5. Sends a grouped HTML digest email via [Resend](https://resend.com)

## Setup

### 1. Get API Keys

- **twitterapi.io**: Sign up at https://twitterapi.io and get an API key
- **Resend**: Sign up at https://resend.com and get an API key

### 2. Configure GitHub Secrets

Add these secrets to your GitHub repository (Settings > Secrets and variables > Actions):

| Secret | Description |
|--------|-------------|
| `TWITTERAPI_KEY` | API key from twitterapi.io |
| `RESEND_API_KEY` | API key from Resend |
| `EMAIL_TO` | Recipient email address |
| `EMAIL_FROM` | Sender address (must be verified in Resend) |

### 3. Customize Config

Edit `config.yaml` to add/remove accounts and keyword searches. Set `tier: hot` on groups that need 2x/day monitoring. Changes take effect on the next run.

## Running Locally

The script auto-loads `.env` files via `python-dotenv`. It checks `x_monitor/.env` first, then the repo root `.env`.

```bash
pip install -r requirements.txt
```

Add to your `.env` (use `=` not `:`):
```
TWITTERAPI_KEY=your-key
RESEND_API_KEY=your-key
```

Then run:

```bash
# Dry run — full tier, saves HTML to digest_preview.html (no email sent)
python monitor.py --dry-run

# Dry run — hot tier only
python monitor.py --tier hot --dry-run

# Full run — sends email
python monitor.py --tier full

# Hot run — sends email
python monitor.py --tier hot
```

The `--dry-run` flag saves HTML to `digest_preview.html` instead of sending email. Open it in your browser to check the layout.

Email addresses fall back to `config.yaml` values (`email_to`, `email_from`) if the `EMAIL_TO`/`EMAIL_FROM` env vars aren't set.

## Running via GitHub Actions

The workflow runs automatically:
- **08:00 UTC** and **20:00 UTC**: Hot tier (competitors + pain points)
- **13:00 UTC**: Full tier (all groups)

You can also trigger manually from the Actions tab with a tier choice.

## Config Structure

- **settings**: Lookback windows per tier, engagement thresholds, email addresses
- **relevance_keywords**: Account posts must match at least one keyword (keyword searches skip this filter)
- **groups**: Each group has a name, tier (daily/hot), description (reply guidance), accounts, and keyword searches

## Validating Config

```bash
python test_config.py
```

Checks YAML structure, validates tier fields, counts accounts/searches per tier, and estimates API calls.
