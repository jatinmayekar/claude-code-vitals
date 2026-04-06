"""OAuth endpoint integration for claude_code_vitals.

Uses the community-discovered Anthropic OAuth endpoint to get richer
rate limit data (exact utilization %, reset timestamps, fallback status).

This is SUPPLEMENTARY to the statusline stdin JSON — not required.
The OAuth endpoint is undocumented and may change without notice.

Data flow:
  ~/.claude/.credentials.json → Bearer token → GET /api/oauth/usage → cache → merge with statusline data
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from .config import Config, CLAUDE_CREDENTIALS_PATH
from .logger import RateLimitSnapshot


OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
MIN_POLL_INTERVAL_SECONDS = 60  # Don't hit the endpoint more than once per minute


@dataclass
class OAuthUsageData:
    """Parsed response from the OAuth usage endpoint."""
    five_hour_utilization: Optional[float] = None
    five_hour_resets_at: Optional[str] = None
    seven_day_utilization: Optional[float] = None
    seven_day_resets_at: Optional[str] = None
    status: Optional[str] = None  # "allowed" | "rate_limited" | etc.
    fallback: Optional[str] = None
    fetched_at: Optional[str] = None


def get_oauth_token() -> Optional[str]:
    """Read the OAuth access token from Claude Code's credentials file.
    
    Claude Code stores this at ~/.claude/.credentials.json under
    the key claudeAiOauth.accessToken.
    """
    creds_path = CLAUDE_CREDENTIALS_PATH
    if not creds_path.exists():
        return None

    try:
        data = json.loads(creds_path.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, KeyError, PermissionError):
        return None


def fetch_usage(config: Config) -> Optional[OAuthUsageData]:
    """Fetch usage data from the Anthropic OAuth endpoint.
    
    Respects rate limiting: won't call more than once per MIN_POLL_INTERVAL_SECONDS.
    Caches results to ~/.claude-code-vitals/usage-cache.json.
    
    Returns cached data if the cache is still fresh.
    Returns None if no token available or request fails.
    """
    # Check cache freshness
    cached = _load_cache(config)
    if cached is not None:
        return cached

    # Get token
    token = get_oauth_token()
    if not token:
        return None

    # Make the request
    try:
        req = urllib.request.Request(
            OAUTH_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "anthropic-beta": OAUTH_BETA_HEADER,
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            OSError, TimeoutError):
        return None

    # Parse response
    # Expected shape:
    # {
    #   "five_hour": {"utilization": 42.0, "resets_at": "2026-03-30T19:00:00Z"},
    #   "seven_day": {"utilization": 67.0, "resets_at": "2026-04-05T08:00:00Z"},
    #   "status": "allowed",
    #   "fallback": "available"
    # }
    now = datetime.now(timezone.utc).isoformat()

    five_hour = data.get("five_hour", {})
    seven_day = data.get("seven_day", {})

    usage = OAuthUsageData(
        five_hour_utilization=five_hour.get("utilization"),
        five_hour_resets_at=five_hour.get("resets_at"),
        seven_day_utilization=seven_day.get("utilization"),
        seven_day_resets_at=seven_day.get("resets_at"),
        status=data.get("status"),
        fallback=data.get("fallback"),
        fetched_at=now,
    )

    # Cache it
    _save_cache(usage, config)

    return usage


def oauth_to_snapshot(usage: OAuthUsageData, model_id: str = "unknown",
                      model_name: str = "unknown") -> RateLimitSnapshot:
    """Convert OAuth usage data to a RateLimitSnapshot for unified storage."""
    return RateLimitSnapshot(
        ts=usage.fetched_at or datetime.now(timezone.utc).isoformat(),
        provider="anthropic",
        model_id=model_id,
        model_name=model_name,
        session_5h_pct=usage.five_hour_utilization,
        session_5h_reset=usage.five_hour_resets_at,
        weekly_7d_pct=usage.seven_day_utilization,
        weekly_7d_reset=usage.seven_day_resets_at,
        context_used_pct=None,
        context_window_size=None,
        session_cost_usd=None,
        source="oauth",
    )


def _load_cache(config: Config) -> Optional[OAuthUsageData]:
    """Load cached usage data if it's still fresh."""
    cache_path = config.cache_path
    if not cache_path.exists():
        return None

    try:
        data = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, PermissionError):
        return None

    # Check age
    fetched_at = data.get("fetched_at")
    if not fetched_at:
        return None

    try:
        fetched_dt = datetime.fromisoformat(fetched_at)
        age = (datetime.now(timezone.utc) - fetched_dt).total_seconds()
        if age > MIN_POLL_INTERVAL_SECONDS:
            return None  # Cache expired
    except (ValueError, TypeError):
        return None

    return OAuthUsageData(
        five_hour_utilization=data.get("five_hour_utilization"),
        five_hour_resets_at=data.get("five_hour_resets_at"),
        seven_day_utilization=data.get("seven_day_utilization"),
        seven_day_resets_at=data.get("seven_day_resets_at"),
        status=data.get("status"),
        fallback=data.get("fallback"),
        fetched_at=fetched_at,
    )


def _save_cache(usage: OAuthUsageData, config: Config) -> None:
    """Save usage data to cache file."""
    config.ensure_data_dir()
    data = {
        "five_hour_utilization": usage.five_hour_utilization,
        "five_hour_resets_at": usage.five_hour_resets_at,
        "seven_day_utilization": usage.seven_day_utilization,
        "seven_day_resets_at": usage.seven_day_resets_at,
        "status": usage.status,
        "fallback": usage.fallback,
        "fetched_at": usage.fetched_at,
    }
    config.cache_path.write_text(json.dumps(data, indent=2))
