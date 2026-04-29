"""
Phase 10: Generalizability benchmark fixtures and markers.

Tests in this directory are marked @pytest.mark.generalizability and
SKIPPED by default in the standard test run. Run explicitly with:
    pytest -m generalizability

Domain configs live in domains/*.json. Each config defines the input
prompt, expected scopes, per-scope assertions, and cost ceiling.
"""
import json
from pathlib import Path

import pytest

DOMAINS_DIR = Path(__file__).parent / "domains"


def load_domain_config(domain_name: str) -> dict:
    """Load a domain config JSON file by name."""
    path = DOMAINS_DIR / f"{domain_name}.json"
    if not path.exists():
        pytest.skip(f"Domain config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def all_domain_names() -> list[str]:
    """List all domain config names (without .json extension)."""
    return sorted(p.stem for p in DOMAINS_DIR.glob("*.json"))


@pytest.fixture
def domain_config(request):
    """Parametrized fixture that loads a domain config by name."""
    return load_domain_config(request.param)
