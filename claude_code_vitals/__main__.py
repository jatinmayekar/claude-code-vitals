"""CLI entry point for claude_code_vitals. Run 'ccvitals --help' for usage."""

import sys
import json
from typing import Optional

from .config import load_config, Config
from .logger import parse_statusline_json, extract_snapshot, append_snapshot, should_log
from .detector import detect_drift, Signal
from .renderer import render, render_expanded, C
from .oauth import fetch_usage, oauth_to_snapshot


HELP_TEXT = """\
ccvitals — passive LLM rate limit drift detector for Claude Code
"Know your limits before they know you."

Usage:
  ccvitals <command> [options]

Commands:
  run                 Statusline pipeline: read stdin JSON, log, detect, render
  init                One-time setup: wire into Claude Code settings.json
  status              Show current drift signal and recent readings
  suggest             Ranked model availability with burn rates
  budget              Remaining time per model at current burn rate
  compare             Usage trending across sessions
  baseline            Show rolling median baseline (subcommand: window <N>)
  report              Generate HTML trend report
  explain <topic>     Guides: cache | compact | peak | models
  config              View/update configuration (set | list)
  privacy             Show what data is stored and where
  uninstall           Remove all ccvitals data and configuration
  help, --help, -h    Show this message
  --version           Show version

Common options:
  --session           Scope to the current session
  --global            Scope to all sessions
  --show-readings     (status)           Include recent raw readings
  --all-models        (status, compare)  Include every tracked model
  --show-remaining    (status)           Include time-remaining columns
  --log-only          (run)              Log only, produce no statusline output
  --debug             (run)              Print debug info to stderr

Scope defaults:
  status, suggest, budget, baseline   default to --global
  compare                             defaults to --session

Examples:
  ccvitals init
  ccvitals status --session
  ccvitals suggest
  ccvitals explain cache

Data lives in ~/.claude-code-vitals/ — run 'ccvitals privacy' for details.
"""


def print_help():
    """Print the top-level help screen."""
    print(HELP_TEXT)


# ---------------------------------------------------------------------------
# Model family grouping
# ---------------------------------------------------------------------------
# Claude Code has emitted multiple identity variants for the same underlying
# model over time (e.g. `claude-opus-4-6[1m]` vs `opus` with display_name
# `Opus 4.6 (1M)`). Instead of silently canonicalizing — which would hide a
# real observability signal about API drift — we group rows under family
# headers and show every raw variant as an indented member. Transparency is
# the tool's core principle (see PRD §2, CLAUDE.md design decision #4).

_FAMILY_ORDER = ["Opus", "Sonnet", "Haiku", "Other"]

_FAMILY_KEYWORDS = [
    ("Opus",   ("opus",)),
    ("Sonnet", ("sonnet",)),
    ("Haiku",  ("haiku",)),
]


def _detect_family(model_id: str, model_name: str) -> str:
    """Return the family name (Opus / Sonnet / Haiku / Other) for a model.

    Uses case-insensitive substring matching on both model_id and model_name
    so it works for any future Claude release without a hardcoded enumeration.
    """
    blob = f"{model_id} {model_name}".lower()
    for family, keywords in _FAMILY_KEYWORDS:
        if any(k in blob for k in keywords):
            return family
    return "Other"


def _member_label(family: str, model_name: str) -> str:
    """Strip the family prefix from a display name.

    'Opus 4.6 (1M context)' in family 'Opus' -> '4.6 (1M context)'
    'Sonnet 4.6' in family 'Sonnet' -> '4.6'
    Fallback to full name if the prefix isn't present.
    """
    prefix = family + " "
    if model_name.startswith(prefix):
        return model_name[len(prefix):]
    return model_name


def _group_by_family(by_model: dict) -> dict:
    """Group per-model readings into {family: [(label, model_id, readings), ...]}.

    Preserves every raw identity as a distinct member row — no canonicalization,
    no silent drops. Within each family, members are sorted by label so base
    versions appear before parenthesized variants alphabetically.
    Returns only families that have at least one member.
    """
    families: dict[str, list] = {name: [] for name in _FAMILY_ORDER}
    for model_id, readings in by_model.items():
        if not readings:
            continue
        display_name = readings[-1].model_name
        family = _detect_family(model_id, display_name)
        label = _member_label(family, display_name)
        families[family].append((label, model_id, readings))
    for family in families:
        families[family].sort(key=lambda entry: entry[0])
    return {k: v for k, v in families.items() if v}


# NOTE: superseded by _group_by_family; kept as safety fallback.
# Remove in a later cleanup once we're confident all callers use grouping.
def _dedupe_models(by_model: dict) -> dict:
    """Deduplicate models with same display name, keeping most recent readings."""
    by_name = {}
    for model_id, readings in by_model.items():
        name = readings[-1].model_name
        if name not in by_name or readings[-1].ts > by_name[name][-1].ts:
            by_name[name] = readings
    return by_name


def main():
    args = sys.argv[1:]
    command = args[0] if args else "run"

    config = load_config()

    if command == "init":
        from .init_cmd import init
        init(config)

    elif command == "run":
        log_only = "--log-only" in args
        debug = "--debug" in args
        run_statusline(config, log_only=log_only, debug=debug)

    elif command == "status":
        if "--session" in args or "--global" in args:
            print("  Note: --session/--global is supported by compare only. Showing global data.\n")
        show_readings = "--show-readings" in args
        all_models = "--all-models" in args
        show_remaining = "--show-remaining" in args
        show_status(config, show_readings=show_readings,
                    all_models=all_models, show_remaining=show_remaining)

    elif command == "suggest":
        if "--session" in args or "--global" in args:
            print("  Note: --session/--global is supported by compare only. Showing global data.\n")
        show_suggest(config)

    elif command == "budget":
        if "--session" in args or "--global" in args:
            print("  Note: --session/--global is supported by compare only. Showing global data.\n")
        show_budget(config)

    elif command == "compare":
        all_models = "--all-models" in args
        session_mode = "--global" not in args  # default: session
        show_compare(config, all_models=all_models, session_mode=session_mode)

    elif command == "baseline":
        baseline_command(config, args[1:])

    elif command == "config":
        config_command(config, args[1:])

    elif command == "report":
        generate_report(config)

    elif command == "uninstall":
        from .init_cmd import uninstall
        uninstall(config)

    elif command == "explain":
        subtopic = args[1] if len(args) > 1 else None
        if subtopic:
            from .explain import get_topic, list_topics
            fn = get_topic(subtopic)
            if fn:
                print(fn())
            else:
                print(f"  Unknown topic: {subtopic}")
                print(list_topics())
        else:
            show_explain()

    elif command == "privacy":
        show_privacy()

    elif command in ("-h", "--help", "help"):
        print_help()

    elif command == "--version":
        from . import __version__
        print(f"ccvitals {__version__}")

    else:
        print(f"Unknown command: {command}")
        print("Run 'ccvitals --help' for usage.")
        sys.exit(1)


def run_statusline(config: Config, log_only: bool = False, debug: bool = False):
    """Main statusline handler. Called by Claude Code on every refresh.
    
    Reads JSON from stdin → logs → detects drift → renders output.
    """
    # Read stdin (Claude Code sends JSON)
    try:
        raw = sys.stdin.read()
    except KeyboardInterrupt:
        return

    if debug:
        try:
            import pathlib
            pathlib.Path("/tmp/claude_code_vitals-debug.json").write_text(raw)
        except Exception:
            pass

    if not raw.strip():
        if not log_only:
            print("\u25CB ccvitals  |  waiting for data...")
        return

    # Parse the JSON
    data = parse_statusline_json(raw)
    if data is None:
        if not log_only:
            print("\u25CB ccvitals  |  invalid input")
        return

    # Extract snapshot
    snapshot = extract_snapshot(data)

    # OAuth enrichment — non-blocking, supplementary only
    try:
        oauth_data = fetch_usage(config)
        if oauth_data is not None:
            if snapshot is None:
                model_id = data.get("model", {}).get("id", "unknown")
                model_name = data.get("model", {}).get("display_name", "unknown")
                snapshot = oauth_to_snapshot(oauth_data, model_id, model_name)
            else:
                if snapshot.session_5h_pct is None:
                    snapshot.session_5h_pct = oauth_data.five_hour_utilization
                if snapshot.session_5h_reset is None:
                    snapshot.session_5h_reset = oauth_data.five_hour_resets_at
                if snapshot.weekly_7d_pct is None:
                    snapshot.weekly_7d_pct = oauth_data.seven_day_utilization
                if snapshot.weekly_7d_reset is None:
                    snapshot.weekly_7d_reset = oauth_data.seven_day_resets_at
    except Exception:
        pass  # OAuth is supplementary — never break the run loop

    # Persist current session_id for CLI commands (! ccvitals compare --session)
    if data and data.get("session_id"):
        try:
            config.ensure_data_dir()
            (config.data_dir / "current-session-id").write_text(data["session_id"])
        except Exception:
            pass

    # If snapshot is still None after OAuth, show waiting message
    if snapshot is None:
        if not log_only:
            model_name = data.get("model", {}).get("display_name", "")
            if model_name:
                print(f"{model_name}  |  waiting for rate limit data...")
            else:
                print("\u25CB ccvitals  |  waiting for data...")
        return

    # Log to history (with debouncing)
    if snapshot is not None and should_log(snapshot, config):
        append_snapshot(snapshot, config)

    # If log-only mode (wrapper), stop here
    if log_only:
        return

    # Detect drift
    result = detect_drift(snapshot, config)

    # Switch hint — when current model >70% used, suggest the best alternative
    if snapshot is not None and snapshot.session_5h_pct is not None and snapshot.session_5h_pct > 70:
        result.switch_hint = _compute_switch_hint(snapshot, config)

    # Render and output
    output = render(result, config)
    print(output)


def _compute_switch_hint(current_snapshot, config: Config) -> Optional[str]:
    """Find the best alternative model when current model is running low."""
    from .logger import load_history
    from collections import defaultdict

    history = load_history(config, max_age_days=1)
    if not history:
        return None

    # Get latest reading per model (excluding current)
    by_model = defaultdict(list)
    for s in history:
        if s.model_id != current_snapshot.model_id:
            by_model[s.model_id].append(s)

    if not by_model:
        return None

    # Find model with most 5h remaining
    best_model = None
    best_remaining = 0
    for model_id, readings in by_model.items():
        latest = readings[-1]
        if latest.session_5h_pct is not None:
            remaining = 100 - latest.session_5h_pct
            if remaining > best_remaining:
                best_remaining = remaining
                best_model = latest

    if best_model and best_remaining > 50:
        return f"try {best_model.model_name} ({round(best_remaining)}% left)"
    return None


def _compute_burn_rate(readings: list) -> Optional[str]:
    """Compute per-model burn rate from the last 2 readings within 2 hours.

    Args:
        readings: List of RateLimitSnapshot for a single model, sorted by time.

    Returns:
        Burn rate string like "3%/hr", or None if not enough data.
    """
    from datetime import datetime, timezone

    if len(readings) < 2:
        return None

    # Take last two readings that have 5h pct data
    valid = [r for r in readings if r.session_5h_pct is not None]
    if len(valid) < 2:
        return None

    r1 = valid[-2]
    r2 = valid[-1]

    try:
        t1 = datetime.fromisoformat(r1.ts)
        t2 = datetime.fromisoformat(r2.ts)
    except (ValueError, TypeError):
        return None

    hours_elapsed = (t2 - t1).total_seconds() / 3600.0
    if hours_elapsed < 0.25 or hours_elapsed > 2:
        return None

    delta = r2.session_5h_pct - r1.session_5h_pct
    rate = abs(delta) / hours_elapsed
    return f"{round(rate)}%/hr"


def show_compare(config: Config, all_models: bool = False, session_mode: bool = True):
    """Compare burn rates across time periods to show usage trends.

    When session_mode=True: short-term periods (this/last hour) use session data [S],
    long-term periods (today/yesterday/week) use global data [G].
    When session_mode=False: all periods use global data [G].
    """
    from .logger import load_history
    from datetime import datetime, timezone, timedelta
    from collections import defaultdict
    import statistics

    history = load_history(config, max_age_days=config.tracking.baseline_window_days)
    if not history:
        print("  No data yet. Use Claude Code to collect readings.")
        return

    now = datetime.now(timezone.utc)

    # Get current session_id for filtering
    current_sid = _get_current_session_id(config) if session_mode else None

    # Group by model (keep raw model_id keys — no dedupe).
    # _group_by_family is called below only for the --all-models path;
    # single-model mode uses the raw by_model dict directly.
    by_model: dict[str, list] = defaultdict(list)
    for s in history:
        by_model[s.model_id].append(s)

    # If single model, pick the most recent by model_id (not dedupe-by-name).
    if not all_models:
        latest = history[-1]
        models_to_show = [latest.model_id]
    else:
        models_to_show = None  # handled by family iteration below

    def _parse_ts(ts_str: str) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None

    def _bucket_readings(readings: list, sid: Optional[str] = None) -> dict[str, tuple[list, str]]:
        """Bucket readings into time periods with scope labels.

        For session mode: short-term (this/last hour) filtered by session_id [S],
        long-term (today/yesterday/week) use all readings [G].
        Returns dict of {period: (points, scope_label)}.
        """
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        one_hour_ago = now - timedelta(hours=1)
        two_hours_ago = now - timedelta(hours=2)

        # Session-filtered readings for short-term
        if sid:
            session_readings = [r for r in readings if r.session_id == sid]
        else:
            session_readings = readings

        buckets: dict[str, tuple[list, str]] = {}

        # Short-term: session-scoped when sid provided
        short_source = session_readings if sid else readings
        short_label = "[S]" if sid else "[G]"

        # Long-term: always global
        long_label = "[G]"

        for period, source, label, time_filter in [
            ("This hour", short_source, short_label, lambda dt: dt >= one_hour_ago),
            ("Last hour", short_source, short_label, lambda dt: two_hours_ago <= dt < one_hour_ago),
            ("Today", readings, long_label, lambda dt: dt >= today_start),
            ("Yesterday", readings, long_label, lambda dt: yesterday_start <= dt < today_start),
            ("This week", readings, long_label, lambda dt: True),
        ]:
            points = []
            for r in source:
                if r.session_5h_pct is None:
                    continue
                dt = _parse_ts(r.ts)
                if dt is None:
                    continue
                if time_filter(dt):
                    points.append((dt, r.session_5h_pct))
            buckets[period] = (points, label)

        return buckets

    def _compute_period_stats(points: list[tuple]) -> tuple[Optional[float], Optional[float]]:
        """Compute burn rate (%/hr) and avg per-prompt delta from sorted (dt, pct) pairs.

        Returns:
            (burn_rate, avg_prompt_delta) or (None, None) if insufficient data.
        """
        if len(points) < 2:
            return None, None

        sorted_pts = sorted(points, key=lambda p: p[0])
        total_hours = (sorted_pts[-1][0] - sorted_pts[0][0]).total_seconds() / 3600.0
        if total_hours <= 0:
            return None, None

        positive_deltas = []
        total_positive = 0.0
        for i in range(1, len(sorted_pts)):
            delta = sorted_pts[i][1] - sorted_pts[i - 1][1]
            if delta > 0:
                positive_deltas.append(delta)
                total_positive += delta

        burn_rate = total_positive / total_hours if total_hours > 0 else None

        if positive_deltas:
            avg_delta = statistics.median(positive_deltas)
        else:
            avg_delta = 0.0

        return burn_rate, avg_delta

    if all_models:
        print(f"\n  \u26A1 ccvitals compare --all-models\n")

        families = _group_by_family(by_model)
        use_color = config.display.color
        all_above: list[str] = []  # collect "above baseline" rows across all families

        for family_name in _FAMILY_ORDER:
            if family_name not in families:
                continue
            header = f"{C.BOLD}{family_name}{C.RESET}" if use_color else family_name
            print(f"  {header}")
            for label, model_id, readings in families[family_name]:
                buckets = _bucket_readings(readings, sid=current_sid)
                week_rate, _ = _compute_period_stats(buckets["This week"][0])
                hour_rate, _ = _compute_period_stats(buckets["This hour"][0])

                multiplier = None
                if hour_rate is not None:
                    if week_rate and week_rate > 0:
                        multiplier = hour_rate / week_rate
                        mult_str = f"({multiplier:.1f}x your avg)"
                    else:
                        mult_str = "(no baseline)"
                    rate_str = f"This hour: {hour_rate:.0f}%/hr {mult_str}"
                else:
                    rate_str = "This hour: \u2014"

                if multiplier is not None and multiplier > 1.2:
                    status = "\u26A0 Above baseline"
                    all_above.append(f"{family_name} {label}")
                elif multiplier is not None and multiplier < 0.8:
                    status = "\u2713 Below baseline"
                else:
                    status = "\u2713 Normal"

                indent_label = f"  {label}"
                print(f"  {indent_label:<30} {rate_str:<35} {status}")
            print()

        # Verdict
        if all_above:
            models_str = ", ".join(all_above)
            print(f"  Verdict: {models_str} burning faster than usual. Consider switching to another model.")
        else:
            print(f"  Verdict: All models are within normal range.")
        print()
        return

    # Single model detailed view
    model_id = models_to_show[0]
    readings = by_model[model_id]
    model_name = readings[-1].model_name
    buckets = _bucket_readings(readings, sid=current_sid)

    print(f"\n  \u26A1 ccvitals compare \u2014 How is your usage trending?\n")
    header = f"  {model_name}"
    if current_sid:
        header += f"    Session: {current_sid[:8]}"
    print(f"{header}\n")
    print(f"  {'Period':<20} {'Burn rate':<14} {'Avg per prompt':<19} {'vs. your baseline'}")
    print(f"  {'─' * 20}    {'─' * 10}    {'─' * 14}     {'─' * 17}")

    # Compute baseline from This week (always global)
    week_points, _ = buckets["This week"]
    week_rate, week_delta = _compute_period_stats(week_points)

    period_order = ["This hour", "Last hour", "Today", "Yesterday", "This week"]
    period_results = {}

    for period in period_order:
        points, label = buckets[period]
        rate, delta = _compute_period_stats(points)
        period_results[period] = (rate, delta)

        period_label = f"{period} {label}"
        if rate is None:
            print(f"  {period_label:<20} {'—':<14} {'—':<19} {'—'}")
            continue

        rate_str = f"{rate:.0f}%/hr"
        delta_str = f"+{delta:.1f}%/prompt"

        if period == "This week":
            baseline_str = "\u2190 your baseline"
        elif week_rate and week_rate > 0:
            mult = rate / week_rate
            if mult > 1.1:
                baseline_str = f"{mult:.1f}x faster"
            elif mult < 0.9:
                baseline_str = f"{mult:.1f}x (slower)"
            else:
                baseline_str = f"{mult:.1f}x (normal)"
        else:
            baseline_str = "—"

        print(f"  {period_label:<20} {rate_str:<14} {delta_str:<19} {baseline_str}")

    # Legend
    if current_sid:
        print(f"\n  [S] = this session only    [G] = all sessions (account-level)")

    # Verdict
    hour_rate = period_results.get("This hour", (None, None))[0]
    if hour_rate is not None and week_rate and week_rate > 0:
        mult = hour_rate / week_rate
        if mult > 1.1:
            print(f"\n  Verdict: You're burning {mult:.1f}x faster this hour than your weekly average.")
            print(f"           Your burn rate increased \u2014 this is likely your own usage pattern.")
        elif mult < 0.9:
            print(f"\n  Verdict: You're burning {mult:.1f}x slower this hour than your weekly average.")
        else:
            print(f"\n  Verdict: Your current burn rate is in line with your weekly average.")
    else:
        print(f"\n  Verdict: Not enough data in this hour to compare against your baseline.")
    print()


def baseline_command(config: Config, args: list):
    """Manage baselines: view, reset, freeze, unfreeze, set window.

    Usage:
        ccvitals baseline                   # Show current baselines
        ccvitals baseline reset             # Show warning
        ccvitals baseline reset --confirm   # Clear all data
        ccvitals baseline window <N>        # Set baseline window days
        ccvitals baseline freeze            # Freeze current baselines
        ccvitals baseline unfreeze          # Unfreeze baselines
    """
    import statistics
    from .logger import load_history
    from collections import defaultdict
    from datetime import datetime, timezone

    frozen_path = config.data_dir / "baseline-frozen.json"

    subcmd = args[0] if args else None

    if subcmd is None:
        # Show current baselines per model
        history = load_history(config, max_age_days=config.tracking.baseline_window_days)
        if not history:
            print("  No baseline data yet. Use Claude Code to collect readings.")
            return

        by_model: dict[str, list] = defaultdict(list)
        for s in history:
            by_model[s.model_id].append(s)
        families = _group_by_family(by_model)

        is_frozen = frozen_path.exists()

        print(f"\n  \u26A1 ccvitals baseline \u2014 Current baselines (window: {config.tracking.baseline_window_days} days)")
        if is_frozen:
            print(f"  \u26A1 FROZEN \u2014 baselines are locked to a saved snapshot")
        print()
        print(f"  {'Model':<25} {'5h median':>10} {'7d median':>10} {'Points':>8} {'Oldest'}")
        print(f"  {'─' * 25} {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 12}")

        use_color = config.display.color
        for family_name in _FAMILY_ORDER:
            if family_name not in families:
                continue
            header = f"{C.BOLD}{family_name}{C.RESET}" if use_color else family_name
            print(f"  {header}")
            for label, model_id, readings in families[family_name]:
                h5_vals = [r.session_5h_pct for r in readings if r.session_5h_pct is not None]
                d7_vals = [r.weekly_7d_pct for r in readings if r.weekly_7d_pct is not None]

                h5_med = f"{statistics.median(h5_vals):.1f}%" if h5_vals else "—"
                d7_med = f"{statistics.median(d7_vals):.1f}%" if d7_vals else "—"
                count = len(readings)
                oldest = readings[0].ts[:10]

                indent_label = f"  {label}"
                print(f"  {indent_label:<25} {h5_med:>10} {d7_med:>10} {count:>8} {oldest}")

        print()
        return

    if subcmd == "reset":
        if "--confirm" in args:
            # Count readings before deleting
            count = 0
            if config.history_path.exists():
                count = sum(1 for line in open(config.history_path) if line.strip())
                config.history_path.unlink()

            state_path = config.data_dir / "state.json"
            if state_path.exists():
                state_path.unlink()

            if frozen_path.exists():
                frozen_path.unlink()

            print(f"  Cleared {count} readings.")
            print(f"  Deleted: history.jsonl, state.json, baseline-frozen.json")
            print(f"  Run 'ccvitals init' to reconfigure if needed.")
        else:
            print("  \u26A0 This will clear all history and state.")
            print("  Run with --confirm to proceed:")
            print("    ccvitals baseline reset --confirm")
        return

    if subcmd == "window":
        if len(args) < 2:
            print(f"  Current baseline window: {config.tracking.baseline_window_days} days")
            print(f"  Usage: ccvitals baseline window <N>")
            return

        try:
            days = int(args[1])
        except ValueError:
            print(f"  Invalid number: {args[1]}")
            return

        if days < 1 or days > 84:
            print(f"  Window must be between 1 and 84 days.")
            return

        # Update config file
        if not config.config_path.exists():
            from .config import write_default_config
            write_default_config(config)

        lines = config.config_path.read_text().splitlines()
        found = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            line_key = stripped.split("=")[0].strip()
            if line_key == "baseline_window_days":
                lines[i] = f"baseline_window_days = {days}"
                found = True
                break

        if not found:
            # Find [tracking] section and insert
            for i, line in enumerate(lines):
                if line.strip() == "[tracking]":
                    lines.insert(i + 1, f"baseline_window_days = {days}")
                    found = True
                    break

        if found:
            config.config_path.write_text("\n".join(lines) + "\n")
            print(f"  Baseline window set to {days} days.")
        else:
            print(f"  Could not update config. Add baseline_window_days = {days} to [tracking] in config.toml.")
        return

    if subcmd == "freeze":
        history = load_history(config, max_age_days=config.tracking.baseline_window_days)
        if not history:
            print("  No data to freeze. Use Claude Code to collect readings first.")
            return

        by_model: dict[str, dict] = {}
        model_readings: dict[str, list] = defaultdict(list)
        for s in history:
            model_readings[s.model_id].append(s)

        for model_id, readings in model_readings.items():
            h5_vals = [r.session_5h_pct for r in readings if r.session_5h_pct is not None]
            d7_vals = [r.weekly_7d_pct for r in readings if r.weekly_7d_pct is not None]

            by_model[model_id] = {
                "5h_median": round(statistics.median(h5_vals), 2) if h5_vals else None,
                "7d_median": round(statistics.median(d7_vals), 2) if d7_vals else None,
            }

        frozen_path.write_text(json.dumps(by_model, indent=2))
        print(f"  Baseline frozen for {len(by_model)} model(s).")
        print(f"  Saved to: {frozen_path}")
        print(f"  Drift detection will compare against this snapshot.")
        print(f"  Run 'ccvitals baseline unfreeze' to resume live baselines.")
        return

    if subcmd == "unfreeze":
        if frozen_path.exists():
            frozen_path.unlink()
            print(f"  Baseline unfrozen. Live rolling baselines restored.")
        else:
            print(f"  No frozen baseline found. Already using live baselines.")
        return

    print("  Usage:")
    print("    ccvitals baseline                   # Show current baselines")
    print("    ccvitals baseline reset             # Clear all data")
    print("    ccvitals baseline window <N>        # Set baseline window days")
    print("    ccvitals baseline freeze            # Lock baselines to current values")
    print("    ccvitals baseline unfreeze          # Resume live baselines")


def _get_current_session_id(config: Config) -> Optional[str]:
    """Read the current session_id from the persisted file.

    Written by run_statusline() on each statusbar refresh.
    Best-effort: may be from a different session if multiple are active.
    """
    sid_path = config.data_dir / "current-session-id"
    if not sid_path.exists():
        return None
    try:
        sid = sid_path.read_text().strip()
        return sid if sid else None
    except (PermissionError, OSError):
        return None


def show_budget(config: Config):
    """Show remaining session budget across all models."""
    from .logger import load_history
    from collections import defaultdict
    from datetime import datetime, timezone

    history = load_history(config, max_age_days=1)
    if not history:
        print("  No data yet. Use Claude Code to collect readings.")
        return

    by_model = defaultdict(list)
    for s in history:
        by_model[s.model_id].append(s)
    families = _group_by_family(by_model)

    print("\n  \u26A1 ccvitals budget \u2014 Session capacity\n")
    print(f"  {'Model':<25} {'Remaining':>10} {'Burn rate':>10} {'Time left':>10}")
    print(f"  {'─'*25} {'─'*10} {'─'*10} {'─'*10}")

    use_color = config.display.color
    for family_name in _FAMILY_ORDER:
        if family_name not in families:
            continue
        header = f"{C.BOLD}{family_name}{C.RESET}" if use_color else family_name
        print(f"\n  {header}")
        for label, model_id, readings in families[family_name]:
            latest = readings[-1]
            if latest.session_5h_pct is None:
                continue

            remaining_pct = 100 - latest.session_5h_pct

            burn_rate = None
            valid = [r for r in readings if r.session_5h_pct is not None]
            if len(valid) >= 2:
                r1, r2 = valid[-2], valid[-1]
                try:
                    t1 = datetime.fromisoformat(r1.ts).replace(tzinfo=timezone.utc)
                    t2 = datetime.fromisoformat(r2.ts).replace(tzinfo=timezone.utc)
                    hrs = (t2 - t1).total_seconds() / 3600
                    if 0.25 <= hrs <= 2:
                        delta = r2.session_5h_pct - r1.session_5h_pct
                        if delta > 0:
                            burn_rate = delta / hrs
                except (ValueError, TypeError):
                    pass

            if burn_rate and burn_rate > 0:
                hours_left = remaining_pct / burn_rate
                time_str = f"~{int(hours_left * 60)}min" if hours_left < 1 else f"~{hours_left:.1f}hrs"
                rate_str = f"{burn_rate:.0f}%/hr"
            else:
                time_str = "—"
                rate_str = "—"

            indent_label = f"  {label}"
            print(f"  {indent_label:<25} {remaining_pct:.0f}% left{' ':>3} {rate_str:>10} {time_str:>10}")

    print()
    print("  Tip: Switch to a model with more budget to extend your session.")
    print("  Run: ccvitals suggest  for model recommendations.")
    print()


def _parse_pattern_hours(pattern: str) -> Optional[tuple[int, int]]:
    """Parse a time-pattern string like ``"8AM-12PM"`` into 24-hour start/end.

    Args:
        pattern: A bucket label from :func:`detector.detect_time_pattern`, formatted
            as ``"<start><AM|PM>-<end><AM|PM>"`` (e.g. ``"8AM-12PM"``, ``"10PM-12AM"``).

    Returns:
        A ``(start_hour, end_hour)`` tuple in 24-hour local time, or ``None`` if the
        string cannot be parsed. ``end_hour`` of 24 represents midnight rollover.
    """
    import re
    m = re.match(r"^\s*(\d{1,2})(AM|PM)-(\d{1,2})(AM|PM)\s*$", pattern, re.IGNORECASE)
    if not m:
        return None

    def to_24(h: int, mer: str) -> int:
        mer = mer.upper()
        if mer == "AM":
            return 0 if h == 12 else h
        return 12 if h == 12 else h + 12

    start = to_24(int(m.group(1)), m.group(2))
    end = to_24(int(m.group(3)), m.group(4))
    if end <= start:
        end += 24  # wrap past midnight
    return (start, end)


def _peak_overlap_tip(history: list) -> Optional[str]:
    """Build a tip string if the user's personal pattern overlaps Anthropic's peak.

    Anthropic's authoritative peak window is 5\u201311 AM Pacific Time on weekdays.
    The user's personal heavy-usage window (from
    :func:`detector.detect_time_pattern`) is expressed in their local timezone.
    We convert the user's pattern start/end to PT, then check for interval overlap
    with ``[5, 11)`` PT. Returns ``None`` when there is no pattern, no overlap, or
    the conversion fails. Returns ``None`` on weekends (peak is weekdays only).

    Args:
        history: The list of snapshots to pass to ``detect_time_pattern``.

    Returns:
        A multi-line tip string, or ``None``.
    """
    from datetime import datetime, timedelta, timezone
    try:
        from .detector import detect_time_pattern
    except ImportError:
        return None

    try:
        pattern = detect_time_pattern(history)
    except Exception:
        return None
    if not pattern or not isinstance(pattern, str):
        return None

    hours = _parse_pattern_hours(pattern)
    if hours is None:
        return None
    start_h, end_h = hours

    # Resolve timezones.
    now_local = datetime.now().astimezone()
    local_tz = now_local.tzinfo
    local_abbrev = now_local.strftime("%Z") or "local"

    try:
        import zoneinfo
        try:
            pt_tz = zoneinfo.ZoneInfo("America/Los_Angeles")
        except zoneinfo.ZoneInfoNotFoundError:
            pt_tz = timezone(timedelta(hours=-8))
    except ImportError:
        pt_tz = timezone(timedelta(hours=-8))

    # Build today's datetime for the pattern start in local tz, convert to PT.
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        start_local = today_local + timedelta(hours=start_h)
        end_local = today_local + timedelta(hours=end_h)
        start_pt = start_local.astimezone(pt_tz)
        end_pt = end_local.astimezone(pt_tz)
    except (ValueError, OverflowError):
        return None

    # Weekday check in PT (peak is Mon-Fri PT).
    now_pt = now_local.astimezone(pt_tz)
    if now_pt.weekday() >= 5:
        return None

    # Represent the user's window as hour-offsets from start_pt's midnight (PT).
    pt_midnight = start_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    user_start_offset = (start_pt - pt_midnight).total_seconds() / 3600.0
    user_end_offset = (end_pt - pt_midnight).total_seconds() / 3600.0

    # Anthropic peak: [5, 11) PT today. Allow overlap against both the base
    # day and a +24h shift in case the user's window wraps into a different PT day.
    def overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
        return a0 < b1 and b0 < a1

    peak_intervals = [(5.0, 11.0), (29.0, 35.0), (-19.0, -13.0)]
    has_overlap = any(
        overlaps(user_start_offset, user_end_offset, p0, p1)
        for p0, p1 in peak_intervals
    )
    if not has_overlap:
        return None

    return (
        f"  Tip: your peak usage ({pattern} {local_abbrev}) overlaps Anthropic's peak window\n"
        f"       (5\u201311 AM PT weekdays), when 5-hour session limits burn faster."
    )


def show_suggest(config: Config):
    """Show all models ranked by remaining quota with burn rates."""
    from .logger import load_history
    from collections import defaultdict

    history = load_history(config, max_age_days=1)
    if not history:
        print("  No data yet. Use Claude Code to collect readings.")
        return

    by_model = defaultdict(list)
    for s in history:
        by_model[s.model_id].append(s)
    families = _group_by_family(by_model)

    # Build rows per family: (family, label, 5h_left, 7d_left, burn_rate)
    # Each raw identity variant gets its own row — no silent merging.
    all_rows = []
    for family_name in _FAMILY_ORDER:
        if family_name not in families:
            continue
        for label, model_id, model_readings in families[family_name]:
            latest = model_readings[-1]
            h5_left = round(100 - latest.session_5h_pct) if latest.session_5h_pct is not None else None
            d7_left = round(100 - latest.weekly_7d_pct) if latest.weekly_7d_pct is not None else None
            burn = _compute_burn_rate(model_readings)
            all_rows.append((family_name, label, h5_left, d7_left, burn))

    # The overall "Best available" is the row with the highest 5h-left across all families.
    best_idx = max(
        range(len(all_rows)),
        key=lambda i: all_rows[i][2] if all_rows[i][2] is not None else -1,
        default=None,
    ) if all_rows else None

    print("\n  \u26A1 ccvitals suggest \u2014 Model availability\n")
    print(f"  {'Model':<25} {'5h left':>8} {'7d left':>8} {'Burn':>9}   {'Status'}")
    print(f"  {'─'*25} {'─'*8} {'─'*8} {'─'*9}   {'─'*20}")

    use_color = config.display.color
    current_family = None
    for i, (family, label, h5, d7, burn) in enumerate(all_rows):
        if family != current_family:
            header = f"{C.BOLD}{family}{C.RESET}" if use_color else family
            print(f"\n  {header}")
            current_family = family
        h5_str = f"{h5}%" if h5 is not None else "?"
        d7_str = f"{d7}%" if d7 is not None else "?"
        burn_str = burn if burn is not None else "—"
        if h5 is not None and h5 < 30:
            status = "\u26A0 Running low"
        elif i == best_idx:
            status = "\u2713 Best available"
        else:
            status = "  Available"
        indent_label = f"  {label}"  # indent under family header
        print(f"  {indent_label:<25} {h5_str:>8} {d7_str:>8} {burn_str:>9}   {status}")

    print()

    # Peak-window overlap tip (factual, weekday-only).
    tip = _peak_overlap_tip(history)
    if tip:
        print(tip)
        print()


def show_status(config: Config, show_readings: bool = False,
                all_models: bool = False, show_remaining: bool = False):
    """Show current drift status from stored history (no stdin needed).

    Flags:
        --all-models      Show status for every model on its own line
        --show-readings   Append readings count
        --show-remaining  Show remaining % instead of used %
    """
    from .logger import load_history
    from .renderer import render_compact
    from collections import defaultdict

    # CLI flags override config defaults
    show_readings = show_readings or config.display.show_readings
    all_models = all_models or config.display.all_models
    if show_remaining:
        config.display.show_remaining = True

    history = load_history(config, max_age_days=config.tracking.baseline_window_days)

    if not history:
        print("\u25CB ccvitals \u2014 no data yet")
        print(f"  History file: {config.history_path}")
        print(f"  Start using Claude Code with ccvitals configured.")
        return

    if all_models:
        # Group by model_id, then by family for hierarchical display.
        # Every raw identity variant is preserved as its own member row
        # (transparency principle — see _group_by_family docstring).
        by_model = defaultdict(list)
        for s in history:
            by_model[s.model_id].append(s)
        families = _group_by_family(by_model)

        if not families:
            print("  No data yet.")
            return

        use_color = config.display.color
        for family_name in _FAMILY_ORDER:
            if family_name not in families:
                continue
            header = f"{C.BOLD}{family_name}{C.RESET}" if use_color else family_name
            print(f"\n{header}")
            for label, model_id, model_history in families[family_name]:
                latest = model_history[-1]
                result = detect_drift(latest, config)
                # Override model_name to the stripped member label so the row
                # renders as "4.6 (1M context)" instead of "Opus 4.6 (1M context)"
                result.model_name = label
                config.display.compact = True
                line = render_compact(result, config)
                print(f"  {line}")
    else:
        # Single model — most recent reading
        latest = history[-1]
        result = detect_drift(latest, config)

        config.display.compact = False
        output = render_expanded(result, config)
        print(output)

        if show_readings:
            # Count readings for this model only
            model_count = sum(1 for s in history if s.model_id == latest.model_id)
            print(f"\n  Readings: {model_count} for {latest.model_name}")
            print(f"  Total: {len(history)} across all models")


def config_command(config: Config, args: list):
    """View or modify claude_code_vitals configuration.

    Usage:
        ccvitals config list                    # show all settings
        ccvitals config set <key> <value>       # change a setting
    """
    if not args or args[0] == "list":
        # Print current config.toml
        if config.config_path.exists():
            print(config.config_path.read_text())
        else:
            print("  No config file yet. Run 'ccvitals init' first.")
        return

    if args[0] == "set" and len(args) >= 3:
        key = args[1]
        value = args[2]

        if not config.config_path.exists():
            from .config import write_default_config
            write_default_config(config)

        lines = config.config_path.read_text().splitlines()
        found = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            line_key = stripped.split("=")[0].strip()
            if line_key == key:
                # Preserve inline comment if any
                comment = ""
                if " #" in stripped:
                    comment = "  #" + stripped.split(" #", 1)[1]
                # Format value
                if value.lower() in ("true", "false"):
                    formatted = value.lower()
                elif value.startswith('"'):
                    formatted = value
                else:
                    try:
                        float(value)
                        formatted = value
                    except ValueError:
                        formatted = f'"{value}"'
                NUMERIC_KEYS = {"threshold_pct", "debounce_count", "baseline_window_days"}
                if key in NUMERIC_KEYS:
                    try:
                        float(value)
                    except ValueError:
                        print(f"  Error: {key} must be a number, got '{value}'")
                        return
                lines[i] = f"{key} = {formatted}{' ' * max(0, 25 - len(key) - len(formatted))}{comment}"
                found = True
                break

        if not found:
            # Key is valid but missing from file — add it to the right section
            section_map = {
                "baseline_window_days": "tracking", "threshold_pct": "tracking",
                "debounce_count": "tracking",
                "compact": "display", "show_pattern": "display",
                "show_source": "display", "show_remaining": "display",
                "show_cost": "display",
                "show_readings": "display", "all_models": "display",
                "color": "display",
            }
            section = section_map.get(key)
            if section is None:
                print(f"  Unknown key: {key}")
                print(f"  Valid keys: {', '.join(sorted(section_map.keys()))}")
                return
            # Find the section header and insert after last key in that section
            section_header = f"[{section}]"
            insert_at = None
            for i, line in enumerate(lines):
                if line.strip() == section_header:
                    insert_at = i + 1
                elif insert_at is not None:
                    if line.strip().startswith("["):
                        break
                    if line.strip() and not line.strip().startswith("#"):
                        insert_at = i + 1
            if insert_at is not None:
                NUMERIC_KEYS = {"threshold_pct", "debounce_count", "baseline_window_days"}
                if key in NUMERIC_KEYS:
                    try:
                        float(value)
                    except ValueError:
                        print(f"  Error: {key} must be a number, got '{value}'")
                        return
                if value.lower() in ("true", "false"):
                    formatted = value.lower()
                else:
                    formatted = value
                lines.insert(insert_at, f"{key} = {formatted}")
                found = True

        config.config_path.write_text("\n".join(lines) + "\n")
        print(f"  {key} = {value}")
        return

    print("  Usage:")
    print("    ccvitals config list")
    print("    ccvitals config set <key> <value>")


def generate_report(config: Config):
    """Generate an HTML trend report and open it in the browser."""
    from .logger import load_history
    import webbrowser
    from pathlib import Path

    history = load_history(config)

    if len(history) < 5:
        print(f"\u25CB ccvitals — not enough data for a report ({len(history)} points)")
        print("  Keep using Claude Code and check back in a few days.")
        return

    # Build simple HTML report with inline chart
    html = _build_report_html(history)

    report_path = config.data_dir / "report.html"
    report_path.write_text(html)
    print(f"  Report saved: {report_path}")

    try:
        webbrowser.open(f"file://{report_path}")
        print("  Opened in browser.")
    except Exception:
        print(f"  Open manually: file://{report_path}")


def _build_report_html(history) -> str:
    """Build a standalone HTML report with trend charts using Chart.js CDN."""

    # Prepare data series
    timestamps = []
    weekly_pcts = []
    session_pcts = []

    for snap in history:
        timestamps.append(snap.ts[:16])  # Trim to minute precision
        weekly_pcts.append(snap.weekly_7d_pct if snap.weekly_7d_pct is not None else "null")
        session_pcts.append(snap.session_5h_pct if snap.session_5h_pct is not None else "null")

    ts_json = json.dumps(timestamps)
    weekly_json = json.dumps(weekly_pcts)
    session_json = json.dumps(session_pcts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ccvitals — Rate Limit Trend Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 2rem; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; color: #58a6ff; }}
  .subtitle {{ color: #8b949e; margin-bottom: 2rem; }}
  .chart-container {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
  .stat {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1rem; }}
  .stat-value {{ font-size: 1.8rem; font-weight: bold; color: #58a6ff; }}
  .stat-label {{ color: #8b949e; font-size: 0.85rem; }}
  footer {{ color: #484f58; font-size: 0.8rem; margin-top: 2rem; text-align: center; }}
</style>
</head>
<body>
<h1>\u26A1 ccvitals — Rate Limit Trends</h1>
<p class="subtitle">Generated from {len(history)} data points</p>

<div class="stats">
  <div class="stat">
    <div class="stat-value">{len(history)}</div>
    <div class="stat-label">Data Points</div>
  </div>
  <div class="stat">
    <div class="stat-value">{history[0].ts[:10] if history else 'N/A'}</div>
    <div class="stat-label">Tracking Since</div>
  </div>
  <div class="stat">
    <div class="stat-value">{history[-1].provider if history else 'N/A'}</div>
    <div class="stat-label">Provider</div>
  </div>
  <div class="stat">
    <div class="stat-value">{history[-1].model_name if history else 'N/A'}</div>
    <div class="stat-label">Current Model</div>
  </div>
</div>

<div class="chart-container">
  <canvas id="trendChart"></canvas>
</div>

<div class="chart-container">
  <canvas id="sessionChart"></canvas>
</div>

<footer>ccvitals v0.2.0 — Know your limits before they know you.</footer>

<script>
const timestamps = {ts_json};
const weeklyData = {weekly_json};
const sessionData = {session_json};

new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: timestamps,
    datasets: [{{
      label: '7-Day Utilization %',
      data: weeklyData,
      borderColor: '#f85149',
      backgroundColor: 'rgba(248, 81, 73, 0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 2,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ title: {{ display: true, text: 'Weekly (7d) Rate Limit Utilization', color: '#c9d1d9' }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8b949e', maxTicksLimit: 12 }}, grid: {{ color: '#21262d' }} }},
      y: {{ min: 0, max: 100, ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }}
    }}
  }}
}});

new Chart(document.getElementById('sessionChart'), {{
  type: 'line',
  data: {{
    labels: timestamps,
    datasets: [{{
      label: '5-Hour Session Utilization %',
      data: sessionData,
      borderColor: '#58a6ff',
      backgroundColor: 'rgba(88, 166, 255, 0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 2,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ title: {{ display: true, text: 'Session (5h) Rate Limit Utilization', color: '#c9d1d9' }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8b949e', maxTicksLimit: 12 }}, grid: {{ color: '#21262d' }} }},
      y: {{ min: 0, max: 100, ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""


def show_explain():
    """Explain what every part of the status line means."""
    print("""
\u26A1 ccvitals \u2014 Status Line Guide

  EXAMPLE (normal usage \u2014 everything is fine):

    Opus 4.6 (1M context)  |  5h: 100% left  |  7d: 91% left  |  $40.17  |  resets 4h 40m  |  92 readings  |  \u2191 8PM-12AM
    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    Model                    5h window        7d window        Cost     Reset countdown    Baseline       Pattern

  EXAMPLE (running low \u2014 switch hint appears):

    Opus 4.6  |  5h: 25% left  |  7d: 12% left  |  $5.00  |  resets 1h 20m  |  try Sonnet (96% left)

  EXAMPLE (usage spike detected \u2014 alert with attribution):

    Opus 4.6  |  \u26A0 USAGE SPIKE +25% (baseline shift)  |  5h: 32% left  |  7d: 12% left

  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

  SIGNALS (is your usage pattern different from your baseline?):

    When everything is normal, NO signal is shown \u2014 clean status bar.
    Signals ONLY appear when your utilization deviates from your baseline:

    \u26A0 USAGE SPIKE \u2014 Your utilization is ABOVE your rolling baseline.
                     Attribution is shown when burn rate data is available:
                     "(you're using more)" = your burn rate increased
                     "(baseline shift)" = same burn rate, higher utilization
    \u2B07 USAGE DROP  \u2014 Your utilization is BELOW your baseline.
                     You're consuming less than usual.
    \u25CB COLLECTING  \u2014 Building a baseline. Need 10+ readings before
                     deviation detection kicks in. Just keep using Claude Code.

    NOTE: USAGE SPIKE is a pure observability signal — your utilization
    pattern shifted relative to your own rolling baseline. The attribution
    in parentheses uses burn rate comparison to describe the shift.

  PERCENTAGES (color-coded):

    5h: 100% left \u2014 100% of your 5-hour window quota remains
    7d: 91% left  \u2014 91% of your 7-day window quota remains

    Colors: green (<50% used), yellow (50-80% used), red (>80% used)

    Toggle: ccvitals config set show_remaining true   \u2192 "94% left"
            ccvitals config set show_remaining false  \u2192 "6% used"

  COST:

    $40.17  \u2014 Total cost of this Claude Code session in USD.
              This is the cumulative cost since the session started.

    Toggle: ccvitals config set show_cost true/false

  COUNTDOWN:

    resets 4h 40m \u2014 Time until your 5-hour window resets and usage goes back to 0%.
                    The reset happens regardless of how much you've used.

  READINGS:

    92 readings \u2014 Data points collected for this model's baseline.
                  Need 10+ for drift detection. More = more accurate.

    Toggle: ccvitals config set show_readings true/false

  PATTERN:

    \u2191 8PM-12AM \u2014 ccvitals detected higher capacity during 8PM-12AM.
                 This indicates higher capacity available during this window.
                 Schedule heavy work during this period for more capacity.

    Toggle: ccvitals config set show_pattern true/false

  SWITCH HINTS:

    try Sonnet (96% left) \u2014 Appears when your current model is >70% used.
                            Each model has its own separate rate limit pool.
                            Switching gives you a fresh quota window.

    For a full comparison: ! ccvitals suggest

  PEAK HOURS:

    \u26A0 PEAK \u2014 ends 2h 14m \u2014 Anthropic's official peak: 5am-11am PT weekdays.
                             During peak, your 5-hour limit burns faster.
                             Schedule heavy work for off-peak when possible.

  CONTEXT & CACHE:

    ctx: 48% (96k)  \u2014 Your context window usage. Higher = more tokens per prompt.
    Cache: 94%      \u2014 Percentage of tokens served from cache (cheap) vs reprocessed (expensive).
                      Green (>80%): healthy. Yellow (50-80%): degraded. Red (<50%): broken.

    \u26A0 COMPACT WARNING \u2014 Appears when context approaches auto-compact threshold.
                         Opus compacts at ~75%, Sonnet at ~85%, Haiku at ~90%.
                         Compaction resets the cache \u2014 first prompt after is expensive.

    \u26A0 CACHE MISS     \u2014 Detected when cache efficiency drops sharply.
                        Causes: idle >5min (TTL expired), compaction, or Claude Code bug (fixed v2.1.88).

    \u23F8 IDLE WARNING   \u2014 Appears when >5min between prompts. Cache has a 5-minute TTL.
                        Send prompts regularly to keep cache warm and costs low.

  PER-PROMPT DELTA:

    +2.3% last prompt (avg 0.8%)  \u2014 How much of your 5h budget the last prompt consumed.
                                     If way above average, something may be wrong (cache break).
    \u26A0 ABNORMAL +7.2% (avg 0.8%)  \u2014 Flagged when delta > 5x your rolling average.

  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

  COMMANDS:

    ccvitals status               \u2014 Full drift analysis
    ccvitals status --all-models  \u2014 Compare all models at once
    ccvitals suggest              \u2014 Which model should I switch to?
    ccvitals config list          \u2014 See all current settings
    ccvitals config set <k> <v>   \u2014 Change any setting

  ALL CONFIG TOGGLES:

    ccvitals config set show_remaining true/false  \u2014 "94% left" vs "6% used"
    ccvitals config set show_readings true/false   \u2014 Show/hide readings count
    ccvitals config set show_cost true/false       \u2014 Show/hide session cost
    ccvitals config set show_pattern true/false    \u2014 Show/hide time patterns
    ccvitals config set all_models true/false      \u2014 Show all models in status
    ccvitals config set threshold_pct <number>     \u2014 Drift sensitivity (default: 10)
    ccvitals config set debounce_count <number>    \u2014 Readings before signal change (default: 3)
    ccvitals config set color true/false           \u2014 Enable/disable colors
""")


def show_privacy():
    """Display privacy information."""
    print("""
╔══════════════════════════════════════════════════════╗
║              ccvitals — Privacy Policy               ║
╠══════════════════════════════════════════════════════╣
║                                                      ║
║  Everything stays local. Nothing is sent anywhere.   ║
║  Period.                                             ║
║                                                      ║
║  STORED LOCALLY:                                     ║
║    • Rate limit utilization % (5h and 7d)            ║
║    • Timestamp                                       ║
║    • Provider + model name                           ║
║    • Context window usage                            ║
║    • Session cost                                    ║
║                                                      ║
║  NEVER LEAVES YOUR MACHINE:                          ║
║    • Prompt content                                  ║
║    • API keys or tokens                              ║
║    • User identity or IP                             ║
║    • Conversation content                            ║
║                                                      ║
║  Repo: github.com/jatinmayekar/claude-code-vitals    ║
╚══════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    main()
