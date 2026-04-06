"""Data logger for claude_code_vitals.

Reads Claude Code statusLine JSON from stdin, extracts rate limit fields,
and appends timestamped snapshots to ~/.claude-code-vitals/history.jsonl.

Zero API calls. Zero token cost. Pure passive observation.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

from .config import Config


@dataclass
class RateLimitSnapshot:
    """A single point-in-time observation of rate limit ceilings."""

    # Timestamp
    ts: str  # ISO 8601

    # Provider / model context
    provider: str  # "anthropic", "openai", etc.
    model_id: str  # "claude-opus-4-6"
    model_name: str  # "Opus 4.6"

    # Rate limit utilization (from statusline JSON rate_limits field)
    session_5h_pct: Optional[float]  # 5-hour window utilization %
    session_5h_reset: Optional[str]  # ISO 8601 reset time
    weekly_7d_pct: Optional[float]  # 7-day window utilization %
    weekly_7d_reset: Optional[str]  # ISO 8601 reset time

    # Context window
    context_used_pct: Optional[float]
    context_window_size: Optional[int]

    # Token counts (cumulative for the session)
    total_input_tokens: Optional[int] = None
    total_output_tokens: Optional[int] = None

    # Cache tokens (cumulative — diff consecutive readings for per-prompt)
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None

    # Cost tracking
    session_cost_usd: Optional[float] = None

    # Source metadata
    source: str = "statusline"  # "statusline" | "oauth"

    # Session tracking
    session_id: Optional[str] = None

    def to_json_line(self) -> str:
        """Serialize to a single JSON line for .jsonl storage."""
        d = asdict(self)
        # Remove None values to keep lines compact
        d = {k: v for k, v in d.items() if v is not None}
        return json.dumps(d, separators=(",", ":"))


def parse_statusline_json(raw: str) -> Optional[dict]:
    """Parse the JSON blob Claude Code sends to statusline scripts via stdin.
    
    Expected shape (relevant fields):
    {
        "model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6"},
        "rate_limits": {
            "session": {"used_percentage": 42, "resets_at": "..."},
            "weekly": {"used_percentage": 67, "resets_at": "..."}
        },
        "context_window": {"used_percentage": 12, "context_window_size": 200000},
        "cost": {"total_cost_usd": 0.80}
    }
    """
    try:
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_reset_time(value) -> Optional[str]:
    """Normalize resets_at to ISO 8601 string.

    Claude Code sends either a Unix timestamp (int) or an ISO string.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value)


def extract_snapshot(data: dict) -> Optional[RateLimitSnapshot]:
    """Extract a RateLimitSnapshot from parsed statusline JSON."""
    if not data:
        return None

    now = datetime.now(timezone.utc).isoformat()

    # Model info
    model = data.get("model", {})
    model_id = model.get("id", "unknown")
    model_name = model.get("display_name", "unknown")

    # Session ID (if provided)
    session_id = data.get("session_id")

    # Infer provider from model ID
    provider = _infer_provider(model_id)

    # Rate limits (the key data we're tracking)
    # Claude Code has used two different field name formats:
    #   Old: rate_limits.session / rate_limits.weekly, resets_at as ISO string
    #   New: rate_limits.five_hour / rate_limits.seven_day, resets_at as Unix timestamp
    rate_limits = data.get("rate_limits", {})
    session = rate_limits.get("session") or rate_limits.get("five_hour") or {}
    weekly = rate_limits.get("weekly") or rate_limits.get("seven_day") or {}

    session_5h_pct = session.get("used_percentage")
    session_5h_reset = _parse_reset_time(session.get("resets_at"))
    weekly_7d_pct = weekly.get("used_percentage")
    weekly_7d_reset = _parse_reset_time(weekly.get("resets_at"))

    # Context window + token counts + cache tokens
    ctx = data.get("context_window", {})
    context_used_pct = ctx.get("used_percentage")
    context_window_size = ctx.get("context_window_size")
    total_input_tokens = ctx.get("total_input_tokens")
    total_output_tokens = ctx.get("total_output_tokens")
    usage = ctx.get("current_usage", {})
    cache_read_tokens = usage.get("cache_read_input_tokens")
    cache_creation_tokens = usage.get("cache_creation_input_tokens")

    # Cost
    cost = data.get("cost", {})
    session_cost_usd = cost.get("total_cost_usd")

    # Skip if we got no useful rate limit data at all
    # (e.g., before the first API response in a session)
    if session_5h_pct is None and weekly_7d_pct is None:
        return None

    return RateLimitSnapshot(
        ts=now,
        provider=provider,
        model_id=model_id,
        model_name=model_name,
        session_5h_pct=session_5h_pct,
        session_5h_reset=session_5h_reset,
        weekly_7d_pct=weekly_7d_pct,
        weekly_7d_reset=weekly_7d_reset,
        context_used_pct=context_used_pct,
        context_window_size=context_window_size,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        session_cost_usd=session_cost_usd,
        source="statusline",
        session_id=session_id,
    )


def append_snapshot(snapshot: RateLimitSnapshot, config: Config) -> None:
    """Append a snapshot to the history file."""
    config.ensure_data_dir()
    line = snapshot.to_json_line() + "\n"
    with open(config.history_path, "a") as f:
        f.write(line)


def load_history(config: Config, max_age_days: int | None = None) -> list[RateLimitSnapshot]:
    """Load snapshot history from disk.
    
    Args:
        config: App configuration
        max_age_days: If set, only return snapshots from the last N days
    """
    if not config.history_path.exists():
        return []

    cutoff = None
    if max_age_days is not None:
        cutoff_ts = time.time() - (max_age_days * 86400)
        cutoff = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()

    snapshots = []
    with open(config.history_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            if cutoff and d.get("ts", "") < cutoff:
                continue

            # Reconstruct snapshot with defaults for missing fields
            snap = RateLimitSnapshot(
                ts=d.get("ts", ""),
                provider=d.get("provider", "unknown"),
                model_id=d.get("model_id", "unknown"),
                model_name=d.get("model_name", "unknown"),
                session_5h_pct=d.get("session_5h_pct"),
                session_5h_reset=d.get("session_5h_reset"),
                weekly_7d_pct=d.get("weekly_7d_pct"),
                weekly_7d_reset=d.get("weekly_7d_reset"),
                context_used_pct=d.get("context_used_pct"),
                context_window_size=d.get("context_window_size"),
                session_cost_usd=d.get("session_cost_usd"),
                source=d.get("source", "statusline"),
            )
            snapshots.append(snap)

    return snapshots


def should_log(snapshot: RateLimitSnapshot, config: Config) -> bool:
    """Debounce logging: don't log if last entry was < 30 seconds ago with same values.
    
    Prevents flooding the history file on rapid statusline refreshes.
    """
    if not config.history_path.exists():
        return True

    # Read last line
    try:
        with open(config.history_path, "rb") as f:
            # Seek to end, walk back to find last newline
            f.seek(0, 2)
            size = f.tell()
            if size < 10:
                return True
            # Read last 2KB (more than enough for one JSON line)
            f.seek(max(0, size - 2048))
            lines = f.read().decode("utf-8", errors="replace").strip().split("\n")
            last_line = lines[-1] if lines else ""
    except Exception:
        return True

    if not last_line:
        return True

    try:
        last = json.loads(last_line)
    except json.JSONDecodeError:
        return True

    # Check if values actually changed
    same_values = (
        last.get("session_5h_pct") == snapshot.session_5h_pct
        and last.get("weekly_7d_pct") == snapshot.weekly_7d_pct
        and last.get("model_id") == snapshot.model_id
    )

    if not same_values:
        return True  # Values changed, always log

    # Same values — check time delta
    last_ts = last.get("ts", "")
    try:
        last_dt = datetime.fromisoformat(last_ts)
        now_dt = datetime.fromisoformat(snapshot.ts)
        delta = (now_dt - last_dt).total_seconds()
        # Log at most once per 5 minutes if values haven't changed
        return delta >= 300
    except (ValueError, TypeError):
        return True


def _infer_provider(model_id: str) -> str:
    """Infer the LLM provider from the model ID string."""
    model_lower = model_id.lower()
    if "claude" in model_lower or "opus" in model_lower or "sonnet" in model_lower or "haiku" in model_lower:
        return "anthropic"
    if "gpt" in model_lower or "o1" in model_lower or "o3" in model_lower:
        return "openai"
    if "gemini" in model_lower or "palm" in model_lower:
        return "google"
    if "grok" in model_lower:
        return "xai"
    return "unknown"
