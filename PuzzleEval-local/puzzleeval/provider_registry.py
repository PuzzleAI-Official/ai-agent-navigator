# ============================================================================
# Provider Registry — Centralized API Key Management
# ============================================================================
# Loads provider credentials from a JSON registry file. Used by Agent 5
# (live validation and test execution) to inject API keys into
# harness environments without requiring users to set env vars manually.
#
# DESIGN: The registry is a simple JSON file today. The interface is
# designed so it can be swapped to a secrets manager (AWS Secrets Manager,
# HashiCorp Vault, GCP Secret Manager) later without changing callers.
#
# FALLBACK: If no registry file exists, credentials are looked up from
# the process's environment variables. This preserves backward compatibility
# and supports development workflows where keys are in .env files.
#
# SCHEMA:
#   {
#     "providers": {
#       "mindee": {
#         "env_vars": {"MINDEE_API_KEY": "sk-..."},
#         "tier": "free",
#         "monthly_limit": 250,
#         "usage_this_month": 0
#       },
#       "aws_textract": {
#         "env_vars": {
#           "AWS_ACCESS_KEY_ID": "AKIA...",
#           "AWS_SECRET_ACCESS_KEY": "...",
#           "AWS_DEFAULT_REGION": "us-east-1"
#         },
#         "tier": "free_tier",
#         "monthly_limit": 1000,
#         "usage_this_month": 0
#       }
#     }
#   }
# ============================================================================

import json
import os
import re
from pathlib import Path

from puzzleeval.config import PROVIDER_REGISTRY_PATH


# ============================================================================
# Registry data structure
# ============================================================================

class ProviderEntry:
    """One provider's credentials and metadata."""

    def __init__(self, name: str, env_vars: dict[str, str], tier: str = "unknown",
                 monthly_limit: int | None = None, usage_this_month: int = 0):
        self.name = name
        self.env_vars = env_vars
        self.tier = tier
        self.monthly_limit = monthly_limit
        self.usage_this_month = usage_this_month

    def has_capacity(self) -> bool:
        """Check if the provider has remaining capacity this month."""
        if self.monthly_limit is None:
            return True
        return self.usage_this_month < self.monthly_limit


class ProviderRegistry:
    """Container for all provider credentials."""

    def __init__(self, providers: dict[str, ProviderEntry] | None = None):
        self.providers = providers or {}

    def is_empty(self) -> bool:
        return len(self.providers) == 0


# ============================================================================
# Loading
# ============================================================================

def load_registry(path: str | None = None) -> ProviderRegistry:
    """
    Load provider_registry.json if it exists. Returns empty registry if
    the file doesn't exist or is invalid.

    Args:
        path: Override path. Defaults to PROVIDER_REGISTRY_PATH from config.
    """
    registry_path = Path(path or PROVIDER_REGISTRY_PATH)

    if not registry_path.exists():
        return ProviderRegistry()

    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ProviderRegistry()

    providers_raw = raw.get("providers", {})
    providers = {}
    for name, data in providers_raw.items():
        env_vars = data.get("env_vars", {})
        if not env_vars:
            continue
        providers[name.lower()] = ProviderEntry(
            name=name,
            env_vars=env_vars,
            tier=data.get("tier", "unknown"),
            monthly_limit=data.get("monthly_limit"),
            usage_this_month=data.get("usage_this_month", 0),
        )

    return ProviderRegistry(providers=providers)


# ============================================================================
# Credential lookup
# ============================================================================

def _normalize(name: str) -> str:
    """Normalize a provider/candidate name for matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def get_credentials(
    registry: ProviderRegistry,
    provider_name: str,
    candidate_name: str,
    auth_env_vars: list[str] | None = None,
) -> dict[str, str] | None:
    """
    Look up credentials for a provider. Returns a dict of env var name → value,
    or None if no credentials are available.

    Matching strategy (in order):
    1. Exact match on normalized candidate name in registry
    2. Exact match on normalized provider name in registry
    3. Partial match: any registry key that is a substring of the candidate
       or provider name (or vice versa)
    4. Fallback: check if the auth_env_vars are already set in os.environ
    """
    norm_candidate = _normalize(candidate_name)
    norm_provider = _normalize(provider_name)

    # Strategy 1: exact candidate name match
    for key, entry in registry.providers.items():
        if _normalize(key) == norm_candidate and entry.has_capacity():
            return entry.env_vars

    # Strategy 2: exact provider name match
    for key, entry in registry.providers.items():
        if _normalize(key) == norm_provider and entry.has_capacity():
            return entry.env_vars

    # Strategy 3: partial/substring match
    for key, entry in registry.providers.items():
        norm_key = _normalize(key)
        if (norm_key in norm_candidate or norm_candidate in norm_key or
                norm_key in norm_provider or norm_provider in norm_key):
            if entry.has_capacity():
                return entry.env_vars

    # Strategy 4: fallback to environment variables
    if auth_env_vars:
        env_creds = {}
        for var in auth_env_vars:
            val = os.environ.get(var)
            if val:
                env_creds[var] = val
        if env_creds:
            return env_creds

    return None


def get_all_credentials(
    registry: ProviderRegistry,
    candidates: list,
) -> dict[str, dict[str, str]]:
    """
    Build a provider_name → credentials dict for all candidates.

    Used by the CLI to pass credentials into Agent5Input.provider_credentials.
    Only includes candidates where credentials were found.
    """
    result = {}
    for candidate in candidates:
        name = candidate.name
        provider = candidate.provider
        auth_vars = getattr(candidate, "auth_env_vars", None)

        creds = get_credentials(registry, provider, name, auth_vars)
        if creds:
            # Use normalized provider name as key
            result[_normalize(provider)] = creds
            # Also store under normalized candidate name for direct lookup
            result[_normalize(name)] = creds

    return result if result else None
