"""Regression guards for the Agent 4 → Agent 5 doc-prefetch handoff.

Two concurrent handoff mechanisms exist in this codebase:

  1. **Filesystem prefetch handoff** (was): Agent 4 fetches a docs URL,
     extracts text, writes `fetched_docs_*.txt` to the candidate's
     future sandbox dir. Agent 5 enumerates them later. The shared
     path helper `candidate_sandbox_dir(trace_id, name)` is the contract.

  2. **Structured checklist handoff** (new — this pass): Agent 4
     populates a `BuildReadinessChecklist` per candidate (provider
     surface + ten build-readiness fields), parsed deterministically
     from the verification findings via
     `_extract_checklist_from_findings`. The checklist becomes
     `ScreenedCandidate.checklist` and is the load-bearing artifact for
     Agent 5's Phase A inventory + Phase B gap analysis.

Both must stay wired. The prefetched docs are background reading; the
checklist is the contract. Tests below lock both.

Reference: PLAN_AGENT5_RESEARCH_AGENCY.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCREENING_SRC = ((ROOT / "puzzleeval" /"agents" / "agent4" / "core.py").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent4" / "templates" / "verification_system.md").read_text(encoding="utf-8") + chr(10) + (ROOT / "puzzleeval" /"agents" / "agent4" / "templates" / "structure_system.md").read_text(encoding="utf-8"))
# Phase 5 Step 3 note: ``_build_single_harness`` moved to
# ``puzzleeval/agents/agent5/build_loop.py``. Source-grep tests that
# look for the function body (count_existing_fetched_docs seeding,
# while-loop start, etc.) need build_loop.py text included alongside
# implement_test_env.py.
IMPL_SRC = (
    (ROOT / "puzzleeval" / "agents" / "implement_test_env.py").read_text(encoding="utf-8")
    + "\n# === build_loop.py boundary ===\n"
    + (ROOT / "puzzleeval" / "agents" / "agent5" / "build_loop.py").read_text(encoding="utf-8")
)


# ============================================================================
# 1. screening.py persists fetched docs after verification
# ============================================================================


class TestScreeningPersistsFetchedDocs:

    def test_screening_imports_shared_helper(self):
        assert "from puzzleeval.web_doc_cache import" in SCREENING_SRC
        assert "save_web_fetches_to_sandbox" in SCREENING_SRC
        assert "candidate_sandbox_dir" in SCREENING_SRC

    def test_screening_calls_helper_inside_verify_loop(self):
        # The call must happen inside _verify_single_candidate, after
        # the response is received. We locate the function and check
        # the helper call lives inside it.
        fn_start = SCREENING_SRC.find("def _verify_single_candidate")
        assert fn_start != -1
        next_fn = SCREENING_SRC.find("\ndef ", fn_start + 1)
        fn_body = SCREENING_SRC[fn_start:next_fn if next_fn != -1 else len(SCREENING_SRC)]
        assert "save_web_fetches_to_sandbox" in fn_body, (
            "The persist call must be INSIDE _verify_single_candidate — "
            "otherwise it never runs during per-candidate verification."
        )
        assert "candidate_sandbox_dir" in fn_body

    def test_screening_uses_trace_id_and_candidate_name(self):
        """The sandbox-dir computation must route by trace_id +
        candidate.name — drift here makes Agent 4 write to one dir
        and Agent 5 read from another."""
        assert "candidate_sandbox_dir(" in SCREENING_SRC
        fn_start = SCREENING_SRC.find("def _verify_single_candidate")
        next_fn = SCREENING_SRC.find("\ndef ", fn_start + 1)
        fn_body = SCREENING_SRC[fn_start:next_fn if next_fn != -1 else len(SCREENING_SRC)]
        call_pos = fn_body.rfind("candidate_sandbox_dir(")
        assert call_pos != -1
        call_block = fn_body[call_pos:call_pos + 300]
        assert "trace_id" in call_block
        assert "candidate.name" in call_block

    def test_screening_handoff_wrapped_in_try_except(self):
        """The doc-handoff call must NEVER crash screening. A disk
        error, perms issue, or malformed response must surface as a
        logged skip, not a candidate rejection."""
        fn_start = SCREENING_SRC.find("def _verify_single_candidate")
        next_fn = SCREENING_SRC.find("\ndef ", fn_start + 1)
        fn_body = SCREENING_SRC[fn_start:next_fn if next_fn != -1 else len(SCREENING_SRC)]
        call_pos = fn_body.find("save_web_fetches_to_sandbox")
        assert call_pos != -1
        preceding = fn_body[max(0, call_pos - 500):call_pos]
        assert "try:" in preceding


# ============================================================================
# 2. Agent 5 seeds its counter from pre-existing files
# ============================================================================


class TestAgent5SeedsFromPrefetchedDocs:

    def test_build_single_harness_calls_count_existing(self):
        assert "count_existing_fetched_docs" in IMPL_SRC
        assert "_prefetched_count" in IMPL_SRC or "prefetched_count" in IMPL_SRC

    def test_seed_happens_before_builder_loop_starts(self):
        """The seed must run BEFORE the first `client.beta.messages.create`
        call in the builder loop, otherwise Agent 5's first web_fetch
        (if any) collides with Agent 4's fetched_docs_0.txt."""
        fn_start = IMPL_SRC.find("def _build_single_harness")
        assert fn_start != -1
        seed_pos = IMPL_SRC.find("count_existing_fetched_docs", fn_start)
        loop_pos = IMPL_SRC.find("while turn <", fn_start)
        assert seed_pos != -1 and loop_pos != -1
        assert seed_pos < loop_pos


# ============================================================================
# 3. Initial message lists pre-fetched docs (ranked, not gated)
# ============================================================================


class TestInitialMessageSurfacesPrefetchedDocs:

    def test_helper_function_exists(self):
        assert "_format_prefetched_docs_block" in IMPL_SRC

    def test_helper_is_called_from_initial_message(self):
        fn_start = IMPL_SRC.find("def _build_initial_message")
        next_fn = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:next_fn if next_fn != -1 else len(IMPL_SRC)]
        assert "_format_prefetched_docs_block" in fn_body

    def test_sandbox_dir_threaded_into_initial_message_call(self):
        # Phase 5 Step 3: function body lives in build_loop.py (the
        # canonical owner). IMPL_SRC includes both files; search for the
        # canonical name `def build_single_harness` (no leading `_`).
        # The implement_test_env.py shim is irrelevant for this test —
        # it doesn't call _build_initial_message.
        fn_start = IMPL_SRC.find("def build_single_harness")
        assert fn_start != -1, (
            "build_single_harness must exist in build_loop.py — IMPL_SRC "
            "includes that file alongside implement_test_env.py."
        )
        next_fn = IMPL_SRC.find("\ndef ", fn_start + 1)
        fn_body = IMPL_SRC[fn_start:next_fn if next_fn != -1 else len(IMPL_SRC)]
        call_pos = fn_body.find("_build_initial_message(")
        assert call_pos != -1
        call_block = fn_body[call_pos:call_pos + 400]
        assert "sandbox_dir=" in call_block


class TestPrefetchedDocsBlockRendersInventory:
    """The new block lists files by usefulness signal (descending) but
    does NOT inline content, does NOT tier into HIGH/MEDIUM/THIN, does
    NOT branch instructions. The checklist is the load-bearing artifact;
    these files are background reading material."""

    def _helper(self):
        from puzzleeval.agents.implement_test_env import _format_prefetched_docs_block
        return _format_prefetched_docs_block

    def test_empty_string_when_sandbox_dir_is_none(self):
        helper = self._helper()
        assert helper(None) == ""

    def test_empty_string_when_no_prefetched_files(self, tmp_path):
        helper = self._helper()
        assert helper(tmp_path) == ""

    def test_renders_list_of_files_with_source_url(self, tmp_path):
        """The new block lists every prefetched file with its source URL
        in an inventory section — no content inlining."""
        helper = self._helper()
        content = (
            "# Fetched from: https://api.example.com/docs\n"
            "# Saved for reference during build phase\n\n"
            "## API\n\n"
            "POST /v1/endpoint\n"
            "Authorization: Bearer\n"
            "```python\nrequests.post(...)\n```"
        )
        (tmp_path / "fetched_docs_0.txt").write_text(content, encoding="utf-8")
        rendered = helper(tmp_path)
        # New block title
        assert "Prefetched Documentation Inventory" in rendered
        # Filename surfaces in inventory
        assert "fetched_docs_0.txt" in rendered
        # Source URL surfaces
        assert "https://api.example.com/docs" in rendered

    def test_block_explains_when_to_read_files(self, tmp_path):
        """The new block tells the builder when to read the files (Phase A
        / Phase C) and points to the checklist as the load-bearing
        artifact — not as a passive inventory."""
        helper = self._helper()
        (tmp_path / "fetched_docs_0.txt").write_text(
            "# Fetched from: https://x.com\n\nAnything", encoding="utf-8",
        )
        rendered = helper(tmp_path)
        # Phase A or Phase C is named
        assert "Phase A" in rendered or "Phase C" in rendered
        # Checklist is named as the load-bearing artifact
        assert "checklist" in rendered.lower()

    def test_robust_to_unreadable_file(self, tmp_path):
        """If a file exists but its first line is unreadable, the
        rendered block still lists the filename."""
        helper = self._helper()
        filepath = tmp_path / "fetched_docs_0.txt"
        filepath.write_text("# no recognizable header\n", encoding="utf-8")
        rendered = helper(tmp_path)
        assert "fetched_docs_0.txt" in rendered


# ============================================================================
# 4. Phase 1 prompt teaches the five-phase flow (replaces old STEP teaching)
# ============================================================================


class TestPhase1TeachesFivePhaseFlow:
    """Phase 1's research flow used to be five sub-phases (A-E); the
    prompt-refactor collapsed them into three canonical steps (Inventory →
    Gap analysis → Fill+commit). The CONTRACT — checklist-first inventory,
    prefetched-docs awareness, gap-driven research — is preserved."""

    def _prompt(self) -> str:
        from puzzleeval.agents.implement_test_env import BUILDER_SYSTEM_PROMPT
        return BUILDER_SYSTEM_PROMPT

    def test_phase_1_teaches_three_step_flow(self):
        """Phase 1 is structured around three canonical steps."""
        prompt = self._prompt()
        for step in ("Step 1 — Inventory", "Step 2 — Gap analysis", "Step 3 — Fill the gaps"):
            assert step in prompt, f"{step!r} missing from prompt"

    def test_inventory_step_directs_builder_to_checklist(self):
        prompt = self._prompt()
        idx = prompt.find("Step 1 — Inventory")
        idx_end = prompt.find("Step 2 — Gap analysis", idx)
        section = prompt[idx:idx_end] if idx_end > 0 else prompt[idx:idx + 1000]
        assert "checklist" in section.lower() or "BuildReadinessChecklist" in section

    def test_phase1_references_prefetched_docs(self):
        """Phase 1 should still point at the prefetched docs Agent 4
        produced; the inventory step is the natural place but the
        reference may also live in the FAST-PATH preamble."""
        prompt = self._prompt()
        idx = prompt.find("PHASE 1: RESEARCH")
        idx_end = prompt.find("PHASE 2", idx)
        section = prompt[idx:idx_end] if idx_end > 0 else prompt[idx:]
        assert (
            "fetched_docs_" in section
            or "prefetched" in section.lower()
            or "Agent 4" in section
        )


# ============================================================================
# 5. ScreenedCandidate.checklist is wired end-to-end
# ============================================================================


class TestChecklistAttachmentEndToEnd:
    """The checklist must flow from verification findings → parser →
    ScreenedCandidate.checklist. Plus the post-process must handle the
    sentinel cases (system_failure) without rejecting the candidate."""

    def _findings_block(self, all_confirmed: bool = True) -> str:
        status = "confirmed" if all_confirmed else "unknown"
        value = '"value": "x"' if all_confirmed else '"reasoning": "not in docs"'
        body = json.dumps({
            "provider_surface": [
                {"name": "POST /v1/foo", "purpose": "do foo",
                 "relevance_to_use_case": "primary"},
            ],
            "selected_endpoint": "POST /v1/foo",
            "selection_justification": "primary fit",
            "endpoint_path": {"status": status},
            "auth_method": {"status": status},
            "request_body_shape": {"status": status},
            "response_body_shape": {"status": status},
            "auth_refresh": {"status": "unknown"},
            "error_response_schema": {"status": "unknown"},
            "rate_limit_signal": {"status": "unknown"},
            "async_pattern": {"status": "unknown"},
            "content_type_quirks": {"status": "unknown"},
            "sandbox_availability": {"status": "unknown"},
        })
        # Ensure we use the right status / sentinel value placeholders for
        # confirmed = "value", unknown = "reasoning"
        if all_confirmed:
            body = body.replace('"status": "confirmed"',
                                '"status": "confirmed", "value": "x", "source_url": "https://docs.x.com"')
        return (
            "CANDIDATE: TestCo\n"
            "DETERMINATION: PASS\n"
            "EVIDENCE: docs found\n\n"
            "```json BUILD_READINESS_CHECKLIST\n"
            f"{body}\n"
            "```\n"
        )

    def test_extractor_attaches_verified_pass_checklist(self):
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        findings = self._findings_block(all_confirmed=True)
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert c.is_verified_pass()
        assert c.populated_by == "agent_4"
        assert c.has_provider_surface()

    def test_extractor_attaches_inconclusive_checklist(self):
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        findings = self._findings_block(all_confirmed=False)
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert not c.is_verified_pass()
        # Still populated_by agent_4 — not rejected
        assert c.populated_by == "agent_4"

    def test_extractor_returns_sentinel_on_missing_block(self):
        from puzzleeval.agents.agent4.core import _extract_checklist_from_findings
        findings = "CANDIDATE: TestCo\nDETERMINATION: PASS\nNo block.\n"
        c = _extract_checklist_from_findings(findings, "TestCo")
        assert c.populated_by == "system_failure"

    def test_attach_helper_logs_per_candidate(self, tmp_path):
        """_attach_checklists_to_result must walk the validated
        candidates, attach the checklist, and log per candidate."""
        from puzzleeval.agents.agent4.core import _attach_checklists_to_result
        from puzzleeval.schemas import (
            Agent4Result, ScreenedCandidate,
        )
        import logging

        sc = ScreenedCandidate(
            name="TestCo",
            provider="TestProvider",
            description="d",
            pricing_model="per-token",
            claimed_capabilities=["foo"],
            relevance_score=0.9,
            adoption_difficulty="easy",
            relevant_subtasks=["foo"],
            source="https://x.com",
            verified_api_docs_url="https://docs.x.com",
            auth_method="bearer_token",
            api_access_method="free_signup",
            confirmed_capabilities=["foo"],
            data_format_notes="JSON",
            screening_notes="ok",
        )
        result = Agent4Result(
            validated_candidates=[sc],
            rejected_candidates=[],
            screening_summary="ok",
            total_candidates_screened=1,
        )
        findings = self._findings_block(all_confirmed=True)
        logger = logging.getLogger("test")
        _attach_checklists_to_result(
            result, {"TestCo": findings}, logger, "trace-x",
        )
        assert result.validated_candidates[0].checklist is not None
        assert result.validated_candidates[0].checklist.is_verified_pass()

    def test_attach_helper_uses_sentinel_on_missing_findings(self):
        """When a validated candidate's name doesn't appear in the
        findings dict (catastrophic structuring failure), the post-
        process attaches the sentinel and the candidate is NOT
        rejected."""
        from puzzleeval.agents.agent4.core import _attach_checklists_to_result
        from puzzleeval.schemas import (
            Agent4Result, ScreenedCandidate,
        )
        import logging

        sc = ScreenedCandidate(
            name="MissingCo", provider="X", description="y",
            pricing_model="per-token", claimed_capabilities=[],
            relevance_score=0.5, adoption_difficulty="easy",
            relevant_subtasks=[], source="https://x.com",
            verified_api_docs_url="https://docs.x.com",
            auth_method="unknown", api_access_method="unknown",
            confirmed_capabilities=[], data_format_notes="",
            screening_notes="",
        )
        result = Agent4Result(
            validated_candidates=[sc],
            rejected_candidates=[],
            screening_summary="ok",
            total_candidates_screened=1,
        )
        logger = logging.getLogger("test")
        _attach_checklists_to_result(result, {}, logger, "trace-x")
        assert result.validated_candidates[0].checklist is not None
        assert result.validated_candidates[0].checklist.populated_by == "system_failure"


# ============================================================================
# End-to-end behavioral: files written by Agent 4 are found by Agent 5
# ============================================================================


def _mock_fetch_response(url: str, text: str):
    source = SimpleNamespace(data=text)
    document = SimpleNamespace(source=source)
    fetch_block = SimpleNamespace(url=url, content=document)
    tool_result = SimpleNamespace(type="web_fetch_tool_result", content=fetch_block)
    return SimpleNamespace(content=[tool_result])


class TestEndToEndHandoffViaFilesystem:
    """The highest-value test: simulate Agent 4 saving docs, then
    verify Agent 5's helpers see them on the same path. Catches the
    most common regression (path drift between writer and reader)."""

    def test_agent4_saves_then_agent5_sees(self, tmp_path):
        from puzzleeval.web_doc_cache import (
            candidate_sandbox_dir,
            count_existing_fetched_docs,
            save_web_fetches_to_sandbox,
        )
        trace_id = "trace-handoff-test"
        candidate_name = "ElevenLabs Conversational AI"

        # Simulate Agent 4 save
        agent4_dir = candidate_sandbox_dir(trace_id, candidate_name, runs_root=tmp_path)
        resp = _mock_fetch_response(
            "https://elevenlabs.io/docs/convai",
            "WebSocket wss://...\nAuthorization: xi-api-key: $KEY",
        )
        save_web_fetches_to_sandbox(resp, agent4_dir)

        # Simulate Agent 5 looking at the same path (via same helper)
        agent5_dir = candidate_sandbox_dir(trace_id, candidate_name, runs_root=tmp_path)
        assert agent4_dir == agent5_dir, (
            "Path drift between Agent 4 and Agent 5 — they'd write/read "
            "different directories and the handoff would silently break."
        )
        assert count_existing_fetched_docs(agent5_dir) == 1
        content = (agent5_dir / "fetched_docs_0.txt").read_text(encoding="utf-8")
        assert "WebSocket wss://" in content
