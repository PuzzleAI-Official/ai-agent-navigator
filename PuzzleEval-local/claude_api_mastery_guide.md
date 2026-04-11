---
name: claude_api_mastery_guide
description: Comprehensive Claude API reference — tool use, caching, optimization, agent patterns. For building high-performance autonomous coding agents.
type: reference
---

# Claude API Mastery Guide — Agent Builder Reference

> Written from exhaustive study of Anthropic's 2026 API documentation.
> For AI assistants building autonomous tool-use agents (like PuzzleEval Agent 5).

---

## 1. CRITICAL THINGS PUZZLEEVAL IS DOING WRONG

### 1A. `is_error: true` NOT SET on tool failure results

When a tool fails, the `tool_result` should include `"is_error": true`. This tells Claude the result is an error and triggers 2-3 automatic retry/correction attempts. **PuzzleEval Agent 5 never sets this flag** — all tool results look like successes to Claude, even when they contain error text.

**Impact:** Claude doesn't know to retry or adjust its approach after a tool error. It treats "Error: file not found" the same as "Written 500 chars to harness.py".

**Fix:**
```python
tool_results.append({
    "type": "tool_result",
    "tool_use_id": block.id,
    "content": result_text,
    "is_error": "Error" in result_text or "Traceback" in result_text,  # ADD THIS
})
```

### 1B. Tool result ordering violation

The API requires tool_result blocks to come BEFORE any text in a user message. PuzzleEval sometimes appends text messages (nudges, reassessments) as separate user messages after tool results. While this works as separate messages, combining them into one message with text after tool_results is the documented best practice.

### 1C. `max_content_tokens` on web_fetch — DECIDED AGAINST

Web fetches can return 25K-125K tokens per page. `max_content_tokens` caps this. However, for a coding agent that needs COMPLETE API docs, truncation risks missing critical endpoint/auth details at the bottom of pages. **Performance > cost.** Context compression handles token bloat after content is consumed. Only use `max_content_tokens` if you're hitting hard context limits, not as a default.

### 1D. Not using token counting pre-check

The `client.messages.count_tokens()` endpoint is FREE and gives exact token counts. We estimate from characters (~4 chars/token). Using the real API would prevent PTL errors entirely — we'd know exactly when to compact.

### 1E. Not using `strict: true` on custom tools

The `strict: true` flag guarantees Claude's tool inputs match our schema via grammar-constrained sampling. Without it, Claude might send malformed inputs (wrong types, missing fields) which we'd have to handle. With it, inputs are guaranteed valid.

### 1F. Not using parallel tool calls in the system prompt

The docs explicitly recommend adding this to system prompts for agent loops:
```
For maximum efficiency, whenever you need to perform multiple independent operations, invoke all relevant tools simultaneously rather than sequentially.
```
Agent 5 could benefit: e.g., writing requirements.txt AND harness.py in one turn.

---

## 2. TOOL USE — COMPLETE REFERENCE

### Tool Categories

| Category | Where it runs | Examples |
|----------|--------------|---------|
| User-defined (custom) | Your code | write_file, run_code, read_file, patch_file, ask_research |
| Anthropic-schema (client) | Your code | bash_20250124, text_editor_20250728 |
| Server-executed | Anthropic servers | web_search, web_fetch, code_execution |

### Tool Definition Fields

| Field | Type | Purpose |
|-------|------|---------|
| `name` | string | 1-64 chars, `[a-zA-Z0-9_-]` |
| `description` | string | 3-4+ sentences. **THE most important factor for tool selection.** |
| `input_schema` | object | JSON Schema |
| `cache_control` | object | Set on LAST tool to cache all tool definitions |
| `strict` | boolean | Grammar-constrained input validation |
| `defer_loading` | boolean | Exclude from system prompt; load via tool search |
| `input_examples` | object[] | Example inputs (must validate against schema) |
| `eager_input_streaming` | boolean | Stream inputs without JSON validation |
| `allowed_callers` | string[] | Restrict to `["direct"]`, `["code_execution_20260120"]`, or both |

### Tool Result Format

```json
{
    "type": "tool_result",
    "tool_use_id": "toolu_...",
    "content": "result text or content blocks",
    "is_error": true  // SET THIS WHEN TOOL FAILS — triggers Claude retry behavior
}
```

Content can be: string, content block array (text, image, document), or omitted (side-effect-only).

### Message Ordering Rules (CRITICAL)

1. Tool results MUST immediately follow assistant's tool_use (no messages between)
2. In user messages, tool_result blocks MUST come BEFORE text blocks
3. ALL parallel tool results in ONE user message (separate messages break parallelism)

### stop_reason Values

| Value | Meaning | Action |
|-------|---------|--------|
| `end_turn` | Claude done | Check for completion signals |
| `tool_use` | Claude wants client tools | Dispatch tools, loop back |
| `max_tokens` | Output truncated | Increase max_tokens or compact |
| `pause_turn` | Server tool loop timed out | Re-send to continue |
| `refusal` | Claude declined | Handle gracefully |

### tool_choice Options

| Value | Behavior |
|-------|----------|
| `{"type": "auto"}` | Claude decides (default, works with thinking) |
| `{"type": "any"}` | Must use a tool (incompatible with thinking) |
| `{"type": "tool", "name": "X"}` | Must use specific tool |
| `{"type": "none"}` | No tools allowed |

---

## 3. SERVER TOOLS — CONFIGURATION

### Web Search
```python
{"type": "web_search_20250305", "name": "web_search",
 "max_uses": 5, "allowed_domains": [...], "blocked_domains": [...],
 "user_location": {"type": "approximate", "country": "US"}}
```
- Cost: $0.01 per search + token costs
- Results include `encrypted_content` (pass back in multi-turn)

### Web Fetch
```python
{"type": "web_fetch_20250910", "name": "web_fetch",
 "max_uses": 5, "max_content_tokens": 20000,
 "allowed_domains": [...], "citations": {"enabled": true},
 "use_cache": true}
```
- Cost: Token costs only (no per-fetch charge)
- `max_content_tokens`: truncates page content (CRITICAL for cost control)
- `use_cache: false`: get fresh content (skip CDN/server cache)
- Can only fetch URLs that appeared in conversation context
- Average page: ~2,500 tokens. Large docs: ~25,000. PDFs: ~125,000.

### Valid Tool Versions (Complete)
| Tool | Basic | Dynamic Filtering |
|------|-------|-------------------|
| web_search | `web_search_20250305` | `web_search_20260209` |
| web_fetch | `web_fetch_20250910` | `web_fetch_20260209`, `web_fetch_20260309` |
| code_execution | `code_execution_20250522`, `code_execution_20250825` | `code_execution_20260120` |

**`web_fetch_20250305` DOES NOT EXIST.** Use `web_fetch_20250910`.

---

## 4. PROMPT CACHING — STRATEGY

### Two Modes
1. **Automatic**: Top-level `cache_control` on request body. System places breakpoint automatically.
2. **Explicit**: `cache_control` on individual content blocks. Max 4 breakpoints.

### TTL Options
- 5-minute (default): `{"type": "ephemeral"}` or `{"type": "ephemeral", "ttl": "5m"}`
- 1-hour: `{"type": "ephemeral", "ttl": "1h"}` — 2x write cost, better for slow loops

### Pricing
- Cache write (5m): 1.25x base input
- Cache write (1h): 2.0x base input
- Cache read: 0.10x base input (**10x cheaper**)

### Minimum Cacheable Tokens (SILENT FAILURE BELOW)
- Opus 4.6: 4,096
- Sonnet 4.6: **2,048** (different from Sonnet 4.5!)
- Sonnet 4.5: 1,024
- Haiku 4.5: 4,096

### Invalidation Hierarchy
`tools` > `system` > `messages`. Changes at one level invalidate that level + all downstream.

### Rate Limit Advantage
Cached tokens (`cache_read_input_tokens`) do NOT count toward ITPM limits. With 80% cache hit rate and 2M ITPM limit, effective throughput = 10M tokens/minute.

---

## 5. TOKEN COUNTING — FREE PRE-CHECK

```python
count = client.messages.count_tokens(
    model="claude-sonnet-4-6",
    system="...",
    messages=[...],
    tools=[...],
)
print(count.input_tokens)  # exact token count, FREE
```

Rate limits: 100-8,000 RPM depending on tier. Separate from Messages API.

**Use case for PuzzleEval:** Replace character-based estimation in Agent 5's context management with exact token counts. Prevents PTL errors entirely.

---

## 6. EXTENDED THINKING — FOR AGENT 7

```python
response = client.messages.create(
    model="claude-opus-4-6",
    thinking={"type": "adaptive", "effort": "high"},
    ...
)
```

- Opus 4.6 / Sonnet 4.6: use `"adaptive"` with `"effort"` level
- Only compatible with `tool_choice: "auto"` (default)
- Must pass thinking blocks back in subsequent tool-result messages
- Thinking tokens billed as OUTPUT tokens (expensive)

**PuzzleEval use:** Agent 7 (Analyze) — AI-as-judge needs deep reasoning. Extended thinking with `effort: "high"` could improve scoring quality.

---

## 7. BATCH API — 50% COST SAVINGS

```python
batch = client.messages.batches.create(requests=[
    Request(custom_id="cand-1", params=MessageCreateParamsNonStreaming(...)),
    Request(custom_id="cand-2", params=MessageCreateParamsNonStreaming(...)),
])
# Poll for completion, then fetch results
```

- 50% discount on ALL models
- Max 100K requests per batch
- Most complete within 1 hour
- Supports everything Messages API does

**PuzzleEval candidates:** Agent 4 screening (7 candidates), Agent 7 analysis (N candidates), Agent 2 structuring step.

---

## 8. SERVER-SIDE CONTEXT MANAGEMENT (IMPLEMENTED in Agent 5)

Agent 5 uses Anthropic's native context management instead of manual compaction.
This is a pure performance upgrade — Claude summarizes intelligently instead of
our code blindly dropping messages.

### Two-Stage Strategy

```python
response = client.beta.messages.create(
    betas=["context-management-2025-06-27", "compact-2026-01-12"],
    context_management={
        "edits": [
            # Stage 1: Clear old tool results at 80K tokens (keeps last 5)
            {"type": "clear_tool_uses_20250919",
             "trigger": {"type": "input_tokens", "value": 80000},
             "keep": {"type": "tool_uses", "value": 5}},
            # Stage 2: Claude-powered summary at 150K tokens
            {"type": "compact_20260112",
             "trigger": {"type": "input_tokens", "value": 150000}},
        ],
    },
)
```

### Tool Result Clearing (`clear_tool_uses_20250919`)

| Option | Default | Description |
|--------|---------|-------------|
| `trigger` | 100K tokens | When to activate (`input_tokens` or `tool_uses` type) |
| `keep` | 3 tool uses | How many recent tool use/result pairs to preserve |
| `clear_at_least` | None | Minimum tokens to clear (skip if not worth cache invalidation) |
| `exclude_tools` | None | Tool names whose results should never be cleared |
| `clear_tool_inputs` | false | Whether to also clear tool call parameters |

### Server Compaction (`compact_20260112`)

| Option | Default | Description |
|--------|---------|-------------|
| `trigger.value` | 150K tokens | Minimum 50K |
| `pause_after_compaction` | false | Pause to let you preserve specific messages |
| `instructions` | null | Custom summarization prompt (replaces default entirely) |

**stop_reason values:** When compaction occurs, response may have `stop_reason: "compaction"`.
Handle like pause_turn — append response content and continue.

**Cost:** Compaction summary is billed as output tokens (~3.5K tokens ≈ $0.05).
Subsequent turns read the compacted summary from cache at 0.10x cost.

### Why Server-Side > Manual

| Aspect | Manual (old) | Server-Side (current) |
|--------|---|---|
| Token counting | ~4 chars/token guess | Exact API count |
| Clearing quality | Blind placeholder replacement | API preserves cache, smart selection |
| Summary quality | None (just drop messages) | Claude reads and summarizes |
| Risk of losing info | High | Low — Claude decides what matters |
| Maintenance | Our code | API manages automatically |

---

## 9. MCP (Model Context Protocol) — NOT USEFUL FOR AGENT 5

MCP connector lets you point the API at a remote HTTPS server that exposes tools via MCP protocol.

**Why NOT useful for PuzzleEval:**
- Our tools (write_file, run_code, etc.) are LOCAL operations
- MCP adds a network hop (API → your server → filesystem) for zero benefit
- Requires hosting a public HTTPS MCP server
- Beta only, not on Bedrock/Vertex, not ZDR-eligible

**When MCP IS useful:** Connecting to third-party services that already run MCP servers
(Stripe, GitHub, Google Calendar). Not for local tools you dispatch in-process.

---

## 10. CONTEXT ENGINEERING — KEY CONCEPTS

### Context Window Sizes (2026)
- Claude Opus 4.6 / Sonnet 4.6 / Mythos: **1M tokens**
- Claude Sonnet 4.5 / Sonnet 4 / all others: **200K tokens**

### Built-in Context Budget Tracking (FREE, automatic)
On Sonnet 4.6 / 4.5 / Haiku 4.5, Claude automatically receives:
```xml
<system_warning>Token usage: 35000/1000000; 965000 remaining</system_warning>
```
No need to inject this yourself — the API does it.

### Extended Thinking and Context
- Thinking blocks count during current turn, automatically stripped from previous turns
- EXCEPTION: When posting tool results, the FULL thinking block (with signature) that
  accompanied that tool request MUST be included. Strip only AFTER tool cycle completes.

### Context Editing (`clear_tool_uses_20250919`)
- Server-side clearing of old tool results
- Replaces manual microcompact
- Beta header: `context-management-2025-06-27`
- Can combine with compaction (clearing runs FIRST)

### What Overflows Look Like
Newer models return a **validation error** on overflow — they do NOT silently truncate.
Handle by reducing max_tokens or triggering compaction.

---

## 11. COST OPTIMIZATION CHECKLIST

| Technique | Savings | Where | Implemented? |
|-----------|---------|-------|---|
| `is_error: true` on failed tool results | Fewer wasted turns | Agent 5 builder | ✅ Yes |
| Prompt caching (ephemeral TTL) | 90% on system prompt reads | Agent 5 builder | ✅ Yes |
| `strict: true` on custom tools | Avoids malformed inputs | All custom tools | ✅ Yes |
| Parallel tool calls in prompt | Fewer turns | Agent 5 builder | ✅ Yes |
| Server-side context management | Better compaction quality | Agent 5 builder | ✅ Yes |
| Batch API for non-realtime work | 50% across the board | Agent 4, 7 | Not yet |
| Token counting pre-check | Avoids wasted PTL calls | Agent 5 | Not yet |
| `output_config.effort: "low"` | Less compute on simple tasks | Agent 2/4 structuring | Not yet |

---

## 12. AGENT LOOP BEST PRACTICES (from docs)

1. **Descriptions are the #1 factor** for tool selection. 3-4+ sentences, explain what/when/how.
2. **Return only high-signal data** from tools. Semantic identifiers, only fields needed for next step.
3. **Write instructive error messages.** Include what went wrong AND what to try next.
4. **Feedback loops greatly improve quality.** Validate → fix → repeat.
5. **Progressive disclosure.** Don't load everything upfront. Load on demand.
6. **If you're writing regex to parse model output, that should have been a tool call.**
7. **Checklist pattern** for complex tasks — model copies checklist and checks off items.
8. **Solve, don't punt** — handle errors explicitly, don't let them bubble up.
9. **Use `is_error: true`** on tool_result blocks when tools fail — triggers Claude's retry behavior.
10. **Server-side context management** over manual compaction — Claude summarizes better than code drops.
