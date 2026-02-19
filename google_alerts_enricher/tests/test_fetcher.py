"""Tests for fetcher.py — mocked trafilatura calls."""

from unittest.mock import patch

from google_alerts_enricher.fetcher import (
    FETCH_FAILED,
    SKIP_DOMAINS,
    extract_domain,
    fetch_article_text,
)


class TestExtractDomain:
    def test_simple_url(self):
        assert extract_domain("https://reuters.com/article/123") == "reuters.com"

    def test_www_prefix_stripped(self):
        assert extract_domain("https://www.reuters.com/article/123") == "reuters.com"

    def test_subdomain_preserved(self):
        assert extract_domain("https://news.bbc.co.uk/story") == "news.bbc.co.uk"

    def test_empty_string(self):
        assert extract_domain("") == ""

    def test_invalid_url(self):
        assert extract_domain("not a url") == ""


class TestFetchArticleText:
    @patch("google_alerts_enricher.fetcher.trafilatura")
    def test_successful_extraction(self, mock_traf):
        mock_traf.fetch_url.return_value = "<html>content</html>"
        mock_traf.extract.return_value = "Article body text here"
        result = fetch_article_text("https://example.com/article")
        assert result == "Article body text here"

    @patch("google_alerts_enricher.fetcher.trafilatura")
    def test_truncation_to_2000_chars(self, mock_traf):
        mock_traf.fetch_url.return_value = "<html>content</html>"
        mock_traf.extract.return_value = "A" * 5000
        result = fetch_article_text("https://example.com/article")
        assert len(result) == 2000

    @patch("google_alerts_enricher.fetcher.trafilatura")
    def test_download_failure(self, mock_traf):
        mock_traf.fetch_url.return_value = None
        result = fetch_article_text("https://example.com/article")
        assert result == FETCH_FAILED

    @patch("google_alerts_enricher.fetcher.trafilatura")
    def test_extraction_failure(self, mock_traf):
        mock_traf.fetch_url.return_value = "<html>content</html>"
        mock_traf.extract.return_value = None
        result = fetch_article_text("https://example.com/article")
        assert result == FETCH_FAILED

    @patch("google_alerts_enricher.fetcher.trafilatura")
    def test_network_exception(self, mock_traf):
        mock_traf.fetch_url.side_effect = ConnectionError("timeout")
        result = fetch_article_text("https://example.com/article")
        assert result == FETCH_FAILED

    def test_skip_domain(self):
        SKIP_DOMAINS.add("blocked.com")
        try:
            result = fetch_article_text("https://blocked.com/page")
            assert result == FETCH_FAILED
        finally:
            SKIP_DOMAINS.discard("blocked.com")

    @patch("google_alerts_enricher.fetcher.trafilatura")
    def test_google_redirect_resolved(self, mock_traf):
        mock_traf.fetch_url.return_value = "<html>content</html>"
        mock_traf.extract.return_value = "Real article text"
        url = "https://www.google.com/url?q=https://reuters.com/article/123&sa=U"
        result = fetch_article_text(url)
        mock_traf.fetch_url.assert_called_once_with("https://reuters.com/article/123")
        assert result == "Real article text"
