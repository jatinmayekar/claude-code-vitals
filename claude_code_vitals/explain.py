"""Explain subtopics for claude_code_vitals.

Each function returns a formatted string explaining a specific concept.
Called via: claude_code_vitals explain <topic>
"""


def explain_cache() -> str:
    return """
\u26A1 claude_code_vitals explain cache \u2014 Prompt Cache

  HOW IT WORKS:

    Claude Code caches the computation from processing your conversation.
    When your next prompt starts with the same prefix (system prompt + prior
    messages), the model skips reprocessing what it already computed.

  PRICING:

    Cache READ  = 0.1x base input price  (90% savings \u2014 the happy path)
    Cache WRITE = 1.25x base input price  (first time processing)
    No cache    = 1.0x base input price   (full reprocessing)

  THE 5-MINUTE TTL:

    The cache expires after 5 minutes of inactivity. Each cache hit resets
    the timer. So an active coding session keeps the cache warm indefinitely.
    Stop typing for 5+ minutes \u2192 cache evaporates \u2192 next prompt pays full price.

    This is why claude_code_vitals shows idle warnings: "Idle 6min \u2014 cache expired."

  WHAT BREAKS THE CACHE:

    1. Idle timeout (>5min between prompts) \u2014 most common, expected
    2. Auto-compaction (context summarized = new text = hash mismatch)
    3. Tool schema mutation (Claude Code bug, fixed in v2.1.88)

  WHAT TO DO:

    \u2022 Keep prompts flowing (<5min gaps) to maintain cache
    \u2022 After a break, expect the first prompt to cost more (cache warming)
    \u2022 Watch the Cache % in the status bar: green (>80%) = healthy
    \u2022 If Cache % stays red without breaks, check your Claude Code version
"""


def explain_compact() -> str:
    return """
\u26A1 claude_code_vitals explain compact \u2014 Auto-Compaction

  WHAT IT IS:

    When your context window fills up, Claude Code automatically summarizes
    the conversation to free space. This is called auto-compaction.

  WHEN IT TRIGGERS (model-specific):

    Opus 4.6    \u2192 compacts at ~75% context used
    Sonnet 4.6  \u2192 compacts at ~85% context used
    Haiku 4.5   \u2192 compacts at ~90% context used

    claude_code_vitals shows: "ctx: 72% \u2014 Opus compacts at ~75%"

  THE COMPACTION-CACHE CHAIN (why it's expensive):

    1. Before compact: Context at 75%. Cache is warm. Prompts are cheap.
    2. Compact triggers: Old conversation replaced with a summary. Context drops to ~12%.
    3. BUT the summary is NEW TEXT. The cache hash doesn't match the old conversation.
    4. Cache is fully invalidated. First post-compact prompt reprocesses everything
       at full price (or 1.25x for cache write).
    5. Second prompt onward: Cache is warm again with the new summary.

    That one expensive prompt is unavoidable. But you can CONTROL the timing.

  WHAT TO DO:

    \u2022 When claude_code_vitals warns "approaching compact", finish your current thought
    \u2022 Use /compact manually to control WHEN the expensive prompt happens
    \u2022 Manual /compact has the same cost \u2014 but you choose the timing
    \u2022 After compact, expect one expensive prompt, then normal pricing resumes
"""


def explain_peak() -> str:
    return """
\u26A1 claude_code_vitals explain peak \u2014 Peak Hours

  TWO DIFFERENT CONCEPTS \u2014 DON'T CONFUSE THEM:

    1. ANTHROPIC PEAK (authoritative, applies to everyone)
       \u2014 Set by Anthropic. Affects your actual rate limits.

    2. YOUR PERSONAL PEAK USAGE (behavioral, opt-in, only about YOU)
       \u2014 Learned from your own history. Just a pattern, not a limit.

  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

  1. ANTHROPIC PEAK (AUTHORITATIVE):

    Window: 5am \u2013 11am PT (12pm \u2013 6pm UTC), weekdays only.
    Confirmed by Anthropic on March 26, 2026. Still active as of April 2026.

    "During weekdays between 5am\u201311am PT, you'll move through your 5-hour
    session limits faster than before."
    \u2014 Anthropic official statement, March 26, 2026

    What changes: your 5-hour session budget depletes faster. Same amount of
    work = higher burn rate. Weekly (7-day) limits are NOT affected \u2014 only
    the 5-hour session window.

    Who's affected: ~7% of users. Impact is strongest on Opus.

  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

  2. YOUR PERSONAL PEAK USAGE (BEHAVIORAL, OPT-IN):

    This is the window when YOU personally tend to use Claude Code heavily.
    ccvitals learns it from your own 7+ day history. It is NOT a rate limit
    and NOT set by Anthropic \u2014 it's just a description of your habits.

    Example: if you code mostly 8PM\u201312AM, your personal pattern is "8PM-12AM".

    This is OFF by default. Enable it with:

        ccvitals config set display.show_personal_pattern true

    When enabled, Row 3 of the status bar shows your learned window.
    It's purely informational \u2014 it does not change any limits.

  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

  WHAT TO DO:

    \u2022 Schedule heavy/agentic work for off-peak hours (outside 5\u201311 AM PT) when possible
    \u2022 During Anthropic peak, prefer Sonnet over Opus for routine tasks
    \u2022 Break large sessions into shorter runs during peak
    \u2022 If your personal window overlaps Anthropic's peak, `ccvitals suggest`
      will show a tip pointing it out
    \u2022 ccvitals shows: "\u26A0 PEAK \u2014 ends 2h 14m" when you're in Anthropic's window
"""


def explain_models() -> str:
    return """
\u26A1 claude_code_vitals explain models \u2014 Model Rate Limits

  SEPARATE RATE LIMIT POOLS:

    Each model has its own independent 5-hour rate limit pool.
    Switching models gives you a fresh budget window.

    Opus 4.6            \u2014 Highest quality, highest burn rate (~12-20%/hr)
    Opus 4.6 (1M ctx)   \u2014 Separate pool from standard Opus
    Sonnet 4.6          \u2014 Good quality, ~3-5%/hr burn rate
    Haiku 4.5           \u2014 Fastest, cheapest, ~1-2%/hr burn rate

  BURN RATE COMPARISON:

    Opus consumes roughly 3-5x more rate limit budget per request than Sonnet.
    For rate-limit-conscious users, using Sonnet for routine tasks and
    reserving Opus for complex work dramatically extends daily capacity.

  THE 7-DAY POOL:

    The weekly (7-day) limit appears to be shared or correlated across
    lighter models. Opus standard is the outlier with much higher 7d usage.

  WHAT TO DO:

    \u2022 Run: claude_code_vitals suggest  to see which model has the most room
    \u2022 When Opus is running low, switch to Sonnet \u2014 most coding tasks work fine
    \u2022 Use Haiku for simple queries, reviews, and explanations
    \u2022 claude_code_vitals shows switch hints: "try Sonnet (96% left)"
    \u2022 Run: claude_code_vitals budget  to see remaining hours per model
"""


TOPICS = {
    "cache": explain_cache,
    "compact": explain_compact,
    "peak": explain_peak,
    "models": explain_models,
}


def get_topic(name: str):
    """Get explain function by topic name. Returns None if not found."""
    return TOPICS.get(name.lower())


def list_topics() -> str:
    """Return list of available explain topics."""
    return """
  Available topics:
    claude_code_vitals explain cache    \u2014 How prompt caching works and why it matters
    claude_code_vitals explain compact  \u2014 Auto-compaction, when it triggers, cache impact
    claude_code_vitals explain peak     \u2014 Anthropic's peak hours (5am-11am PT)
    claude_code_vitals explain models   \u2014 Model differences, burn rates, pool separation

  Or just: claude_code_vitals explain   \u2014 Full status bar guide
"""
