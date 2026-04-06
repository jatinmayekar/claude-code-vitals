"""Status bar renderer for claude_code_vitals.

Formats DriftResult into ANSI-colored terminal output for Claude Code's
statusLine system. Supports compact (single-line) and expanded views.
"""

from datetime import datetime, timezone
from typing import Optional

from .config import Config
from .detector import DriftResult, Signal


# ANSI color codes
class C:
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


# Signal display configs
SIGNAL_DISPLAY = {
    Signal.SPIKE: {
        "icon": "\u26A0",  # ⚠
        "label": "USAGE SPIKE",
        "color": C.YELLOW,
    },
    Signal.NORMAL: {
        "icon": "\u2713",  # ✓
        "label": "STABLE",
        "color": C.GREEN,
    },
    Signal.DROP: {
        "icon": "\u2B07",  # ⬇
        "label": "USAGE DROP",
        "color": C.BLUE,
    },
    Signal.COLLECTING: {
        "icon": "\u25CB",  # ○
        "label": "COLLECTING",
        "color": C.DIM,
    },
}


def render_compact(result: DriftResult, config: Config) -> str:
    """Render a single-line status bar output with progressive disclosure.

    Normal (clean, 4 elements):
        Opus 4.6  |  5h: 87% left  |  7d: 84% left  |  resets 2h 17m
    Running low (alerts expand):
        Opus 4.6  |  5h: 25% left  |  7d: 12% left  |  runs out 45m  |  try Sonnet (96% left)
    Cache problem:
        Opus 4.6  |  5h: 87% left  |  7d: 84% left  |  Cache: 34%  |  ⚠ CACHE MISS — idle 6min
    Usage spike:
        ⚠ USAGE SPIKE +25% (you're using more)  |  5h: 32% left  |  3.2x avg  |  ⚠ PEAK ends 2h
    """
    sig = SIGNAL_DISPLAY[result.signal]
    use_color = config.display.color

    row1_parts = []
    row2_parts = []

    # Model name (dim prefix)
    if result.model_name:
        if use_color:
            row1_parts.append(f"{C.DIM}{result.model_name}{C.RESET}")
        else:
            row1_parts.append(result.model_name)

    # Signal indicator — only show when something changed (SPIKE/DROP/COLLECTING)
    # STABLE is hidden to save space for useful info
    if result.signal != Signal.NORMAL:
        signal_str = f"{sig['icon']} {sig['label']}"
        if result.signal == Signal.SPIKE and result.deviation_pct is not None:
            signal_str += f" +{abs(result.deviation_pct)}%"
        if result.signal == Signal.DROP and result.deviation_pct is not None:
            signal_str += f" -{abs(result.deviation_pct)}%"
        if result.attribution:
            signal_str += f" ({result.attribution})"

        if use_color:
            signal_str = f"{sig['color']}{C.BOLD}{signal_str}{C.RESET}"
        row1_parts.append(signal_str)

    # Utilization values (used % or remaining % based on config)
    # Color-coded by raw utilization: green <50%, yellow 50-80%, red >80%
    suffix = " left" if config.display.show_remaining else " used"
    if result.current_5h_pct is not None:
        val = round(100 - result.current_5h_pct) if config.display.show_remaining else round(result.current_5h_pct)
        pct_text = f"5h: {val}%{suffix}"
        if use_color:
            row1_parts.append(f"{_color_pct(result.current_5h_pct)}{pct_text}{C.RESET}")
        else:
            row1_parts.append(pct_text)
    if result.current_7d_pct is not None:
        val = round(100 - result.current_7d_pct) if config.display.show_remaining else round(result.current_7d_pct)
        pct_text = f"7d: {val}%{suffix}"
        if use_color:
            row1_parts.append(f"{_color_pct(result.current_7d_pct)}{pct_text}{C.RESET}")
        else:
            row1_parts.append(pct_text)

    # Depletion prediction (only show if <5 hours)
    if result.depletion_minutes is not None and 0 < result.depletion_minutes < 300:
        if result.depletion_minutes <= 60:
            dep_text = f"runs out {result.depletion_minutes}m"
        else:
            h = result.depletion_minutes // 60
            m = result.depletion_minutes % 60
            dep_text = f"runs out {h}h {m}m"
        if use_color:
            dep_color = C.RED if result.depletion_minutes <= 60 else C.YELLOW
            row2_parts.append(f"{dep_color}{dep_text}{C.RESET}")
        else:
            row2_parts.append(dep_text)

    # Hourly comparison (only show when >1.5x)
    if result.hourly_multiplier is not None and result.hourly_multiplier > 1.5:
        mult_text = f"{result.hourly_multiplier}x avg"
        if use_color:
            mult_color = C.RED if result.hourly_multiplier > 3 else C.YELLOW
            row2_parts.append(f"{mult_color}{mult_text}{C.RESET}")
        else:
            row2_parts.append(mult_text)

    # Session cost (dim)
    if config.display.show_cost and result.session_cost is not None:
        cost_text = f"${result.session_cost:.2f}"
        if use_color:
            row2_parts.append(f"{C.DIM}{cost_text}{C.RESET}")
        else:
            row2_parts.append(cost_text)

    # Per-prompt delta — ONLY show when meaningful (delta > 0 or anomalous)
    if result.prompt_delta is not None and (result.prompt_delta > 0 or result.is_anomalous):
        sign = "+" if result.prompt_delta >= 0 else ""
        if result.is_anomalous:
            avg_str = f" (avg {result.avg_prompt_delta}%)" if result.avg_prompt_delta is not None else ""
            delta_text = f"\u26A0 {sign}{result.prompt_delta}% last prompt{avg_str}"
            if use_color:
                row2_parts.append(f"{C.RED}{delta_text}{C.RESET}")
            else:
                row2_parts.append(delta_text)
        else:
            delta_text = f"{sign}{result.prompt_delta}% last prompt"
            if use_color:
                row2_parts.append(f"{C.DIM}{delta_text}{C.RESET}")
            else:
                row2_parts.append(delta_text)

    # 5h reset countdown (cyan)
    if result.reset_5h_at is not None:
        countdown = _format_countdown(result.reset_5h_at)
        if countdown:
            if use_color:
                row1_parts.append(f"{C.CYAN}resets {countdown}{C.RESET}")
            else:
                row1_parts.append(f"resets {countdown}")

    # Peak indicator (after countdown)
    if result.is_peak and result.peak_ends_in_minutes is not None:
        h = result.peak_ends_in_minutes // 60
        m = result.peak_ends_in_minutes % 60
        peak_time = f"{h}h {m}m" if h > 0 else f"{m}m"
        peak_text = f"\u26A0 PEAK ends {peak_time}"
        if use_color:
            row2_parts.append(f"{C.YELLOW}{peak_text}{C.RESET}")
        else:
            row2_parts.append(peak_text)

    # Contextual info based on signal
    if result.signal == Signal.COLLECTING:
        row2_parts.append(f"{result.baseline_count}/10 readings")
    elif result.signal == Signal.SPIKE and result.change_detected_at:
        since = _format_relative_time(result.change_detected_at)
        row2_parts.append(f"since {since}")
    elif config.display.show_readings and result.baseline_count > 0:
        row2_parts.append(f"{result.baseline_count} readings")

    # Context + cache — ONLY show when something needs attention
    if result.compact_warning:
        # Compact warning is always important
        if use_color:
            row2_parts.append(f"{C.YELLOW}{result.compact_warning}{C.RESET}")
        else:
            row2_parts.append(result.compact_warning)
    else:
        # Context: only show when >50% (approaching compact territory)
        if result.context_pct is not None and result.context_pct > 50:
            tokens_str = ""
            if result.context_tokens is not None:
                tokens_str = f" ({result.context_tokens // 1000}k)"
            ctx_text = f"ctx: {result.context_pct:.0f}%{tokens_str}"
            if use_color:
                row2_parts.append(f"{C.DIM}{ctx_text}{C.RESET}")
            else:
                row2_parts.append(ctx_text)
        # Cache: only show when degraded (<80%)
        if result.cache_efficiency is not None and result.cache_efficiency < 80:
            cache_text = f"Cache: {result.cache_efficiency:.0f}%"
            if use_color:
                cache_color = C.YELLOW if result.cache_efficiency >= 50 else C.RED
                row2_parts.append(f"{cache_color}{cache_text}{C.RESET}")
            else:
                row2_parts.append(cache_text)

    # Cache miss alert (when detected)
    if result.cache_miss_detected and result.cache_miss_reason:
        miss_text = f"\u26A0 CACHE MISS \u2014 {result.cache_miss_reason}"
        if use_color:
            row2_parts.append(f"{C.RED}{miss_text}{C.RESET}")
        else:
            row2_parts.append(miss_text)

    # Idle warning (when present)
    if result.idle_warning:
        if use_color:
            row2_parts.append(f"{C.YELLOW}{result.idle_warning}{C.RESET}")
        else:
            row2_parts.append(result.idle_warning)

    # Switch hint (when running low on current model)
    if result.switch_hint:
        if use_color:
            row2_parts.append(f"{C.CYAN}{result.switch_hint}{C.RESET}")
        else:
            row2_parts.append(result.switch_hint)

    # Personal peak usage window (opt-in, Row 3) — learned from history
    # Only shown when user explicitly enables display.show_personal_pattern.
    # Rendered dim; row-joining code promotes to Row 2 if Row 2 is empty.
    if (
        config.display.show_personal_pattern
        and result.pattern is not None
        and "12AM-12AM" not in result.pattern
    ):
        peak_text = f"your peak usage: {result.pattern}"
        if use_color:
            row2_parts.append(f"{C.DIM}{peak_text}{C.RESET}")
        else:
            row2_parts.append(peak_text)

    # Source
    if config.display.show_source:
        if use_color:
            row2_parts.append(f"{C.DIM}{result.source}{C.RESET}")
        else:
            row2_parts.append(result.source)

    separator = "  |  " if not use_color else f"  {C.DIM}|{C.RESET}  "
    if row2_parts:
        row1 = separator.join(row1_parts)
        row2 = separator.join(row2_parts)
        return f"{row1}\n  {row2}"
    return separator.join(row1_parts)


def render_expanded(result: DriftResult, config: Config) -> str:
    """Render a multi-line expanded view.
    
    ┌─ claude_code_vitals ─────────────────────┐
    │  Status:    ⚠ LIMITS DECREASED   │
    │  5h usage:  68%  (baseline: 42%) │
    │  7d usage:  91%  (baseline: 67%) │
    │  Changed:   Mar 28  (2 days ago) │
    │  Pattern:   8PM-12AM             │
    │  Source:    Local (847 points)    │
    └──────────────────────────────────┘
    """
    sig = SIGNAL_DISPLAY[result.signal]
    use_color = config.display.color
    W = 40  # Inner width

    lines = []
    lines.append(f"\u250C\u2500 claude_code_vitals {'─' * (W - 13)}\u2510")

    # Status line
    status_text = f"{sig['icon']} {sig['label']}"
    if result.deviation_pct is not None and result.signal != Signal.NORMAL:
        status_text += f" ({abs(result.deviation_pct):.0f}% deviation)"
    lines.append(f"\u2502  {'Status:':<12}{status_text:<{W-14}}\u2502")

    # 5h usage
    if result.current_5h_pct is not None:
        baseline_note = ""
        if result.baseline_5h_pct is not None:
            baseline_note = f"  (baseline: {result.baseline_5h_pct:.0f}%)"
        val = f"{result.current_5h_pct:.0f}%{baseline_note}"
        lines.append(f"\u2502  {'5h usage:':<12}{val:<{W-14}}\u2502")

    # 7d usage
    if result.current_7d_pct is not None:
        baseline_note = ""
        if result.baseline_7d_pct is not None:
            baseline_note = f"  (baseline: {result.baseline_7d_pct:.0f}%)"
        val = f"{result.current_7d_pct:.0f}%{baseline_note}"
        lines.append(f"\u2502  {'7d usage:':<12}{val:<{W-14}}\u2502")

    # Change date
    if result.change_detected_at and result.signal != Signal.NORMAL:
        since = _format_relative_time(result.change_detected_at)
        try:
            dt = datetime.fromisoformat(result.change_detected_at)
            date_str = dt.strftime("%b %d")
        except (ValueError, TypeError):
            date_str = "unknown"
        val = f"{date_str}  ({since})"
        lines.append(f"\u2502  {'Changed:':<12}{val:<{W-14}}\u2502")

    # Pattern
    if result.pattern:
        lines.append(f"\u2502  {'Pattern:':<12}{result.pattern:<{W-14}}\u2502")

    # Source
    source = f"Local ({result.baseline_count} points)"
    lines.append(f"\u2502  {'Source:':<12}{source:<{W-14}}\u2502")

    lines.append(f"\u2514{'─' * (W - 1)}\u2518")

    return "\n".join(lines)


def render(result: DriftResult, config: Config) -> str:
    """Main render function — dispatches to compact or expanded."""
    if config.display.compact:
        return render_compact(result, config)
    return render_expanded(result, config)


def _color_pct(raw_used_pct: float) -> str:
    """Return ANSI color based on raw utilization. Always uses 'used' perspective."""
    if raw_used_pct >= 80:
        return C.RED
    if raw_used_pct >= 50:
        return C.YELLOW
    return C.GREEN


def _format_countdown(iso_str: str) -> Optional[str]:
    """Return time remaining until iso_str, e.g. '1h 50m'. None if expired or invalid."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        remaining = (dt - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return None
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"
    except (ValueError, TypeError):
        return None


def _format_relative_time(iso_str: str) -> str:
    """Format an ISO timestamp as relative time (e.g., '2 days ago')."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt

        if delta.days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                minutes = delta.seconds // 60
                return f"{minutes}m ago"
            return f"{hours}h ago"
        if delta.days == 1:
            return "yesterday"
        if delta.days < 7:
            return f"{delta.days}d ago"
        weeks = delta.days // 7
        return f"{weeks}w ago"
    except (ValueError, TypeError):
        return "unknown"
