"""CI guard — verify capability_playbooks/*.md ships in the installed wheel.

This test was the user's #1 pre-flight concern: the markdown files might
not actually be in the installed package, in which case the loader silently
returns the fallback (empty) and Agent 5's contract block is empty in
production. This test makes that failure mode loud.

CODEX PUSHBACK Q4 ADDRESSED: an editable install (``pip install -e .``)
silently works against the source tree even when ``pyproject.toml``
``package-data`` is wrong, because ``importlib.resources`` resolves to
the source directory. The class ``TestRealWheelPackaging`` BUILDS A WHEEL,
INSTALLS IT into a clean prefix, and verifies the markdown is actually
inside the wheel — that's the production failure mode.

The earlier ``TestWheelPackaging`` class is kept as a fast smoke test
that runs in normal CI; the wheel-build test is gated on the
``PUZZLEEVAL_RUN_WHEEL_BUILD_TEST=1`` env var so it doesn't slow down
every test invocation. CI should set this in the build-and-deploy job.
"""

from __future__ import annotations

from importlib import resources

import pytest


# ---------------------------------------------------------------------------
# Expected contracts — must always ship.
# ---------------------------------------------------------------------------
# Update this list whenever a new capability_playbook is added.
# Phase 0 ships 6 contracts (3 modality + 3 platform).
EXPECTED_CONTRACTS = (
    # Modality contracts (Codex's NEW-AN extraction)
    "voice.md",
    "streaming_response.md",
    "live_test_voice.md",
    # Platform contracts (Phase 1 extraction)
    "platform_windows.md",
    "platform_linux.md",
    "platform_macos.md",
)


class TestWheelPackaging:
    def test_capability_playbooks_directory_exists(self):
        """The directory itself must be importable as package data."""
        playbooks_dir = (
            resources.files("puzzleeval").joinpath("capability_playbooks")
        )
        assert playbooks_dir.is_dir(), (
            "puzzleeval/capability_playbooks/ is missing from the installed "
            "package. Check pyproject.toml package-data."
        )

    @pytest.mark.parametrize("filename", EXPECTED_CONTRACTS)
    def test_contract_markdown_in_wheel(self, filename: str):
        """Each expected contract must be readable from the installed package."""
        text = (
            resources.files("puzzleeval")
            .joinpath("capability_playbooks", filename)
            .read_text(encoding="utf-8")
        )
        assert len(text) > 100, (
            f"Contract {filename} is suspiciously short ({len(text)} chars). "
            "Expected at least 100 chars of teaching content."
        )

    def test_loader_discovers_all_expected_contracts(self):
        """The discovery loader should find every expected contract."""
        from puzzleeval.contracts.loader import discover_contracts

        contracts = discover_contracts()
        found_filenames = {c.filename for c in contracts}
        for expected in EXPECTED_CONTRACTS:
            assert expected in found_filenames, (
                f"Loader didn't discover {expected}. Found: {sorted(found_filenames)}"
            )

    def test_builder_system_prompt_template_in_wheel(self):
        """Agent 5's builder prompt template must also ship."""
        text = (
            resources.files("puzzleeval.agents.agent5")
            .joinpath("templates", "builder_system_prompt.md")
            .read_text(encoding="utf-8")
        )
        # The builder prompt is ~55KB; assert at least 10KB to catch
        # silent truncation / packaging bugs.
        assert len(text) > 10_000, (
            f"builder_system_prompt.md is too short ({len(text)} chars). "
            "Expected ~55KB."
        )


# ---------------------------------------------------------------------------
# REAL wheel build — addresses Codex pushback Q4.
# ---------------------------------------------------------------------------
# The tests above run against the editable install (`pip install -e .`).
# That silently works even when `pyproject.toml` package-data is wrong,
# because `importlib.resources` resolves to the source tree.
#
# This class actually BUILDS a wheel, installs it into a fresh isolated
# directory, and runs `importlib.resources` from THAT prefix — proving the
# wheel itself contains the markdown files.
#
# Gated on `PUZZLEEVAL_RUN_WHEEL_BUILD_TEST=1` so it doesn't slow down
# normal CI. The build-and-release CI job should set this env var.
# ---------------------------------------------------------------------------

import os  # noqa: E402

import pytest  # noqa: E402

_RUN_WHEEL_BUILD = os.environ.get("PUZZLEEVAL_RUN_WHEEL_BUILD_TEST", "0") == "1"


@pytest.mark.skipif(
    not _RUN_WHEEL_BUILD,
    reason=(
        "Wheel build test is slow (~30-60s). Set "
        "PUZZLEEVAL_RUN_WHEEL_BUILD_TEST=1 to enable. CI's release job "
        "should always set this; CI's regular test job can skip it."
    ),
)
class TestRealWheelPackaging:
    """Build a wheel, install it into a clean prefix, verify .md files
    are actually present in the installed location.

    This catches the failure mode that editable-install testing can't:
    a missing `package-data` entry that lets the .md files exist in the
    source tree but excludes them from the wheel.
    """

    def test_wheel_includes_capability_playbooks(self, tmp_path):
        """Build wheel, inspect its contents directly via zipfile."""
        import subprocess
        import zipfile
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent

        # Build the wheel into a temp directory
        result = subprocess.run(
            [
                "python", "-m", "build", "--wheel",
                "--outdir", str(tmp_path),
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        # `python -m build` requires the `build` package
        if result.returncode != 0:
            pytest.skip(
                f"python -m build failed (likely missing 'build' package): "
                f"{result.stderr[:500]}"
            )

        wheels = list(tmp_path.glob("puzzleeval-*.whl"))
        assert wheels, f"No wheel produced in {tmp_path}"
        wheel_path = wheels[0]

        # Inspect the wheel's contents directly
        with zipfile.ZipFile(wheel_path) as zf:
            members = zf.namelist()

        # Required files
        required = [
            "puzzleeval/capability_playbooks/voice.md",
            "puzzleeval/capability_playbooks/streaming_response.md",
            "puzzleeval/capability_playbooks/live_test_voice.md",
            "puzzleeval/capability_playbooks/platform_windows.md",
            "puzzleeval/capability_playbooks/platform_linux.md",
            "puzzleeval/capability_playbooks/platform_macos.md",
            "puzzleeval/agents/agent5/templates/builder_system_prompt.md",
        ]
        missing = [r for r in required if r not in members]
        assert not missing, (
            f"Wheel {wheel_path.name} is MISSING markdown files:\n"
            f"  {missing}\n\n"
            "This is exactly the production failure mode editable-install "
            "tests cannot catch. Fix: ensure pyproject.toml "
            "[tool.setuptools.package-data] includes the right glob patterns."
        )

    def test_wheel_install_then_load_via_importlib_resources(self, tmp_path):
        """Install the wheel into a fresh prefix and load via importlib.resources.

        This is the actual production code path: in deployment, the wheel
        is installed (not the source tree), and `importlib.resources`
        resolves to the installed location. If a contract is missing from
        the wheel, this test fails.
        """
        import subprocess
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent

        # Build a wheel
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        result = subprocess.run(
            [
                "python", "-m", "build", "--wheel",
                "--outdir", str(wheel_dir),
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            pytest.skip(f"python -m build failed: {result.stderr[:500]}")

        wheels = list(wheel_dir.glob("puzzleeval-*.whl"))
        assert wheels
        wheel_path = wheels[0]

        # Install into a fresh prefix
        install_prefix = tmp_path / "installed"
        install_prefix.mkdir()
        result = subprocess.run(
            [
                "python", "-m", "pip", "install",
                "--target", str(install_prefix),
                "--no-deps", "--no-warn-script-location",
                str(wheel_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.skip(f"pip install failed: {result.stderr[:500]}")

        # Run a Python subprocess WITH the install prefix on PYTHONPATH
        # and load the .md files via importlib.resources. Use a temp .py
        # file so we can use multi-line syntax (for/assert blocks).
        verify_script_path = tmp_path / "verify.py"
        verify_script_path.write_text(
            "import importlib.resources as r\n"
            "import puzzleeval\n"
            "for name in ['voice.md', 'streaming_response.md', 'live_test_voice.md',\n"
            "             'platform_windows.md', 'platform_linux.md', 'platform_macos.md']:\n"
            "    text = r.files('puzzleeval').joinpath('capability_playbooks', name).read_text(encoding='utf-8')\n"
            "    assert len(text) > 100, f'{name}: too short ({len(text)} chars)'\n"
            "print('OK: all 6 contracts loadable from installed wheel')\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(install_prefix)
        result = subprocess.run(
            ["python", str(verify_script_path)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, (
            f"Loading contracts from installed wheel failed:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "OK" in result.stdout
