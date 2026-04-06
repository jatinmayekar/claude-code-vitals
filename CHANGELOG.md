# Changelog

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
