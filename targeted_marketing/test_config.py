"""
Config validation test for the X Monitor.
Run: python targeted_marketing/test_config.py
"""

from datetime import date
from pathlib import Path
from collections import Counter

import yaml


def main() -> None:
    config_path = Path(__file__).parent / "config.yaml"
    assert config_path.exists(), f"config.yaml not found at {config_path}"

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Validate top-level structure
    assert "settings" in config, "Missing 'settings' key"
    assert "relevance_keywords" in config, "Missing 'relevance_keywords' key"
    assert "groups" in config, "Missing 'groups' key"
    assert isinstance(config["groups"], list), "'groups' must be a list"

    settings = config["settings"]
    for key in ("lookback_hours_daily", "lookback_hours_hot",
                "min_likes_accounts", "min_likes_search"):
        assert key in settings, f"Missing setting: {key}"

    # Count per tier
    all_usernames: list[str] = []
    group_names: list[str] = []
    tier_counts: dict[str, int] = {"daily": 0, "hot": 0}
    tier_accounts: dict[str, int] = {"daily": 0, "hot": 0}
    tier_searches: dict[str, int] = {"daily": 0, "hot": 0}

    for group in config["groups"]:
        assert "name" in group, "Group missing 'name'"
        assert "tier" in group, f"Group '{group['name']}' missing 'tier'"
        tier = group["tier"]
        assert tier in ("daily", "hot"), (
            f"Group '{group['name']}' has invalid tier '{tier}' "
            f"(must be 'daily' or 'hot')"
        )

        group_names.append(group["name"])
        tier_counts[tier] += 1

        accounts = group.get("accounts", [])
        searches = group.get("keyword_searches", [])

        for acct in accounts:
            assert "username" in acct, f"Account in '{group['name']}' missing 'username'"
            all_usernames.append(acct["username"])

        for ks in searches:
            assert "query" in ks, f"Search in '{group['name']}' missing 'query'"

        tier_accounts[tier] += len(accounts)
        tier_searches[tier] += len(searches)

    # Check for duplicate usernames (warn only)
    username_counts = Counter(all_usernames)
    dupes = {u: c for u, c in username_counts.items() if c > 1}
    if dupes:
        print(f"  [WARN] Duplicate usernames across groups: {dupes}")

    # Check for duplicate group names
    name_counts = Counter(group_names)
    dupe_names = {n: c for n, c in name_counts.items() if c > 1}
    if dupe_names:
        print(f"  [WARN] Duplicate group names: {dupe_names}")

    # API call estimates
    total_accounts = tier_accounts["daily"] + tier_accounts["hot"]
    total_searches = tier_searches["daily"] + tier_searches["hot"]
    full_calls = total_accounts + total_searches
    hot_calls = tier_accounts["hot"] + tier_searches["hot"]

    # -------------------------------------------------------------------
    # Validate campaigns (if present)
    # -------------------------------------------------------------------
    campaign_count = 0
    phase_count = 0
    campaigns = config.get("campaigns", [])
    if campaigns is not None:
        assert isinstance(campaigns, list), "'campaigns' must be a list"
        for campaign in campaigns:
            assert "name" in campaign, "Campaign missing 'name'"
            cname = campaign["name"]
            assert "phases" in campaign, f"Campaign '{cname}' missing 'phases'"
            assert isinstance(campaign["phases"], list), (
                f"Campaign '{cname}': 'phases' must be a list"
            )
            campaign_count += 1

            for phase in campaign["phases"]:
                assert "name" in phase, (
                    f"Campaign '{cname}': phase missing 'name'"
                )
                pname = phase["name"]
                assert "start" in phase, (
                    f"Campaign '{cname}' phase '{pname}' missing 'start'"
                )
                assert "end" in phase, (
                    f"Campaign '{cname}' phase '{pname}' missing 'end'"
                )

                # Validate ISO dates
                try:
                    start = date.fromisoformat(phase["start"])
                except ValueError:
                    raise AssertionError(
                        f"Campaign '{cname}' phase '{pname}': "
                        f"invalid start date '{phase['start']}'"
                    )
                try:
                    end = date.fromisoformat(phase["end"])
                except ValueError:
                    raise AssertionError(
                        f"Campaign '{cname}' phase '{pname}': "
                        f"invalid end date '{phase['end']}'"
                    )

                assert end >= start, (
                    f"Campaign '{cname}' phase '{pname}': "
                    f"end ({end}) is before start ({start})"
                )

                # Validate tier if specified
                if "tier" in phase:
                    assert phase["tier"] in ("daily", "hot"), (
                        f"Campaign '{cname}' phase '{pname}': "
                        f"invalid tier '{phase['tier']}' (must be 'daily' or 'hot')"
                    )

                # Warn if no accounts or keyword_searches
                has_accounts = bool(phase.get("accounts"))
                has_searches = bool(phase.get("keyword_searches"))
                if not has_accounts and not has_searches:
                    print(
                        f"  [WARN] Campaign '{cname}' phase '{pname}' "
                        f"has no accounts or keyword_searches"
                    )

                phase_count += 1

    # Summary
    print(f"  Config OK")
    print(f"  {len(config['groups'])} groups: "
          f"{tier_counts['daily']} daily, {tier_counts['hot']} hot")
    print(f"  Daily tier: {tier_accounts['daily']} accounts, "
          f"{tier_searches['daily']} keyword searches")
    print(f"  Hot tier: {tier_accounts['hot']} accounts, "
          f"{tier_searches['hot']} keyword searches")
    print(f"  Total API calls -- full run: ~{full_calls}, hot run: ~{hot_calls}")
    print(f"  {len(config['relevance_keywords'])} relevance keywords")
    print(f"  Lookback: {settings['lookback_hours_daily']}h (daily), "
          f"{settings['lookback_hours_hot']}h (hot)")
    if campaign_count:
        print(f"  {campaign_count} campaigns, {phase_count} phases")


if __name__ == "__main__":
    main()
