"""Tests for tagger.py — source tier, company extraction, financial detection, priority."""

from google_alerts_enricher.tagger import (
    classify_source_tier,
    derive_priority,
    detect_financial_impact,
    extract_company,
)


# ---------------------------------------------------------------------------
# Source tier classification
# ---------------------------------------------------------------------------

class TestClassifySourceTier:
    def test_t1_domain(self):
        assert classify_source_tier("reuters.com") == "t1"

    def test_t1_with_www(self):
        assert classify_source_tier("www.reuters.com") == "t1"

    def test_t1_bloomberg(self):
        assert classify_source_tier("bloomberg.com") == "t1"

    def test_t2_domain(self):
        assert classify_source_tier("politico.eu") == "t2"

    def test_t2_with_www(self):
        assert classify_source_tier("www.darkreading.com") == "t2"

    def test_t3_unknown_domain(self):
        assert classify_source_tier("randomsite.com") == "t3"

    def test_t3_empty_string(self):
        assert classify_source_tier("") == "t3"

    def test_case_insensitive(self):
        assert classify_source_tier("REUTERS.COM") == "t1"


# ---------------------------------------------------------------------------
# Company extraction
# ---------------------------------------------------------------------------

class TestExtractCompany:
    def test_deepfake_of_company(self):
        text = "A deepfake of Pfizer CEO was used in a scam video"
        assert extract_company(text) == "Pfizer"

    def test_company_breach(self):
        text = "Microsoft data leak exposed customer records"
        assert extract_company(text) == "Microsoft"

    def test_targeting_company(self):
        text = "Hackers targeting Goldman Sachs with phishing emails"
        assert extract_company(text) == "Goldman Sachs"

    def test_no_company(self):
        text = "EU discusses new regulations on artificial intelligence"
        assert extract_company(text) == ""

    def test_empty_string(self):
        assert extract_company("") == ""

    def test_false_positive_filtered(self):
        text = "Deepfake of United States president shown online"
        assert extract_company(text) == ""

    def test_company_stock(self):
        text = "Tesla stock dropped 12% after the announcement"
        assert extract_company(text) == "Tesla"

    def test_impersonating_company(self):
        text = "Scammers impersonating Apple support to steal credentials"
        assert extract_company(text) == "Apple"

    def test_shareholder_notice(self):
        text = "Picard Medical shareholder notice issued by law firm"
        assert extract_company(text) == "Picard Medical"

    def test_company_lawsuit(self):
        text = "Acme Corp lawsuit filed over patent infringement"
        assert extract_company(text) == "Acme Corp"

    def test_company_acquisition(self):
        text = "Broadcom acquisition of VMware faces regulatory scrutiny"
        assert extract_company(text) == "Broadcom"


# ---------------------------------------------------------------------------
# Financial impact detection
# ---------------------------------------------------------------------------

class TestDetectFinancialImpact:
    def test_dollar_amount(self):
        assert detect_financial_impact("The breach cost $15 billion") is True

    def test_euro_amount(self):
        assert detect_financial_impact("Losses estimated at €2.3 million") is True

    def test_stock_dropped(self):
        assert detect_financial_impact("stock dropped 12%") is True

    def test_shares_fell(self):
        assert detect_financial_impact("shares fell sharply after the news") is True

    def test_percentage_decline(self):
        assert detect_financial_impact("declined 15% in Q3") is True

    def test_market_cap(self):
        assert detect_financial_impact("The company's market capitalization shrank") is True

    def test_no_financial_data(self):
        assert detect_financial_impact("General discussion about cybersecurity trends") is False

    def test_empty_string(self):
        assert detect_financial_impact("") is False

    def test_revenue(self):
        assert detect_financial_impact("revenue of $500 million last quarter") is True


# ---------------------------------------------------------------------------
# Priority derivation
# ---------------------------------------------------------------------------

class TestDerivePriority:
    def test_high_t1_company_financial(self):
        assert derive_priority("t1", "Pfizer", True) == "high"

    def test_high_t1_company(self):
        assert derive_priority("t1", "Pfizer", False) == "high"

    def test_high_t1_financial(self):
        assert derive_priority("t1", "", True) == "high"

    def test_high_company_financial(self):
        assert derive_priority("t3", "Pfizer", True) == "high"

    def test_medium_t1_only(self):
        assert derive_priority("t1", "", False) == "medium"

    def test_medium_t2_company(self):
        assert derive_priority("t2", "Pfizer", False) == "medium"

    def test_medium_t2_only(self):
        assert derive_priority("t2", "", False) == "medium"

    def test_low_t3_nothing(self):
        assert derive_priority("t3", "", False) == "low"

    def test_medium_t3_company(self):
        assert derive_priority("t3", "Pfizer", False) == "medium"

    def test_medium_t3_financial(self):
        assert derive_priority("t3", "", True) == "medium"
