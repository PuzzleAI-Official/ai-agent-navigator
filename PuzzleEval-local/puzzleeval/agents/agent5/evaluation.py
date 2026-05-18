"""LLM-judge evaluation for Agent 5 test results.

Phase 6.2 — extracts the evaluation pipeline (mechanical eval helpers
+ LLM-judge prompt builder + the judge call itself + score aggregator)
from ``implement_test_env.py`` into a focused module.

Public surface:
  * ``evaluate_with_llm(client, test_case, response, ...)`` — the judge
    call that returns ``EvaluationBatchResult``.
  * ``evaluate_mechanical(...)`` — exact-match + format-compliance for
    pre-LLM filtering of trivially-correct/trivially-wrong cases.
  * ``compute_weighted_score(...)`` — aggregator that turns
    per-criterion scores into a single overall_score.
  * ``build_evaluation_prompt(...)`` — pure prompt builder. Tested in
    isolation; useful for cost-estimation tools.

Helper-private:
  * ``try_parse_number`` — used by exact-match for numeric tolerance.
  * ``evaluate_exact_match``, ``evaluate_format_compliance`` — called
    by ``evaluate_mechanical``.

AD-007: this module is pure backbone. Markdown contracts don't gate
behavior here.
"""

from __future__ import annotations

import json
import logging
import re
import time
try:
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - exercised in minimal test envs
    from puzzleeval.anthropic_client import anthropic  # type: ignore

from puzzleeval.config import (
    AGENT6_EVAL_MAX_TOKENS,
    AGENT6_EVAL_MODEL,
)
from puzzleeval.schemas import (
    CriterionScore,
    EvaluationBatchResult,
    TestCase,
)


# Truncation cap for raw API responses sent to the LLM judge. 15K captures
# all key fields (vendor, line_items, totals) even in verbose responses.
# Cost: ~$0.01/test at Sonnet rates.
RAW_RESPONSE_MAX_CHARS = 15000


def _normalize_for_comparison(text: str | None) -> str:
    """Normalize text for mechanical exact-match comparisons."""

    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def try_parse_number(text: str) -> float | None:
    """Try to extract a number from text (handles $, commas)."""
    cleaned = re.sub(r"[$,\s]", "", text.strip())
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def evaluate_exact_match(
    output: str,
    expected: str,
    criterion: str,
    weight: float,
) -> CriterionScore:
    """Evaluate an exact_match criterion mechanically."""
    norm_output = _normalize_for_comparison(output)
    norm_expected = _normalize_for_comparison(expected)

    quoted = re.findall(r"['\"]([^'\"]+)['\"]", criterion)

    if quoted:
        matches_found = 0
        for q in quoted:
            norm_q = _normalize_for_comparison(q)
            if norm_q in norm_output:
                matches_found += 1
            else:
                q_num = try_parse_number(q)
                if q_num is not None:
                    output_numbers = re.findall(r"[\d,]+\.?\d*", output)
                    for on in output_numbers:
                        on_num = try_parse_number(on)
                        if on_num is not None and abs(on_num - q_num) < 0.01:
                            matches_found += 1
                            break

        score = matches_found / len(quoted) if quoted else 0.0
        return CriterionScore(
            criterion=criterion,
            eval_type="exact_match",
            weight=weight,
            score=score,
            passed=score >= 0.5,
            reasoning=f"Found {matches_found}/{len(quoted)} expected values in output",
        )

    if norm_expected in norm_output or norm_output in norm_expected:
        return CriterionScore(
            criterion=criterion,
            eval_type="exact_match",
            weight=weight,
            score=1.0,
            passed=True,
            reasoning="Output contains expected content",
        )

    return CriterionScore(
        criterion=criterion,
        eval_type="exact_match",
        weight=weight,
        score=0.0,
        passed=False,
        reasoning="Expected content not found in output",
    )


def evaluate_format_compliance(
    output: str,
    criterion: str,
    weight: float,
) -> CriterionScore:
    """Evaluate a format_compliance criterion mechanically."""
    is_valid_json = False
    try:
        json.loads(output)
        is_valid_json = True
    except (json.JSONDecodeError, TypeError):
        json_match = re.search(r"\{[^{}]*\}", output, re.DOTALL)
        if json_match:
            try:
                json.loads(json_match.group())
                is_valid_json = True
            except json.JSONDecodeError:
                pass

    if "json" in criterion.lower() or "valid json" in criterion.lower():
        score = 1.0 if is_valid_json else 0.0
        return CriterionScore(
            criterion=criterion,
            eval_type="format_compliance",
            weight=weight,
            score=score,
            passed=score >= 0.5,
            reasoning="Output is valid JSON" if is_valid_json else "Output is not valid JSON",
        )

    score = 1.0 if output.strip() else 0.0
    return CriterionScore(
        criterion=criterion,
        eval_type="format_compliance",
        weight=weight,
        score=score,
        passed=score >= 0.5,
        reasoning="Output is non-empty" if output.strip() else "Output is empty",
    )


def evaluate_mechanical(
    output: str,
    expected: str,
    criteria: list[dict],
) -> list[CriterionScore]:
    """Evaluate mechanical criteria (exact_match, format_compliance)."""
    scores = []
    for c in criteria:
        if c["eval_type"] == "exact_match":
            scores.append(evaluate_exact_match(
                output, expected, c["criterion"], c["weight"]
            ))
        elif c["eval_type"] == "format_compliance":
            scores.append(evaluate_format_compliance(
                output, c["criterion"], c["weight"]
            ))
    return scores


def build_evaluation_prompt(
    test_results: list[tuple[TestCase, dict, list[dict]]],
    candidate_name: str,
) -> str:
    """Build the user message for LLM judge evaluation.

    Feeds raw API response + ground truth + criteria to the judge.
    Raw responses are truncated to RAW_RESPONSE_MAX_CHARS to control cost.
    """
    parts = [f"Evaluate the following API results for {candidate_name}:\n"]

    for tc, result, criteria in test_results:
        if not criteria:
            continue

        # Use raw_response (the actual API output) instead of formatted "output"
        raw_resp = result.get("raw_response", {})
        if isinstance(raw_resp, dict):
            raw_resp_str = json.dumps(raw_resp, indent=2, default=str)
        else:
            raw_resp_str = str(raw_resp)
        # Truncate to control input token cost
        if len(raw_resp_str) > RAW_RESPONSE_MAX_CHARS:
            raw_resp_str = raw_resp_str[:RAW_RESPONSE_MAX_CHARS] + "\n... (truncated)"

        parts.append(f"\n=== TEST CASE [{tc.id}] ===")
        parts.append(f"Scenario: {tc.scenario}")
        parts.append(f"API success: {result.get('success', False)}")
        if result.get("error"):
            parts.append(f"Error: {result['error'][:200]}")
        parts.append(f"\nGround Truth (expected):\n{tc.expected_output}")
        parts.append(f"\nRaw API Response:\n{raw_resp_str}")
        parts.append("\nCriteria to evaluate:")
        for i, c in enumerate(criteria, 1):
            parts.append(f"  {i}. {c['criterion']} (weight: {c['weight']})")

    return "\n".join(parts)


def evaluate_with_llm(
    client: anthropic.Anthropic,
    test_results: list[tuple[TestCase, dict, list[dict]]],
    candidate_name: str,
    logger: logging.Logger,
    trace_id: str,
) -> tuple[dict[str, list[CriterionScore]], float]:
    """Batch-evaluate test results using LLM for semantic/subjective criteria."""

    # Phase 6 lazy imports — implement_test_env imports this
    # module via re-export shims; importing back at MODULE TOP
    # would cycle.
    from puzzleeval.agents.implement_test_env import (
        EVALUATION_SYSTEM_PROMPT,
        _calculate_call_cost,
        _with_shared_preamble,
    )

    llm_items = [(tc, r, c) for tc, r, c in test_results if c]
    if not llm_items:
        return {}, 0.0

    prompt = build_evaluation_prompt(llm_items, candidate_name)
    cost = 0.0

    for attempt in range(2):
        try:
            from puzzleeval.structured_output import parse_with_fallback
            # Evaluator rigor upgrade: pass thinking=adaptive so the judge
            # can reason through synonym mapping, partial-match semantics,
            # and edge-case criteria weighting — previously it one-shot
            # rubber-stamped. The strict-grammar path will pick up the
            # output_config.effort tier (defaults to "high") via
            # parse_with_fallback's ``extra`` kwarg, so evaluators respect
            # the same PUZZLEEVAL_EFFORT knob as every other agent.
            from puzzleeval.config import output_config_for_request
            _eval_extra: dict[str, object] = {"thinking": {"type": "adaptive"}}
            _eval_ocfg = output_config_for_request()
            if _eval_ocfg:
                _eval_extra["output_config"] = _eval_ocfg
            response = parse_with_fallback(
                client=client,
                model=AGENT6_EVAL_MODEL,
                max_tokens=AGENT6_EVAL_MAX_TOKENS,
                system=_with_shared_preamble(EVALUATION_SYSTEM_PROMPT),
                messages=[{"role": "user", "content": prompt}],
                output_format=EvaluationBatchResult,
                extra=_eval_extra,
                trace_id=trace_id,
            )
            cost += _calculate_call_cost(response, AGENT6_EVAL_MODEL)

            logger.info(
                f"LLM evaluation completed for {candidate_name}",
                extra={
                    "operation": "llm_evaluation",
                    "trace_id": trace_id,
                    "candidate_name": candidate_name,
                    "cost_usd": cost,
                    "tokens_in": response.usage.input_tokens,
                    "tokens_out": response.usage.output_tokens,
                },
            )

            parsed: EvaluationBatchResult = response.parsed_output
            result_map: dict[str, list[CriterionScore]] = {}

            for eval_item in parsed.evaluations:
                scores = []
                original_criteria = {}
                for tc, _, criteria in llm_items:
                    if tc.id == eval_item.test_case_id:
                        original_criteria = {c["criterion"]: c for c in criteria}
                        break

                for cs_out in eval_item.criteria_scores:
                    orig = original_criteria.get(cs_out.criterion, {})
                    scores.append(CriterionScore(
                        criterion=cs_out.criterion,
                        eval_type=orig.get("eval_type", "semantic_similarity"),
                        weight=orig.get("weight", 0.5),
                        score=max(0.0, min(1.0, cs_out.score)),
                        passed=cs_out.passed,
                        reasoning=cs_out.reasoning,
                    ))
                result_map[eval_item.test_case_id] = scores

            return result_map, cost

        except anthropic.RateLimitError:
            time.sleep(15)
            continue
        except Exception as e:
            logger.warning(
                f"LLM evaluation error for {candidate_name}: {e}",
                extra={"operation": "llm_evaluation_error", "trace_id": trace_id},
            )
            if attempt == 0:
                time.sleep(5)
                continue
            break

    logger.warning(
        f"LLM evaluation failed after retries for {candidate_name}",
        extra={"operation": "llm_evaluation_failed", "trace_id": trace_id},
    )
    return {}, cost


def compute_weighted_score(criteria_scores: list[CriterionScore]) -> float:
    """Compute weighted average score from criteria scores."""
    if not criteria_scores:
        return 0.0
    total_weight = sum(cs.weight for cs in criteria_scores)
    if total_weight == 0:
        return 0.0
    weighted_sum = sum(cs.score * cs.weight for cs in criteria_scores)
    return round(weighted_sum / total_weight, 4)


__all__ = [
    "RAW_RESPONSE_MAX_CHARS",
    "build_evaluation_prompt",
    "compute_weighted_score",
    "evaluate_exact_match",
    "evaluate_format_compliance",
    "evaluate_mechanical",
    "evaluate_with_llm",
    "try_parse_number",
]
