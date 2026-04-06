"""Tests for claude_code_vitals core modules."""

import json
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from claude_code_vitals.config import Config, load_config, _parse_simple_toml
from claude_code_vitals.logger import (
    parse_statusline_json, extract_snapshot, append_snapshot,
    load_history, RateLimitSnapshot, should_log,
)
from claude_code_vitals.detector import (
    detect_drift, Signal, detect_time_pattern,
    compute_prompt_delta, detect_peak_status, compute_cache_health,
    DriftResult,
)
from claude_code_vitals.renderer import render_compact, render_expanded
from claude_code_vitals.__main__ import _peak_overlap_tip, _parse_pattern_hours


def make_config(tmp_dir: Path) -> Config:
    config = Config()
    config.data_dir = tmp_dir
    return config


# ===== Config Tests =====

def test_parse_simple_toml():
    text = """
[tracking]
baseline_window_days = 14
threshold_pct = 15.5

[display]
compact = true
color = false

"""
    result = _parse_simple_toml(text)
    assert result["tracking"]["baseline_window_days"] == 14
    assert result["tracking"]["threshold_pct"] == 15.5
    assert result["display"]["compact"] is True
    assert result["display"]["color"] is False
    print("  ✓ test_parse_simple_toml")


# ===== Logger Tests =====

SAMPLE_STDIN_JSON = json.dumps({
    "model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6"},
    "rate_limits": {
        "session": {"used_percentage": 42.0, "resets_at": "2026-03-30T19:00:00Z"},
        "weekly": {"used_percentage": 67.0, "resets_at": "2026-04-05T08:00:00Z"},
    },
    "context_window": {"used_percentage": 12, "context_window_size": 200000},
    "cost": {"total_cost_usd": 0.80},
    "workspace": {"current_dir": "/home/user/project"},
})


def test_parse_statusline_json():
    data = parse_statusline_json(SAMPLE_STDIN_JSON)
    assert data is not None
    assert data["model"]["id"] == "claude-opus-4-6"
    assert data["rate_limits"]["session"]["used_percentage"] == 42.0
    print("  ✓ test_parse_statusline_json")


def test_parse_invalid_json():
    assert parse_statusline_json("not json") is None
    assert parse_statusline_json("") is None
    print("  ✓ test_parse_invalid_json")


def test_extract_snapshot():
    data = parse_statusline_json(SAMPLE_STDIN_JSON)
    snap = extract_snapshot(data)
    assert snap is not None
    assert snap.provider == "anthropic"
    assert snap.model_id == "claude-opus-4-6"
    assert snap.session_5h_pct == 42.0
    assert snap.weekly_7d_pct == 67.0
    assert snap.context_used_pct == 12
    print("  ✓ test_extract_snapshot")


def test_extract_no_rate_limits():
    data = {"model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6"}}
    snap = extract_snapshot(data)
    assert snap is None  # No rate limit data = don't log
    print("  ✓ test_extract_no_rate_limits")


def test_append_and_load():
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        snap = RateLimitSnapshot(
            ts="2026-03-30T14:00:00Z",
            provider="anthropic",
            model_id="claude-opus-4-6",
            model_name="Opus 4.6",
            session_5h_pct=42.0,
            session_5h_reset="2026-03-30T19:00:00Z",
            weekly_7d_pct=67.0,
            weekly_7d_reset="2026-04-05T08:00:00Z",
            context_used_pct=12,
            context_window_size=200000,
            session_cost_usd=0.80,
        )
        append_snapshot(snap, config)
        history = load_history(config)
        assert len(history) == 1
        assert history[0].session_5h_pct == 42.0
        assert history[0].weekly_7d_pct == 67.0
        print("  ✓ test_append_and_load")


def test_json_line_compact():
    snap = RateLimitSnapshot(
        ts="2026-03-30T14:00:00Z",
        provider="anthropic",
        model_id="claude-opus-4-6",
        model_name="Opus 4.6",
        session_5h_pct=42.0,
        session_5h_reset=None,  # Should be omitted
        weekly_7d_pct=67.0,
        weekly_7d_reset=None,
        context_used_pct=None,
        context_window_size=None,
        session_cost_usd=None,
    )
    line = snap.to_json_line()
    d = json.loads(line)
    assert "session_5h_reset" not in d  # None values omitted
    assert "context_used_pct" not in d
    assert d["session_5h_pct"] == 42.0
    print("  ✓ test_json_line_compact")


# ===== Detector Tests =====

def _make_history(config, count=20, weekly_pct=67.0, session_pct=42.0, start_hours_ago=48):
    """Generate synthetic history for testing."""
    now = datetime.now(timezone.utc)
    for i in range(count):
        ts = now - timedelta(hours=start_hours_ago - i * (start_hours_ago / count))
        snap = RateLimitSnapshot(
            ts=ts.isoformat(),
            provider="anthropic",
            model_id="claude-opus-4-6",
            model_name="Opus 4.6",
            session_5h_pct=session_pct,
            session_5h_reset=None,
            weekly_7d_pct=weekly_pct,
            weekly_7d_reset=None,
            context_used_pct=12,
            context_window_size=200000,
            session_cost_usd=0.50,
        )
        append_snapshot(snap, config)


def test_detect_collecting():
    """With <10 data points, should return COLLECTING."""
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        _make_history(config, count=5)
        snap = RateLimitSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=42.0, session_5h_reset=None,
            weekly_7d_pct=67.0, weekly_7d_reset=None,
            context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
        )
        result = detect_drift(snap, config)
        assert result.signal == Signal.COLLECTING
        print("  ✓ test_detect_collecting")


def test_detect_normal():
    """With stable data matching current, should return NORMAL."""
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        _make_history(config, count=20, weekly_pct=67.0)
        snap = RateLimitSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=42.0, session_5h_reset=None,
            weekly_7d_pct=67.0, weekly_7d_reset=None,  # Same as baseline
            context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
        )
        result = detect_drift(snap, config)
        assert result.signal == Signal.NORMAL
        assert result.baseline_7d_pct == 67.0
        print("  ✓ test_detect_normal")


def test_detect_spike():
    """If utilization spikes above baseline, should signal SPIKE."""
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        config.tracking.debounce_count = 1  # Lower debounce for testing
        _make_history(config, count=20, weekly_pct=40.0)

        # Current reading is way higher — utilization spiked above baseline
        snap = RateLimitSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=80.0, session_5h_reset=None,
            weekly_7d_pct=85.0, weekly_7d_reset=None,  # Way above 40% baseline
            context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
        )
        result = detect_drift(snap, config)
        assert result.signal == Signal.SPIKE
        assert result.deviation_pct is not None
        assert result.deviation_pct > 0
        print("  ✓ test_detect_spike")


def test_detect_drop():
    """If utilization drops below baseline, should signal DROP."""
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        config.tracking.debounce_count = 1
        _make_history(config, count=20, weekly_pct=80.0)

        snap = RateLimitSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=20.0, session_5h_reset=None,
            weekly_7d_pct=30.0, weekly_7d_reset=None,  # Way below 80% baseline
            context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
        )
        result = detect_drift(snap, config)
        assert result.signal == Signal.DROP
        print("  ✓ test_detect_drop")


def test_debounce():
    """Signal should not change on a single reading when debounce > 1."""
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        config.tracking.debounce_count = 3  # Need 3 consecutive readings
        _make_history(config, count=20, weekly_pct=40.0)

        snap = RateLimitSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=80.0, session_5h_reset=None,
            weekly_7d_pct=85.0, weekly_7d_reset=None,
            context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
        )
        # First reading — should NOT trigger DOWN yet (debounce = 3)
        result = detect_drift(snap, config)
        # May still be NORMAL because debounce hasn't been met
        # (state starts at "collecting" → becomes "normal" on first real detection)
        print("  ✓ test_debounce")


# ===== Renderer Tests =====

def test_render_compact_normal():
    from claude_code_vitals.detector import DriftResult
    result = DriftResult(
        signal=Signal.NORMAL,
        current_5h_pct=42.0,
        current_7d_pct=67.0,
        baseline_count=100,
    )
    config = Config()
    config.display.color = False  # No ANSI for testing
    output = render_compact(result, config)
    assert "NO CHANGE" not in output  # Hidden when stable
    assert "5h: 42% used" in output
    assert "7d: 67% used" in output
    print("  ✓ test_render_compact_normal")


def test_render_compact_spike():
    from claude_code_vitals.detector import DriftResult
    result = DriftResult(
        signal=Signal.SPIKE,
        current_5h_pct=80.0,
        current_7d_pct=91.0,
        deviation_pct=25.0,
        change_detected_at="2026-03-28T10:00:00Z",
        baseline_count=100,
    )
    config = Config()
    config.display.color = False
    output = render_compact(result, config)
    assert "USAGE SPIKE" in output
    assert "25" in output
    print("  ✓ test_render_compact_spike")


def test_render_collecting():
    from claude_code_vitals.detector import DriftResult
    result = DriftResult(
        signal=Signal.COLLECTING,
        current_5h_pct=42.0,
        baseline_count=5,
    )
    config = Config()
    config.display.color = False
    output = render_compact(result, config)
    assert "COLLECTING" in output
    assert "5/10" in output
    print("  ✓ test_render_collecting")


def test_render_expanded():
    from claude_code_vitals.detector import DriftResult
    result = DriftResult(
        signal=Signal.SPIKE,
        current_5h_pct=68.0,
        current_7d_pct=91.0,
        baseline_5h_pct=42.0,
        baseline_7d_pct=67.0,
        deviation_pct=24.0,
        change_detected_at="2026-03-28T10:00:00Z",
        baseline_count=200,
        pattern="\u2191 8PM-12AM",
    )
    config = Config()
    config.display.color = False
    output = render_expanded(result, config)
    assert "claude_code_vitals" in output
    assert "68%" in output
    assert "baseline: 42%" in output
    assert "200 points" in output
    print("  ✓ test_render_expanded")


# ===== New Feature Tests =====

# --- compute_prompt_delta tests ---

def test_prompt_delta_normal():
    """Normal delta: snapshot at 45%, previous at 42% -> delta=3.0."""
    snap = RateLimitSnapshot(
        ts="2026-03-30T15:00:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=45.0, session_5h_reset=None,
        weekly_7d_pct=67.0, weekly_7d_reset=None,
        context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
    )
    # History with several readings so avg_delta is meaningful
    history = []
    for i in range(5):
        history.append(RateLimitSnapshot(
            ts=f"2026-03-30T14:{50 + i}:00Z",
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=30.0 + i * 3.0, session_5h_reset=None,
            weekly_7d_pct=60.0, weekly_7d_reset=None,
            context_used_pct=10, context_window_size=200000, session_cost_usd=0.4,
        ))
    delta, avg_delta, is_anomalous = compute_prompt_delta(snap, history)
    assert delta == 3.0, f"Expected delta=3.0, got {delta}"
    assert avg_delta is not None
    assert is_anomalous is False
    print("  ✓ test_prompt_delta_normal")


def test_prompt_delta_anomalous():
    """Anomalous delta: snapshot at 60%, previous at 42%, avg delta ~1.0 -> anomalous."""
    # Build history with small consistent deltas (~1.0 each)
    history = []
    for i in range(10):
        history.append(RateLimitSnapshot(
            ts=f"2026-03-30T14:{40 + i}:00Z",
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=30.0 + i * 1.0, session_5h_reset=None,
            weekly_7d_pct=60.0, weekly_7d_reset=None,
            context_used_pct=10, context_window_size=200000, session_cost_usd=0.4,
        ))
    # Last history entry is at 39.0, snapshot jumps to 60.0 -> delta=21.0
    snap = RateLimitSnapshot(
        ts="2026-03-30T15:00:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=60.0, session_5h_reset=None,
        weekly_7d_pct=67.0, weekly_7d_reset=None,
        context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
    )
    delta, avg_delta, is_anomalous = compute_prompt_delta(snap, history)
    assert delta is not None and delta > 15.0, f"Expected large delta, got {delta}"
    assert avg_delta is not None and avg_delta == 1.0, f"Expected avg_delta=1.0, got {avg_delta}"
    assert is_anomalous is True, "Expected anomalous=True for delta >> 5*avg"
    print("  ✓ test_prompt_delta_anomalous")


def test_prompt_delta_empty_history():
    """Empty history -> returns (None, None, False)."""
    snap = RateLimitSnapshot(
        ts="2026-03-30T15:00:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=45.0, session_5h_reset=None,
        weekly_7d_pct=67.0, weekly_7d_reset=None,
        context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
    )
    delta, avg_delta, is_anomalous = compute_prompt_delta(snap, [])
    assert delta is None
    assert avg_delta is None
    assert is_anomalous is False
    print("  ✓ test_prompt_delta_empty_history")


# --- detect_peak_status tests ---

def test_peak_status_weekday_peak():
    """8am PT on a weekday (UTC 15:00 Mon) -> is_peak=True."""
    # 2026-03-30 is a Monday. 15:00 UTC = 8am PT (UTC-7).
    # Peak is 5am-11am PT, so 8am PT is peak.
    is_peak, minutes_left = detect_peak_status("2026-03-30T15:00:00Z")
    assert is_peak is True, f"Expected is_peak=True for 8am PT weekday, got {is_peak}"
    assert minutes_left is not None and minutes_left > 0
    # 8am PT -> peak ends at 11am PT = 3 hours = 180 minutes
    assert minutes_left == 180, f"Expected 180 minutes until peak ends, got {minutes_left}"
    print("  ✓ test_peak_status_weekday_peak")


def test_peak_status_weekday_offpeak():
    """2pm PT on a weekday (UTC 21:00) -> is_peak=False."""
    # 21:00 UTC = 2pm PT. Outside 5am-11am PT window.
    is_peak, minutes_left = detect_peak_status("2026-03-30T21:00:00Z")
    assert is_peak is False, f"Expected is_peak=False for 2pm PT weekday, got {is_peak}"
    assert minutes_left is None
    print("  ✓ test_peak_status_weekday_offpeak")


def test_peak_status_weekend():
    """8am PT on a Saturday -> is_peak=False (weekends not peak)."""
    # 2026-03-28 is a Saturday. 15:00 UTC = 8am PT.
    is_peak, minutes_left = detect_peak_status("2026-03-28T15:00:00Z")
    assert is_peak is False, f"Expected is_peak=False for weekend, got {is_peak}"
    assert minutes_left is None
    print("  ✓ test_peak_status_weekend")


# --- compute_cache_health tests ---

def test_cache_health_compact_warning_opus():
    """Opus with context at 72% -> compact_warning should exist (threshold 75%)."""
    snap = RateLimitSnapshot(
        ts="2026-03-30T15:00:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=42.0, session_5h_reset=None,
        weekly_7d_pct=67.0, weekly_7d_reset=None,
        context_used_pct=72, context_window_size=200000, session_cost_usd=0.5,
    )
    result = compute_cache_health(snap, None)
    assert result["context_pct"] == 72
    assert result["compact_threshold"] == 75
    # 72 > 75 - 10 = 65, so compact_warning should be present
    assert "compact_warning" in result, "Expected compact_warning for 72% ctx on Opus (threshold 75%)"
    assert "compacts at ~75%" in result["compact_warning"]
    print("  ✓ test_cache_health_compact_warning_opus")


def test_cache_health_efficiency():
    """Good cache efficiency: cache_read increases, cache_creation stays same."""
    prev = RateLimitSnapshot(
        ts="2026-03-30T14:55:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=40.0, session_5h_reset=None,
        weekly_7d_pct=65.0, weekly_7d_reset=None,
        context_used_pct=50, context_window_size=200000, session_cost_usd=0.4,
        cache_read_tokens=80000, cache_creation_tokens=5000,
        session_id="same-session",
    )
    snap = RateLimitSnapshot(
        ts="2026-03-30T14:58:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=42.0, session_5h_reset=None,
        weekly_7d_pct=67.0, weekly_7d_reset=None,
        context_used_pct=55, context_window_size=200000, session_cost_usd=0.5,
        cache_read_tokens=90000, cache_creation_tokens=5000,
        session_id="same-session",
    )
    result = compute_cache_health(snap, prev)
    # delta_reads=10000, delta_writes=0, total=10000 -> efficiency=100%
    assert "cache_efficiency" in result
    assert result["cache_efficiency"] == 100.0, f"Expected 100.0 efficiency, got {result['cache_efficiency']}"
    assert result.get("cache_miss_detected", False) is False
    print("  ✓ test_cache_health_efficiency")


def test_cache_health_idle_warning():
    """Idle gap >5min -> idle_warning present."""
    prev = RateLimitSnapshot(
        ts="2026-03-30T14:00:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=40.0, session_5h_reset=None,
        weekly_7d_pct=65.0, weekly_7d_reset=None,
        context_used_pct=50, context_window_size=200000, session_cost_usd=0.4,
    )
    snap = RateLimitSnapshot(
        ts="2026-03-30T14:10:00Z",  # 10 minutes later -> 600 seconds idle
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=42.0, session_5h_reset=None,
        weekly_7d_pct=67.0, weekly_7d_reset=None,
        context_used_pct=55, context_window_size=200000, session_cost_usd=0.5,
    )
    result = compute_cache_health(snap, prev)
    assert "idle_warning" in result, "Expected idle_warning for 10min gap"
    assert "Idle 10min" in result["idle_warning"]
    assert "cache expired" in result["idle_warning"].lower()
    print("  ✓ test_cache_health_idle_warning")


# ===== Baseline Tests =====

def test_frozen_baseline():
    """Frozen baseline should override computed median."""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        config.tracking.debounce_count = 1
        # Create history with median at 40%
        _make_history(config, count=20, weekly_pct=40.0)

        # Freeze baseline at 80% (different from computed 40%)
        frozen_path = Path(tmp) / "baseline-frozen.json"
        frozen_path.write_text(json.dumps({
            "claude-opus-4-6": {"5h_median": 80.0, "7d_median": 80.0}
        }))

        # Current at 85% — vs computed baseline (40%) this would be SPIKE
        # But vs frozen baseline (80%) this is within threshold (10%)
        snap = RateLimitSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=85.0, session_5h_reset=None,
            weekly_7d_pct=85.0, weekly_7d_reset=None,
            context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
        )
        result = detect_drift(snap, config)
        # With frozen baseline at 80%, deviation is only 5% (below 10% threshold)
        # So signal should be NORMAL, not SPIKE
        assert result.signal == Signal.NORMAL, f"Expected NORMAL with frozen baseline, got {result.signal}"
    print("  ✓ test_frozen_baseline")


def test_frozen_baseline_missing():
    """Without frozen file, computed baseline should be used."""
    with tempfile.TemporaryDirectory() as tmp:
        config = make_config(Path(tmp))
        config.tracking.debounce_count = 1
        _make_history(config, count=20, weekly_pct=40.0)

        # No frozen file — current at 85% vs baseline 40% = SPIKE
        snap = RateLimitSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=85.0, session_5h_reset=None,
            weekly_7d_pct=85.0, weekly_7d_reset=None,
            context_used_pct=12, context_window_size=200000, session_cost_usd=0.5,
        )
        result = detect_drift(snap, config)
        assert result.signal == Signal.SPIKE, f"Expected SPIKE without frozen baseline, got {result.signal}"
    print("  ✓ test_frozen_baseline_missing")


# ===== Session Filtering Tests =====

def test_burn_rate_session_filter():
    """Burn rate should only use same-session readings."""
    from claude_code_vitals.detector import compute_burn_rate
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    history = []
    for i in range(6):
        history.append(RateLimitSnapshot(
            ts=(now - timedelta(minutes=60-i*10)).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=10.0 + i * 2.0, session_5h_reset=None,
            weekly_7d_pct=50.0, weekly_7d_reset=None,
            context_used_pct=20, context_window_size=200000,
            session_cost_usd=1.0 + i * 0.5, session_id="session-A",
        ))
        history.append(RateLimitSnapshot(
            ts=(now - timedelta(minutes=59-i*10)).isoformat(),
            provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
            session_5h_pct=1.0, session_5h_reset=None,
            weekly_7d_pct=50.0, weekly_7d_reset=None,
            context_used_pct=5, context_window_size=200000,
            session_cost_usd=0.1, session_id="session-B",
        ))
    history.sort(key=lambda s: s.ts)

    snap = RateLimitSnapshot(
        ts=now.isoformat(),
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=22.0, session_5h_reset=None,
        weekly_7d_pct=50.0, weekly_7d_reset=None,
        context_used_pct=20, context_window_size=200000,
        session_cost_usd=4.0, session_id="session-A",
    )
    burn_rate, depletion = compute_burn_rate(snap, history)
    assert burn_rate is not None, "Burn rate should compute from same-session readings"
    assert burn_rate > 0, f"Burn rate should be positive, got {burn_rate}"
    print("  ✓ test_burn_rate_session_filter")


def test_prompt_delta_cross_session():
    """Prompt delta should skip readings from different sessions."""
    from claude_code_vitals.detector import compute_prompt_delta

    history = [RateLimitSnapshot(
        ts="2026-04-01T10:00:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=5.0, session_5h_reset=None,
        weekly_7d_pct=50.0, weekly_7d_reset=None,
        context_used_pct=10, context_window_size=200000,
        session_cost_usd=0.5, session_id="session-B",
    )]
    snap = RateLimitSnapshot(
        ts="2026-04-01T10:05:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=20.0, session_5h_reset=None,
        weekly_7d_pct=50.0, weekly_7d_reset=None,
        context_used_pct=20, context_window_size=200000,
        session_cost_usd=2.0, session_id="session-A",
    )
    delta, avg_delta, is_anomalous = compute_prompt_delta(snap, history)
    assert delta is None, f"Expected None delta for cross-session, got {delta}"
    print("  ✓ test_prompt_delta_cross_session")


def test_cache_health_cross_session():
    """Cache health should skip token diffs from different sessions."""
    from claude_code_vitals.detector import compute_cache_health

    prev = RateLimitSnapshot(
        ts="2026-04-01T10:00:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=40.0, session_5h_reset=None,
        weekly_7d_pct=65.0, weekly_7d_reset=None,
        context_used_pct=70, context_window_size=200000,
        session_cost_usd=5.0, session_id="session-B",
        cache_read_tokens=100000, cache_creation_tokens=5000,
    )
    snap = RateLimitSnapshot(
        ts="2026-04-01T10:05:00Z",
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=42.0, session_5h_reset=None,
        weekly_7d_pct=67.0, weekly_7d_reset=None,
        context_used_pct=10, context_window_size=200000,
        session_cost_usd=1.0, session_id="session-A",
        cache_read_tokens=5000, cache_creation_tokens=2000,
    )
    result = compute_cache_health(snap, prev)
    # Cross-session: diff-based efficiency is skipped, but session-level fallback
    # computes from snapshot's own cumulative tokens (5000 reads / 7000 total = 71.4%)
    # The key check: context drop 70→10% should NOT be flagged as compaction
    assert result.get("cache_miss_detected", False) is False, "Should not detect cache miss cross-session"
    print("  ✓ test_cache_health_cross_session")


# ===== Regression Tests =====

def test_burn_rate_min_gap():
    """Burn rate should return None when readings are too close together."""
    from claude_code_vitals.detector import compute_burn_rate
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    # Two readings 5 seconds apart — should return None (below 15min minimum)
    history = [RateLimitSnapshot(
        ts=(now - timedelta(seconds=5)).isoformat(),
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=40.0, session_5h_reset=None,
        weekly_7d_pct=50.0, weekly_7d_reset=None,
        context_used_pct=20, context_window_size=200000,
        session_cost_usd=1.0, session_id="test-session",
    )]
    snap = RateLimitSnapshot(
        ts=now.isoformat(),
        provider="anthropic", model_id="claude-opus-4-6", model_name="Opus 4.6",
        session_5h_pct=45.0, session_5h_reset=None,
        weekly_7d_pct=50.0, weekly_7d_reset=None,
        context_used_pct=20, context_window_size=200000,
        session_cost_usd=1.5, session_id="test-session",
    )
    burn_rate, depletion = compute_burn_rate(snap, history)
    # 5 seconds is below the 15-minute minimum — should return None
    assert burn_rate is None, f"Expected None for 5-second gap, got {burn_rate}"
    print("  ✓ test_burn_rate_min_gap")


def test_no_rate_limits_shows_message():
    """JSON with model but no rate_limits should not produce blank output."""
    from claude_code_vitals.logger import parse_statusline_json, extract_snapshot

    raw = '{"model":{"id":"claude-opus-4-6","display_name":"Opus 4.6"},"context_window":{"used_percentage":30,"context_window_size":200000}}'
    data = parse_statusline_json(raw)
    snapshot = extract_snapshot(data)
    # No rate_limits → snapshot should be None
    assert snapshot is None, "Snapshot should be None when no rate_limits present"
    # The fix: run_statusline() checks for None snapshot and prints a message
    # instead of passing None to detect_drift() which produces blank output
    print("  ✓ test_no_rate_limits_shows_message")


# ===== Multi-Row Tests =====

def test_render_multirow_normal_single_line():
    """Normal state with no alerts should produce single-line output (no newline)."""
    from claude_code_vitals.detector import DriftResult
    result = DriftResult(
        signal=Signal.NORMAL,
        current_5h_pct=30.0,
        current_7d_pct=40.0,
        model_name="Opus 4.6",
        baseline_count=50,
    )
    config = Config()
    config.display.color = False
    output = render_compact(result, config)
    # No alerts → should be single line (no \n)
    assert "\n" not in output, f"Expected single line, got multi-line: {output!r}"
    assert "Opus 4.6" in output
    assert "5h: 30% used" in output
    print("  ✓ test_render_multirow_normal_single_line")


def test_render_multirow_with_alerts_two_lines():
    """State with alerts should produce two-line output (has newline)."""
    from claude_code_vitals.detector import DriftResult
    result = DriftResult(
        signal=Signal.SPIKE,
        current_5h_pct=85.0,
        current_7d_pct=90.0,
        model_name="Opus 4.6",
        deviation_pct=25.0,
        depletion_minutes=45,
        session_cost=5.00,
        context_pct=72.0,
        context_tokens=144000,
        cache_efficiency=45.0,
        baseline_count=100,
    )
    config = Config()
    config.display.color = False
    config.display.show_cost = True
    output = render_compact(result, config)
    # With alerts → should have newline (row 2)
    assert "\n" in output, f"Expected multi-line output, got: {output!r}"
    # Row 1 should have essentials
    row1 = output.split("\n")[0]
    assert "Opus 4.6" in row1
    assert "5h" in row1
    # Row 2 should have alerts
    row2 = output.split("\n")[1]
    assert row2.startswith("  "), f"Row 2 should be indented: {row2!r}"
    print("  ✓ test_render_multirow_with_alerts_two_lines")


# ===== Personal Peak Usage Tests =====

def _make_pattern_history(hot_hours: set[int], days: int = 8, per_hour: int = 1):
    """Build synthetic history where snapshots in hot_hours have elevated 7d_pct.

    Generates readings every 2 hours over the given number of days. Hours in
    hot_hours get weekly_7d_pct = 90.0; all other hours get 20.0. This produces
    a clear high-bucket pattern that detect_time_pattern should pick up.
    """
    snaps = []
    base = datetime(2026, 3, 20, 0, 0, 0, tzinfo=timezone.utc)
    for d in range(days):
        for h in range(0, 24, 2):
            ts = base + timedelta(days=d, hours=h)
            pct = 90.0 if h in hot_hours else 20.0
            snaps.append(RateLimitSnapshot(
                ts=ts.isoformat(),
                provider="anthropic",
                model_id="claude-opus-4-6",
                model_name="Opus 4.6",
                session_5h_pct=50.0,
                session_5h_reset=None,
                weekly_7d_pct=pct,
                weekly_7d_reset=None,
                context_used_pct=10.0,
                context_window_size=200000,
            ))
    return snaps


def test_detect_time_pattern_2hr_buckets_and_format():
    """Detected pattern is a bare string in the new 2-hour-bucket format."""
    # Hot hours: 8, 10 -> contiguous 8AM-12PM
    history = _make_pattern_history(hot_hours={8, 10})
    pattern = detect_time_pattern(history)
    assert pattern is not None, "Expected a pattern with clear high buckets"
    assert pattern == "8AM-12PM", f"Expected '8AM-12PM', got {pattern!r}"
    # No legacy arrow prefix or promo suffix
    assert "\u2191" not in pattern
    assert "+2x" not in pattern
    print("  ✓ test_detect_time_pattern_2hr_buckets_and_format")


def test_detect_time_pattern_requires_7_days():
    """detect_time_pattern returns None when history spans < 7 days."""
    # Only 2 days of data -> should fail the span check
    history = _make_pattern_history(hot_hours={8, 10}, days=2)
    assert detect_time_pattern(history) is None
    # Also None when fewer than 84 readings even across days
    sparse = _make_pattern_history(hot_hours={8}, days=8)[:80]
    assert detect_time_pattern(sparse) is None
    print("  ✓ test_detect_time_pattern_requires_7_days")


def test_config_show_personal_pattern_defaults_false():
    """The opt-in flag must default to False."""
    config = Config()
    assert config.display.show_personal_pattern is False
    print("  ✓ test_config_show_personal_pattern_defaults_false")


def test_renderer_personal_pattern_hidden_by_default():
    """With default config, a set pattern must NOT appear in the rendered bar."""
    result = DriftResult(
        signal=Signal.NORMAL,
        current_5h_pct=30.0,
        current_7d_pct=40.0,
        model_name="Opus 4.6",
        baseline_count=100,
        pattern="8AM-12PM",
    )
    config = Config()
    config.display.color = False
    # Default: show_personal_pattern is False
    output = render_compact(result, config)
    assert "your peak usage" not in output, (
        f"Expected no personal pattern with default flag, got: {output!r}"
    )
    # When flag is explicitly enabled, it must appear
    config.display.show_personal_pattern = True
    output_on = render_compact(result, config)
    assert "your peak usage: 8AM-12PM" in output_on, (
        f"Expected personal pattern when enabled, got: {output_on!r}"
    )
    print("  ✓ test_renderer_personal_pattern_hidden_by_default")


def test_parse_pattern_hours_roundtrip():
    """_parse_pattern_hours correctly parses the new bare-label format."""
    assert _parse_pattern_hours("8AM-12PM") == (8, 12)
    assert _parse_pattern_hours("12AM-6AM") == (0, 6)
    assert _parse_pattern_hours("10PM-12AM") == (22, 24)
    # Nonsense input returns None rather than raising
    assert _parse_pattern_hours("") is None
    assert _parse_pattern_hours("garbage") is None
    print("  ✓ test_parse_pattern_hours_roundtrip")


def test_suggest_tip_no_expired_promo_language():
    """Whatever the tip says, it must NEVER reference the expired 2x promo."""
    # Build a history whose detected pattern overlaps PT peak (5-11 AM PT weekdays).
    # 5-11 AM PT == 13:00-19:00 UTC; those UTC hours fall in buckets 12, 14, 16, 18.
    history = _make_pattern_history(hot_hours={12, 14, 16, 18})
    tip = _peak_overlap_tip(history)
    # Tip may be None (weekend/tz edge cases) or a string. Either way, no promo words.
    text = tip or ""
    forbidden = ("2x", "promo", "bonus", "promotional", "PROMO")
    for word in forbidden:
        assert word not in text, (
            f"Expired promo language {word!r} leaked into suggest tip: {text!r}"
        )
    print("  ✓ test_suggest_tip_no_expired_promo_language")


# ===== Run All =====

def run_all():
    print("\n⚡ claude_code_vitals test suite\n")

    print("Config:")
    test_parse_simple_toml()

    print("\nLogger:")
    test_parse_statusline_json()
    test_parse_invalid_json()
    test_extract_snapshot()
    test_extract_no_rate_limits()
    test_append_and_load()
    test_json_line_compact()

    print("\nDetector:")
    test_detect_collecting()
    test_detect_normal()
    test_detect_spike()
    test_detect_drop()
    test_debounce()

    print("\nRenderer:")
    test_render_compact_normal()
    test_render_compact_spike()
    test_render_collecting()
    test_render_expanded()

    # ===== New Feature Tests =====
    print("\nPrompt Delta:")
    test_prompt_delta_normal()
    test_prompt_delta_anomalous()
    test_prompt_delta_empty_history()

    print("\nPeak Status:")
    test_peak_status_weekday_peak()
    test_peak_status_weekday_offpeak()
    test_peak_status_weekend()

    print("\nCache Health:")
    test_cache_health_compact_warning_opus()
    test_cache_health_efficiency()
    test_cache_health_idle_warning()

    # ===== Baseline Tests =====
    print("\nBaseline:")
    test_frozen_baseline()
    test_frozen_baseline_missing()

    print("\nRegression:")
    test_burn_rate_min_gap()
    test_no_rate_limits_shows_message()

    print("\nSession Filtering:")
    test_burn_rate_session_filter()
    test_prompt_delta_cross_session()
    test_cache_health_cross_session()

    # ===== Multi-Row Tests =====
    print("\nMulti-Row:")
    test_render_multirow_normal_single_line()
    test_render_multirow_with_alerts_two_lines()

    # ===== Personal Peak Usage Tests =====
    print("\nPersonal Peak Usage:")
    test_detect_time_pattern_2hr_buckets_and_format()
    test_detect_time_pattern_requires_7_days()
    test_config_show_personal_pattern_defaults_false()
    test_renderer_personal_pattern_hidden_by_default()
    test_parse_pattern_hours_roundtrip()
    test_suggest_tip_no_expired_promo_language()

    print("\nConfig Template:")
    test_write_default_config_branding_and_completeness()

    print("\nFamily Grouping:")
    test_detect_family_known_models()
    test_member_label_strips_family_prefix()
    test_group_by_family_preserves_all_variants()

    print("\nInit Upgrade:")
    test_init_upgrades_legacy_limitwatch_in_place()
    test_init_wraps_unrelated_third_party_statusline()

    print(f"\n🎉 All tests passed!\n")


def test_write_default_config_branding_and_completeness():
    """Config template must use claude-code-vitals branding and include all keys."""
    import tempfile
    from claude_code_vitals.config import Config, write_default_config
    with tempfile.TemporaryDirectory() as tmp:
        config = Config()
        config.data_dir = Path(tmp)
        write_default_config(config)
        content = config.config_path.read_text()
        # Branding
        assert "# claude-code-vitals configuration" in content, "header must use hyphens"
        assert "github.com/jatinmayekar/claude-code-vitals" in content
        assert "# limitwatch configuration" not in content, "old branding must not appear"
        # All tracking keys
        assert "threshold_pct" in content
        assert "debounce_count" in content
        assert "baseline_window_days" in content
        # All display keys
        assert "show_personal_pattern" in content
        assert "show_cost" in content
        assert "show_remaining" in content
        assert "show_readings" in content
        assert "all_models" in content
        assert "color" in content
    print("  ✓ test_write_default_config_branding_and_completeness")


def test_detect_family_known_models():
    """Family detection must recognize every historical identity variant."""
    from claude_code_vitals.__main__ import _detect_family
    # Canonical forms
    assert _detect_family("claude-opus-4-6", "Opus 4.6") == "Opus"
    assert _detect_family("claude-opus-4-6[1m]", "Opus 4.6 (1M context)") == "Opus"
    assert _detect_family("claude-sonnet-4-6", "Sonnet 4.6") == "Sonnet"
    assert _detect_family("claude-haiku-4-5", "Haiku 4.5") == "Haiku"
    # Historical variants seen in real history.jsonl
    assert _detect_family("opus", "Opus 4.6 (1M)") == "Opus"
    assert _detect_family("opus", "Opus 4.6") == "Opus"
    assert _detect_family("claude-haiku-4-5-20251001", "Haiku 4.5") == "Haiku"
    # Future-proof: unknown models fall into "Other"
    assert _detect_family("gpt-4o", "GPT-4o") == "Other"
    assert _detect_family("gemini-2.0-pro", "Gemini 2.0 Pro") == "Other"
    # Case insensitivity
    assert _detect_family("CLAUDE-OPUS-4-6", "OPUS 4.6") == "Opus"
    print("  ✓ test_detect_family_known_models")


def test_member_label_strips_family_prefix():
    """Member labels should be the display name minus the family prefix."""
    from claude_code_vitals.__main__ import _member_label
    assert _member_label("Opus", "Opus 4.6") == "4.6"
    assert _member_label("Opus", "Opus 4.6 (1M context)") == "4.6 (1M context)"
    assert _member_label("Opus", "Opus 4.6 (1M)") == "4.6 (1M)"
    assert _member_label("Sonnet", "Sonnet 4.6") == "4.6"
    assert _member_label("Haiku", "Haiku 4.5") == "4.5"
    # Fallback: no prefix match returns full name unchanged
    assert _member_label("Opus", "SomeOtherModel") == "SomeOtherModel"
    assert _member_label("Opus", "") == ""
    print("  ✓ test_member_label_strips_family_prefix")


def test_group_by_family_preserves_all_variants():
    """CRITICAL TRANSPARENCY GUARD: no variant must ever be silently dropped or merged.

    claude-code-vitals is an observability tool. Silent deduplication of raw
    identities would erase the signal users rely on the tool to surface.
    """
    from claude_code_vitals.__main__ import _group_by_family, _FAMILY_ORDER
    from claude_code_vitals.logger import RateLimitSnapshot

    def mk(model_id, model_name, pct=50.0):
        return RateLimitSnapshot(
            ts="2026-04-05T10:00:00Z",
            provider="anthropic",
            model_id=model_id,
            model_name=model_name,
            session_5h_pct=pct,
            session_5h_reset=None,
            weekly_7d_pct=40.0,
            weekly_7d_reset=None,
            context_used_pct=None,
            context_window_size=None,
        )

    # Simulate the 7-identity drift found in real ~/.claude-code-vitals/history.jsonl
    by_model = {
        "claude-opus-4-6":        [mk("claude-opus-4-6",        "Opus 4.6")],
        "claude-opus-4-6[1m]":    [mk("claude-opus-4-6[1m]",    "Opus 4.6 (1M context)")],
        "opus-ghost":             [mk("opus",                   "Opus 4.6 (1M)")],  # ghost variant
        "claude-sonnet-4-6":      [mk("claude-sonnet-4-6",      "Sonnet 4.6")],
        "claude-haiku-4-5":       [mk("claude-haiku-4-5",       "Haiku 4.5")],
        "claude-haiku-4-5-20251001": [mk("claude-haiku-4-5-20251001", "Haiku 4.5")],
    }

    families = _group_by_family(by_model)

    # All 3 real families present
    assert set(families.keys()) == {"Opus", "Sonnet", "Haiku"}

    # Opus has all 3 variants — none dropped
    opus_labels = [label for label, _, _ in families["Opus"]]
    assert len(opus_labels) == 3, f"Opus must have 3 members (all variants), got {opus_labels}"
    assert "4.6" in opus_labels
    assert "4.6 (1M context)" in opus_labels
    assert "4.6 (1M)" in opus_labels
    # Base version sorts first within family
    assert opus_labels[0] == "4.6", f"expected base '4.6' first, got {opus_labels}"

    # Haiku has both dated and undated id variants — both preserved
    haiku_model_ids = [mid for _, mid, _ in families["Haiku"]]
    assert len(haiku_model_ids) == 2, f"Haiku must preserve both id variants, got {haiku_model_ids}"
    assert "claude-haiku-4-5" in haiku_model_ids
    assert "claude-haiku-4-5-20251001" in haiku_model_ids

    # Sonnet has 1 member
    assert len(families["Sonnet"]) == 1

    # Empty by_model → empty result (not an error)
    assert _group_by_family({}) == {}

    print("  ✓ test_group_by_family_preserves_all_variants")


def test_init_upgrades_legacy_limitwatch_in_place():
    """A pre-rename 'limitwatch' statusLine command must be REPLACED, not wrapped.

    The legacy pipx-installed binary may no longer exist on disk. Wrapping it
    would produce a broken wrapper script that invokes a missing binary.
    """
    import tempfile
    from claude_code_vitals import init_cmd

    with tempfile.TemporaryDirectory() as tmp:
        fake_settings = Path(tmp) / "settings.json"
        fake_settings.write_text(json.dumps({
            "statusLine": {
                "type": "command",
                "command": "/Users/someone/.local/bin/limitwatch run",
            }
        }))
        original_path = init_cmd.CLAUDE_SETTINGS_PATH
        init_cmd.CLAUDE_SETTINGS_PATH = fake_settings
        try:
            assert init_cmd._configure_statusline() is True
            result = json.loads(fake_settings.read_text())
            cmd = result["statusLine"]["command"]
            assert cmd == init_cmd.STATUSLINE_COMMAND, (
                f"legacy limitwatch must be upgraded to STATUSLINE_COMMAND, got: {cmd}"
            )
            assert "limitwatch" not in cmd, f"residual 'limitwatch' in upgraded cmd: {cmd}"
            assert "wrapper" not in cmd, f"legacy upgrade must not create a wrapper: {cmd}"
        finally:
            init_cmd.CLAUDE_SETTINGS_PATH = original_path
    print("  ✓ test_init_upgrades_legacy_limitwatch_in_place")


def test_init_wraps_unrelated_third_party_statusline():
    """A genuine third-party statusLine (no ccvitals/limitwatch) must still be wrapped."""
    import tempfile
    from claude_code_vitals import init_cmd

    with tempfile.TemporaryDirectory() as tmp:
        fake_settings = Path(tmp) / "settings.json"
        fake_settings.write_text(json.dumps({
            "statusLine": {
                "type": "command",
                "command": "bash /some/other/tool/statusline.sh",
            }
        }))
        # Point data_dir at tmp too so the wrapper script lands somewhere safe
        fake_data = Path(tmp) / "data"
        fake_data.mkdir()
        original_path = init_cmd.CLAUDE_SETTINGS_PATH
        init_cmd.CLAUDE_SETTINGS_PATH = fake_settings

        class FakePath(type(Path())):
            pass

        # Monkey-patch Path.home() only for init_cmd's wrapper-path calculation
        import pathlib
        real_home = pathlib.Path.home
        pathlib.Path.home = classmethod(lambda cls: Path(tmp))  # type: ignore
        try:
            # Create the expected data dir the wrapper writer will use
            (Path(tmp) / ".claude-code-vitals").mkdir(exist_ok=True)
            assert init_cmd._configure_statusline() is True
            result = json.loads(fake_settings.read_text())
            cmd = result["statusLine"]["command"]
            assert "statusline-wrapper.sh" in cmd, f"third-party cmd must be wrapped, got: {cmd}"
        finally:
            pathlib.Path.home = real_home  # type: ignore
            init_cmd.CLAUDE_SETTINGS_PATH = original_path
    print("  ✓ test_init_wraps_unrelated_third_party_statusline")


if __name__ == "__main__":
    run_all()
