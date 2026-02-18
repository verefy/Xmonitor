"""Unit tests for get_active_campaign_phases()."""

from datetime import date

import pytest

from targeted_marketing.monitor import get_active_campaign_phases


def _make_campaign(name="Test Campaign", phases=None):
    """Helper to build a campaign dict."""
    return {"name": name, "description": "Test desc", "phases": phases or []}


def _make_phase(
    name="Phase 1",
    start="2026-05-01",
    end="2026-05-10",
    tier=None,
    accounts=None,
    keyword_searches=None,
    frequency=None,
    manual_tasks=None,
):
    phase = {"name": name, "start": start, "end": end}
    if tier is not None:
        phase["tier"] = tier
    if accounts is not None:
        phase["accounts"] = accounts
    if keyword_searches is not None:
        phase["keyword_searches"] = keyword_searches
    if frequency is not None:
        phase["frequency"] = frequency
    if manual_tasks is not None:
        phase["manual_tasks"] = manual_tasks
    return phase


# --- Basic date-range tests ---


class TestDateFiltering:
    def test_phase_in_range_returned(self):
        phase = _make_phase(
            start="2026-05-01",
            end="2026-05-10",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert len(result) == 1

    def test_start_equals_today_is_active(self):
        phase = _make_phase(
            start="2026-05-01",
            end="2026-05-10",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 1))
        assert len(result) == 1

    def test_end_equals_today_is_active(self):
        phase = _make_phase(
            start="2026-05-01",
            end="2026-05-10",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 10))
        assert len(result) == 1

    def test_future_phase_not_returned(self):
        phase = _make_phase(
            start="2026-06-01",
            end="2026-06-10",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert len(result) == 0

    def test_past_phase_not_returned(self):
        phase = _make_phase(
            start="2026-04-01",
            end="2026-04-10",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert len(result) == 0

    def test_two_overlapping_phases_both_returned(self):
        phase_a = _make_phase(
            name="A",
            start="2026-05-01",
            end="2026-05-10",
            keyword_searches=[{"query": "a", "min_likes": 0}],
        )
        phase_b = _make_phase(
            name="B",
            start="2026-05-05",
            end="2026-05-15",
            keyword_searches=[{"query": "b", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase_a, phase_b])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 7))
        assert len(result) == 2


# --- Edge cases ---


class TestEdgeCases:
    def test_empty_campaigns_returns_empty(self):
        result = get_active_campaign_phases([], today=date(2026, 5, 5))
        assert result == []

    def test_malformed_date_raises_value_error(self):
        phase = _make_phase(
            start="not-a-date",
            end="2026-05-10",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        with pytest.raises(ValueError):
            get_active_campaign_phases(campaigns, today=date(2026, 5, 5))

    def test_phase_with_no_accounts_or_searches_skipped(self):
        phase = _make_phase(start="2026-05-01", end="2026-05-10")
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert len(result) == 0


# --- Field mapping ---


class TestFieldMapping:
    def test_missing_tier_defaults_to_hot(self):
        phase = _make_phase(
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        # No tier set in _make_phase when tier=None
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert result[0]["tier"] == "hot"

    def test_explicit_tier_preserved(self):
        phase = _make_phase(
            tier="daily",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert result[0]["tier"] == "daily"

    def test_returned_dict_has_group_compatible_fields(self):
        phase = _make_phase(
            tier="hot",
            accounts=[{"username": "test_user", "label": "Test"}],
            keyword_searches=[{"query": "test q", "min_likes": 5}],
        )
        campaigns = [_make_campaign(name="My Campaign", phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        group = result[0]

        # Standard group fields
        assert "name" in group
        assert "description" in group
        assert "tier" in group
        assert "accounts" in group
        assert "keyword_searches" in group

        # Name format
        assert group["name"] == "My Campaign -- Phase 1"

        # Data passed through
        assert group["accounts"] == [{"username": "test_user", "label": "Test"}]
        assert group["keyword_searches"] == [{"query": "test q", "min_likes": 5}]

    def test_manual_tasks_passed_through(self):
        tasks = [
            {"task": "Do something", "frequency": "Once"},
            {"task": "Do another thing", "frequency": "Daily"},
        ]
        phase = _make_phase(
            manual_tasks=tasks,
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert result[0]["_manual_tasks"] == tasks

    def test_manual_tasks_defaults_to_empty_list(self):
        phase = _make_phase(
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        assert result[0]["_manual_tasks"] == []

    def test_campaign_metadata_fields(self):
        phase = _make_phase(
            frequency="2h",
            keyword_searches=[{"query": "test", "min_likes": 0}],
        )
        campaigns = [_make_campaign(name="Summit", phases=[phase])]
        result = get_active_campaign_phases(campaigns, today=date(2026, 5, 5))
        group = result[0]
        assert group["_campaign_name"] == "Summit"
        assert group["_phase_name"] == "Phase 1"
        assert group["_frequency"] == "2h"
        assert group["_is_campaign"] is True
