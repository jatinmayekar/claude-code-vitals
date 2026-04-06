"""Usage deviation detector for claude_code_vitals.

Compares current utilization against a rolling median baseline.
Signals: SPIKE (above baseline), NORMAL, DROP (below baseline).

SPIKE/DROP indicate usage deviation, NOT provider changes.
Attribution (provider vs user) requires burn rate comparison.
"""

import json
import statistics
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from pathlib import Path

from .config import Config
from .logger import RateLimitSnapshot, load_history


class Signal(Enum):
    SPIKE = "spike"       # Utilization above baseline (was DOWN)
    NORMAL = "normal"
    DROP = "drop"         # Utilization below baseline (was UP)
    COLLECTING = "collecting"


@dataclass
class DriftResult:
    """The output of drift detection — everything the renderer needs."""

    signal: Signal

    # Current values
    current_5h_pct: Optional[float] = None
    current_7d_pct: Optional[float] = None

    # Baseline values (rolling median)
    baseline_5h_pct: Optional[float] = None
    baseline_7d_pct: Optional[float] = None

    # Change magnitude (percentage points of deviation)
    deviation_pct: Optional[float] = None

    # When the change was first detected
    change_detected_at: Optional[str] = None

    # How many data points in the baseline
    baseline_count: int = 0

    # Time-of-day pattern (if detected)
    pattern: Optional[str] = None  # e.g., "8AM-12PM" (user's personal heavy-usage window)

    # Reset time for the 5h window (ISO 8601)
    reset_5h_at: Optional[str] = None

    # Model name for display (e.g. "Opus 4.6")
    model_name: str = ""

    # Session cost in USD
    session_cost: Optional[float] = None

    # Burn rate and depletion prediction
    burn_rate_pct_hr: Optional[float] = None  # %/hour consumption rate
    depletion_minutes: Optional[int] = None   # minutes until 100% used

    # Attribution (why the signal fired)
    attribution: Optional[str] = None  # "you're using more" | "baseline shift"

    # Per-prompt delta
    prompt_delta: Optional[float] = None      # % change from last prompt
    avg_prompt_delta: Optional[float] = None  # rolling avg % per prompt
    is_anomalous: bool = False                # delta > 5x average

    # Peak/off-peak status
    is_peak: bool = False
    peak_ends_in_minutes: Optional[int] = None  # minutes until peak ends

    # Context + cache health
    context_pct: Optional[float] = None
    context_tokens: Optional[int] = None
    compact_threshold: Optional[int] = None     # model-specific auto-compact %
    compact_warning: Optional[str] = None       # warning when approaching compact
    cache_efficiency: Optional[float] = None    # % of tokens that were cache reads
    cache_miss_detected: bool = False
    cache_miss_reason: Optional[str] = None

    # Idle gap
    idle_warning: Optional[str] = None

    # Hourly comparison
    hourly_multiplier: Optional[float] = None  # e.g., 3.2 = burning 3.2x avg          # warning when cache expired from idle

    # Switch suggestion (e.g. "try Sonnet (96% left)")
    switch_hint: Optional[str] = None

    # Data source
    source: str = "local"  # "local" | "local + N users global"


# State file for persisting signal state across invocations
STATE_FILE = "state.json"


@dataclass
class DetectorState:
    """Persisted state for debouncing signal changes."""
    current_signal: str = "collecting"
    consecutive_spike: int = 0
    consecutive_drop: int = 0
    change_detected_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "current_signal": self.current_signal,
            "consecutive_spike": self.consecutive_spike,
            "consecutive_drop": self.consecutive_drop,
            "change_detected_at": self.change_detected_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DetectorState":
        # Migrate old signal names
        signal = d.get("current_signal", "collecting")
        signal_migration = {"down": "spike", "up": "drop"}
        signal = signal_migration.get(signal, signal)
        return cls(
            current_signal=signal,
            consecutive_spike=d.get("consecutive_spike", d.get("consecutive_down", 0)),
            consecutive_drop=d.get("consecutive_drop", d.get("consecutive_up", 0)),
            change_detected_at=d.get("change_detected_at"),
        )


def load_state(config: Config) -> DetectorState:
    state_path = config.data_dir / STATE_FILE
    if not state_path.exists():
        return DetectorState()
    try:
        return DetectorState.from_dict(json.loads(state_path.read_text()))
    except Exception:
        return DetectorState()


def save_state(state: DetectorState, config: Config) -> None:
    config.ensure_data_dir()
    state_path = config.data_dir / STATE_FILE
    state_path.write_text(json.dumps(state.to_dict(), indent=2))


def detect_drift(snapshot: Optional[RateLimitSnapshot], config: Config) -> DriftResult:
    """Main detection function. Called on every statusline refresh.
    
    1. Load history within the baseline window
    2. Compute rolling median of utilization percentages
    3. Compare current observation to baseline
    4. Apply debounce logic
    5. Return signal + context
    """
    state = load_state(config)
    window_days = config.tracking.baseline_window_days

    # Load history
    history = load_history(config, max_age_days=window_days)

    # Filter to current model — each model has its own separate rate limit pool
    if snapshot is not None:
        history = [s for s in history if s.model_id == snapshot.model_id]

    # Compute burn rate (works even with few readings)
    burn_rate, depletion = compute_burn_rate(snapshot, history)
    hourly_mult = hourly_comparison(history, burn_rate, config.tracking.baseline_window_days)

    # Not enough data yet
    min_data_points = 10  # Need at least 10 observations for a meaningful baseline
    if len(history) < min_data_points:
        return DriftResult(
            signal=Signal.COLLECTING,
            current_5h_pct=snapshot.session_5h_pct if snapshot else None,
            current_7d_pct=snapshot.weekly_7d_pct if snapshot else None,
            reset_5h_at=snapshot.session_5h_reset if snapshot else None,
            model_name=snapshot.model_name if snapshot else "",
            session_cost=snapshot.session_cost_usd if snapshot else None,
            burn_rate_pct_hr=burn_rate,
            depletion_minutes=depletion,
            baseline_count=len(history),
            source="local",
        )

    # Compute baseline: rolling median of weekly utilization
    # Weekly (7d) utilization is the better signal — it reflects the provider's
    # actual ceiling changes, not just per-session fluctuations.
    weekly_values = [s.weekly_7d_pct for s in history if s.weekly_7d_pct is not None]
    session_values = [s.session_5h_pct for s in history if s.session_5h_pct is not None]

    if not weekly_values:
        return DriftResult(
            signal=Signal.COLLECTING,
            reset_5h_at=snapshot.session_5h_reset if snapshot else None,
            model_name=snapshot.model_name if snapshot else "",
            session_cost=snapshot.session_cost_usd if snapshot else None,
            burn_rate_pct_hr=burn_rate,
            depletion_minutes=depletion,
            baseline_count=len(history),
            source="local",
        )

    baseline_7d = statistics.median(weekly_values)
    baseline_5h = statistics.median(session_values) if session_values else None

    # Check for frozen baseline
    frozen_path = config.data_dir / "baseline-frozen.json"
    if frozen_path.exists():
        try:
            frozen = json.loads(frozen_path.read_text())
            model_id = snapshot.model_id if snapshot else None
            if model_id and model_id in frozen:
                frozen_model = frozen[model_id]
                baseline_7d = frozen_model.get("7d_median", baseline_7d)
                baseline_5h = frozen_model.get("5h_median", baseline_5h)
        except (json.JSONDecodeError, KeyError, PermissionError):
            pass  # Fall back to computed baseline

    # Current values
    current_7d = snapshot.weekly_7d_pct if snapshot else None
    current_5h = snapshot.session_5h_pct if snapshot else None

    if current_7d is None:
        # No current reading — maintain previous state
        return DriftResult(
            signal=Signal(state.current_signal) if state.current_signal != "collecting" else Signal.NORMAL,
            baseline_7d_pct=baseline_7d,
            baseline_5h_pct=baseline_5h,
            reset_5h_at=snapshot.session_5h_reset if snapshot else None,
            model_name=snapshot.model_name if snapshot else "",
            session_cost=snapshot.session_cost_usd if snapshot else None,
            burn_rate_pct_hr=burn_rate,
            depletion_minutes=depletion,
            baseline_count=len(history),
            change_detected_at=state.change_detected_at,
            source="local",
        )

    # --- Usage Deviation Detection ---
    #
    # We compare current utilization % to the rolling median baseline.
    # A spike means utilization is ABOVE baseline. A drop means BELOW.
    #
    # NOTE: A spike is a pure observability signal — utilization shifted
    # relative to the user's own rolling baseline. Attribution uses burn
    # rate comparison to describe the shape of the shift (same burn rate +
    # higher utilization = baseline shift; otherwise the user is consuming
    # more). We make no claim about why limits were externally changed.

    threshold = config.tracking.threshold_pct
    debounce = config.tracking.debounce_count

    # Calculate deviation
    deviation = current_7d - baseline_7d  # positive = above baseline

    # Determine raw signal before debounce
    if deviation > threshold:
        raw_signal = Signal.SPIKE
        state.consecutive_spike += 1
        state.consecutive_drop = 0
    elif deviation < -threshold:
        raw_signal = Signal.DROP
        state.consecutive_drop += 1
        state.consecutive_spike = 0
    else:
        raw_signal = Signal.NORMAL
        state.consecutive_spike = 0
        state.consecutive_drop = 0

    # Apply debounce
    now = datetime.now(timezone.utc).isoformat()
    confirmed_signal = Signal(state.current_signal) if state.current_signal != "collecting" else Signal.NORMAL

    if raw_signal == Signal.SPIKE and state.consecutive_spike >= debounce:
        confirmed_signal = Signal.SPIKE
        if state.current_signal != "spike":
            state.change_detected_at = now
        state.current_signal = "spike"
    elif raw_signal == Signal.DROP and state.consecutive_drop >= debounce:
        confirmed_signal = Signal.DROP
        if state.current_signal != "drop":
            state.change_detected_at = now
        state.current_signal = "drop"
    elif raw_signal == Signal.NORMAL:
        if state.current_signal != "normal":
            state.change_detected_at = now
        state.current_signal = "normal"
        confirmed_signal = Signal.NORMAL

    # Attribution: compare current burn rate to historical average
    attribution = None
    if confirmed_signal == Signal.SPIKE and burn_rate is not None and burn_rate > 0:
        hist_rates = []
        for i in range(1, len(history)):
            prev_ts = datetime.fromisoformat(history[i-1].ts).replace(tzinfo=timezone.utc)
            curr_ts = datetime.fromisoformat(history[i].ts).replace(tzinfo=timezone.utc)
            hrs = (curr_ts - prev_ts).total_seconds() / 3600
            if 0.1 <= hrs <= 2.0 and history[i].session_5h_pct and history[i-1].session_5h_pct:
                delta = history[i].session_5h_pct - history[i-1].session_5h_pct
                if delta > 0:
                    hist_rates.append(delta / hrs)
        if hist_rates:
            hist_median = statistics.median(hist_rates)
            if hist_median > 0 and burn_rate <= hist_median * 1.3:
                attribution = "baseline shift"
            else:
                attribution = "you're using more"

    # Detect time-of-day patterns
    pattern = detect_time_pattern(history) if config.display.show_pattern else None

    # Per-prompt delta
    prompt_delta, avg_prompt_delta, is_anomalous = compute_prompt_delta(snapshot, history)

    # Peak/off-peak
    is_peak, peak_ends_in = detect_peak_status(snapshot.ts if snapshot else now)

    # Cache health (need previous snapshot from history)
    prev_snap = history[-1] if history else None
    cache_info = compute_cache_health(snapshot, prev_snap)

    # Save state
    save_state(state, config)

    return DriftResult(
        signal=confirmed_signal,
        current_5h_pct=current_5h,
        current_7d_pct=current_7d,
        baseline_5h_pct=baseline_5h,
        baseline_7d_pct=baseline_7d,
        deviation_pct=round(deviation, 1),
        reset_5h_at=snapshot.session_5h_reset if snapshot else None,
        model_name=snapshot.model_name if snapshot else "",
        session_cost=snapshot.session_cost_usd if snapshot else None,
        burn_rate_pct_hr=burn_rate,
        depletion_minutes=depletion,
        hourly_multiplier=hourly_mult,
        attribution=attribution,
        prompt_delta=prompt_delta,
        avg_prompt_delta=avg_prompt_delta,
        is_anomalous=is_anomalous,
        is_peak=is_peak,
        peak_ends_in_minutes=peak_ends_in,
        context_pct=cache_info.get("context_pct"),
        context_tokens=cache_info.get("context_tokens"),
        compact_threshold=cache_info.get("compact_threshold"),
        compact_warning=cache_info.get("compact_warning"),
        cache_efficiency=cache_info.get("cache_efficiency"),
        cache_miss_detected=cache_info.get("cache_miss_detected", False),
        cache_miss_reason=cache_info.get("cache_miss_reason"),
        idle_warning=cache_info.get("idle_warning"),
        change_detected_at=state.change_detected_at,
        baseline_count=len(history),
        pattern=pattern,
        source="local",
    )


def compute_burn_rate(snapshot: Optional[RateLimitSnapshot],
                      history: list[RateLimitSnapshot]) -> tuple[Optional[float], Optional[int]]:
    """Compute burn rate (%/hr) and depletion time (minutes) for the 5h window.

    Compares current reading to the oldest reading within the last 2 hours
    for the same model. Returns (burn_rate_pct_hr, depletion_minutes).
    """
    if snapshot is None or snapshot.session_5h_pct is None:
        return None, None
    if len(history) < 2:
        return None, None

    current_pct = snapshot.session_5h_pct
    current_ts = datetime.fromisoformat(snapshot.ts) if snapshot.ts else datetime.now(timezone.utc)
    if current_ts.tzinfo is None:
        current_ts = current_ts.replace(tzinfo=timezone.utc)

    # Find a reading from ~30min-2hrs ago for the same model
    best_earlier = None
    for s in history:
        if s.session_5h_pct is None or s.model_id != snapshot.model_id:
            continue
        # Filter to same session for accurate burn rate
        if snapshot.session_id and s.session_id and s.session_id != snapshot.session_id:
            continue
        try:
            s_ts = datetime.fromisoformat(s.ts)
            if s_ts.tzinfo is None:
                s_ts = s_ts.replace(tzinfo=timezone.utc)
            age_hours = (current_ts - s_ts).total_seconds() / 3600
            if 0.25 <= age_hours <= 2.0:  # Between 15min and 2hrs ago
                if best_earlier is None or s_ts < datetime.fromisoformat(best_earlier.ts).replace(tzinfo=timezone.utc):
                    best_earlier = s
        except (ValueError, TypeError):
            continue

    if best_earlier is None:
        return None, None

    earlier_ts = datetime.fromisoformat(best_earlier.ts)
    if earlier_ts.tzinfo is None:
        earlier_ts = earlier_ts.replace(tzinfo=timezone.utc)

    hours_elapsed = (current_ts - earlier_ts).total_seconds() / 3600
    if hours_elapsed < 0.1:
        return None, None

    pct_change = current_pct - best_earlier.session_5h_pct
    burn_rate = pct_change / hours_elapsed  # %/hour

    if burn_rate <= 0:
        return burn_rate, None  # Not consuming (or utilization decreased — reset happened)

    remaining = 100 - current_pct
    depletion_minutes = int((remaining / burn_rate) * 60)

    return round(burn_rate, 1), depletion_minutes


def compute_prompt_delta(snapshot: Optional[RateLimitSnapshot],
                         history: list[RateLimitSnapshot]) -> tuple[Optional[float], Optional[float], bool]:
    """Compute per-prompt utilization delta and flag anomalies.

    Returns (delta_pct, avg_delta_pct, is_anomalous).
    """
    if snapshot is None or snapshot.session_5h_pct is None or not history:
        return None, None, False

    # Find the previous reading for the same model
    prev = None
    for s in reversed(history):
        if s.model_id == snapshot.model_id and s.session_5h_pct is not None:
            # Filter to same session
            if snapshot.session_id and s.session_id and s.session_id != snapshot.session_id:
                continue
            prev = s
            break

    if prev is None:
        return None, None, False

    delta = snapshot.session_5h_pct - prev.session_5h_pct

    # Compute rolling average of positive deltas
    deltas = []
    model_history = [s for s in history if s.model_id == snapshot.model_id and s.session_5h_pct is not None]
    for i in range(1, len(model_history)):
        # Only use same-session pairs
        if model_history[i].session_id and model_history[i-1].session_id and model_history[i].session_id != model_history[i-1].session_id:
            continue
        d = model_history[i].session_5h_pct - model_history[i-1].session_5h_pct
        if d > 0:
            deltas.append(d)

    avg_delta = statistics.median(deltas) if deltas else None
    is_anomalous = delta > 0 and avg_delta is not None and avg_delta > 0 and delta > avg_delta * 5

    return round(delta, 2), round(avg_delta, 2) if avg_delta else None, is_anomalous


PEAK_HOURS_PT = (5, 11)  # 5am-11am PT weekdays (confirmed active April 2026)

def detect_peak_status(snapshot_ts: str) -> tuple[bool, Optional[int]]:
    """Check if current time falls in Anthropic's official peak window.

    Peak: 5am-11am PT, weekdays only.
    Returns (is_peak, minutes_until_peak_ends).
    """
    try:
        dt = datetime.fromisoformat(snapshot_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Convert UTC to PT (UTC-7, ignoring DST for simplicity)
        pt_hour = (dt.hour - 7) % 24
        weekday = dt.weekday()  # 0=Monday, 6=Sunday

        is_peak = weekday < 5 and PEAK_HOURS_PT[0] <= pt_hour < PEAK_HOURS_PT[1]
        if is_peak:
            hours_left = PEAK_HOURS_PT[1] - pt_hour
            minutes_left = hours_left * 60 - dt.minute
            return True, max(0, minutes_left)
        return False, None
    except (ValueError, TypeError):
        return False, None


COMPACT_THRESHOLDS = {
    "opus": 75,
    "sonnet": 85,
    "haiku": 90,
}

def compute_cache_health(snapshot: Optional[RateLimitSnapshot],
                         prev_snapshot: Optional[RateLimitSnapshot]) -> dict:
    """Analyze context window, compaction events, and cache efficiency.

    Returns dict with: context_pct, context_tokens, compact_threshold,
    compact_warning, cache_efficiency, cache_miss_detected, cache_miss_reason,
    idle_warning.
    """
    result = {}
    if snapshot is None:
        return result

    # Context window
    ctx_pct = snapshot.context_used_pct
    ctx_size = snapshot.context_window_size or 200000
    ctx_tokens = int(ctx_size * (ctx_pct / 100)) if ctx_pct else None
    result["context_pct"] = ctx_pct
    result["context_tokens"] = ctx_tokens

    # Model-specific compact threshold
    model_lower = snapshot.model_id.lower()
    threshold = 80
    for key, val in COMPACT_THRESHOLDS.items():
        if key in model_lower:
            threshold = val
            break
    result["compact_threshold"] = threshold

    # Compact warning
    if ctx_pct is not None and ctx_pct > threshold - 10:
        result["compact_warning"] = (
            f"ctx: {ctx_pct:.0f}% \u2014 {snapshot.model_name} compacts at ~{threshold}%. "
            f"Cache will reset. Finish task or /compact now."
        )

    # Cache tokens are session-scoped — only diff within same session
    same_session = (prev_snapshot and snapshot.session_id and prev_snapshot.session_id
                    and snapshot.session_id == prev_snapshot.session_id)

    # Auto-compact detection (context drops >30%)
    auto_compacted = False
    if prev_snapshot and prev_snapshot.context_used_pct and ctx_pct:
        if same_session and prev_snapshot.context_used_pct - ctx_pct > 30:
            auto_compacted = True

    # Idle gap detection
    idle_seconds = None
    if prev_snapshot:
        try:
            prev_ts = datetime.fromisoformat(prev_snapshot.ts)
            curr_ts = datetime.fromisoformat(snapshot.ts)
            if prev_ts.tzinfo is None:
                prev_ts = prev_ts.replace(tzinfo=timezone.utc)
            if curr_ts.tzinfo is None:
                curr_ts = curr_ts.replace(tzinfo=timezone.utc)
            idle_seconds = int((curr_ts - prev_ts).total_seconds())
        except (ValueError, TypeError):
            pass

    if idle_seconds is not None and idle_seconds > 300:
        result["idle_warning"] = (
            f"Idle {idle_seconds // 60}min \u2014 cache expired (5min TTL). "
            f"This prompt reprocesses full context."
        )
    elif idle_seconds is not None and idle_seconds > 180:
        remaining = 300 - idle_seconds
        result["idle_warning"] = (
            f"Cache expires in {remaining}s \u2014 send a prompt to keep it warm."
        )

    # Cache efficiency from cumulative token diffs
    if (same_session and snapshot.cache_read_tokens is not None
            and prev_snapshot.cache_read_tokens is not None):
        delta_reads = (snapshot.cache_read_tokens or 0) - (prev_snapshot.cache_read_tokens or 0)
        delta_writes = (snapshot.cache_creation_tokens or 0) - (prev_snapshot.cache_creation_tokens or 0)
        delta_total = delta_reads + delta_writes
        if delta_total > 1000:
            efficiency = (delta_reads / delta_total) * 100
            result["cache_efficiency"] = round(efficiency, 1)
            if efficiency < 20:
                result["cache_miss_detected"] = True
                if auto_compacted:
                    result["cache_miss_reason"] = "Cache reset by compaction. Expected."
                elif idle_seconds and idle_seconds > 300:
                    result["cache_miss_reason"] = f"Cache expired (idle {idle_seconds // 60}min, TTL is 5min)."
                else:
                    result["cache_miss_reason"] = "Unexpected cache miss. Check Claude Code version (cache bug fixed in v2.1.88)."
    elif snapshot.cache_read_tokens is not None and snapshot.cache_creation_tokens is not None:
        total = snapshot.cache_read_tokens + snapshot.cache_creation_tokens
        if total > 0:
            result["cache_efficiency"] = round((snapshot.cache_read_tokens / total) * 100, 1)

    return result


def hourly_comparison(history: list[RateLimitSnapshot],
                      current_burn_rate: Optional[float],
                      window_days: int = 7) -> Optional[float]:
    """Compare current burn rate to 7-day hourly median.

    Returns multiplier (e.g., 3.2 means burning 3.2x faster than average).
    """
    if current_burn_rate is None or current_burn_rate <= 0:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    historical = [s for s in history if s.session_5h_pct is not None]
    try:
        historical = [s for s in historical
                      if datetime.fromisoformat(s.ts).replace(tzinfo=timezone.utc) > cutoff]
    except (ValueError, TypeError):
        return None

    if len(historical) < 20:
        return None

    rates = []
    for i in range(1, len(historical)):
        try:
            # Only use same-session pairs
            if historical[i].session_id and historical[i-1].session_id and historical[i].session_id != historical[i-1].session_id:
                continue
            t1 = datetime.fromisoformat(historical[i-1].ts).replace(tzinfo=timezone.utc)
            t2 = datetime.fromisoformat(historical[i].ts).replace(tzinfo=timezone.utc)
            hrs = (t2 - t1).total_seconds() / 3600
            if 0.05 <= hrs <= 2.0:
                delta = historical[i].session_5h_pct - historical[i-1].session_5h_pct
                if delta > 0:
                    rates.append(delta / hrs)
        except (ValueError, TypeError):
            continue

    if not rates:
        return None

    baseline_rate = statistics.median(rates)
    if baseline_rate <= 0:
        return None

    return round(current_burn_rate / baseline_rate, 1)


def detect_time_pattern(history: list[RateLimitSnapshot]) -> Optional[str]:
    """Detect the user's personal heavy-usage time-of-day window.

    Buckets observations into 2-hour windows, computes the median per bucket,
    and flags buckets that deviate significantly from the overall median. This
    describes the user's own behavioral pattern — it is NOT a limit alert.

    Requires at least 7 days of history (both >= 84 readings and a span of at
    least 7 days between the earliest and latest timestamp) so that a single
    atypical day cannot produce a spurious label.

    Args:
        history: Chronological list of RateLimitSnapshot entries.

    Returns:
        A bare label like "8AM-12PM" describing the contiguous heavy-usage
        window (or corresponding low window if no high window is found), or
        None if insufficient data or no pattern emerges. High windows take
        priority over low windows when both exist.
    """
    # Require >= 84 readings AND a history span covering at least 7 days.
    if len(history) < 84:
        return None

    timestamps: list[datetime] = []
    for snap in history:
        try:
            timestamps.append(datetime.fromisoformat(snap.ts))
        except (ValueError, TypeError):
            continue

    if len(timestamps) < 84:
        return None

    span = max(timestamps) - min(timestamps)
    if span.total_seconds() < 7 * 24 * 3600:
        return None

    # 12 two-hour buckets covering the 24-hour day.
    bucket_names = [
        "12AM-2AM", "2AM-4AM", "4AM-6AM", "6AM-8AM",
        "8AM-10AM", "10AM-12PM", "12PM-2PM", "2PM-4PM",
        "4PM-6PM", "6PM-8PM", "8PM-10PM", "10PM-12AM",
    ]
    buckets: dict[int, list[float]] = {i: [] for i in range(12)}

    for snap in history:
        if snap.weekly_7d_pct is None:
            continue
        try:
            dt = datetime.fromisoformat(snap.ts)
            bucket = dt.hour // 2
            buckets[bucket].append(snap.weekly_7d_pct)
        except (ValueError, TypeError):
            continue

    # Need data in at least 4 active buckets (>= 3 samples each).
    active_buckets = {k: v for k, v in buckets.items() if len(v) >= 3}
    if len(active_buckets) < 4:
        return None

    bucket_medians = {k: statistics.median(v) for k, v in active_buckets.items()}
    overall_median = statistics.median([m for m in bucket_medians.values()])

    if overall_median == 0:
        return None

    high_buckets: list[tuple[int, float]] = []
    low_buckets: list[tuple[int, float]] = []
    for bucket_idx, median in bucket_medians.items():
        deviation = ((median - overall_median) / overall_median) * 100
        if deviation > 15:
            high_buckets.append((bucket_idx, deviation))
        elif deviation < -15:
            low_buckets.append((bucket_idx, deviation))

    if not high_buckets and not low_buckets:
        return None

    # High windows take priority over low windows.
    if high_buckets:
        high_buckets.sort(key=lambda x: x[0])
        start = bucket_names[high_buckets[0][0]].split("-")[0]
        end = bucket_names[high_buckets[-1][0]].split("-")[1]
        return f"{start}-{end}"

    low_buckets.sort(key=lambda x: x[0])
    start = bucket_names[low_buckets[0][0]].split("-")[0]
    end = bucket_names[low_buckets[-1][0]].split("-")[1]
    return f"{start}-{end}"
