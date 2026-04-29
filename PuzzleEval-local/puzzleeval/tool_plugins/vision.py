"""Vision plugin — wraps the existing vision_judge as a registered plugin.

`vision_judge.py` already implements Claude vision-based image evaluation
(Gap 6/26 from earlier passes). This module exposes it through the
plugin interface so the modality detector can pick it the same way it
picks any other plugin — no hardcoded "if media_url then call vision".
"""

from __future__ import annotations

from typing import Any

from puzzleeval.tool_plugins import (
    EvaluationResult,
    PluginCapabilities,
    ToolPlugin,
    register_plugin,
)
from puzzleeval.vision_judge import (
    evaluate_generated_image,
    extract_image_url,
)


class VisionPlugin(ToolPlugin):
    """Vision-based image evaluation. Pulls image URL/data from the response,
    sends to Claude vision, scores against expected description + criteria."""

    name = "vision"

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=["image_description"],
            output_types=["media_url"],
            synthesizes_input=False,
            evaluates_output=True,
            requires_credentials=["ANTHROPIC_API_KEY"],
            notes=(
                "Wraps puzzleeval/vision_judge.py. Handles image responses "
                "(URL or data: URI). Audio / video are out of scope for this "
                "plugin — see transcription/tts plugins for audio."
            ),
        )

    def evaluate_output(
        self, *, response: Any, expected: Any,
        criteria: list[dict] | None = None, **kwargs: Any,
    ) -> EvaluationResult:
        image_source = extract_image_url(response)
        if not image_source:
            return EvaluationResult(
                passed=False, score=0.0, reasoning="no image URL/data in response",
                fallback_reason="no_image_in_response",
            )
        expected_desc = expected if isinstance(expected, str) else str(expected or "")
        verdict = evaluate_generated_image(
            image_source=image_source,
            expected_description=expected_desc,
            criteria=criteria or [],
        )
        if verdict.fallback_reason:
            return EvaluationResult(
                passed=False, score=0.0, reasoning=verdict.reasoning,
                fallback_reason=verdict.fallback_reason,
            )
        return EvaluationResult(
            passed=verdict.passed,
            score=verdict.score,
            reasoning=verdict.reasoning,
            detail={"criterion_scores": verdict.criterion_scores},
        )


register_plugin(VisionPlugin())


__all__ = ["VisionPlugin"]
