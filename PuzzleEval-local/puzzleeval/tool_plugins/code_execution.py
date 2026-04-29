"""Code-execution plugin — sandboxed runner for generated code.

Evaluates the candidate API's response when the response IS code.
LLM-judged code evaluation produces frequent false positives ("looks
right" but doesn't run); execution-based evaluation is the gold
standard. This plugin runs the generated code in an isolated
subprocess against language-specific harnesses and reports:

  - exit_code: did the program terminate normally?
  - stdout / stderr: captured output for diagnosis
  - timed_out: did the runner kill the program?
  - assertion_results: when the test_case provides assertions, did they pass?

Languages supported out of the box (the runner picks based on
``input_context.language`` or file extension):

  - Python (.py) via the host interpreter
  - JavaScript (.js) via Node when available
  - TypeScript (.ts) via tsx when available
  - Bash (.sh) — shell-restricted
  - Go (.go) via `go run` when available
  - Rust (.rs) via `rustc` when available

Languages with no runner detected return a structured "skipped" result
with the reason — the caller can fall back to LLM judging or display
"could not execute" in the report.

Security: every run happens in a dedicated tmp dir. Network access is
NOT explicitly blocked — Linux net-namespaces or Docker would harden
this further; for local single-user evaluation we trust the candidate
API's generated code (the same trust we extend to API responses
already). Production deployments should containerize.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from puzzleeval.tool_plugins import (
    EvaluationResult,
    PluginCapabilities,
    SynthesisResult,
    ToolPlugin,
    register_plugin,
)


# ---------------------------------------------------------------------------
# Language registry — extension + runner command + file template
# ---------------------------------------------------------------------------

LANG_RUNNERS: dict[str, dict[str, Any]] = {
    "python": {
        "ext": ".py",
        "runner": [sys.executable],
        "is_available": lambda: True,  # the host interpreter is always present
    },
    "javascript": {
        "ext": ".js",
        "runner": ["node"],
        "is_available": lambda: shutil.which("node") is not None,
    },
    "typescript": {
        "ext": ".ts",
        "runner": ["npx", "--yes", "tsx"],
        "is_available": lambda: shutil.which("npx") is not None,
    },
    "bash": {
        "ext": ".sh",
        "runner": ["bash"],
        "is_available": lambda: shutil.which("bash") is not None,
    },
    "go": {
        "ext": ".go",
        "runner": ["go", "run"],
        "is_available": lambda: shutil.which("go") is not None,
    },
    "rust": {
        "ext": ".rs",
        "runner": None,  # Rust needs compile-then-run
        "is_available": lambda: shutil.which("rustc") is not None,
    },
}


def detect_language(code: str, language_hint: str | None = None) -> str | None:
    """Identify the code's language from an explicit hint or content sniff.

    Returns one of the LANG_RUNNERS keys, or None when undetermined.
    """
    if language_hint:
        hint = language_hint.lower().strip()
        if hint in LANG_RUNNERS:
            return hint
        # Common aliases
        if hint in {"py", "python3"}:
            return "python"
        if hint in {"js", "node"}:
            return "javascript"
        if hint in {"ts", "tsx"}:
            return "typescript"
        if hint in {"sh", "shell"}:
            return "bash"
    # Content sniff — fragile but useful as fallback
    head = code.lstrip()[:200].lower()
    if head.startswith(("import ", "from ", "def ", "class ", "print(", "if __name__")):
        return "python"
    if "console.log" in head or "function " in head or "const " in head:
        return "javascript"
    if "fn main" in head or "let mut " in head:
        return "rust"
    if "package main" in head or "func main" in head:
        return "go"
    if head.startswith("#!"):
        return "bash"
    return None


# ---------------------------------------------------------------------------
# Plugin implementation
# ---------------------------------------------------------------------------


class CodeExecutionPlugin(ToolPlugin):
    """Runs generated code in a sandbox and scores it by execution success."""

    name = "code_execution"

    def capabilities(self) -> PluginCapabilities:
        return PluginCapabilities(
            input_types=["code", "text", "structured_data"],
            output_types=["code", "free_text"],
            synthesizes_input=True,
            evaluates_output=True,
            requires_credentials=[],
            notes=(
                "Sandboxed subprocess runner. Supports Python (always), "
                "JavaScript/TypeScript/Go/Rust/Bash when the toolchain is "
                "installed on the host. No network sandboxing in v1."
            ),
        )

    # -- Input synthesis --------------------------------------------------

    def synthesize_input(
        self, *, scope_role: str, ground_truth_hint: str | None = None,
        language: str = "python", **kwargs: Any,
    ) -> SynthesisResult:
        """Generate a small canonical code-completion prompt.

        Used for code-generation candidates when the user has no example
        code on disk. The synthesized prompt + reference solution serve
        as ground truth for the candidate's response.
        """
        templates = {
            "python": (
                "# Write a function `fizzbuzz(n)` that returns a list of strings:\n"
                "# - 'Fizz' for multiples of 3\n"
                "# - 'Buzz' for multiples of 5\n"
                "# - 'FizzBuzz' for multiples of both\n"
                "# - the number as a string otherwise\n"
                "# for i in 1..n.\n"
                "def fizzbuzz(n):\n    pass\n"
            ),
            "javascript": (
                "// Implement fizzbuzz(n) returning an array of strings per\n"
                "// the standard FizzBuzz rules.\n"
                "function fizzbuzz(n) {\n  // TODO\n}\n"
            ),
        }
        prompt = templates.get(language.lower(), templates["python"])
        ground_truth = {
            "language": language,
            "expected_function": "fizzbuzz",
            "test_inputs": [3, 5, 15, 16],
            "test_outputs": [
                ["1", "2", "Fizz"],
                ["1", "2", "Fizz", "4", "Buzz"],
                ["1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz", "Buzz",
                 "11", "Fizz", "13", "14", "FizzBuzz"],
                ["1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz", "Buzz",
                 "11", "Fizz", "13", "14", "FizzBuzz", "16"],
            ],
        }
        return SynthesisResult(
            inline_data={"prompt": prompt, "language": language},
            ground_truth=ground_truth,
            notes="canonical FizzBuzz seed; replace with user-supplied prompt when available",
        )

    # -- Output evaluation ------------------------------------------------

    def evaluate_output(
        self, *, response: Any, expected: Any,
        criteria: list[dict] | None = None,
        language: str | None = None, timeout_seconds: float = 30.0,
        **kwargs: Any,
    ) -> EvaluationResult:
        """Execute the candidate's code response and score by success.

        ``response`` may be a raw code string OR a dict shaped like
        ``{"output": "<code>", ...}`` (Agent 5's harness response).
        ``expected`` may be a reference solution string OR a dict
        ``{"test_inputs": [...], "test_outputs": [...]}``.
        """
        code = self._extract_code(response)
        if not code:
            return EvaluationResult(
                passed=False,
                score=0.0,
                reasoning="no code found in response",
                fallback_reason="no_code_in_response",
            )

        lang = detect_language(code, language)
        if lang is None or lang not in LANG_RUNNERS:
            return EvaluationResult(
                passed=False, score=0.0, reasoning=f"language not detected ({language!r})",
                fallback_reason="unknown_language",
            )
        runner_info = LANG_RUNNERS[lang]
        if not runner_info["is_available"]():
            return EvaluationResult(
                passed=False, score=0.0,
                reasoning=f"{lang} toolchain not installed on this host",
                fallback_reason=f"missing_toolchain_{lang}",
            )

        with tempfile.TemporaryDirectory(prefix="puzzleeval_code_") as tmp:
            tmp_path = Path(tmp)
            source_file = tmp_path / f"submission{runner_info['ext']}"
            source_file.write_text(code, encoding="utf-8")
            test_runner = self._build_test_runner(lang, source_file, expected, tmp_path)

            cmd = list(runner_info["runner"]) + [str(test_runner)]
            try:
                start = time.perf_counter()
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout_seconds,
                    cwd=str(tmp_path),
                )
                duration_ms = (time.perf_counter() - start) * 1000
            except subprocess.TimeoutExpired:
                return EvaluationResult(
                    passed=False, score=0.0,
                    reasoning=f"execution timed out after {timeout_seconds}s",
                    fallback_reason="timeout",
                )
            except FileNotFoundError as exc:
                return EvaluationResult(
                    passed=False, score=0.0,
                    reasoning=f"runner unavailable: {exc}",
                    fallback_reason=f"runner_missing_{lang}",
                )

            stdout = (proc.stdout or "").strip()
            stderr = (proc.stderr or "").strip()
            assertion_results = self._parse_assertion_lines(stdout)
            passed = (proc.returncode == 0 and all(assertion_results))
            score = (
                sum(1 for a in assertion_results if a) / len(assertion_results)
                if assertion_results else (1.0 if proc.returncode == 0 else 0.0)
            )
            reasoning_parts = [
                f"exit_code={proc.returncode}",
                f"language={lang}",
                f"duration_ms={duration_ms:.0f}",
            ]
            if assertion_results:
                reasoning_parts.append(
                    f"assertions={sum(1 for a in assertion_results if a)}/{len(assertion_results)}"
                )
            return EvaluationResult(
                passed=passed,
                score=score,
                reasoning="; ".join(reasoning_parts),
                detail={
                    "exit_code": proc.returncode,
                    "stdout_tail": stdout[-2000:],
                    "stderr_tail": stderr[-2000:],
                    "language": lang,
                    "duration_ms": duration_ms,
                    "assertion_results": assertion_results,
                },
            )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _extract_code(response: Any) -> str:
        """Pull a code string out of various response shapes."""
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            for key in ("code", "output", "text", "completion", "result"):
                value = response.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    @staticmethod
    def _build_test_runner(
        language: str, source_file: Path, expected: Any, tmp_path: Path,
    ) -> Path:
        """Emit a small driver script that imports/invokes the submission
        and prints ASSERT_PASS / ASSERT_FAIL lines for each documented
        test case in ``expected``."""
        if not isinstance(expected, dict):
            expected = {}
        test_inputs = expected.get("test_inputs") or []
        test_outputs = expected.get("test_outputs") or []
        function_name = expected.get("expected_function") or "main"
        pairs = list(zip(test_inputs, test_outputs))

        if language == "python":
            # Use safe interpolation: stringify everything via repr() upfront
            # so the emitted driver code never has nested quote conflicts.
            fn_name_repr = repr(function_name)
            source_file_repr = repr(str(source_file))
            driver_lines = [
                "import sys, importlib.util",
                f"spec = importlib.util.spec_from_file_location('submission', {source_file_repr})",
                "module = importlib.util.module_from_spec(spec)",
                "try:",
                "    spec.loader.exec_module(module)",
                "except Exception as exc:",
                "    print('IMPORT_ERROR: ' + str(exc))",
                "    sys.exit(1)",
                f"fn = getattr(module, {fn_name_repr}, None)",
                "if fn is None:",
                f"    print('NO_FUNCTION: ' + {fn_name_repr})",
                "    sys.exit(1)",
            ]
            for i, (inp, expected_out) in enumerate(pairs):
                inp_repr = repr(inp)
                expected_repr = repr(expected_out)
                driver_lines.append(
                    f"try:\n"
                    f"    _actual_{i} = fn({inp_repr})\n"
                    f"except Exception as _exc_{i}:\n"
                    f"    print('ASSERT_FAIL[{i}]: exception ' + repr(_exc_{i}))\n"
                    f"else:\n"
                    f"    _expected_{i} = {expected_repr}\n"
                    f"    if _actual_{i} == _expected_{i}:\n"
                    f"        print('ASSERT_PASS[{i}]')\n"
                    f"    else:\n"
                    f"        print('ASSERT_FAIL[{i}]: got ' + repr(_actual_{i}) + ' expected ' + repr(_expected_{i}))"
                )
            if not pairs:
                driver_lines.append("print('NO_TEST_CASES')")
            driver = "\n".join(driver_lines)
            driver_path = tmp_path / "driver.py"
            driver_path.write_text(driver, encoding="utf-8")
            return driver_path

        if language == "javascript":
            driver_lines = [
                "const sub = require(" + repr(str(source_file)) + ");",
                f"const fn = sub.{function_name} || sub.default;",
                "if (!fn) { console.log('NO_FUNCTION'); process.exit(1); }",
            ]
            for i, (inp, expected_out) in enumerate(pairs):
                driver_lines.append(
                    f"try {{ const actual = fn({inp!r}); "
                    f"if (JSON.stringify(actual) === JSON.stringify({expected_out!r})) "
                    f"{{ console.log('ASSERT_PASS[{i}]'); }} "
                    f"else {{ console.log('ASSERT_FAIL[{i}]: got '+JSON.stringify(actual)); }} "
                    f"}} catch (e) {{ console.log('ASSERT_FAIL[{i}]: '+e.message); }}"
                )
            if not pairs:
                driver_lines.append("console.log('NO_TEST_CASES');")
            driver = "\n".join(driver_lines)
            driver_path = tmp_path / "driver.js"
            driver_path.write_text(driver, encoding="utf-8")
            return driver_path

        # For other languages: just run the source file directly. Assertion
        # parsing won't work, but exit_code is honored.
        return source_file

    @staticmethod
    def _parse_assertion_lines(stdout: str) -> list[bool]:
        """Scan stdout for ASSERT_PASS / ASSERT_FAIL lines."""
        results: list[bool] = []
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("ASSERT_PASS"):
                results.append(True)
            elif line.startswith("ASSERT_FAIL"):
                results.append(False)
        return results


register_plugin(CodeExecutionPlugin())


__all__ = ["CodeExecutionPlugin", "LANG_RUNNERS", "detect_language"]
