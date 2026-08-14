"""Stateless, credential-free discovery and import helpers.

Every public function in this package returns fresh dictionaries compatible with
``app.saas.schemas.JobCreate``.  The package deliberately has no database or
filesystem access: callers decide which authenticated tenant owns the returned
jobs and perform any persistence themselves.
"""

from .common import NormalizedJob
from .importers import parse_csv_bytes, parse_spreadsheet_bytes, parse_xlsx_bytes
from .linkedin import discover_linkedin_guest, parse_linkedin_guest_html
from .providers import (
    detect_provider,
    discover_provider_urls,
    extract_provider_urls,
    public_company_form_target,
)
from .public_ats import (
    MAX_PUBLIC_ATS_BOARDS,
    MAX_PUBLIC_ATS_RESULTS,
    canonical_public_ats_board_url,
    discover_public_ats_board,
    parse_public_ats_board_url,
)
from .referrals import parse_referral_digest, referral_digest_summary
from .rss import DEFAULT_RSS_FEEDS, discover_rss, parse_rss_feed
from .telegram import (
    DEFAULT_TELEGRAM_CHANNELS,
    discover_telegram,
    parse_telegram_preview,
)

__all__ = [
    "DEFAULT_RSS_FEEDS",
    "DEFAULT_TELEGRAM_CHANNELS",
    "MAX_PUBLIC_ATS_BOARDS",
    "MAX_PUBLIC_ATS_RESULTS",
    "NormalizedJob",
    "detect_provider",
    "canonical_public_ats_board_url",
    "discover_linkedin_guest",
    "discover_provider_urls",
    "discover_public_ats_board",
    "discover_rss",
    "discover_telegram",
    "extract_provider_urls",
    "parse_csv_bytes",
    "parse_linkedin_guest_html",
    "parse_referral_digest",
    "referral_digest_summary",
    "parse_public_ats_board_url",
    "parse_rss_feed",
    "parse_spreadsheet_bytes",
    "parse_telegram_preview",
    "parse_xlsx_bytes",
    "public_company_form_target",
]
