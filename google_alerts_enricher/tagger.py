"""
Pure-logic tagging for Google Alerts enrichment.
All tagging functions take strings and return strings/bools.
Source-tier domain lists are loaded from source_tiers.yaml.
"""

import re
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Source-tier domain lists (loaded from YAML)
# ---------------------------------------------------------------------------

_TIERS_PATH = Path(__file__).parent / "source_tiers.yaml"

def _load_tiers() -> tuple[set[str], set[str]]:
    with open(_TIERS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return set(data.get("t1", [])), set(data.get("t2", []))

T1_DOMAINS, T2_DOMAINS = _load_tiers()

# ---------------------------------------------------------------------------
# False positives — generic terms that look like company names
# ---------------------------------------------------------------------------

FALSE_POSITIVES: set[str] = {
    # Common English words that appear Title Case in headlines
    "the", "and", "and the", "or", "but", "for", "with", "from",
    "the collapse", "and the collapse", "the digital", "and the digital",
    "the new", "the world", "the global", "the future",
    # Countries / regions
    "european union", "eu", "united states", "united kingdom",
    "uk", "us", "china", "russia", "india", "japan",
    "germany", "france", "australia", "canada", "brazil",
    # Government bodies
    "congress", "senate", "parliament", "government",
    "fbi", "cia", "nsa", "sec", "ftc", "fda",
    "white house", "pentagon", "un", "nato", "who",
    "world bank", "imf",
}


def _strip_www(domain: str) -> str:
    """Remove leading www. from a domain."""
    d = domain.lower().strip()
    if d.startswith("www."):
        d = d[4:]
    return d


def classify_source_tier(domain: str) -> str:
    """Return 't1', 't2', or 't3' based on the publisher domain."""
    d = _strip_www(domain)
    if d in T1_DOMAINS:
        return "t1"
    if d in T2_DOMAINS:
        return "t2"
    return "t3"


# ---------------------------------------------------------------------------
# Company extraction — regex near contextual keywords
# ---------------------------------------------------------------------------

# Patterns that suggest a company name follows or precedes.
# Company capture: first word [A-Z][A-Za-z&.-]+, subsequent words must be
# Title Case (uppercase then lowercase) to avoid grabbing acronyms like CEO.
# Corporate suffixes — if a Title Case phrase ends with one of these,
# it's almost certainly a company/org name, regardless of context.
_CORPORATE_SUFFIXES = (
    "Medical", "Pharma", "Pharmaceutical", "Therapeutics", "Biotech",
    "Tech", "Technologies", "Technology", "Software", "Systems", "Solutions",
    "Corp", "Corporation", "Inc", "Ltd", "Limited", "Group", "Holdings",
    "Partners", "Capital", "Financial", "Finance", "Ventures", "Labs",
    "Networks", "Dynamics", "Energy", "Logistics", "Agency",
    "Analytics", "Robotics", "Aerospace", "Defense", "Motors",
    "Electric", "Insurance", "Sciences", "Entertainment", "Studios",
)

_COMPANY_PATTERNS: list[re.Pattern] = [
    # HIGH CONFIDENCE: keyword context + company name
    # "deepfake of <Company>", "impersonating <Company>"
    re.compile(
        r"(?i:deepfake|impersonat\w+|spoof\w*|phish\w*|attack\w*|breach\w*|hack\w*|targeting|target)\s+"
        r"(?:of\s+|at\s+|on\s+|against\s+)?"
        r"([A-Z][A-Za-z&.\-]+(?:\s+[A-Z][a-z][A-Za-z&.\-]*){0,2})",
    ),
    # "<Company> keyword" — security, financial, legal, corporate actions
    re.compile(
        r"([A-Z][A-Za-z&.\-]+(?:\s+[A-Z][a-z][A-Za-z&.\-]*){0,2})\s+"
        r"(?:CEO|CFO|CTO|CMO|COO|CIO|CISO|stock|shares|breach|data leak|hack|attack|incident|vulnerability"
        r"|shareholder|lawsuit|settlement|investigation|fraud|penalty|fine|acquisition|merger"
        r"|revenue|earnings|IPO|recall|bankrupt\w*|insolven\w*)",
    ),
    # "<Company>'s" in context
    re.compile(
        r"([A-Z][A-Za-z&.\-]+(?:\s+[A-Z][a-z][A-Za-z&.\-]*){0,2})(?:'s)\s+"
        r"(?:stock|share|data|security|network|system|platform|customer"
        r"|revenue|earnings|shareholder|lawsuit|merger|acquisition)",
    ),
    # FALLBACK: Title Case phrase ending with a corporate suffix (any context)
    # Limited to 1-2 words + suffix to avoid grabbing headline fragments.
    re.compile(
        r"([A-Z][A-Za-z&.\-]+(?:\s+[A-Z][a-z][A-Za-z&.\-]*){0,1}\s+"
        r"(?:" + "|".join(_CORPORATE_SUFFIXES) + r"))"
        r"(?:\s|,|\.|$)",
    ),
]


def _validate_match(match: re.Match) -> str | None:
    """Validate a regex match and return the company name, or None if invalid."""
    # Normalize: collapse newlines/tabs/extra spaces into single spaces
    name = " ".join(match.group(1).split()).rstrip(".")
    if name.lower() in FALSE_POSITIVES:
        return None
    first_word = name.split()[0].lower() if name.split() else ""
    if first_word in {"and", "or", "but", "the", "for", "with", "from",
                      "this", "that", "how", "why", "what", "when", "its",
                      "new", "all", "not", "are", "was", "has", "had",
                      "will", "can", "may", "who", "our", "any", "another",
                      "some", "most", "every", "each", "both", "many",
                      "several", "after", "before", "while", "where",
                      "about", "into", "over", "under", "between"}:
        return None
    if len(name) < 2:
        return None
    return name


def extract_company(text: str, headline: str = "") -> str:
    """Extract company names from headline and text.

    Checks the headline first (higher signal), then the full text.
    Returns comma-separated company names (deduplicated, headline matches first),
    or empty string if none found.
    """
    seen: set[str] = set()
    results: list[str] = []

    for source in (headline, text):
        if not source:
            continue
        for pattern in _COMPANY_PATTERNS:
            for match in pattern.finditer(source):
                name = _validate_match(match)
                if not name:
                    continue
                name_lower = name.lower()
                # Skip exact duplicates
                if name_lower in seen:
                    continue
                # Skip if this name contains or is contained by an existing match
                if any(name_lower in s or s in name_lower for s in seen):
                    continue
                seen.add(name_lower)
                results.append(name)

    return ", ".join(results)


# ---------------------------------------------------------------------------
# Financial-impact detection
# ---------------------------------------------------------------------------

_FINANCIAL_PATTERNS: list[re.Pattern] = [
    # Dollar/euro amounts: $15 billion, €2.3 million, $500,000
    re.compile(r"[\$€£]\s?\d[\d,]*\.?\d*\s?(?:billion|million|trillion|bn|mn|m|b|k)?", re.IGNORECASE),
    # "X million/billion dollars/euros"
    re.compile(r"\d[\d,]*\.?\d*\s+(?:billion|million|trillion)\s+(?:dollars|euros|pounds)", re.IGNORECASE),
    # Stock language: "stock dropped", "shares fell", "market cap"
    re.compile(r"(?:stock|shares?|equity)\s+(?:dropped|fell|plunged|surged|rose|declined|tumbled|crashed)", re.IGNORECASE),
    # Percentage drops/gains: "12% drop", "fell 8.5%", "declined 15 percent"
    re.compile(r"(?:dropped|fell|declined|lost|plunged|surged|gained|rose)\s+\d+\.?\d*\s*%", re.IGNORECASE),
    re.compile(r"\d+\.?\d*\s*(?:%|percent)\s+(?:drop|decline|loss|increase|gain|surge|rise)", re.IGNORECASE),
    # Market cap
    re.compile(r"market\s+cap(?:italization)?", re.IGNORECASE),
    # Revenue / earnings
    re.compile(r"(?:revenue|earnings|profit|loss)\s+(?:of\s+)?[\$€£]\d", re.IGNORECASE),
]


def detect_financial_impact(text: str) -> bool:
    """Return True if the text contains financial data or market impact language."""
    if not text:
        return False
    return any(p.search(text) for p in _FINANCIAL_PATTERNS)


# ---------------------------------------------------------------------------
# Priority derivation
# ---------------------------------------------------------------------------


def derive_priority(source_tier: str, named_company: str, has_financial: bool) -> str:
    """Derive alert priority from enrichment signals.

    Rules:
    - high: any two of (t1, named_company, has_financial) OR t1 + named_company
    - low:  t3 with no company and no financial data
    - medium: everything else
    """
    signals = 0
    if source_tier == "t1":
        signals += 1
    if named_company:
        signals += 1
    if has_financial:
        signals += 1

    if signals >= 2:
        return "high"
    if source_tier == "t3" and not named_company and not has_financial:
        return "low"
    return "medium"
