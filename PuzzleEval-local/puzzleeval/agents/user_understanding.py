# ============================================================================
# Agent 1: User Understanding Agent
# ============================================================================
# PURPOSE:
#   Parse the user's natural language request into structured data that
#   ALL downstream agents can consume.
#
# DESIGN: Pure function — input data in, structured result out.
#
# ┌─────────────────────────────────────────────────────────────────┐
# │  CORE LINES GUIDE                                               │
# │                                                                 │
# │  If you want to understand ONLY the main logic (skip logging,   │
# │  error handling, caching), read these lines:                    │
# │                                                                 │
# │  1. SYSTEM_PROMPT (line ~30)     — instructions to Claude       │
# │  2. _build_system_blocks()       — assembles system message     │
# │  3. _build_messages()            — assembles conversation       │
# │  4. run_user_understanding_agent — THE MAIN FUNCTION            │
# │     Inside it, the core is just 5 lines:                        │
# │       a. client = anthropic.Anthropic(...)                      │
# │       b. system_blocks = _build_system_blocks(file_content)     │
# │       c. messages = _build_messages(user_text, history)         │
# │       d. response = client.messages.parse(...)                  │
# │       e. return response.parsed_output                          │
# │                                                                 │
# │  Everything else is logging, error handling, or file parsing    │
# │  — necessary for production but not for understanding the flow. │
# └─────────────────────────────────────────────────────────────────┘
# ============================================================================

import time
from typing import Any

import anthropic

from puzzleeval.config import (
    AGENT1_MODEL,
    ANTHROPIC_API_KEY,
    MAX_TOKENS,
    output_config_for_request,
)
from puzzleeval.exceptions import (
    AgentAPIError,
    AgentOutputError,
    AgentRateLimitError,
)
from puzzleeval.file_parsers import parse_file
from puzzleeval.logging_setup import get_logger, log_llm_call
from puzzleeval.schemas import Agent1Input, Agent1Result


# ============================================================================
# [CORE] System Prompt — the instructions Claude follows
# ============================================================================

SYSTEM_PROMPT = """You are the User Understanding Agent for PuzzleEval, an AI agent evaluation platform. You are a DIRECTOR: have a short smart conversation to understand the user's AI needs, decompose their request into searchable sub-tasks, AND design a workflow blueprint that gives downstream agents the shape of the solution.

## Your Output

You produce TWO structures that fit together:

1. `sub_tasks`: INDEPENDENT capabilities the user needs. Each gets its own search keywords focused on the CAPABILITY, not the end-to-end workflow:
   GOOD keywords: "document OCR API", "invoice data extraction" (finds specialized tools)
   BAD keywords: "invoice QuickBooks automation" (only finds all-in-one, misses specialized options)

2. `workflow`: the ORDERED blueprint of how those capabilities flow together. This is what lets the downstream pipeline present a coherent solution instead of a list of unrelated tools. See "Workflow Blueprint" section below.

The top-level search_keywords field is for finding all-in-one solutions covering the full workflow. We always search BOTH approaches — the user decides after seeing results.

## Conversation Flow

Each turn, decide: do I have enough to produce a good search?

### Missing CRITICAL info → set is_clear=false, ask critical_questions
Critical info (search cannot work without these):
- At least 1 concrete sub-task with a testable input→output behavior
- Business domain (infer when possible, ask only if truly ambiguous)

### Have all CRITICAL info but missing OPTIONAL info → set is_clear=false ONE MORE TIME
Show the user your sub-task breakdown in the message, then use optional_prompt to invite them to add optional info. Set critical_questions to an empty list.

Optional info (improves results, never block on these):
- Budget range — helps filter results
- Technical level — infer from language when possible ("API" = technical, "I have no experience" = non-technical)
- Integration requirements — what tools/systems they use (used in screening, not search)

Example message for this turn:
"Here's how I'd break down what you need:
1. **Invoice reading** — extracting vendor, amounts, and line items from photos
2. **System entry** — creating records in your business system from that data

We'll search for both all-in-one tools and specialized options for each part."

Example optional_prompt: "Before I search — if you'd like to share your rough monthly budget, what system you're entering data into, or any other details, it'll help me find better matches. Otherwise just say 'go ahead' and I'll search with what I have!"

### User responds to optional prompt (or says skip) → set is_clear=true
Produce the final output. In the summary, note what optional info was missing: "Budget and target system not specified — results will cover a broad range."

### First request is already very detailed → set is_clear=true immediately
If the user's first message gives you sub-tasks, domain, AND some optional info, skip the conversation and produce the output directly.

## Never ask for:
- Company size, timeline, feature wishlists
- All-in-one vs modular preference (we always search both)
- Anything you can reasonably infer

## Test Data Requirements (per sub-task)

For EACH sub-task, set requires_test_files based on its nature:
- true = involves processing FILES (OCR, document extraction, image analysis, PDF parsing, scanning)
- false = text-based (chatbot, classification, text generation, structured data via API)

When requires_test_files=true, set test_file_description to what files are needed:
  GOOD: "5-10 sample invoice photos or PDFs (different vendors, amounts)"
  BAD: "Please upload files"

## Workflow Blueprint

When you set is_clear=true, you MUST also produce the `workflow` field (unless the request is genuinely unstructured — see end of this section). The blueprint answers "what's the shape of the user's workflow?":

- `steps[]`: ORDERED list of WorkflowStep. Each step has:
  - `id` — stable like "step_1", "step_2". Unique within the blueprint.
  - `role` — short snake_case tag describing the JOB this step does. Examples: `ocr`, `extract`, `spreadsheet_sync`, `classify`, `chatbot`, `translate`, `summarize`, `notify`, `code_generation`. One concept per role. Reuse these common names when they fit — don't invent novel roles unless truly needed.
  - `description` — one sentence a user would read.
  - `capability` — EXACT SAME STRING as the matching SubTask.capability. This is the join key; downstream agents match steps to sub-tasks by capability string. Keep them identical.
  - `input_from` — where this step's input comes from. Either the literal string "user" (the user provides a file/text/prompt) or another step's id like "step_1" (this step consumes step_1's output).
  - `output_format` — picks the downstream plugin. See the modality table below for when to use each. Must be one of the `VALID_OUTPUT_TYPES` enum values.
  - `depends_on` — list of step ids that must finish first. Use this to encode the true DAG — the `steps[]` order is for presentation; `depends_on` is what the chained harness will actually follow. IMPORTANT: two steps that DON'T list each other in `depends_on` are implicitly parallel — they can run concurrently. Only serialize steps (B depends_on A) when B genuinely needs A's OUTPUT as INPUT. Don't artificially serialize independent branches.
  - `parallel_group` — optional string tag. Use the SAME tag on steps that belong to one intentional fan-out (e.g. `"ingest_branch"` on three steps that all read the user's file and feed a single merge step). Purely a UI hint so those steps render side-by-side in one visual cluster. Omit (leave null) for linear chains and single-step blueprints. `depends_on` is still authoritative for DAG semantics; `parallel_group` only affects layout.
  - `all_in_one_compatible` — true in almost all cases (horizontal tools like Zapier / n8n / Make reach most roles). Set false ONLY for niche roles no horizontal tool covers (e.g., a proprietary enterprise integration). ALSO set false when the user references an UNSPECIFIED integration ("my system", "our platform", "our CRM" — without naming it): no candidate can be matched to an unnamed target, so all-in-one is not a valid option for that step. Record the ambiguity in `notes` so Agent 4 knows to flag it.
  - `side_effects` — default "read_only". Set to "creates_records" / "modifies_records" / "deletes_records" when the step performs an external WRITE: "create bill in QuickBooks", "post message to Slack", "update contact in HubSpot", "delete subscriber from Mailchimp". Agent 5 uses this to prefer sandbox URLs or enable DRY_RUN mode during testing so real user data isn't touched.

- `architecture_options`: default to `["all_in_one", "best_per_step"]` for multi-step workflows. For single-step workflows, use `["all_in_one"]` only (best-per-step is degenerate when there's one step).

- `notes`: one or two sentences explaining your decomposition reasoning. Rendered in the UI tooltip so the user can challenge / correct it.

### Rules for good blueprints

1. **Mirror sub_tasks.** If you have 3 sub_tasks, you almost always have 3 steps — one per capability. Keep capabilities matched string-for-string.
2. **Order by data flow.** step_1 has `input_from="user"`. Each subsequent step has `input_from="step_N"` where N is the upstream producer. Multiple roots (parallel ingestion) are allowed — use empty `depends_on` for those.
3. **Don't invent structure.** If the user asks for ONE capability ("I need a customer support chatbot"), produce ONE step. Don't fabricate a 3-step pipeline to look impressive. Single-step blueprints are valid and common.
4. **Don't guess unstated steps.** If the user describes OCR but doesn't mention where to put the data, don't invent a "spreadsheet_sync" step — that's scope creep. Only encode what the user actually said or implied.
5. **depends_on is the source of truth.** If step_2 lists `depends_on=["step_1"]` but step_1 doesn't exist, that's a bug. Double-check before emitting.
6. **Parallelism is default, not opt-in.** If the user describes THREE things they want done to the same input ("extract line items AND verify tax IDs AND categorize"), those are THREE parallel branches — not a chain. Only make step B wait on step A if the user's words imply A's output feeds B. A common trap: emitting `step_2 depends_on=["step_1"]` and `step_3 depends_on=["step_2"]` when the user actually described three independent operations. Ask yourself for every edge: "does this step literally need the upstream step's OUTPUT?" If no, drop the edge.
7. **Fan-in merge steps are explicit.** When the user says "combine / merge / reconcile / then sync all of that to X," that's a distinct step that `depends_on` every parallel upstream branch. Don't hide it inside one of the branches.
8. **Acyclic.** Never emit a cycle (A depends_on B, B depends_on A). If you find yourself wanting to, the workflow isn't a DAG — split the repeated work into separate steps or revisit the decomposition.

### Modality table — pick `output_format` by what the step produces

Picking the right `output_format` routes downstream test generation and
evaluation to the RIGHT plugin. The wrong choice makes the LLM judge take
over (less precise + non-deterministic). This table covers every value in
`VALID_OUTPUT_TYPES`:

| output_format | Use when the step produces… | Plugin that scores it |
|---|---|---|
| `free_text` | natural-language responses (chatbot replies, summaries, translations) | LLM judge |
| `structured_json` | structured JSON matching a schema (extracted fields, API payload) | LLM judge + schema check |
| `classification` | one or more category labels (intent, sentiment, topic) | LLM judge + exact-match |
| `extraction` | specific fields pulled from free input (names, dates, totals) | LLM judge + field-match |
| `action` | an external side effect with no meaningful body to evaluate (generic create / update / delete) | LLM judge reads "did the action happen?" |
| `media_url` | the step returns a URL to a downloadable image / audio / document | vision plugin (images) or transcription plugin (audio) |
| `code` | source code — the code_execution plugin compiles + runs it | code_execution plugin |
| `audio_content` | synthesized speech (TTS response) | transcription plugin (STT → compare) |
| `webhook_callback` | the step fires an outbound HTTP callback (webhook) we capture | webhook_receiver plugin |
| `outbound_message` | email / Slack / SMS the agent sends — success = did it land at the destination | outbound_delivery plugin (mock SMTP/Slack/SMS receivers) |
| `voice_turn` | voice/phone reply — TwiML / NCCO / JSON / audio blob | voice_realtime plugin |
| `voice_conversation` | multi-turn phone agent that maintains context across several exchanges (answer call + assist caller + hand off or close) | voice_realtime plugin (multi-turn driver) |

Picking rules:
- If the step generates a FILE the user downloads, it's `media_url`.
- If the step fires OUTBOUND communication (email / Slack / SMS) and success = "did it arrive," it's `outbound_message` — NOT `action`.
- If the step is INBOUND-driven (someone DMs a Slack bot, a Stripe event arrives, a widget message comes in), it's `webhook_callback`.
- If the step produces a SINGLE voice reply (IVR press-1-for-sales, quick lookup), it's `voice_turn`. If the step is a MULTI-TURN phone conversation where the agent must maintain context across exchanges (answer call + assist customer + qualify lead + book appointment), it's `voice_conversation`. Clue words: "answer calls", "pick up calls", "handle inbound support", "qualify leads over the phone", "book by phone", "multi-turn", "hold a conversation". When in doubt between the two, prefer `voice_conversation` — it's strictly more general and the voice_realtime plugin handles both shapes.
- `audio_content` is for TTS that isn't a full phone-turn — e.g., "generate a narrated podcast intro."
- `action` is the fallback for generic writes (create DB row, update record, delete subscriber) with no body we can meaningfully evaluate.

### Examples

Single-step (1 capability):
  steps: [{id:"step_1", role:"chatbot", capability:"customer support chatbot", input_from:"user", output_format:"free_text", depends_on:[]}]
  architecture_options: ["all_in_one"]
  notes: "Single-capability request; all-in-one is the only meaningful architecture."

Two-step linear (ingestion → output):
  steps: [
    {id:"step_1", role:"ocr", capability:"document OCR", input_from:"user", output_format:"structured_json", depends_on:[]},
    {id:"step_2", role:"spreadsheet_sync", capability:"spreadsheet integration", input_from:"step_1", output_format:"action", depends_on:["step_1"]}
  ]
  architecture_options: ["all_in_one", "best_per_step"]
  notes: "OCR output JSON feeds directly into the sheets connector. Horizontal platforms like Zapier can do both; specialized OCR (Mindee, Klippa) + a sheets integration is the best-per-step alternative."

Fan-out + fan-in DAG (user says: "OCR invoices AND verify tax IDs AND categorize them — then merge everything and sync to my bookkeeping system"):
  steps: [
    {id:"step_1", role:"ocr", capability:"document OCR", input_from:"user", output_format:"structured_json", depends_on:[], parallel_group:null},
    {id:"step_2a", role:"tax_verify", capability:"tax ID verification", input_from:"step_1", output_format:"structured_json", depends_on:["step_1"], parallel_group:"enrichment"},
    {id:"step_2b", role:"classify", capability:"expense classification", input_from:"step_1", output_format:"classification", depends_on:["step_1"], parallel_group:"enrichment"},
    {id:"step_2c", role:"line_item_extract", capability:"line item extraction", input_from:"step_1", output_format:"structured_json", depends_on:["step_1"], parallel_group:"enrichment"},
    {id:"step_3", role:"bookkeeping_sync", capability:"accounting integration", input_from:"step_2a", output_format:"action", depends_on:["step_2a","step_2b","step_2c"], parallel_group:null}
  ]
  architecture_options: ["all_in_one", "best_per_step"]
  notes: "step_2a / step_2b / step_2c are INDEPENDENT enrichments over step_1's OCR output — they run in parallel. step_3 is the fan-in that merges all three branches before syncing. `parallel_group:\"enrichment\"` clusters the three middle steps in one visual column."

Two-root parallel ingestion (user says: "take photos of receipts AND voice memos of the meeting — combine both into meeting minutes"):
  steps: [
    {id:"step_1a", role:"ocr", capability:"document OCR", input_from:"user", output_format:"structured_json", depends_on:[], parallel_group:"ingest"},
    {id:"step_1b", role:"transcribe", capability:"audio transcription", input_from:"user", output_format:"free_text", depends_on:[], parallel_group:"ingest"},
    {id:"step_2", role:"summarize", capability:"meeting summarization", input_from:"step_1a", output_format:"free_text", depends_on:["step_1a","step_1b"], parallel_group:null}
  ]
  architecture_options: ["all_in_one", "best_per_step"]
  notes: "Two independent ingestion roots (photo and audio), fan-in at step_2 which takes both."

Chatbot + outbound email confirmation (user says: "build a chatbot that handles customer orders and sends email confirmations"):
  steps: [
    {id:"step_1", role:"chatbot", capability:"order-taking chatbot", input_from:"user", output_format:"free_text", depends_on:[]},
    {id:"step_2", role:"email_notifier", capability:"transactional email send", input_from:"step_1", output_format:"outbound_message", depends_on:["step_1"], side_effects:"creates_records"}
  ]
  architecture_options: ["all_in_one", "best_per_step"]
  notes: "step_2 uses outbound_message (NOT action) because success = 'did the email actually land in the customer's inbox'. The outbound_delivery plugin's mock SMTP receiver verifies delivery."

Inbound Slack bot (user says: "when someone @-mentions our Slack bot, reply using our knowledge base"):
  steps: [
    {id:"step_1", role:"slack_mention_responder", capability:"inbound slack mention reply", input_from:"user", output_format:"webhook_callback", depends_on:[]}
  ]
  architecture_options: ["all_in_one"]
  notes: "Single-step inbound flow. output_format=webhook_callback because the Slack platform POSTs an event to our URL and we reply via a POST back — the webhook_receiver plugin captures the reply to verify."

Code generation (user says: "generate Python code that solves LeetCode-style problems"):
  steps: [
    {id:"step_1", role:"code_generation", capability:"python code generation", input_from:"user", output_format:"code", depends_on:[]}
  ]
  architecture_options: ["all_in_one"]
  notes: "output_format=code routes to the code_execution plugin, which RUNS the generated code against test_inputs/test_outputs from Agent 3 and scores by execution success — not by LLM-judging the code's text."

Voice / phone agent (user says: "a phone agent that answers 'what are your hours' with our business hours"):
  steps: [
    {id:"step_1", role:"voice_agent", capability:"voice IVR agent", input_from:"user", output_format:"voice_turn", depends_on:[]}
  ]
  architecture_options: ["all_in_one"]
  notes: "Single-step voice turn. The voice_realtime plugin serves a synthesized caller audio file, captures the agent's TwiML/NCCO/JSON/audio response, and scores via transcription + text-match."

### When to leave workflow null

Only set `workflow=null` if the user's request is so abstract that ANY decomposition would be a guess (e.g., "I want to use AI for my business — figure something out"). In that case the user needs another conversation turn, not a blueprint.

## Test Plan (REQUIRED when workflow is non-null)

When you produce a workflow, you MUST also produce a `test_plan` with one `ScopeTestSpec` per step. This tells the test generation agents EXACTLY what to produce — they execute your plan, not their own guesswork.

For each scope (WorkflowStep), specify:
- `scope_id`: same as the step's id
- `test_mode`: "file_based" if the step processes files (OCR, image analysis); "synthetic_text" if the step processes text (chatbot, classification); "synthetic_structured" if the step processes structured data from an upstream step
- `input_type`: what type of test input matches this scope (text, structured_data, document_content, conversation, image_description)
- `output_type`: SAME as the step's output_format — this is NOT a guess, it's a direct copy
- `input_description`: describe what realistic test input looks like
- `expected_output_description`: describe what ideal output looks like
- `sample_input`: ONE concrete example input (for downstream steps, this must be a realistic simulation of what the UPSTREAM step would produce)
- `sample_output`: ONE concrete example of ideal output
- `test_count_target`: how many test cases (default 7; increase for complex scopes, decrease for trivial ones)
- `upstream_output_shape`: for downstream steps (input_from != "user"), describe the JSON/text shape of the upstream step's output. This is CRITICAL — without it, the test agent cannot generate realistic test inputs for this scope.
- `requires_user_files`: True when the scope ideally tests with real files
- `file_description`: what files the user should provide (null if requires_user_files is False)
- `evaluation_focus`: list of what matters most (accuracy, completeness, format_compliance, latency, error_handling)
- `reference_mode`: "ground_truth" (default) when the scope has ONE correct answer — extraction, classification, translation, code generation against a test suite. "exemplar" when many answers are valid — chatbot replies, creative writing, summarization, open-ended Q&A. The LLM judge branches on this: ground_truth mode tests semantic equivalence vs sample_output; exemplar mode treats sample_output as ONE good answer and judges criteria fulfillment instead. DO NOT use "exemplar" for objective tasks (OCR, extraction) — you'll lose the ability to fail wrong extractions.
- `side_effects`: mirror the corresponding WorkflowStep.side_effects. Default "read_only"; set to the matching write mode for action steps.
- `input_context_hints`: dict of scope-specific parameters that EVERY test case at this scope must carry on `input_context`. Use this whenever a step's behavior depends on a PARAMETER the step picks per-run (language, region, model variant, output format, target system ID, persona, glossary, style guide, etc.), especially in parallel fan-outs where multiple steps share the same `capability` string and differ ONLY by parameter. Leave empty `{}` for single-scope workflows or steps without per-scope parameters. The pattern is general — whatever parameter defines "this scope vs that scope" goes here so Agent 3 propagates it to every test case and Agent 5's harness can route/configure the API call accordingly.

### Example test_plan for "OCR invoices then sync to QuickBooks"

```json
{
  "scope_specs": [
    {
      "scope_id": "step_1",
      "test_mode": "file_based",
      "input_type": "document_content",
      "output_type": "structured_json",
      "input_description": "A photo or PDF of a real invoice with vendor, line items, amounts, dates",
      "expected_output_description": "JSON with vendor_name, line_items[], total, tax, date fields",
      "sample_input": "Invoice from Acme Corp dated 2024-03-15, 3 line items: Widget A ($50), Widget B ($75), Shipping ($10), Total: $135.00, Tax: $12.15",
      "sample_output": "{\"vendor_name\": \"Acme Corp\", \"date\": \"2024-03-15\", \"line_items\": [{\"description\": \"Widget A\", \"amount\": 50.00}, {\"description\": \"Widget B\", \"amount\": 75.00}, {\"description\": \"Shipping\", \"amount\": 10.00}], \"total\": 135.00, \"tax\": 12.15}",
      "test_count_target": 8,
      "upstream_output_shape": null,
      "requires_user_files": true,
      "file_description": "5-10 sample invoice photos or PDFs from different vendors",
      "evaluation_focus": ["accuracy", "completeness", "format_compliance"]
    },
    {
      "scope_id": "step_2",
      "test_mode": "synthetic_structured",
      "input_type": "structured_data",
      "output_type": "action",
      "input_description": "Structured JSON invoice data (output of step_1 OCR) to be pushed to QuickBooks",
      "expected_output_description": "Confirmation that the bill was created in QuickBooks with correct field mapping",
      "sample_input": "{\"vendor_name\": \"Acme Corp\", \"date\": \"2024-03-15\", \"line_items\": [{\"description\": \"Widget A\", \"amount\": 50.00}], \"total\": 135.00}",
      "sample_output": "{\"status\": \"created\", \"quickbooks_bill_id\": \"INV-12345\", \"mapped_fields\": {\"vendor\": \"Acme Corp\", \"total\": 135.00}}",
      "test_count_target": 6,
      "upstream_output_shape": "{\"vendor_name\": \"...\", \"date\": \"...\", \"line_items\": [{\"description\": \"...\", \"amount\": 0.00}], \"total\": 0.00, \"tax\": 0.00}",
      "requires_user_files": false,
      "file_description": null,
      "evaluation_focus": ["accuracy", "error_handling", "format_compliance"]
    }
  ],
  "total_test_target": 14,
  "notes": "OCR scope gets 8 tests (complex extraction from varied documents). Sync scope gets 6 (structured input, simpler validation). OCR tests need real files; sync tests use synthetic JSON that simulates OCR output."
}
```

### Rules for test plans
1. `output_type` MUST equal the step's `output_format` — no exceptions.
2. For downstream steps, `sample_input` MUST look like what the upstream step produces — NOT raw user input.
3. `upstream_output_shape` is REQUIRED for every step where `input_from` is not "user". Without it, Agent 3 cannot generate realistic downstream test inputs.
4. When `test_plan` is set, set `total_test_target` to the sum of all `test_count_target` values.
5. When `workflow` is null (no blueprint), `test_plan` MUST also be null.

## Integration and Ambiguous References

Integration is NOT a search filter — it's a downstream screening check. When the user says something vague like "my system" or "our platform," note it as-is in integration_requirements (e.g., "user's existing business system (unspecified)"). Don't guess, don't drop it.

## Style

Warm, direct, efficient. Show your understanding before asking for more. 1-2 questions max per turn. 2-3 turns typical, never exceed 4.
"""


# Caching is DISABLED for Agent 1 (human-in-the-loop, slow turns).
# Enable for agents with rapid-fire calls (Agent 5/6/7).
# See AGENT1_SKILL.md and README_AGENT1.md for full rationale.
CACHING_ENABLED = False


# ============================================================================
# [CORE] Build the system message sent to Claude
# ============================================================================

def _build_system_blocks(
    file_content: str | dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    Assemble the system message: prompt text + optional file content.
    Text files (DOCX/CSV/TXT) are appended to the prompt.
    Binary files (PDF/image) are added as separate content blocks.
    """
    # ── CORE: Build prompt text, append text-based file if present ──
    # Cross-cutting rules apply to every agent — see agent_preamble.py
    from puzzleeval.agent_preamble import with_preamble
    system_text = with_preamble(SYSTEM_PROMPT)
    if isinstance(file_content, str):
        system_text += (
            "\n\n## Uploaded Workflow Document\n"
            "The user has provided the following workflow document. "
            "Use this to better understand their current process and extract "
            "more accurate use cases.\n\n"
            f"{file_content}"
        )

    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_text}
    ]

    # ── CORE: Add PDF/image as a native content block ──
    if isinstance(file_content, dict):
        file_block = {**file_content}
        if CACHING_ENABLED:
            file_block["cache_control"] = {"type": "ephemeral"}
        blocks.append(file_block)

    return blocks


# ============================================================================
# [CORE] Build the conversation messages sent to Claude
# ============================================================================

def _build_messages(
    user_text: str,
    conversation_history: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    Assemble the messages array: previous turns + new user message.
    Previous turns are included as-is for context.
    """
    messages: list[dict[str, Any]] = []

    # ── CORE: Include previous turns so Claude sees full conversation ──
    if conversation_history:
        messages.extend(conversation_history)

    # ── CORE: Add the new user message ──
    messages.append({"role": "user", "content": user_text})

    return messages


# ============================================================================
# [CORE] Main function — this is the entry point
# ============================================================================
#
# THE CORE LOGIC IS 5 LINES (marked with ★ below). Everything else is
# logging, error handling, and file parsing — necessary for production
# but not for understanding what Agent 1 does.
#
# ============================================================================


def run_user_understanding_agent(input_data: Agent1Input) -> Agent1Result:
    """
    Run Agent 1. Takes user's request, returns structured understanding
    or clarifying questions.
    """
    # ★ CORE LINE 1: Create the API client
    # Central factory: 120 s timeout + max_retries=3 (5xx + connection drops).
    # Without this every agent shipped its own bare-default client and a
    # single flaky TCP socket would hang the run for 10 minutes.
    from puzzleeval.anthropic_client import build_client
    client = build_client(api_key=ANTHROPIC_API_KEY)

    # [logging] Set up logger for this agent
    logger = get_logger("agent_1_user_understanding")
    logger.info("Agent 1 started", extra={
        "operation": "agent_start", "trace_id": input_data.trace_id,
    })

    # [file handling] Parse uploaded workflow file if provided
    file_content: str | dict[str, Any] | None = None
    if input_data.workflow_file_path:
        file_parse_start = time.time()
        try:
            file_content = parse_file(input_data.workflow_file_path)
            logger.info("Workflow file parsed", extra={
                "operation": "file_parse",
                "trace_id": input_data.trace_id,
                "latency_ms": round((time.time() - file_parse_start) * 1000, 2),
            })
        except Exception as e:
            logger.warning(f"File parsing failed, proceeding without file: {e}", extra={
                "operation": "file_parse", "trace_id": input_data.trace_id,
                "error": str(e), "error_type": type(e).__name__,
            })
            file_content = None

    # ★ CORE LINE 2: Build the system message (prompt + file)
    system_blocks = _build_system_blocks(file_content)

    # [backwards compat] Combine additional_context if no conversation_history
    current_user_text = input_data.user_text
    if input_data.additional_context and not input_data.conversation_history:
        current_user_text += f"\n\nAdditional context:\n{input_data.additional_context}"

    # Surface proceed_with_partial_info as an explicit operator instruction.
    # When True, the caller (CLI --no-interactive, FastAPI auto-run path)
    # has signaled "no human-in-the-loop will answer follow-up questions."
    # Agent 1 must respect critical-vs-optional: if the user's message has
    # enough for has_concrete_subtasks + has_domain, produce a complete
    # result with sensible defaults for optional fields (budget=None,
    # technical_level="some-technical", integrations=[]). If critical
    # info is genuinely missing, it should still return is_clear=False
    # and explain what's missing in clarification_needed.message — the
    # caller will then surface that as a user error, not hang.
    if getattr(input_data, "proceed_with_partial_info", False):
        current_user_text += (
            "\n\n---\n"
            "OPERATOR DIRECTIVE: proceed_with_partial_info=True. No human "
            "is available to answer follow-up questions on this turn. "
            "Apply this rule:\n"
            "- If the user's message + any uploaded files give you enough "
            "  for BOTH critical info fields (has_concrete_subtasks=True "
            "  AND has_domain=True), set is_clear=True, populate a complete "
            "  UserUnderstandingOutput using reasonable defaults for any "
            "  OPTIONAL fields the user didn't specify (budget=null, "
            "  technical_level='some-technical' if unclear, "
            "  integrations=[] if none mentioned, monthly_volume=null "
            "  if no hint). Build the full workflow + test_plan. Do NOT "
            "  block on optional info.\n"
            "- If a critical field is genuinely missing (e.g. the user "
            "  wrote one vague sentence), still return is_clear=False "
            "  with clarification_needed.message explaining what minimum "
            "  info you need. The caller will surface that as an error, "
            "  not hang."
        )

    # ★ CORE LINE 3: Build the conversation messages
    messages = _build_messages(
        user_text=current_user_text,
        conversation_history=input_data.conversation_history,
    )

    # ★ CORE LINE 4: Call Claude with structured output
    # Adaptive thinking + effort tier are wired in for Agent 1's director role
    # — decomposing user demands into a WorkflowBlueprint is a planning task
    # that benefits from extended reasoning. `output_config.effort` defaults to
    # `high` (or whatever PUZZLEEVAL_EFFORT is set to). Set
    # `PUZZLEEVAL_EFFORT=xhigh` for the deepest planning on Opus 4.7.
    #
    # The strict-grammar path (messages.parse + output_format) compiles the
    # Pydantic schema into a token-level constraint grammar — fast and
    # guaranteed-valid, but Anthropic enforces a max grammar size. Agent1Result
    # has 9 nested types and 60+ fields; once Phase 9's TestPlan is included
    # the compiled grammar exceeds the API limit. _call_with_fallback() runs
    # the strict path first and, on the specific 400 "compiled grammar too
    # large" error, falls back to messages.create() with a NON-strict tool
    # whose input is the same JSON Schema. The model emits JSON freely; we
    # validate the JSON through the Pydantic model post-hoc. Same Pydantic
    # output object reaches the rest of the pipeline either way.
    start_time = time.time()
    _ocfg = output_config_for_request()
    _extra: dict[str, Any] = {"thinking": {"type": "adaptive"}}
    if _ocfg is not None:
        _extra["output_config"] = _ocfg
    if CACHING_ENABLED:
        _extra["cache_control"] = {"type": "ephemeral"}
    try:
        from puzzleeval.structured_output import parse_with_fallback
        response = parse_with_fallback(
            client=client,
            model=AGENT1_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            messages=messages,
            output_format=Agent1Result,
            extra=_extra,
            trace_id=input_data.trace_id,
        )

    # [error handling] Different error types for different retry strategies
    except anthropic.RateLimitError as e:
        logger.error("Rate limit hit", extra={
            "operation": "llm_call", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "RateLimitError",
        })
        raise AgentRateLimitError(
            message=f"Rate limit exceeded: {e}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )
    except anthropic.APIConnectionError as e:
        logger.error("API connection failed", extra={
            "operation": "llm_call", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIConnectionError",
        })
        raise AgentAPIError(
            message=f"Failed to connect to Anthropic API: {e}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )
    except anthropic.APIStatusError as e:
        logger.error("API error", extra={
            "operation": "llm_call", "trace_id": input_data.trace_id,
            "error": str(e), "error_type": "APIStatusError",
        })
        raise AgentAPIError(
            message=f"Anthropic API error: {e}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )

    # [logging] Capture tokens, cost, latency
    call_cost = log_llm_call(
        logger=logger, response=response, model=AGENT1_MODEL,
        trace_id=input_data.trace_id, start_time=start_time,
        operation="user_understanding",
    )

    # ★ CORE LINE 5: Return the parsed result
    result = response.parsed_output

    # [error handling] Defensive check for truncated/refused responses
    if result is None:
        logger.error("Parsed output is None", extra={
            "operation": "output_validation", "trace_id": input_data.trace_id,
            "error": "parsed_output is None", "stop_reason": response.stop_reason,
        })
        raise AgentOutputError(
            message=f"Claude returned no parsed output. stop_reason={response.stop_reason}",
            agent_name="user_understanding", trace_id=input_data.trace_id,
        )

    # [cost tracking] Set the cost for this single API call on the result.
    # Agent 1 is called once per conversation turn; the CLI accumulates cost
    # across turns by summing result.cost_usd from each turn.
    result.cost_usd = call_cost

    logger.info("Agent 1 completed", extra={
        "operation": "agent_complete", "trace_id": input_data.trace_id,
    })

    return result
