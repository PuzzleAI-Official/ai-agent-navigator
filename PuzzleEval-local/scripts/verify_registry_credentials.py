"""Verify every provider_registry.json entry against its real API.

One cheap authenticated call per provider — proves the key works TODAY,
not "worked once and maybe expired." Catches:
  - expired keys
  - revoked keys
  - credentials that got rotated upstream but not updated here
  - wrong auth header format
  - quota exhaustion

Each provider has a dedicated minimal probe (GET /me, GET /user, etc.)
that authenticates but doesn't consume monthly quota where possible.

Exit 0 = every credentialed provider responds 200. Exit 1 = at least one
is broken; user should rotate that key.

Usage:
    cd PuzzleEval-local
    python scripts/verify_registry_credentials.py
    python scripts/verify_registry_credentials.py --only openai elevenlabs
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Trigger registry → os.environ propagation so we read the same values
# a real pipeline run would see.
import puzzleeval  # noqa: F401

import requests

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


# ---------------------------------------------------------------------------
# One probe per provider — minimal authenticated call that doesn't burn quota.
# Each returns (status_code, detail_string). status_code in 200-299 = pass.
# ---------------------------------------------------------------------------


def probe_mindee() -> tuple[int, str]:
    key = os.environ.get("MINDEE_API_KEY", "")
    if not key:
        return 0, "no key in env"
    # Mindee V2 platform — the registry's md_-prefix keys are V2 format
    # (V1 keys were UUID-shaped). Probe the V2 enqueue endpoint with an
    # empty body: auth'd returns 400 "missing document"; unauth'd returns
    # 401 with code 401-001. Legacy V1 endpoints return 401 even for
    # valid V2 keys, so V2 probe is the right check.
    r = requests.post(
        "https://api-v2.mindee.net/v2/inferences/enqueue",
        headers={"Authorization": f"Token {key}"},
        data={},
        timeout=10,
    )
    if r.status_code == 401:
        return 401, f"invalid Mindee V2 API key: {r.text[:150]}"
    if r.status_code in (200, 400, 422):
        return 200, f"auth accepted (probe returned {r.status_code})"
    return r.status_code, f"unexpected status: {r.text[:200]}"


def probe_nanonets() -> tuple[int, str]:
    key = os.environ.get("NANONETS_API_KEY", "")
    model_id = os.environ.get("NANONETS_MODEL_ID", "")
    if not key:
        return 0, "no key in env"
    # Nanonets: GET /OCR/Model/{model_id}/ returns model metadata when
    # auth'd; 401 when not.
    if not model_id:
        # Try a general list endpoint
        r = requests.get(
            "https://app.nanonets.com/api/v2/OCR/Model/",
            auth=(key, ""), timeout=10,
        )
    else:
        r = requests.get(
            f"https://app.nanonets.com/api/v2/OCR/Model/{model_id}/",
            auth=(key, ""), timeout=10,
        )
    if r.status_code == 401 or r.status_code == 403:
        return r.status_code, "Nanonets rejects key"
    if r.status_code == 200:
        return 200, "model metadata retrieved"
    if r.status_code == 404:
        return 404, f"model {model_id!r} not found (key may still be valid)"
    return r.status_code, f"unexpected: {r.text[:200]}"


def probe_veryfi() -> tuple[int, str]:
    client_id = os.environ.get("VERYFI_CLIENT_ID", "")
    username = os.environ.get("VERYFI_USERNAME", "")
    api_key = os.environ.get("VERYFI_API_KEY", "")
    if not (client_id and username and api_key):
        return 0, "missing one of client_id/username/api_key"
    # Veryfi: GET /documents returns the doc list; auth'd with 4-header
    # scheme (Client-Id, Authorization: apikey).
    r = requests.get(
        "https://api.veryfi.com/api/v8/partner/documents/?page_size=1",
        headers={
            "Client-Id": client_id,
            "Authorization": f"apikey {username}:{api_key}",
            "Accept": "application/json",
        },
        timeout=10,
    )
    if r.status_code == 401 or r.status_code == 403:
        return r.status_code, f"Veryfi rejects credentials: {r.text[:200]}"
    if r.status_code == 200:
        return 200, "documents endpoint reachable"
    return r.status_code, f"unexpected: {r.text[:200]}"


def probe_klippa() -> tuple[int, str]:
    key = os.environ.get("KLIPPA_API_KEY", "")
    if not key:
        return 0, "no key in env"
    # Klippa DocHorizon — exercise the actual parseDocument endpoint
    # with an empty body. Auth'd requests return 400 ("document field
    # required"); unauth'd return 401. Either way we learn key validity.
    # Real endpoint: /services/document_capturing/v1/components (not
    # the /services that I originally probed — 404ed). Confirmed from
    # a prior successful harness build in runs/.
    # Auth'd empty-body request returns 400/422 "documents field
    # required"; unauth'd returns 401.
    r = requests.post(
        "https://dochorizon.klippa.com/api/services/document_capturing/v1/components",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json={},  # intentionally empty — auth check
        timeout=10,
    )
    if r.status_code == 401 or r.status_code == 403:
        return r.status_code, f"Klippa rejects key: {r.text[:200]}"
    if r.status_code in (200, 400, 422):
        return 200, f"auth accepted (probe returned {r.status_code})"
    return r.status_code, f"unexpected: {r.text[:200]}"


def probe_openai() -> tuple[int, str]:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return 0, "no key in env"
    # OpenAI: GET /v1/models is the canonical validity check. Free.
    r = requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    if r.status_code == 401:
        return 401, "invalid OpenAI API key"
    if r.status_code == 200:
        data = r.json()
        count = len(data.get("data", []))
        return 200, f"account has access to {count} models"
    return r.status_code, f"unexpected: {r.text[:200]}"


def probe_elevenlabs() -> tuple[int, str]:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        return 0, "no key in env"
    # Two-phase probe: account validity + TTS permission. Some ElevenLabs
    # accounts authenticate fine on /v1/user but get 401 on TTS endpoints
    # when payment method is missing / account is flagged / key lacks
    # TTS permission. Reporting both separately tells the user which
    # upstream fix is needed.
    r = requests.get(
        "https://api.elevenlabs.io/v1/user",
        headers={"xi-api-key": key}, timeout=10,
    )
    if r.status_code == 401:
        return 401, f"invalid key (even /v1/user rejected): {r.text[:150]}"
    if r.status_code != 200:
        return r.status_code, f"/v1/user unexpected: {r.text[:200]}"
    data = r.json()
    tier = data.get("subscription", {}).get("tier", "unknown")
    char_used = data.get("subscription", {}).get("character_count", "?")
    char_limit = data.get("subscription", {}).get("character_limit", "?")
    # Now probe the TTS endpoint actually used by the plugin.
    voice_id = os.environ.get(
        "PUZZLEEVAL_ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM",
    )
    tts = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={"text": ".", "model_id": "eleven_monolingual_v1"},
        timeout=15,
    )
    if tts.status_code in (200, 201):
        return 200, f"tier={tier}, chars={char_used}/{char_limit}, TTS synthesis OK"
    if tts.status_code == 401:
        return 401, (
            f"key authenticates on /v1/user (tier={tier}) BUT /v1/text-to-speech "
            f"returns 401. Upstream account issue: add payment method OR "
            f"check API key permissions in ElevenLabs dashboard. Detail: "
            f"{tts.text[:200]}"
        )
    return tts.status_code, f"TTS probe unexpected: {tts.text[:200]}"


PROBES = {
    "mindee":     probe_mindee,
    "nanonets":   probe_nanonets,
    "veryfi":     probe_veryfi,
    "klippa":     probe_klippa,
    "openai":     probe_openai,
    "elevenlabs": probe_elevenlabs,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*",
                        help="run only these providers by name")
    args = parser.parse_args()

    # Load the registry + verify what the JSON declares matches PROBES.
    from puzzleeval.provider_registry import load_registry
    registry = load_registry()
    print(f"{BOLD}Provider registry credential verification{RESET}")
    print(f"Loaded {len(registry.providers)} providers from registry\n")

    targets = args.only or list(PROBES.keys())
    passes = []
    fails = []
    skipped = []

    for name in targets:
        name_lower = name.lower()
        probe = PROBES.get(name_lower)
        if probe is None:
            print(f"  {YELLOW}[..]{RESET} {name}: no probe defined")
            skipped.append(name)
            continue
        in_registry = name_lower in registry.providers
        reg_tag = "" if in_registry else f" {YELLOW}(not in registry.json){RESET}"
        try:
            status, detail = probe()
        except requests.exceptions.RequestException as exc:
            status, detail = -1, f"network error: {type(exc).__name__}: {exc}"
        if status == 0:
            print(f"  {YELLOW}[..]{RESET} {name}: SKIPPED — {detail}{reg_tag}")
            skipped.append(name)
        elif 200 <= status < 300:
            print(f"  {GREEN}[OK]{RESET} {name}: {detail}{reg_tag}")
            passes.append(name)
        else:
            print(f"  {RED}[XX]{RESET} {name}: http {status} — {detail}{reg_tag}")
            fails.append(name)

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}Summary:{RESET} {len(passes)} valid / {len(fails)} invalid / {len(skipped)} skipped\n")
    if passes:
        print(f"{GREEN}Valid:{RESET} {', '.join(passes)}")
    if skipped:
        print(f"{YELLOW}Skipped:{RESET} {', '.join(skipped)}")
    if fails:
        print(f"{RED}Invalid (rotate these keys):{RESET} {', '.join(fails)}")
        sys.exit(1)
    if not passes:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
