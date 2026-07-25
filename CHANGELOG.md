# Changelog

## [Unreleased]
### Fixed (workflow-testing round)
- **Uninstall broke wrapped statuslines.** With a wrapped third-party
  statusline, `uninstall` left settings pointing at the wrapper and then
  deleted it — breaking the user's own status bar and losing their original
  command. Uninstall now recovers the original command from the wrapper and
  restores it before removing the wrapper.
- **Five crash classes on hostile input.** Null JSON objects/leaves, absurd
  epoch `resets_at`, string/negative/out-of-range percentages, and corrupted
  history records (non-string `ts`) each crashed the status bar. Inputs are
  now validated (`_valid_pct`, null-safe access, guarded epoch parse) and
  corrupted history lines are skipped; junk readings are rejected rather than
  logged so they can't poison the baseline.

### Added
- **Sawtooth replay harness** (`tests/test_replay.py`) — end-to-end audit that
  drives the real CLI through 7-day traces: a stable sawtooth week must fire
  zero signals; injected ±18 weekly shifts must fire within the debounce
  window; multi-model weeks must not false-positive. Uses a new test-only
  `CCVITALS_FAKE_NOW` clock hook (inert in production).

### Removed (slim-down before launch)
- **`compare` and `report` commands.** Display surfaces overlapping `status`,
  with the least testing behind them. `baseline` stays (it owns data
  maintenance: `reset` / `freeze` / `window`).
- **Peak-hours feature** (status-bar PEAK indicator, `explain peak`, suggest
  tip). It hardcoded an undocumented "5–11am PT" claim we can't source.
- **Hourly multiplier ("3.2x avg") and per-prompt anomaly flag.** Two extra
  "you're using more" signals overlapping the drift signal; the basic
  per-prompt delta display stays.
- **OAuth enrichment unwired.** `oauth.py` remains in the package but is no
  longer called from the run pipeline until it's verified against the live
  endpoint. The statusline data path is unaffected.

### Fixed
- **`status` box misrendered.** The expanded status box was titled
  `claude_code_vitals` (module name, not the CLI) and its top/body/bottom
  borders used three different widths, producing ragged edges; long status
  text could also overflow the frame. Now titled `ccvitals` with every row
  clamped and padded to one width.
- **Idle-gap tracking during COLLECTING.** `detect_drift`'s early returns
  skipped saving `last_run_ts`, so the first idle check after the collecting
  phase measured from a debounce-inflated history timestamp.
- **README timing claim.** "<50ms" measured ~80ms wall including interpreter
  startup; now says "well under 100ms".

## [0.3.0] — 2026-07-21
### Added
- **Claude Fable 5 awareness.** Fable is now a recognized model family (was
  grouping as "Other" in status/suggest/budget/compare), gets a status-bar switch
  hint as a heavy burner (previously only Opus did), and has its own compaction
  threshold. `explain models` now documents the Fable sub-cap alongside Opus, and
  a README "Where did my usage go?" FAQ explains the six observable causes of
  usage drain. All plan/pricing specifics are intentionally left out — ccvitals
  reports only what it observes and links to Anthropic's pricing for eligibility.

### Fixed
- **`explain` used the wrong command name.** The `ccvitals explain` pages
  printed `claude_code_vitals` (the Python module) instead of `ccvitals` (the
  CLI) in titles and example commands. Corrected across all subtopics.
- **Rate-limit model corrected.** The 5-hour and 7-day windows are SHARED across
  all models (and across Claude Code, chat, and Cowork) — switching models does
  not reset them. Earlier copy claimed each model had its own independent pool
  and that switching gave a "fresh window"; this contradicted official Claude
  Code docs. Corrected README, `explain models`, and CLI help.
- **`suggest` / `budget` / switch-hint reframed around burn rate.** These now
  show the one shared remaining % once and rank models by burn rate (slower =
  extends the shared window longer), instead of treating each model's % as an
  independent balance. The status-bar switch hint (`try Sonnet (slower burn)`)
  now fires only when you're on Opus, since only switching off Opus extends the
  shared window / bypasses the Opus-specific cap.

## [0.2.0] — 2026-04-05
### Renamed + rebranded
- Package: `limitwatch` → `claude-code-vitals`
- CLI: `limitwatch` → `ccvitals`
- Python module: `limitwatch` → `claude_code_vitals`
- Data directory: `~/.limitwatch/` → `~/.claude-code-vitals/`
- Tagline refreshed to neutral, observability-focused framing
- Signal attribution language: `(possible limit change)` → `(baseline shift)`
- All detection algorithms and features unchanged from 0.1.0

## [0.1.0] — 2026-03-30

### Added
- Core engine: passive rate limit monitoring via Claude Code statusLine
- Data logger with JSONL storage and debounced writes
- Drift detector with rolling median baseline and 3-signal output (DOWN/NORMAL/UP)
- Debounce state machine to prevent false positives
- Time-of-day pattern detection for personal heavy-usage windows
- Compact (single-line) and expanded (box) status bar renderers with ANSI colors
- `ccvitals init` — one-command setup with Claude Code auto-configuration
- `ccvitals status` — show current drift analysis from stored history
- `ccvitals report` — generate HTML trend report with Chart.js charts
- `ccvitals privacy` — display privacy policy
- `ccvitals uninstall` — clean removal
- OAuth endpoint integration (supplementary data source)
- Wrapper script support for users with existing statusLine configs
- Custom TOML config parser (zero external dependencies)
- 16-test suite covering all core modules
- One-line installer: `curl -fsSL ... | bash`
- PyPI-ready packaging with entry points

### Technical Details
- Zero external dependencies — pure Python standard library
- Targets <50ms execution per statusline refresh
- Debounced logging: max 1 write per 5 minutes if values unchanged
- Provider inference from model ID (Anthropic, OpenAI, Google, xAI)
