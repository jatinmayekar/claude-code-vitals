# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Yes       |
| < 0.2   | No        |

## Reporting a Vulnerability

Email `jatinmayekar27@gmail.com` with a description, reproduction steps, and impact assessment. You will receive a response within 72 hours.

## Scope

claude-code-vitals is a local-only CLI tool. It reads data that Claude Code provides via stdin. There is no server and no network requests in Phase 1.

The `oauth.py` module handles bearer tokens sourced from `~/.claude/.credentials.json`. These tokens are never transmitted to third parties, never written to logs, and never cached beyond the OAuth response cache at `~/.claude-code-vitals/usage-cache.json`.
