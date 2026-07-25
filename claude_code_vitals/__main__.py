"""CLI entry point for claude_code_vitals. Run 'ccvitals --help' for usage."""

import sys
import json
from typing import Optional

from .config import load_config, Config
from .logger import _parse_iso, parse_statusline_json, extract_snapshot, append_snapshot, should_log
from .detector import detect_drift, Signal
from .renderer import render, render_expanded, C


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
  baseline            Show rolling median baseline (subcommand: window <N>)
  explain <topic>     Guides: cache | compact | models
  config              View/update configuration (set | list)
  privacy             Show what data is stored and where
  uninstall           Remove all ccvitals data and configuration
  help, --help, -h    Show this message
  --version           Show version

Common options:
  --show-readings     (status)           Include recent raw readings
  --all-models        (status)           Include every tracked model
  --show-remaining    (status)           Include time-remaining columns
  --log-only          (run)              Log only, produce no statusline output
  --debug             (run)              Print debug info to stderr

Examples:
  ccvitals init
  ccvitals status
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
# the tool's core principle: never silently canonicalize observable data.

_FAMILY_ORDER = ["Fable", "Opus", "Sonnet", "Haiku", "Other"]

_FAMILY_KEYWORDS = [
    ("Fable",  ("fable",)),
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


def _aggregate_to_selectable(families: dict) -> list[tuple[str, list]]:
    """Pick the most-recent variant per family for actionable commands.

    Does NOT merge readings across variants — old variants may represent
    a different underlying model (Anthropic updates model names over time).
    Uses only the variant with the latest timestamp within each family.
    Returns [(family_name, readings_of_latest_variant), ...] in _FAMILY_ORDER.
    """
    result = []
    for family_name in _FAMILY_ORDER:
        if family_name not in families:
            continue
        best_variant = None
        best_ts = ""
        for label, model_id, readings in families[family_name]:
            if readings and readings[-1].ts > best_ts:
                best_ts = readings[-1].ts
                best_variant = readings
        if best_variant:
            result.append((family_name, best_variant))
    return result


def _merge_family_history(families: dict) -> list[tuple[str, list]]:
    """Merge ALL variants' readings within each family for historical trending.

    Unlike _aggregate_to_selectable, this COMBINES readings across variants
    because historical trend analysis should span model renames. If Anthropic
    renamed 'opus' -> 'claude-opus-4-6[1m]', the user was still 'using Opus'.
    Returns [(family_name, sorted_merged_readings), ...] in _FAMILY_ORDER.
    """
    result = []
    for family_name in _FAMILY_ORDER:
        if family_name not in families:
            continue
        all_readings = []
        for label, model_id, readings in families[family_name]:
            all_readings.extend(readings)
        all_readings.sort(key=lambda r: r.ts)
        result.append((family_name, all_readings))
    return result


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
        show_readings = "--show-readings" in args
        all_models = "--all-models" in args
        show_remaining = "--show-remaining" in args
        show_status(config, show_readings=show_readings,
                    all_models=all_models, show_remaining=show_remaining)

    elif command == "suggest":
        show_suggest(config)

    elif command == "budget":
        show_budget(config)

    elif command == "baseline":
        baseline_command(config, args[1:])

    elif command == "config":
        config_command(config, args[1:])

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

    # Persist current session_id for CLI commands (! ccvitals compare --session)
    if data and data.get("session_id"):
        try:
            config.ensure_data_dir()
            (config.data_dir / "current-session-id").write_text(data["session_id"])
        except Exception:
            pass

    # If snapshot is None, show waiting message
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

    # Switch hint — when the shared 5h window is >70% used and you're on a
    # fast-burning model (Opus), suggest a lighter model to extend the window.
    if snapshot is not None and snapshot.session_5h_pct is not None and snapshot.session_5h_pct > 70:
        result.switch_hint = _compute_switch_hint(snapshot)

    # Render and output
    output = render(result, config)
    print(output)


def _compute_switch_hint(current_snapshot) -> Optional[str]:
    """Suggest switching off a heavy model when the shared 5h window is low.

    The 5h/7d windows are shared across all models, so switching does not reset
    them and there is no separate per-model balance to compare. But the heavy
    models (Opus, Fable) burn the shared window fastest and each carry their own
    tighter cap, so a switch is only actionable when the current model is one of
    them — on Sonnet/Haiku a switch wouldn't extend the window. Needs no
    cross-model history: the "slower burn" claim is model-intrinsic.
    """
    family = _detect_family(current_snapshot.model_id, current_snapshot.model_name or "")
    if family not in ("Opus", "Fable"):
        return None
    return "try Sonnet (slower burn)"


def _burn_rate_value(readings: list) -> Optional[float]:
    """Per-model burn rate (%/hr of the shared 5h window) from the last 2 readings.

    The 5h window is shared across models; this measures how fast this model
    drains it. Only positive consumption counts — a drop means the window reset,
    not burn.

    Args:
        readings: RateLimitSnapshot list for a single model, sorted by time.

    Returns:
        Burn rate in percent-per-hour, or None if there aren't two readings
        with 5h data 15min-2hr apart consuming the window.
    """
    valid = [r for r in readings if r.session_5h_pct is not None]
    if len(valid) < 2:
        return None

    r1, r2 = valid[-2], valid[-1]
    try:
        t1 = _parse_iso(r1.ts)
        t2 = _parse_iso(r2.ts)
    except (ValueError, TypeError):
        return None

    hours_elapsed = (t2 - t1).total_seconds() / 3600.0
    if hours_elapsed < 0.25 or hours_elapsed > 2:
        return None

    delta = r2.session_5h_pct - r1.session_5h_pct
    if delta <= 0:
        return None
    return delta / hours_elapsed


def _compute_burn_rate(readings: list) -> Optional[str]:
    """Format the per-model burn rate as a string like '3%/hr', or None."""
    v = _burn_rate_value(readings)
    return f"{round(v)}%/hr" if v is not None else None


def _shared_remaining_5h(history: list) -> Optional[int]:
    """Remaining % of the shared 5h window from the latest reading of any model.

    The 5h window is one shared pool, so the most recent reading across all
    models is the current remaining headroom for every model.
    """
    for s in reversed(history):
        if s.session_5h_pct is not None:
            return round(100 - s.session_5h_pct)
    return None


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
    from .logger import _parse_iso, load_history
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
    """Show how long the shared 5h window lasts at each model's burn rate.

    The 5h window is a single pool shared across all models, so there is one
    remaining %. Models differ only in how fast they drain it, so the per-model
    figure is time-to-depletion at that burn rate, not an independent balance.
    """
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
    selectable = _aggregate_to_selectable(families)

    print("\n  \u26A1 ccvitals budget \u2014 Shared session capacity\n")
    shared_remaining = _shared_remaining_5h(history)
    if shared_remaining is not None:
        print(f"  Shared 5h window: {shared_remaining}% left  (all models draw from this one pool)\n")
    print(f"  {'Model':<15} {'Burn rate':>10} {'Window lasts':>14}")
    print(f"  {'─'*15} {'─'*10} {'─'*14}")

    for family_name, readings in selectable:
        burn = _burn_rate_value(readings)
        if burn and shared_remaining is not None:
            hours_left = shared_remaining / burn
            time_str = f"~{int(hours_left * 60)}min" if hours_left < 1 else f"~{hours_left:.1f}hrs"
            rate_str = f"{burn:.0f}%/hr"
        else:
            time_str = "—"
            rate_str = "—"
        print(f"  {family_name:<15} {rate_str:>10} {time_str:>14}")

    print()
    print("  Opus also has its own tighter cap; switching off Opus lets you work past it.")
    print("  Switch models with /model in Claude Code.")


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


def show_suggest(config: Config):
    """Rank selectable models by burn rate against the shared 5h window.

    The 5h/7d windows are shared across all models, so there is one remaining %
    for everyone. Models differ by burn rate — a slower-burning model makes the
    shared window last longer. Ranked slowest-burn first; the shared window is
    shown once, not as a per-model balance.
    """
    from .logger import load_history
    from collections import defaultdict

    history = load_history(config, max_age_days=1)
    if not history:
        print("  No data yet. Use Claude Code to collect readings.")
        return

    shared_remaining = _shared_remaining_5h(history)

    by_model = defaultdict(list)
    for s in history:
        by_model[s.model_id].append(s)
    families = _group_by_family(by_model)
    selectable = _aggregate_to_selectable(families)

    # Rank by burn rate ascending — a slower-burning model extends the shared
    # window longest. Models with no recent burn data sort last.
    rows = []  # (family_name, burn_value_or_None)
    for family_name, readings in selectable:
        rows.append((family_name, _burn_rate_value(readings)))
    rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0.0))
    ranked_burns = [b for _, b in rows if b is not None]
    max_burn = max(ranked_burns) if ranked_burns else None

    print("\n  \u26A1 ccvitals suggest \u2014 Model availability\n")
    if shared_remaining is not None:
        print(f"  Shared 5h window: {shared_remaining}% left  (one pool \u2014 every model draws from it)")
    print("  Ranked by burn rate \u2014 slower models make this window last longer.\n")
    print(f"  {'Model':<15} {'Burn':>9} {'Window lasts':>14}   {'Status'}")
    print(f"  {'─'*15} {'─'*9} {'─'*14}   {'─'*20}")

    for i, (family, burn) in enumerate(rows):
        burn_str = f"{round(burn)}%/hr" if burn is not None else "—"
        if burn is not None and shared_remaining is not None:
            hours_left = shared_remaining / burn
            lasts = f"~{int(hours_left * 60)}min" if hours_left < 1 else f"~{hours_left:.1f}hrs"
        else:
            lasts = "—"
        if burn is None:
            status = "no burn data yet"
        elif i == 0:
            status = "\u2713 Extends longest"
        elif burn == max_burn:
            status = "\u26A0 Fast burn"
        else:
            status = "  Available"
        print(f"  {family:<15} {burn_str:>9} {lasts:>14}   {status}")

    print()

    print("  Opus also has its own tighter cap; switching off Opus keeps you working past it.")
    print("  Switch models with /model in Claude Code.")


def show_status(config: Config, show_readings: bool = False,
                all_models: bool = False, show_remaining: bool = False):
    """Show current drift status from stored history (no stdin needed).

    Flags:
        --all-models      Show status for every model on its own line
        --show-readings   Append readings count
        --show-remaining  Show remaining % instead of used %
    """
    from .logger import _parse_iso, load_history
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

            # Determine the most-recent variant in this family for the active badge
            members = families[family_name]
            best_ts = ""
            best_model_id = None
            for _label, _mid, _readings in members:
                if _readings and _readings[-1].ts > best_ts:
                    best_ts = _readings[-1].ts
                    best_model_id = _mid

            for label, model_id, model_history in members:
                latest = model_history[-1]
                result = detect_drift(latest, config)
                # Override model_name to the stripped member label so the row
                # renders as "4.6 (1M context)" instead of "Opus 4.6 (1M context)"
                result.model_name = label
                config.display.compact = True
                line = render_compact(result, config)
                if model_id == best_model_id:
                    badge = f"{C.GREEN}\u25cf{C.RESET} " if use_color else "\u25cf "
                else:
                    badge = "  "
                print(f"  {badge}{line}")
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
                ENUM_KEYS = {"show_context": {"auto", "always", "never"}}
                if key in ENUM_KEYS and value.strip('"').lower() not in ENUM_KEYS[key]:
                    print(f"  Error: {key} must be one of {sorted(ENUM_KEYS[key])}, got '{value}'")
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
                "show_context": "display",
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
                ENUM_KEYS = {"show_context": {"auto", "always", "never"}}
                if key in ENUM_KEYS and value.strip('"').lower() not in ENUM_KEYS[key]:
                    print(f"  Error: {key} must be one of {sorted(ENUM_KEYS[key])}, got '{value}'")
                    return
                if value.lower() in ("true", "false"):
                    formatted = value.lower()
                elif key in ENUM_KEYS:
                    formatted = f'"{value.strip(chr(34)).lower()}"'
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


def show_explain():
    """Explain what every part of the status line means."""
    print("""
\u26A1 ccvitals \u2014 Status Line Guide

  EXAMPLE (normal usage \u2014 everything is fine):

    Opus 4.6 (1M context)  |  5h: 100% left  |  7d: 91% left  |  $40.17  |  resets 4h 40m  |  92 readings  |  \u2191 8PM-12AM
    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500    \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    Model                    5h window        7d window        Cost     Reset countdown    Baseline       Pattern

  EXAMPLE (running low \u2014 switch hint appears):

    Opus 4.6  |  5h: 25% left  |  7d: 12% left  |  $5.00  |  resets 1h 20m  |  try Sonnet (slower burn)

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

    try Sonnet (slower burn) \u2014 Appears when you're on Opus and the shared 5h
                            window is >70% used. The 5h/7d windows are SHARED
                            across all models, so switching doesn't reset them \u2014
                            but Opus burns the shared window ~3-5x faster and has
                            its own tighter cap, so a lighter model extends it.

    For a full comparison: ! ccvitals suggest

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
    ccvitals suggest              \u2014 Which model burns slowest (extends the window)?
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
