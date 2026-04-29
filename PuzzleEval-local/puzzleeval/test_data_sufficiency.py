"""Test data sufficiency analyzer + 3-tier fallback ladder.

Today's pipeline implicitly handles "user has no test files" with a single
fallback (Agent 3F → Agent 3 text-only synthesis). That's the right move
when files are completely absent, but it conflates several distinct
conditions:

  1. **Truly absent**           — user uploaded zero files; we synthesize text
  2. **Insufficient quantity**  — user uploaded 1 invoice but coverage matrix
                                  needs 6 dimensions; we should ASK for more
  3. **Wrong type**             — user uploaded .docx but the scope is OCR
                                  (image/PDF only)
  4. **Inadequate variety**     — 5 invoices all from the SAME vendor; tests
                                  won't generalize
  5. **Plugin can synthesize**  — audio_content scope + TTS plugin available
                                  → generate audio test inputs from text
  6. **Plugin cannot synthesize** — audio_content scope but no TTS keys →
                                    fall back to text proxies + degrade signal

This module turns those into FIRST-CLASS conditions with a structured
verdict (`SufficiencyVerdict`) and a deterministic action ladder
(`recommend_action`). The pipeline reads the verdict and:

  - **READY**         — proceed with current files
  - **AUGMENT**       — synthesize via plugin (TTS, image gen) + use real files
  - **SYNTHESIZE**    — no files; fall back to text/synthetic-only
  - **REQUEST_MORE**  — pause and ask the user for more / different files
  - **DEGRADE**       — note in verdict, proceed with reduced confidence

Order of resolution from cheapest → most disruptive:
    READY < AUGMENT < SYNTHESIZE < DEGRADE < REQUEST_MORE

This module is PURE: it never calls Claude, never hits the network. It
only inspects file paths + scope specs + plugin availability. The pipeline
calls `assess_sufficiency()` BEFORE Agent 3F runs, surfaces the verdict in
SSE events, and chooses the action accordingly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Modality groups: which file extensions are appropriate for which
# scope output_types. Drives the "wrong type" detection. Kept narrow on
# purpose — adding modalities means adding a row here, not changing logic.
_MODALITY_FILE_EXTS: dict[str, frozenset[str]] = {
    # OCR / vision / structured extraction from documents
    "structured_json": frozenset({
        ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif",
        ".webp", ".gif", ".bmp", ".docx", ".doc", ".csv",
        ".xlsx", ".xls", ".txt", ".json", ".xml", ".html",
    }),
    "extraction": frozenset({
        ".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif",
        ".webp", ".docx", ".doc", ".csv", ".txt", ".json",
    }),
    "classification": frozenset({
        ".pdf", ".png", ".jpg", ".jpeg", ".txt", ".csv",
        ".docx", ".html", ".json",
    }),
    # Vision / image generation evaluation
    "media_url": frozenset({
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3",
        ".wav", ".m4a", ".ogg", ".mp4", ".mov", ".webm",
    }),
    # Audio responses
    "audio_content": frozenset({
        ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac",
    }),
    # Code generation - source files
    "code": frozenset({
        ".py", ".js", ".ts", ".tsx", ".jsx", ".go",
        ".rs", ".sh", ".bash", ".java", ".rb", ".cpp", ".c",
    }),
    # Free-text / chatbot / structured action — usually no files needed,
    # but accept anything readable
    "free_text": frozenset({
        ".txt", ".md", ".json", ".csv", ".html",
    }),
    "action": frozenset({  # external write — files are usually requests, not "tests"
        ".json", ".txt", ".csv",
    }),
    # New plugin-modality output types: files are usually request templates
    # / sample payloads / sample audio, never fixtures the agent reads
    # directly. Accept the obvious shapes; reject nothing.
    "webhook_callback": frozenset({".json", ".txt", ".xml", ".html"}),
    "outbound_message": frozenset({".json", ".txt", ".eml", ".html", ".csv"}),
    "voice_turn": frozenset({
        ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac",
        ".txt",  # caller-script transcripts
    }),
}


# Minimum recommended file count per scope. The 6-dimension coverage matrix
# in Agent 3 (happy_path / input_variation / edge_case / scale /
# domain_specific / error_resilience) reads best with at least 1 file per
# dimension where files are the input. Below this we still RUN, but flag
# DEGRADE so the user knows precision is limited.
DEFAULT_MIN_FILES = int(os.environ.get("PUZZLEEVAL_MIN_FILES_PER_SCOPE", "3"))
IDEAL_MIN_FILES = int(os.environ.get("PUZZLEEVAL_IDEAL_FILES_PER_SCOPE", "6"))

# Variety threshold: files with the same extension (e.g. all .pdf) AND
# names that share a long common prefix (e.g. "invoice_001.pdf",
# "invoice_002.pdf") look like one source. We don't reject — we just note.
VARIETY_PREFIX_LEN = 6


# ---------------------------------------------------------------------------
# Verdict types
# ---------------------------------------------------------------------------


@dataclass
class FileInventory:
    """A scan of what the user uploaded.

    Pure data class — populated by `inventory_files()` from a list of
    paths. No file content is read; we trust extensions and stat() only.
    """
    total_count: int = 0
    by_extension: dict[str, int] = field(default_factory=dict)
    total_bytes: int = 0
    missing_paths: list[str] = field(default_factory=list)
    suspected_single_source: bool = False  # all share long common prefix

    @property
    def extension_count(self) -> int:
        return len(self.by_extension)


@dataclass
class SufficiencyVerdict:
    """One scope's data-readiness verdict.

    ``action`` is the recommended path forward. The pipeline branches on
    this and on the populated metadata fields (e.g. for AUGMENT, which
    plugin can manufacture inputs).
    """

    scope_id: str
    output_type: str
    input_type: str
    inventory: FileInventory

    action: str  # "ready" | "augment" | "synthesize" | "request_more" | "degrade"
    reason: str
    plugin_for_augment: str | None = None  # e.g. "tts", "vision"
    request_message: str | None = None  # human-readable ask when action=request_more
    degraded_confidence: float = 1.0  # 0.0–1.0; <1.0 when DEGRADE; reported in summary

    advisories: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _safe_stat(path: str) -> int:
    """Return file size in bytes, or 0 if unreadable. Never raises."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def inventory_files(paths: list[str] | None) -> FileInventory:
    """Scan a list of file paths into a structured inventory.

    Files that don't exist on disk are recorded as ``missing_paths``;
    they don't contribute to counts. The caller can decide whether
    missing files count toward DEGRADE / REQUEST_MORE.
    """
    inv = FileInventory()
    if not paths:
        return inv
    by_ext: dict[str, int] = {}
    stems: list[str] = []
    for p in paths:
        if not os.path.exists(p):
            inv.missing_paths.append(p)
            continue
        ext = Path(p).suffix.lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
        stems.append(Path(p).stem)
        inv.total_count += 1
        inv.total_bytes += _safe_stat(p)
    inv.by_extension = by_ext
    # Heuristic: 3+ files all sharing a 6-char common prefix → likely
    # one batch from one source. Doesn't block; just notes.
    if len(stems) >= 3:
        prefix = os.path.commonprefix(stems)
        if len(prefix) >= VARIETY_PREFIX_LEN:
            inv.suspected_single_source = True
    return inv


# ---------------------------------------------------------------------------
# Plugin availability lookup (decoupled — accepts a list of plugin names
# that the registry has confirmed available, so this module doesn't need
# to import the registry directly and unit tests can pass a fake list).
# ---------------------------------------------------------------------------


def _plugin_for_augmentation(
    output_type: str,
    available_plugin_names: set[str],
) -> str | None:
    """Pick the synthesis-capable plugin for this output_type.

    Returns the plugin name string when one is available, or None when
    no available plugin can manufacture inputs of this modality.

    The lookup is hard-coded to the bundled plugins. Adding a new
    synthesizer means adding a row here AND registering the plugin —
    we want this resolver to fail-fast at code review when modalities
    drift, not silently pick "any plugin."
    """
    candidates_by_output: dict[str, tuple[str, ...]] = {
        "audio_content": ("tts",),
        "media_url": ("tts", "vision"),  # vision can sanity-check images
        "code": ("code_execution",),
        # Direct plugin-modality output types — these route 1:1.
        "webhook_callback": ("webhook_receiver",),
        "outbound_message": ("outbound_delivery",),
        "voice_turn": ("voice_realtime",),
        # Generic shapes that often map to plugin work — last resort matches.
        # If a clearer enum (e.g. webhook_callback) is available, use that;
        # these "structured_json" / "action" rows catch legacy blueprints
        # that pre-date the new plugin-modality enum values.
        "structured_json": ("webhook_receiver",),
        "action": ("outbound_delivery",),
    }
    for cand in candidates_by_output.get(output_type, ()):
        if cand in available_plugin_names:
            return cand
    return None


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def assess_sufficiency(
    *,
    scope_id: str,
    input_type: str,
    output_type: str,
    requires_test_files: bool,
    file_paths: list[str] | None,
    available_plugin_names: set[str] | None = None,
    min_files: int | None = None,
) -> SufficiencyVerdict:
    """Decide the action for one scope based on uploaded files + plugins.

    Decision ladder (cheapest first):
      0. requires_test_files=False → READY (no files needed)
      1. file_count >= ideal → READY
      2. file_count >= min, variety acceptable → READY
      3. file_count > 0 but type mismatch → DEGRADE with advisory
      4. file_count == 0 but synth plugin available → AUGMENT
      5. file_count == 0 and no synth plugin → SYNTHESIZE (text fallback)
      6. file_count > 0 but < min → REQUEST_MORE (with concrete ask)
    """
    available_plugin_names = available_plugin_names or set()
    min_files = min_files if min_files is not None else DEFAULT_MIN_FILES

    inv = inventory_files(file_paths)

    # Step 0 — text-only scope, no files needed
    if not requires_test_files:
        return SufficiencyVerdict(
            scope_id=scope_id, output_type=output_type, input_type=input_type,
            inventory=inv, action="ready",
            reason="scope does not require test files (text-based)",
        )

    valid_exts = _MODALITY_FILE_EXTS.get(output_type, frozenset())
    type_mismatch_count = 0
    type_match_count = 0
    for ext, count in inv.by_extension.items():
        if valid_exts and ext not in valid_exts:
            type_mismatch_count += count
        else:
            type_match_count += count

    # Step 1/2 — sufficient real files
    if inv.total_count >= IDEAL_MIN_FILES and type_match_count >= min_files:
        verdict = SufficiencyVerdict(
            scope_id=scope_id, output_type=output_type, input_type=input_type,
            inventory=inv, action="ready",
            reason=(
                f"{inv.total_count} files uploaded, "
                f"{type_match_count} match {output_type} modality"
            ),
        )
        if inv.suspected_single_source:
            verdict.advisories.append(
                "all uploaded files share a common prefix — variety may be limited; "
                "test results may not generalize beyond this batch source"
            )
        return verdict

    if type_match_count >= min_files:
        verdict = SufficiencyVerdict(
            scope_id=scope_id, output_type=output_type, input_type=input_type,
            inventory=inv, action="ready",
            reason=(
                f"{type_match_count} files match {output_type} modality "
                f"(below ideal {IDEAL_MIN_FILES} but at min {min_files})"
            ),
            degraded_confidence=0.8,
        )
        verdict.advisories.append(
            f"upload {IDEAL_MIN_FILES - type_match_count} more sample(s) "
            f"to reach ideal coverage matrix depth"
        )
        return verdict

    # Step 3 — files present but wrong type for this modality
    if inv.total_count > 0 and type_match_count == 0:
        bad_exts = sorted(inv.by_extension.keys())
        good_exts = sorted(valid_exts)[:6] if valid_exts else ["(any)"]
        return SufficiencyVerdict(
            scope_id=scope_id, output_type=output_type, input_type=input_type,
            inventory=inv, action="request_more",
            reason=(
                f"uploaded files have extensions {bad_exts} but "
                f"{output_type} expects one of {good_exts}"
            ),
            request_message=(
                f"This scope tests {output_type} responses. The files you "
                f"uploaded ({', '.join(bad_exts)}) aren't a fit. Please add "
                f"sample files in: {', '.join(good_exts)}."
            ),
            degraded_confidence=0.4,
        )

    # Step 4 — no files but a plugin can synthesize them
    plugin_name = _plugin_for_augmentation(output_type, available_plugin_names)
    if inv.total_count == 0 and plugin_name is not None:
        return SufficiencyVerdict(
            scope_id=scope_id, output_type=output_type, input_type=input_type,
            inventory=inv, action="augment",
            plugin_for_augment=plugin_name,
            reason=(
                f"no files uploaded; {plugin_name} plugin will synthesize "
                f"test inputs of type {output_type}"
            ),
            degraded_confidence=0.85,
            advisories=[
                f"synthesized inputs are representative but not real-world; "
                f"upload sample {output_type} files for higher-confidence scoring"
            ],
        )

    # Step 5 — no files, no synth plugin → text fallback (Agent 3 default)
    if inv.total_count == 0:
        return SufficiencyVerdict(
            scope_id=scope_id, output_type=output_type, input_type=input_type,
            inventory=inv, action="synthesize",
            reason=(
                f"no files uploaded and no synthesis plugin available for "
                f"{output_type}; falling back to text-described tests"
            ),
            degraded_confidence=0.6,
            advisories=[
                "tests will use text-described inputs (no real files); "
                "candidates that strictly require file uploads will return "
                "INCOMPATIBLE — that's a valid signal, not a bug"
            ],
        )

    # Step 6 — some valid files but below min → ask for more
    needed = min_files - type_match_count
    return SufficiencyVerdict(
        scope_id=scope_id, output_type=output_type, input_type=input_type,
        inventory=inv, action="request_more",
        reason=(
            f"{type_match_count}/{min_files} valid {output_type} files; "
            f"need {needed} more for minimum coverage"
        ),
        request_message=(
            f"Please upload {needed} more sample {output_type} file(s) "
            f"({', '.join(sorted(valid_exts)[:5]) or 'any format'}) so we "
            f"can build a representative test set. Aim for variety: "
            f"different sources, different sizes, different content."
        ),
        degraded_confidence=0.5,
    )


def assess_test_plan(
    test_plan: Any,
    *,
    file_paths_by_scope: dict[str, list[str]] | None = None,
    requires_files_by_scope: dict[str, bool] | None = None,
    available_plugin_names: set[str] | None = None,
) -> dict[str, SufficiencyVerdict]:
    """Assess every scope in a TestPlan and return a per-scope verdict.

    ``file_paths_by_scope`` keys are scope_ids; values are file paths the
    user uploaded for that scope. Falsy → no files for that scope.

    ``requires_files_by_scope`` lets the caller override the default
    requires_test_files derivation (e.g. when Agent 1 set it on a
    SubTask but Phase 9's scope spec didn't carry it forward yet).
    """
    out: dict[str, SufficiencyVerdict] = {}
    if test_plan is None:
        return out
    file_paths_by_scope = file_paths_by_scope or {}
    requires_files_by_scope = requires_files_by_scope or {}
    available_plugin_names = available_plugin_names or set()

    specs = getattr(test_plan, "scope_specs", None) or []
    for spec in specs:
        scope_id = getattr(spec, "scope_id", "")
        if not scope_id:
            continue
        input_type = getattr(spec, "input_type", "")
        output_type = getattr(spec, "output_type", "")
        # Default: file-requiring modalities get requires=True; others False.
        # Caller can override via the explicit map.
        requires_default = input_type in {
            "document_content", "image_description",
            "audio_content", "file_reference",
        }
        requires_test_files = requires_files_by_scope.get(
            scope_id, requires_default,
        )
        out[scope_id] = assess_sufficiency(
            scope_id=scope_id,
            input_type=input_type,
            output_type=output_type,
            requires_test_files=requires_test_files,
            file_paths=file_paths_by_scope.get(scope_id, []),
            available_plugin_names=available_plugin_names,
        )
    return out


def summarize_verdicts(verdicts: dict[str, SufficiencyVerdict]) -> dict[str, Any]:
    """Build a summary dict for SSE events / pipeline_summary.json.

    Includes counts by action, the lowest confidence across all scopes,
    and a flat list of advisories so the UI can render them once.
    """
    by_action: dict[str, int] = {}
    advisories: list[str] = []
    request_messages: list[dict[str, str]] = []
    min_confidence = 1.0
    for v in verdicts.values():
        by_action[v.action] = by_action.get(v.action, 0) + 1
        advisories.extend(v.advisories)
        if v.degraded_confidence < min_confidence:
            min_confidence = v.degraded_confidence
        if v.action == "request_more" and v.request_message:
            request_messages.append({
                "scope_id": v.scope_id, "message": v.request_message,
            })
    needs_user_action = "request_more" in by_action
    return {
        "by_action": by_action,
        "min_confidence": min_confidence,
        "advisories": advisories,
        "request_messages": request_messages,
        "needs_user_action": needs_user_action,
        "total_scopes": len(verdicts),
    }


__all__ = [
    "DEFAULT_MIN_FILES",
    "FileInventory",
    "IDEAL_MIN_FILES",
    "SufficiencyVerdict",
    "assess_sufficiency",
    "assess_test_plan",
    "inventory_files",
    "summarize_verdicts",
]
