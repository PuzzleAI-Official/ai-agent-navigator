"""Mechanical code diagnostics for Agent 5 generated Python files.

The registry is intentionally narrow: it checks Python syntax after successful
builder writes/patches, persists active findings, and dedupes diagnostics that
have already been delivered to the model. It never imports generated code or
runs provider SDKs.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REGISTRY_RELATIVE_PATH = "_agent_state/code_diagnostics.json"
REGISTRY_VERSION = 1
SCAFFOLD_PYTHON_FILES = ("harness.py", "smoke_test.py", "live_test.py")
MAX_DELIVERED_FINGERPRINTS = 500


@dataclass
class CodeDiagnosticsResult:
    """Result of one bounded diagnostics run."""

    checked_paths: list[str] = field(default_factory=list)
    new_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    active_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    cleared_paths: list[str] = field(default_factory=list)
    registry_path: str = REGISTRY_RELATIVE_PATH
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_paths": self.checked_paths,
            "new_diagnostics": self.new_diagnostics,
            "active_diagnostics": self.active_diagnostics,
            "cleared_paths": self.cleared_paths,
            "registry_path": self.registry_path,
            "error": self.error,
        }


def _default_registry() -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "files": {},
        "delivered_fingerprints": [],
        "latest_new_diagnostics": [],
        "last_updated_turn": None,
    }


def _registry_path(sandbox_dir: Path) -> Path:
    return sandbox_dir / REGISTRY_RELATIVE_PATH


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


def read_code_diagnostics(sandbox_dir: Path) -> dict[str, Any]:
    """Read the persisted diagnostics registry, returning an empty registry."""

    path = _registry_path(sandbox_dir)
    if not path.exists():
        return _default_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_registry()
    if not isinstance(data, dict):
        return _default_registry()
    registry = _default_registry()
    registry.update(data)
    if not isinstance(registry.get("files"), dict):
        registry["files"] = {}
    if not isinstance(registry.get("delivered_fingerprints"), list):
        registry["delivered_fingerprints"] = []
    if not isinstance(registry.get("latest_new_diagnostics"), list):
        registry["latest_new_diagnostics"] = []
    registry["version"] = REGISTRY_VERSION
    return registry


def _relative_path(sandbox_dir: Path, raw_path: str) -> str | None:
    raw = (raw_path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or (len(raw) > 2 and raw[1] == ":"):
        return None
    if raw.startswith("./"):
        raw = raw[2:]
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return None
    rel = "/".join(parts)
    if not rel.endswith(".py"):
        return None
    sandbox_root = sandbox_dir.resolve()
    target = (sandbox_root / rel).resolve()
    try:
        target.relative_to(sandbox_root)
    except ValueError:
        return None
    return rel


def _fingerprint(item: dict[str, Any]) -> str:
    payload = {
        "path": item.get("path"),
        "kind": item.get("kind"),
        "line": item.get("line"),
        "column": item.get("column"),
        "message": item.get("message"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]


def _syntax_diagnostics(
    sandbox_dir: Path,
    relative_path: str,
    *,
    turn: int,
    source_tool: str,
) -> list[dict[str, Any]]:
    target = sandbox_dir / relative_path
    if not target.exists():
        return []
    try:
        source = target.read_text(encoding="utf-8")
        compile(source, relative_path, "exec", dont_inherit=True)
    except (SyntaxError, IndentationError) as exc:
        diagnostic = {
            "path": relative_path,
            "severity": "error",
            "kind": "syntax",
            "line": int(getattr(exc, "lineno", 0) or 0),
            "column": int(getattr(exc, "offset", 0) or 0),
            "message": str(getattr(exc, "msg", "") or str(exc)),
            "source_tool": source_tool,
            "turn": turn,
        }
        diagnostic["fingerprint"] = _fingerprint(diagnostic)
        return [diagnostic]
    except OSError:
        return []
    return []


def active_code_diagnostics(
    registry: dict[str, Any],
    *,
    paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return active diagnostics, optionally filtered to sandbox-relative paths."""

    items: list[dict[str, Any]] = []
    files = registry.get("files")
    if not isinstance(files, dict):
        return items
    for rel, payload in files.items():
        if paths is not None and str(rel) not in paths:
            continue
        if not isinstance(payload, dict):
            continue
        for item in payload.get("active_diagnostics") or []:
            if isinstance(item, dict):
                items.append(item)
    return sorted(items, key=lambda d: (str(d.get("path")), int(d.get("line") or 0)))


def run_python_diagnostics(
    sandbox_dir: Path,
    paths: list[str] | tuple[str, ...] | set[str],
    *,
    turn: int,
    source_tool: str,
) -> CodeDiagnosticsResult:
    """Run side-effect-free syntax diagnostics for successful Python mutations."""

    registry = read_code_diagnostics(sandbox_dir)
    files = registry.setdefault("files", {})
    delivered = {str(item) for item in registry.get("delivered_fingerprints") or []}
    checked: list[str] = []
    cleared: list[str] = []
    new: list[dict[str, Any]] = []

    try:
        for raw_path in paths:
            rel = _relative_path(sandbox_dir, str(raw_path))
            if rel is None:
                continue
            checked.append(rel)
            previous_active = active_code_diagnostics(registry, paths={rel})
            diagnostics = _syntax_diagnostics(
                sandbox_dir,
                rel,
                turn=turn,
                source_tool=source_tool,
            )
            if previous_active and not diagnostics:
                cleared.append(rel)
            files[rel] = {
                "active_diagnostics": diagnostics,
                "last_checked_turn": turn,
                "last_checked_source_tool": source_tool,
            }
            for item in diagnostics:
                fp = str(item.get("fingerprint") or "")
                if fp and fp not in delivered:
                    new.append(item)
                    delivered.add(fp)
        registry["delivered_fingerprints"] = sorted(delivered)[-MAX_DELIVERED_FINGERPRINTS:]
        registry["latest_new_diagnostics"] = new
        registry["last_updated_turn"] = turn
        registry["version"] = REGISTRY_VERSION
        _atomic_write_json(_registry_path(sandbox_dir), registry)
    except Exception:  # noqa: BLE001 - diagnostics must fail open
        return CodeDiagnosticsResult(
            checked_paths=checked,
            new_diagnostics=[],
            active_diagnostics=active_code_diagnostics(registry),
            cleared_paths=cleared,
            error=traceback.format_exc(limit=2)[-500:],
        )

    return CodeDiagnosticsResult(
        checked_paths=checked,
        new_diagnostics=new,
        active_diagnostics=active_code_diagnostics(registry),
        cleared_paths=cleared,
    )


def format_new_diagnostics_for_tool_result(result: CodeDiagnosticsResult) -> str:
    """Render only newly delivered diagnostics for appending to a tool result."""

    if result.error:
        return ""
    if not result.new_diagnostics:
        return ""
    lines = [
        "",
        "",
        "[code_diagnostics]",
        "New Python syntax diagnostics found. The write/patch succeeded, but fix these before running tests or signaling HARNESS_COMPLETE:",
    ]
    for item in result.new_diagnostics[:8]:
        lines.append(
            "- "
            f"{item.get('path')}:{item.get('line')}:{item.get('column')} "
            f"{item.get('message')}"
        )
    if len(result.new_diagnostics) > 8:
        lines.append(f"- ... {len(result.new_diagnostics) - 8} more diagnostic(s)")
    return "\n".join(lines)


def completion_blocking_diagnostics(sandbox_dir: Path) -> list[dict[str, Any]]:
    """Return active syntax diagnostics for required scaffold Python files."""

    registry = read_code_diagnostics(sandbox_dir)
    return active_code_diagnostics(registry, paths=set(SCAFFOLD_PYTHON_FILES))


def summarize_code_diagnostics(sandbox_dir: Path, *, limit: int = 8) -> dict[str, Any]:
    """Compact summary for build-state and failure-packet context."""

    registry = read_code_diagnostics(sandbox_dir)
    active = active_code_diagnostics(registry)
    return {
        "registry_path": REGISTRY_RELATIVE_PATH,
        "last_updated_turn": registry.get("last_updated_turn"),
        "active_count": len(active),
        "latest_new_count": len(registry.get("latest_new_diagnostics") or []),
        "active_diagnostics": active[:limit],
    }


__all__ = [
    "CodeDiagnosticsResult",
    "REGISTRY_RELATIVE_PATH",
    "SCAFFOLD_PYTHON_FILES",
    "active_code_diagnostics",
    "completion_blocking_diagnostics",
    "format_new_diagnostics_for_tool_result",
    "read_code_diagnostics",
    "run_python_diagnostics",
    "summarize_code_diagnostics",
]
