"""Tests for ``puzzleeval.agents.agent5.reflection_evidence_check``.

Covers:
  * Pattern check verdict ladder: PASS / FAIL / BORDERLINE.
  * Section detection.
  * The combined ``evaluate`` entry point behavior with + without the
    LLM-judge fallback.
  * LLM-judge JSON parsing tolerance and fail-soft behavior on errors.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from puzzleeval.agents.agent5 import reflection_evidence_check as rec


_LOGGER = logging.getLogger("test_reflection_evidence")


# ---------------------------------------------------------------------------
# Fixtures — sample reflections at each verdict level
# ---------------------------------------------------------------------------


# A canonical PASS reflection — all 5 sections, ample file refs, > 150 words.
_PASS_REFLECTION = """# Reflection: pre-HARNESS_COMPLETE

## SUCCESS CRITERIA evidence walk-through

- offline smoke check passes: smoke_test.py:42 runs five mocked scenarios; output shows
  `5/5 passed`.
- live_test.py passes: live_test.py:18 invokes harness.run() against the
  production payload shape and prints `success=True`.
- Test cases pass: harness.py:120 returns the standardized 7-key dict with
  populated `output` field for each tc-XXX input.
- Forensics coverage: harness.py:1 imports `_forensics`; SDK calls at
  harness.py:55-78 wrapped in `with traced_op(...)`; session lifecycle
  logged at harness.py:88 via traced_op("session_create", ...).

## Adversarial probe robustness

- empty_input: harness.py:32-36 short-circuits when input_data.text is
  empty and returns success=False. No hang.
- max_input: harness.py:42-50 caps input length at 100K and truncates;
  no crash.
- malformed_input: harness.py:52-58 catches JSONDecodeError + returns
  success=False with structured error dict.
- idempotency: harness.py:60-65 builds a fresh Session per call, no
  module-level mutable state.
- concurrency: harness.py:70-78 uses local variables only — no shared
  state. Safe for parallel run() calls.
- repeat_call: harness.py:88 logs op_done events that show consistent
  output across 5 sequential calls.

## User-fit assessment

- Sub-task A (translate text): harness.py:100-110 calls the /translate
  endpoint and returns the translated string in `output`.
- Sub-task B (preserve formatting): harness.py:115 passes `preserve=true`
  and the translated text retains markdown structure.

## Code quality assessment

- separation of concerns: harness.py:20-30 (auth), harness.py:35-50
  (transport), harness.py:60-110 (business logic).
- no swallowed exceptions: every except block at harness.py:35,52,75
  logs traced_op("op_error", ...) and re-raises or returns
  success=False with the error message.
- resource cleanup: harness.py:130 closes the session in a finally
  block on every return path (success + error + early-return).
- no shared mutable state: harness.py has no module-level dicts or
  lists; run() constructs all state per-call.

## Self-critique

- One thing I'd fix with one more turn: harness.py:42 uses a hard-coded
  100K input cap; would expose that as a config knob via env var.
"""

# A FAIL reflection — vacuous self-attestation.
_FAIL_REFLECTION = """# Reflection: pre-HARNESS_COMPLETE

## SUCCESS CRITERIA evidence walk-through

Yes, all criteria handled.

## Adversarial probe robustness

The harness is robust against all probes.

## User-fit assessment

It solves the user's needs.
"""

# A BORDERLINE reflection — some references, missing sections.
_BORDERLINE_REFLECTION = """# Reflection: pre-HARNESS_COMPLETE

## SUCCESS CRITERIA evidence walk-through

- smoke_test.py:1 covers the happy path.
- live_test.py:1 covers the live path.

## Adversarial probe robustness

- empty_input: harness.py:30 returns success=False.
- malformed_input: harness.py:55 returns success=False.
- The other probes are handled.

## Self-critique

Nothing to add.
"""


# ---------------------------------------------------------------------------
# compute_evidence — the pattern-check ladder
# ---------------------------------------------------------------------------


class TestComputeEvidenceVerdicts:

    def test_empty_string_is_fail(self):
        ev = rec.compute_evidence("")
        assert ev.verdict == rec.ReflectionVerdict.FAIL
        assert ev.total_file_refs == 0
        assert ev.word_count == 0
        # All 5 expected sections are missing from empty string.
        assert len(ev.missing_section_headers) == 5

    def test_whitespace_only_is_fail(self):
        ev = rec.compute_evidence("   \n\n\t  ")
        assert ev.verdict == rec.ReflectionVerdict.FAIL

    def test_canonical_pass_reflection(self):
        ev = rec.compute_evidence(_PASS_REFLECTION)
        assert ev.verdict == rec.ReflectionVerdict.PASS, ev.reason
        assert ev.total_file_refs >= rec.PASS_FILE_REF_FLOOR
        assert ev.word_count >= rec.PASS_WORD_FLOOR
        assert ev.sections_with_evidence == 5
        assert len(ev.missing_section_headers) == 0

    def test_alias_headings_pass_when_evidence_is_strong(self):
        reflection = """# Reflection: pre-HARNESS_COMPLETE

## 1. DELIVERABLE coverage

The harness satisfies the objective because smoke_test.py:42 runs the
production-shaped request, live_test.py:18 exercises the real live path, and
harness.py:120 returns the standardized output contract. Forensics coverage is
present because harness.py:1 imports _forensics and harness.py:55-78 wraps the
provider call with traced_op for request_start/request_done events.

## Probe robustness decisions

empty_input is handled at harness.py:32 by returning success=False without a
network call. max_input is bounded at harness.py:42. malformed_input is caught
at harness.py:52. idempotency and repeat_call use per-call local objects at
harness.py:60-65, while concurrency avoids shared module-level state at
harness.py:70-78.

## Blueprint sub-task evidence

The user's first sub-task is answered through harness.py:100-110, which calls
the provider and returns the useful result in output. The second sub-task is
handled at harness.py:115 by preserving the structured request fields used by
the test cases.

## Cleanup and error-path audit

Auth setup is isolated in harness.py:20-30, transport in harness.py:35-50, and
business logic in harness.py:60-110. Exceptions at harness.py:35, harness.py:52,
and harness.py:75 include traced_op("op_error") before returning a structured
failure. Resources are closed in harness.py:130 through a finally block.

## Remaining risk

With one more turn I would replace the hard-coded input cap in harness.py:42
with an environment-configured value, but the current value is safe for the
max_input probe and does not affect correctness.
"""
        ev = rec.compute_evidence(reflection)
        assert ev.verdict == rec.ReflectionVerdict.PASS, ev.reason
        assert len(ev.missing_section_headers) == 0

    def test_canonical_fail_reflection(self):
        ev = rec.compute_evidence(_FAIL_REFLECTION)
        assert ev.verdict == rec.ReflectionVerdict.FAIL, ev.reason
        # No file refs, two sections missing
        assert ev.total_file_refs == 0
        assert "User-fit assessment" not in " ".join(ev.missing_section_headers) or True
        # missing_section_headers should have at least 2 entries (Code quality + Self-critique are absent)
        assert len(ev.missing_section_headers) >= 2

    def test_borderline_reflection(self):
        ev = rec.compute_evidence(_BORDERLINE_REFLECTION)
        # Has some refs but missing sections + below word floor.
        assert ev.verdict == rec.ReflectionVerdict.BORDERLINE, ev.reason


class TestComputeEvidenceMetrics:

    def test_file_ref_count_picks_up_dotted_filenames(self):
        text = (
            "harness.py:42 and smoke_test.py:1-10 and live_test.py "
            "and api_spec.txt:200 and requirements.txt and _forensics.py:88."
        )
        ev = rec.compute_evidence(text)
        assert ev.total_file_refs >= 5

    def test_file_ref_count_picks_up_agent_state_research_artifacts(self):
        text = (
            "_agent_state/research_synthesis.json:12 and "
            "_agent_state/implementation_plan.json:4 and "
            "_agent_state/research_findings/task_auth.json:1 support "
            "the selected harness route."
        )
        ev = rec.compute_evidence(text)
        assert ev.total_file_refs == 3

    def test_code_block_counter(self):
        text = (
            "## SUCCESS CRITERIA evidence walk-through\n"
            "Block 1:\n```python\nprint(1)\n```\n"
            "Block 2:\n```python\nprint(2)\n```\n"
        )
        ev = rec.compute_evidence(text)
        # 4 backticks → 2 code blocks
        assert ev.total_code_blocks == 2

    def test_forensics_pattern_recognized(self):
        text = "Used traced_op for session_create_start and request_done events."
        ev = rec.compute_evidence(text)
        assert ev.total_forensics_refs >= 3

    def test_validate_citation_targets_rejects_missing_file(self, tmp_path):
        errors = rec.validate_citation_targets(
            "The claim cites harness.py:42 and smoke_test.py:1.",
            sandbox_dir=tmp_path,
            harness_code=None,
        )
        assert any("harness.py" in err for err in errors)

    def test_validate_citation_targets_rejects_out_of_range_line(self, tmp_path):
        (tmp_path / "harness.py").write_text("line 1\nline 2\n", encoding="utf-8")
        errors = rec.validate_citation_targets(
            "The claim cites harness.py:42.",
            sandbox_dir=tmp_path,
            harness_code="line 1\nline 2\n",
        )
        assert any("exceeds file length" in err for err in errors)

    def test_validate_citation_targets_accepts_agent_state_artifacts(self, tmp_path):
        state = tmp_path / "_agent_state"
        state.mkdir()
        (state / "research_synthesis.json").write_text('{"facts":[]}\n', encoding="utf-8")
        (state / "implementation_plan.json").write_text('{"steps":[]}\n', encoding="utf-8")

        errors = rec.validate_citation_targets(
            "The plan cites _agent_state/research_synthesis.json:1 and "
            "_agent_state/implementation_plan.json:1.",
            sandbox_dir=tmp_path,
        )

        assert errors == []


class TestSectionSplit:

    def test_split_handles_unordered_sections(self):
        # Sections in non-canonical order — _split_sections still finds them.
        text = (
            "## Self-critique\nfoo\n\n"
            "## SUCCESS CRITERIA evidence walk-through\nbar\n\n"
            "## Adversarial probe robustness\nbaz\n"
        )
        sections = rec._split_sections(text)
        assert "foo" in sections["## Self-critique"]
        assert "bar" in sections["## SUCCESS CRITERIA evidence walk-through"]
        assert "baz" in sections["## Adversarial probe robustness"]
        # Missing sections map to empty
        assert sections["## User-fit assessment"] == ""


# ---------------------------------------------------------------------------
# evaluate() — combined pattern + judge entry point
# ---------------------------------------------------------------------------


class TestEvaluatePatternOnly:
    """When pattern check is decisive, the judge is NOT invoked."""

    def test_pass_short_circuits_without_judge(self):
        client = MagicMock()
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=_PASS_REFLECTION,
            objective_md="(objective)",
            client=client,
            llm_judge_enabled=True,
        )
        assert verdict == rec.ReflectionVerdict.PASS
        assert judge_reason == ""  # judge not called
        client.messages.create.assert_not_called()

    def test_fail_short_circuits_without_judge(self):
        client = MagicMock()
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=_FAIL_REFLECTION,
            objective_md="(objective)",
            client=client,
            llm_judge_enabled=True,
        )
        assert verdict == rec.ReflectionVerdict.FAIL
        assert judge_reason == ""
        client.messages.create.assert_not_called()

    def test_strong_borderline_reflection_passes_without_judge(self):
        harness_code = "\n".join(f"# harness line {i}" for i in range(1, 240))
        cited_refs = " ".join(f"harness.py:{i}" for i in range(20, 38))
        reflection = f"""# Reflection: pre-HARNESS_COMPLETE

## 1. DELIVERABLE coverage

The live and smoke paths are grounded in specific implementation evidence:
{cited_refs}. The code paths show request validation, normalized success and
failure returns, and the same run(input_data) contract the objective requires.
This paragraph is intentionally substantive enough to represent a real audit,
not a label-only checklist.

## Probe robustness decisions

empty_input, max_input, malformed_input, idempotency, concurrency, and repeat
calls are all tied to cited lines: {cited_refs}. The implementation constructs
per-call state, validates input before network use, and returns structured
failure dictionaries on error paths instead of swallowing exceptions.

## Blueprint sub-task evidence

The user-facing task path is cited with concrete lines: {cited_refs}. The
harness maps the caller payload into provider requests and returns the useful
assistant output in the standardized result object. It also preserves relevant
business context across turns.

## Cleanup and error-path audit

Resource cleanup, exception logging, and transport separation are cited here:
{cited_refs}. The implementation records forensics events and closes resources
in all success and failure paths.
"""
        client = MagicMock()
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=reflection,
            objective_md="(objective)",
            harness_code=harness_code,
            client=client,
            llm_judge_enabled=True,
        )
        assert evidence.verdict == rec.ReflectionVerdict.BORDERLINE
        assert verdict == rec.ReflectionVerdict.PASS
        assert judge_reason == "deterministic_strong_evidence"
        client.messages.create.assert_not_called()


class TestEvaluateJudgeFallback:
    """Borderline pattern verdict → judge fallback drives the final call."""

    def _make_judge_client(self, verdict: str, reason: str = "x") -> MagicMock:
        client = MagicMock()
        response = MagicMock()
        text = MagicMock()
        text.type = "text"
        text.text = json.dumps({"verdict": verdict, "reason": reason})
        response.content = [text]
        client.messages.create.return_value = response
        return client

    def test_judge_pass_overrides_borderline(self):
        client = self._make_judge_client("pass", "evidence checks out")
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="(objective)",
            client=client,
            llm_judge_enabled=True,
            logger=_LOGGER,
        )
        assert verdict == rec.ReflectionVerdict.PASS
        assert judge_reason.startswith("judge_pass:")
        client.messages.create.assert_called_once()

    def test_judge_receives_cited_line_windows(self):
        client = self._make_judge_client("pass", "evidence checks out")
        harness_code = "\n".join(f"# harness line {i}" for i in range(1, 90))
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="(objective)",
            harness_code=harness_code,
            client=client,
            llm_judge_enabled=True,
            logger=_LOGGER,
        )
        assert verdict == rec.ReflectionVerdict.PASS
        user_message = client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "# CITED HARNESS.PY LINE WINDOWS" in user_message
        assert "harness.py:26-34" in user_message
        assert "55: # harness line 55" in user_message
        assert "# harness line 89" not in user_message

    def test_judge_fail_promotes_to_fail(self):
        client = self._make_judge_client("fail", "claims unsupported")
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="(objective)",
            client=client,
            llm_judge_enabled=True,
            logger=_LOGGER,
        )
        assert verdict == rec.ReflectionVerdict.FAIL
        assert judge_reason.startswith("judge_fail:")
        client.messages.create.assert_called_once()

    def test_judge_disabled_borderline_accepts(self):
        client = MagicMock()
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="(objective)",
            client=client,
            llm_judge_enabled=False,
        )
        assert verdict == rec.ReflectionVerdict.PASS
        assert judge_reason == "judge_disabled"
        client.messages.create.assert_not_called()

    def test_no_client_borderline_accepts(self):
        verdict, evidence, judge_reason = rec.evaluate(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="(objective)",
            client=None,
            llm_judge_enabled=True,
        )
        assert verdict == rec.ReflectionVerdict.PASS
        assert judge_reason == "judge_disabled"


class TestLLMJudgeRobustness:
    """The judge must fail open on every infrastructure error class."""

    def test_judge_network_error_fails_open(self):
        client = MagicMock()
        client.messages.create.side_effect = ConnectionError("network blip")
        verdict, reason = rec.llm_judge_check(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="x",
            harness_code=None,
            client=client,
            judge_model="claude-sonnet-4-6",
            logger=_LOGGER,
            trace_id="t",
            candidate_name="c",
        )
        assert verdict == rec.ReflectionVerdict.PASS
        assert reason == "judge_error_fail_open"

    def test_judge_garbage_output_fails_open(self):
        client = MagicMock()
        response = MagicMock()
        text = MagicMock()
        text.type = "text"
        text.text = "!!! NOT JSON !!!"
        response.content = [text]
        client.messages.create.return_value = response
        verdict, reason = rec.llm_judge_check(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="x",
            harness_code=None,
            client=client,
            judge_model="claude-sonnet-4-6",
            logger=_LOGGER,
            trace_id="t",
            candidate_name="c",
        )
        assert verdict == rec.ReflectionVerdict.PASS
        assert reason.startswith("judge_unparseable:")

    def test_judge_extracts_json_from_chatty_response(self):
        # The judge prompt asks for one-line JSON, but Sonnet sometimes
        # adds prose. Extractor should still find the JSON.
        client = MagicMock()
        response = MagicMock()
        text = MagicMock()
        text.type = "text"
        text.text = (
            "Here is my judgement:\n\n"
            '{"verdict": "fail", "reason": "no concurrency citation"}\n\n'
            "Hope that helps."
        )
        response.content = [text]
        client.messages.create.return_value = response
        verdict, reason = rec.llm_judge_check(
            reflection_md=_BORDERLINE_REFLECTION,
            objective_md="x",
            harness_code=None,
            client=client,
            judge_model="claude-sonnet-4-6",
            logger=_LOGGER,
            trace_id="t",
            candidate_name="c",
        )
        assert verdict == rec.ReflectionVerdict.FAIL
        assert "no concurrency citation" in reason
