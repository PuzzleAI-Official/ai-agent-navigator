"""Tests for the general-principle refactor (post-bandaid audit).

These tests assert the schema supports open-ended inputs rather than prescribed
enums, so the pipeline stays domain-agnostic:

- InteractionModel: multiple flags can be simultaneously true (not mutually-exclusive enum)
- UserSelectableParam: applies to every API class (not only generative)
- upstream_provider: free-text string, not a hardcoded enum
- api_interaction_pattern_hint: soft vocabulary, unknown values coerced to "unknown"
- Candidate-class separation: principle-based prompt (no brand names hardcoded)
"""
from __future__ import annotations

import pytest

from puzzleeval.schemas import (
    Candidate,
    InteractionModel,
    ScreenedCandidate,
    UserSelectableParam,
)
from puzzleeval.agents.agent2.core import _normalize_coverage


class TestInteractionModel:
    def test_defaults_all_false(self):
        im = InteractionModel()
        assert im.synchronous is False
        assert im.async_polling is False
        assert im.webhook_callback is False
        assert im.sse_streaming is False
        assert im.batch_file is False
        assert im.event_subscription is False
        assert im.notes == ""

    def test_multiple_flags_simultaneously(self):
        """Multi-modal APIs set several flags. The model must permit it."""
        im = InteractionModel(
            synchronous=True,
            async_polling=True,
            notes="Most endpoints sync; /v1/batch uses async_polling.",
        )
        assert im.synchronous
        assert im.async_polling
        assert not im.webhook_callback

    def test_json_roundtrip(self):
        im = InteractionModel(sse_streaming=True, batch_file=True, notes="hybrid")
        roundtripped = InteractionModel.model_validate_json(im.model_dump_json())
        assert roundtripped == im


class TestUserSelectableParam:
    @pytest.mark.parametrize(
        "param",
        [
            # OCR-style API
            UserSelectableParam(
                name="language", allowed_values="ISO 639-1", affects=["quality"], default="en"
            ),
            # Transcription-style API
            UserSelectableParam(
                name="diarize",
                allowed_values="true | false",
                affects=["output_shape"],
                default="false",
            ),
            # Translation-style API
            UserSelectableParam(
                name="formality",
                allowed_values="more | less | default",
                affects=["output_style"],
                default="default",
            ),
            # Image-gen-style API
            UserSelectableParam(
                name="size",
                allowed_values="1024x1024 | 1792x1024 | 1024x1792",
                affects=["cost", "output_size"],
                default="1024x1024",
            ),
            # Chat-style API
            UserSelectableParam(
                name="temperature",
                allowed_values="0.0 - 2.0",
                affects=["output_style"],
                default="1.0",
            ),
            # Code-gen-style API
            UserSelectableParam(
                name="language",
                allowed_values="python | typescript | rust | go",
                affects=["output_shape"],
                default="python",
            ),
        ],
    )
    def test_works_across_every_api_class(self, param):
        """UserSelectableParam must model knobs from any domain, not just generative."""
        assert param.name
        assert param.allowed_values
        assert all(
            a in {"quality", "cost", "latency", "output_size", "output_shape", "output_style"}
            for a in param.affects
        )

    def test_default_is_optional(self):
        # Some APIs have undocumented defaults; the field must allow None.
        p = UserSelectableParam(name="seed", allowed_values="int")
        assert p.default is None
        assert p.affects == []


class TestScreenedCandidateGenerality:
    def _base_screened_candidate(self, **overrides) -> ScreenedCandidate:
        defaults = dict(
            name="X",
            provider="Y",
            description="",
            pricing_model="freemium",
            claimed_capabilities=[],
            relevance_score=0.5,
            adoption_difficulty="easy",
            relevant_subtasks=[],
            source="",
            verified_api_docs_url="https://example.com/docs",
            auth_method="api_key",
            api_access_method="free_signup",
            confirmed_capabilities=[],
            data_format_notes="",
            screening_notes="",
        )
        defaults.update(overrides)
        return ScreenedCandidate(**defaults)

    def test_upstream_provider_accepts_any_string(self):
        """No enum — any consistent lowercase string is valid."""
        for upstream in [
            "openai",
            "anthropic",
            "xai",
            "deepseek",
            "groq",
            "mistral",
            "aleph_alpha",
            "self",
            None,
        ]:
            c = self._base_screened_candidate(upstream_provider=upstream)
            assert c.upstream_provider == upstream

    def test_interaction_model_defaults_empty(self):
        c = self._base_screened_candidate()
        assert isinstance(c.interaction_model, InteractionModel)
        assert not c.interaction_model.synchronous

    def test_interaction_model_round_trips_multiple_flags(self):
        c = self._base_screened_candidate(
            interaction_model=InteractionModel(
                synchronous=True, async_polling=True, batch_file=True
            )
        )
        j = c.model_dump_json()
        back = ScreenedCandidate.model_validate_json(j)
        assert back.interaction_model.synchronous is True
        assert back.interaction_model.async_polling is True
        assert back.interaction_model.batch_file is True

    def test_user_selectable_params_list_defaults_empty(self):
        c = self._base_screened_candidate()
        assert c.user_selectable_params == []

    def test_user_selectable_params_accept_heterogeneous_domains(self):
        c = self._base_screened_candidate(
            user_selectable_params=[
                UserSelectableParam(name="temperature", allowed_values="0.0-2.0"),
                UserSelectableParam(name="language", allowed_values="en | fr | de"),
                UserSelectableParam(name="enable_tables", allowed_values="true | false"),
            ]
        )
        assert len(c.user_selectable_params) == 3


class TestInteractionPatternHintCoercion:
    """_normalize_coverage must coerce unknown hint strings to 'unknown'."""

    def _candidate(self, hint: str, covers: set[str] | None = None) -> Candidate:
        return Candidate(
            name="C",
            provider="P",
            description="",
            api_available=True,
            pricing_model="freemium",
            claimed_capabilities=[],
            relevance_score=0.5,
            adoption_difficulty="easy",
            relevant_subtasks=[],
            source="",
            api_interaction_pattern_hint=hint,
            covers_step_ids=frozenset(covers or set()),
        )

    @pytest.mark.parametrize(
        "hint",
        ["sync", "async_polling", "other", "unknown"],
    )
    def test_valid_vocabulary_preserved(self, hint):
        c = self._candidate(hint, covers={"step_1"})
        out = _normalize_coverage([c], ["step_1"])
        assert out[0].api_interaction_pattern_hint == hint

    @pytest.mark.parametrize(
        "bad_hint",
        [
            "ASYNC",  # wrong case
            "streaming",  # not in vocab
            "polls",  # typo
            "",  # empty
            "sync/async",  # combined
        ],
    )
    def test_unknown_strings_coerced(self, bad_hint):
        c = self._candidate(bad_hint, covers={"step_1"})
        out = _normalize_coverage([c], ["step_1"])
        assert out[0].api_interaction_pattern_hint == "unknown"


class TestPromptHasGeneralPrinciples:
    """Prompts must teach principles, not prescribe cases. These tests are
    heuristic guards: if someone re-introduces a hardcoded brand list or a
    single-domain carve-out in a prompt, these tests fail loudly."""

    def test_research_prompt_has_class_separation_principle(self):
        from puzzleeval.agents.research import RESEARCH_SYSTEM_PROMPT as P
        # The generalized principle must be present
        assert "Candidate-class separation" in P or "Developer primitive" in P
        assert "Packaged product" in P

    def test_research_prompt_no_hardcoded_chatbot_carveout(self):
        from puzzleeval.agents.research import RESEARCH_SYSTEM_PROMPT as P
        # The old bandaid specifically named Intercom/Drift/Ada/Zendesk/Tidio.
        # The refactored prompt uses illustrative capabilities without brand names.
        forbidden_brands = ["Intercom Fin", "Drift", "Zendesk AI", "Tidio"]
        for brand in forbidden_brands:
            assert brand not in P, (
                f"Research prompt still hardcodes '{brand}'. Use a principle, "
                f"not a case-based carve-out."
            )

    # Deep-verify prompt tests removed — Agent 4 no longer runs a deep-verify
    # loop. Agent 4 is shallow verify (exists / blocked); Agent 5 does its own
    # Phase-1 research and writes api_spec.txt.
