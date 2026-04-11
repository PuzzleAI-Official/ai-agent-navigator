---
name: claude_api_2026_reference
description: Universal Claude API reference (2026). Covers tool use, context management, caching, agent patterns. Read this BEFORE building anything with the Claude API to avoid outdated patterns.
type: reference
---

# Claude API Reference — 2026 State of the Art

> This file prevents future AI sessions from using outdated API patterns.
> Last updated: 2026-04-08. Source: platform.claude.com/docs

---

## MODELS (Current as of 2026-04)

| Model | ID | Context | Max Output | Input $/MTok | Output $/MTok |
|-------|-----|---------|------------|------|-------|
| Opus 4.6 | `claude-opus-4-6` | 1M | 128K | $5 | $25 |
| Sonnet 4.6 | `claude-sonnet-4-6` | 1M | 64K | $3 | $15 |
| Sonnet 4.5 | `claude-sonnet-4-5-20250929` | 200K | 64K | $3 | $15 |
| Haiku 4.5 | `claude-haiku-4-5-20251001` | 200K | 64K | $1 | $5 |
| Mythos Preview | `claude-mythos-preview` | 1M | 128K | $5 | $25 |

**Sonnet 4.6 is the best default** — same price as 4.5, 1M context, better at tool use and web content.

---

## TOOL USE — COMPLETE REFERENCE

### Three Tool Categories

| Category | Execution | Examples |
|----------|-----------|---------|
| User-defined (custom) | Your code | Any tool you define |
| Anthropic-schema (client) | Your code | `bash_20250124`, `text_editor_20250728`, `memory_20250818` |
| Server-executed | Anthropic servers | `web_search`, `web_fetch`, `code_execution` |

### Tool Definition Fields

```python
{
    "name": "my_tool",                    # 1-64 chars, [a-zA-Z0-9_-]
    "description": "3-4 sentences...",    # THE MOST IMPORTANT FIELD
    "input_schema": {...},                # JSON Schema
    "strict": True,                       # Grammar-constrained validation
    "cache_control": {"type": "ephemeral"},  # Cache this tool definition
    "defer_loading": True,                # Load on-demand via tool search
    "input_examples": [...],              # Schema-validated examples
    "eager_input_streaming": True,        # Stream inputs without buffering
    "allowed_callers": ["direct", "code_execution_20260120"],
}
```

### Critical: `is_error` Flag on Tool Results

```python
{"type": "tool_result", "tool_use_id": "toolu_...", "content": "Error: ...", "is_error": True}
```
Setting `is_error: true` tells Claude the result is a failure. Claude will retry 2-3 times with corrections. **Without this, Claude treats errors the same as successes.**

### Message Ordering Rules (violations cause 400 errors)

1. tool_result MUST immediately follow its tool_use (no messages between)
2. In user messages: tool_result blocks BEFORE text blocks
3. ALL parallel tool results in ONE user message (separate messages break parallelism)

### stop_reason Values

| Value | Meaning | Action |
|-------|---------|--------|
| `end_turn` | Done | Process response |
| `tool_use` | Wants client tools | Dispatch, loop back |
| `max_tokens` | Truncated | Increase max_tokens |
| `pause_turn` | Server tool loop timed out | Re-send to continue |
| `compaction` | Context was compacted | Append response, continue |

### Parallel Tool Use

Enable with system prompt instruction:
```
For maximum efficiency, whenever you need to perform multiple independent operations,
invoke all relevant tools simultaneously rather than sequentially.
```

Disable with: `"disable_parallel_tool_use": true` in `tool_choice`.

---

## SERVER TOOLS — VALID VERSIONS

| Tool | Basic Version | Dynamic Filtering Version |
|------|--------------|--------------------------|
| Web Search | `web_search_20250305` | `web_search_20260209` |
| Web Fetch | `web_fetch_20250910` | `web_fetch_20260209`, `web_fetch_20260309` |
| Code Execution | `code_execution_20250825` | `code_execution_20260120` |

**`web_fetch_20250305` DOES NOT EXIST.** Use `web_fetch_20250910`.

**Dynamic filtering (`_20260209`)** auto-injects code_execution. Do NOT also pass an explicit code_execution tool — causes 400 "conflicting tool names."

### Web Fetch Configuration

```python
{"type": "web_fetch_20250910", "name": "web_fetch",
 "max_uses": 5,
 "max_content_tokens": 20000,     # Truncate large pages (optional)
 "allowed_domains": ["docs.example.com"],
 "citations": {"enabled": True},
 "use_cache": True}               # False for fresh content
```
- No per-fetch charge (just token costs)
- Can only fetch URLs that appeared in conversation context
- Typical: ~2,500 tokens/page, ~25K for docs, ~125K for PDFs

### Web Search Configuration

```python
{"type": "web_search_20250305", "name": "web_search",
 "max_uses": 5,
 "allowed_domains": [...],
 "user_location": {"type": "approximate", "country": "US"}}
```
- Cost: $0.01 per search + token costs
- `max_uses` is the most important cost control

---

## PROMPT CACHING

### TTL Options
- 5-minute: `{"type": "ephemeral"}` — 1.25x write, 0.10x read
- 1-hour: `{"type": "ephemeral", "ttl": "1h"}` — 2.0x write, 0.10x read

### Minimum Cacheable Tokens (SILENT FAILURE BELOW)
- Opus 4.6 / Haiku 4.5: 4,096
- Sonnet 4.6: 2,048
- Sonnet 4.5: 1,024

### Cache Hierarchy
`tools` > `system` > `messages`. Changes at one level invalidate that level + downstream.

### Rate Limit Advantage
`cache_read_input_tokens` do NOT count toward ITPM rate limits. With 80% cache hit rate, effective throughput is 5x higher.

### What Invalidates Cache
Tool definitions, web search toggle, citations toggle, speed mode, tool_choice, images added/removed, thinking parameters.

---

## CONTEXT MANAGEMENT (Server-Side, Recommended)

### Tool Result Clearing

```python
# Beta header: context-management-2025-06-27
context_management={
    "edits": [{
        "type": "clear_tool_uses_20250919",
        "trigger": {"type": "input_tokens", "value": 80000},
        "keep": {"type": "tool_uses", "value": 5},
        "exclude_tools": ["web_search"],
        "clear_tool_inputs": False,
    }]
}
```

Clears old tool results server-side. Keeps last N. Replaces manual microcompaction.

### Server Compaction

```python
# Beta header: compact-2026-01-12
context_management={
    "edits": [{
        "type": "compact_20260112",
        "trigger": {"type": "input_tokens", "value": 150000},
        "pause_after_compaction": False,
        "instructions": None,   # Custom prompt replaces default entirely
    }]
}
```

When triggered, Claude generates a summary. API auto-drops old messages on next call.

### Combining Both (Recommended for Agent Loops)

```python
context_management={
    "edits": [
        {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 80000}, "keep": {"type": "tool_uses", "value": 5}},
        {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 150000}},
    ]
}
```

Use `client.beta.messages.create()` with `betas=["context-management-2025-06-27", "compact-2026-01-12"]`.

### Built-in Context Budget Tracking
On Sonnet 4.6/4.5/Haiku 4.5, Claude automatically receives token usage warnings. No need to inject manually.

---

## TOKEN COUNTING (FREE)

```python
count = client.messages.count_tokens(
    model="claude-sonnet-4-6", system="...", messages=[...], tools=[...]
)
print(count.input_tokens)  # exact count, FREE, separate rate limits
```

Use before API calls to prevent prompt-too-long errors.

---

## EXTENDED THINKING

```python
thinking={"type": "adaptive", "effort": "high"}  # low, medium, high
```

- Use `"adaptive"` (not manual budget) on Opus 4.6 / Sonnet 4.6
- Only compatible with `tool_choice: "auto"`
- Must pass thinking blocks back during tool use cycles
- Thinking tokens billed as output tokens

---

## BATCH API — 50% OFF

```python
batch = client.messages.batches.create(requests=[...])
```

50% discount on all models. Max 100K requests. Most complete within 1 hour. Supports everything Messages API does.

---

## STRUCTURED OUTPUTS

```python
# SDK helper (recommended):
response = client.messages.parse(output_format=MyPydanticModel, ...)

# Raw API:
output_config={"format": {"type": "json_schema", "schema": {...}}, "effort": "low"}
```

The `effort` parameter (`low`/`medium`/`high`/`max`) controls compute. Use `"low"` for simple formatting tasks.

---

## MCP (Model Context Protocol)

**For remote third-party services only.** Connects API to HTTPS MCP servers (Stripe, GitHub, etc.).

**NOT useful for:** Local tools (write_file, run_code). These should be user-defined tools dispatched in-process. MCP adds network latency for zero benefit on local tools.

**Requires:** Public HTTPS endpoint, beta header `mcp-client-2025-11-20`.

---

## ANTI-PATTERNS TO AVOID

1. **Using `web_fetch_20250305`** — doesn't exist. Use `web_fetch_20250910`.
2. **Not setting `is_error: true` on tool failures** — Claude won't retry smartly.
3. **Putting text before tool_result in user messages** — causes 400 error.
4. **Sending parallel tool results as separate messages** — trains Claude to avoid parallelism.
5. **Manual context compaction** when server-side is available — less accurate, more code.
6. **Character-based token estimation** — use `client.messages.count_tokens()` (free).
7. **Combining explicit code_execution with `_20260209` server tools** — 400 "conflicting tool names."
8. **Using `tool_choice: "any"` with extended thinking** — incompatible.
9. **Guessing token counts for cache thresholds** — use exact API counts.
10. **Ignoring `pause_turn` stop_reason** — loses server tool output.

---

## AGENT LOOP TEMPLATE (2026 Best Practice)

```python
import anthropic

client = anthropic.Anthropic()

messages = [{"role": "user", "content": "Build X"}]

while True:
    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        betas=["context-management-2025-06-27", "compact-2026-01-12"],
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        tools=TOOLS,  # with strict=True, is_error on failures
        context_management={
            "edits": [
                {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 80000}, "keep": {"type": "tool_uses", "value": 5}},
                {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 150000}},
            ]
        },
    )

    if response.stop_reason == "end_turn":
        break
    elif response.stop_reason == "compaction":
        messages.append({"role": "assistant", "content": response.content})
        continue
    elif response.stop_reason == "pause_turn":
        messages.append({"role": "assistant", "content": response.content})
        continue
    elif response.stop_reason == "tool_use":
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = dispatch_tool(block.name, block.input)
                has_error = "Error" in result
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    **({"is_error": True} if has_error else {}),
                })
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```
