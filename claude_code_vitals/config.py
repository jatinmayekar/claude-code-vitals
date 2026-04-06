"""Configuration management for claude_code_vitals.

Reads from ~/.claude-code-vitals/config.toml (simple key=value parser, no toml dep needed).
Falls back to sensible defaults for everything.
"""

import os
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict


DEFAULT_DATA_DIR = Path.home() / ".claude-code-vitals"
HISTORY_FILE = "history.jsonl"
CONFIG_FILE = "config.toml"
CACHE_FILE = "usage-cache.json"
BASELINE_FILE = "global-baseline.json"

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


@dataclass
class TrackingConfig:
    baseline_window_days: int = 7
    threshold_pct: float = 10.0
    debounce_count: int = 3


@dataclass
class DisplayConfig:
    compact: bool = True
    show_pattern: bool = True
    show_source: bool = False
    show_remaining: bool = False  # True = show remaining %, False = show used %
    show_readings: bool = False   # Show readings count in status
    all_models: bool = False      # Show all models in status command
    show_cost: bool = False       # Show session cost in status bar
    show_personal_pattern: bool = False  # Opt-in: show learned heavy-usage window on Row 3
    color: bool = True


@dataclass
class Config:
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)

    @property
    def history_path(self) -> Path:
        return self.data_dir / HISTORY_FILE

    @property
    def cache_path(self) -> Path:
        return self.data_dir / CACHE_FILE

    @property
    def baseline_path(self) -> Path:
        return self.data_dir / BASELINE_FILE

    @property
    def config_path(self) -> Path:
        return self.data_dir / CONFIG_FILE

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def _migrate_legacy_data_dir() -> None:
    """One-time silent copy from ~/.limitwatch to ~/.claude-code-vitals on first run.

    Safe to call repeatedly — no-op if new dir exists or old dir absent.
    Handles the creator's own dogfooding data during the rename. Not a
    public-facing migration shim (there are no public users yet).
    """
    new_dir = Path.home() / ".claude-code-vitals"
    old_dir = Path.home() / ".limitwatch"
    if new_dir.exists() or not old_dir.exists():
        return
    import shutil
    shutil.copytree(old_dir, new_dir)
    (new_dir / ".migrated-from-limitwatch").write_text("one-time copy from ~/.limitwatch\n")


def _parse_simple_toml(text: str) -> dict:
    """Minimal TOML parser — handles [sections] and key = value.
    
    Supports: strings (quoted/unquoted), ints, floats, bools.
    Does NOT support: arrays, inline tables, multiline strings.
    Good enough for our config file without adding a toml dependency.
    """
    result = {}
    current_section = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Section header
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1].strip()
            if current_section not in result:
                result[current_section] = {}
            continue

        # Key = value
        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()

            # Strip inline comments
            if " #" in val:
                val = val[:val.index(" #")].strip()

            # Parse value type
            parsed = _parse_value(val)

            if current_section:
                result[current_section][key] = parsed
            else:
                result[key] = parsed

    return result


def _parse_value(val: str):
    """Parse a TOML value string into a Python type."""
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False

    # Quoted string
    if (val.startswith('"') and val.endswith('"')) or \
       (val.startswith("'") and val.endswith("'")):
        return val[1:-1]

    # Number
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        pass

    # Unquoted string
    return val


def load_config(data_dir: Path | None = None) -> Config:
    """Load config from disk, falling back to defaults."""
    _migrate_legacy_data_dir()
    config = Config()
    if data_dir:
        config.data_dir = data_dir

    config_path = config.config_path
    if not config_path.exists():
        return config

    try:
        raw = _parse_simple_toml(config_path.read_text())
    except Exception:
        return config

    # Apply tracking settings
    if "tracking" in raw:
        t = raw["tracking"]
        if "baseline_window" in t:
            # Parse "7d", "4w", etc.
            config.tracking.baseline_window_days = _parse_duration(t["baseline_window"])
        if "baseline_window_days" in t:
            config.tracking.baseline_window_days = int(t["baseline_window_days"])
        if "threshold_pct" in t:
            config.tracking.threshold_pct = float(t["threshold_pct"])
        if "debounce_count" in t:
            config.tracking.debounce_count = int(t["debounce_count"])

    # Apply display settings
    if "display" in raw:
        d = raw["display"]
        if "compact" in d:
            config.display.compact = bool(d["compact"])
        if "show_pattern" in d:
            config.display.show_pattern = bool(d["show_pattern"])
        if "show_source" in d:
            config.display.show_source = bool(d["show_source"])
        if "show_remaining" in d:
            config.display.show_remaining = bool(d["show_remaining"])
        if "show_readings" in d:
            config.display.show_readings = bool(d["show_readings"])
        if "all_models" in d:
            config.display.all_models = bool(d["all_models"])
        if "show_cost" in d:
            config.display.show_cost = bool(d["show_cost"])
        if "show_personal_pattern" in d:
            config.display.show_personal_pattern = bool(d["show_personal_pattern"])
        if "color" in d:
            config.display.color = bool(d["color"])

    return config


def _parse_duration(val: str) -> int:
    """Parse duration string like '7d', '4w', '12w' into days."""
    val = val.strip().lower()
    if val.endswith("d"):
        return int(val[:-1])
    if val.endswith("w"):
        return int(val[:-1]) * 7
    # Fallback: assume days
    try:
        return int(val)
    except ValueError:
        return 7


def write_default_config(config: Config) -> None:
    """Write a default config file with comments."""
    config.ensure_data_dir()
    content = """# claude-code-vitals configuration
# See: https://github.com/jatinmayekar/claude-code-vitals

[tracking]
baseline_window_days = 7       # Rolling window in days (1-84)
threshold_pct = 10             # % deviation to trigger signal change
debounce_count = 3             # Consecutive readings to confirm change

[display]
compact = true                 # Single-line status bar
show_pattern = true            # Show time-of-day patterns
show_source = false            # Show "local" vs "global" tag
show_remaining = false         # true = show 88% left, false = show 12% used
show_readings = false          # Show readings count in status
all_models = false             # Show all models in status command
show_cost = false              # Show session cost ($) in status bar
show_personal_pattern = false  # Show your personal heavy-usage window on Row 3 (learned from history)
color = true                   # ANSI color output
"""
    config.config_path.write_text(content)
