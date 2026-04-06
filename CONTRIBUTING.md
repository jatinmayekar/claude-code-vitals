# Contributing to claude-code-vitals

Thanks for your interest in contributing.

## Setup

```bash
git clone https://github.com/jatinmayekar/claude-code-vitals && cd claude-code-vitals
pip install -e .
python3 tests/test_core.py
```

## Before Opening a PR

- Add a test for every change (bug fix or feature).
- Run the full test suite: `python3 tests/test_core.py` -- 30+ tests must pass.
- Keep zero external dependencies. Pure stdlib only.
- Use `tempfile.TemporaryDirectory()` for test isolation. Never touch the real `~/.claude-code-vitals/` directory.
- Keep functions small. Type hints on all signatures. Google-style docstrings on public functions.

## Commit Style

Use conventional prefixes:

- `feat:` -- new feature
- `fix:` -- bug fix
- `docs:` -- documentation only
- `test:` -- adding or updating tests
- `refactor:` -- code change that neither fixes a bug nor adds a feature
- `chore:` -- maintenance (CI, packaging, etc.)

One logical change per commit.

## Questions?

Open an issue on GitHub. We will get back to you.
