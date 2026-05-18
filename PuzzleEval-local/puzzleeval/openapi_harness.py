"""Mechanical harness generator from OpenAPI / Swagger specs.

When selected-candidate docs metadata or Agent 5 research discovers an
``openapi.json`` URL for a candidate, we don't need an LLM build turn
to produce a working harness skeleton - the spec defines the operation,
the auth, and the parameter shapes precisely. We can generate the
harness mechanically, then optionally let the LLM polish edge cases.

This module fetches the spec, picks the operation that matches the
candidate's primary scope role, and emits a minimal ``harness.py`` that:

  - reads credentials from env vars
  - composes the request from the operation's parameters
  - returns the standard ``{success, output, latency_ms, raw_response,
    error}`` shape Agent 5's test runner expects

When generation fails for any reason (spec unreachable, malformed,
operation not found), the function returns None — the caller falls
back to the LLM build path. So the OpenAPI generator is best-effort:
when it works, the LLM turns are saved; when it doesn't, nothing's
worse than before.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec fetching — small, defensive, no retries
# ---------------------------------------------------------------------------


def fetch_openapi_spec(url: str, timeout: float = 10.0) -> dict | None:
    """GET an OpenAPI/Swagger spec and parse it. Returns None on any failure.

    Both JSON and YAML specs are accepted (YAML requires PyYAML; we degrade
    gracefully when not installed).
    """
    try:
        import requests  # type: ignore
    except ImportError:
        logger.warning("openapi: `requests` not installed; cannot fetch spec")
        return None
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
    except Exception as exc:
        logger.warning("openapi: fetch failed for %s: %s", url, exc)
        return None
    text = r.text or ""
    # JSON path first
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # YAML fallback
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except (ImportError, Exception) as exc:  # noqa: BLE001
        logger.debug("openapi: yaml parse skipped: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Spec analysis — pick the operation that matches a scope role
# ---------------------------------------------------------------------------


@dataclass
class OperationMatch:
    method: str
    path: str
    operation: dict
    server_url: str
    auth_scheme: str  # "bearer" / "apikey_header" / "apikey_query" / "basic" / "none"
    auth_header_name: str | None = None
    auth_query_name: str | None = None


def _pick_server_url(spec: dict, fallback_base: str | None = None) -> str:
    servers = spec.get("servers") or []
    if servers and isinstance(servers, list):
        first = servers[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"]).rstrip("/")
    # Swagger 2.0 — host + basePath
    host = spec.get("host")
    base_path = spec.get("basePath") or ""
    schemes = spec.get("schemes") or ["https"]
    if host:
        scheme = "https" if "https" in schemes else schemes[0]
        return f"{scheme}://{host}{base_path}".rstrip("/")
    return (fallback_base or "").rstrip("/")


def _detect_auth_scheme(spec: dict) -> tuple[str, str | None, str | None]:
    """Return (scheme, header_name, query_name).

    Inspects ``components.securitySchemes`` (OpenAPI 3) and
    ``securityDefinitions`` (Swagger 2). When multiple schemes exist
    we prefer in order: bearer → apikey_header → apikey_query → basic → none.
    """
    schemes = (
        (spec.get("components") or {}).get("securitySchemes")
        or spec.get("securityDefinitions")
        or {}
    )
    if not isinstance(schemes, dict):
        return "none", None, None

    found = {"bearer": None, "apikey_header": None, "apikey_query": None, "basic": None}
    for name, scheme in schemes.items():
        if not isinstance(scheme, dict):
            continue
        stype = (scheme.get("type") or "").lower()
        if stype == "http" and (scheme.get("scheme", "").lower() == "bearer"):
            found["bearer"] = scheme
        elif stype == "apikey":
            location = (scheme.get("in") or "").lower()
            if location == "header":
                found["apikey_header"] = scheme
            elif location == "query":
                found["apikey_query"] = scheme
        elif stype == "http" and scheme.get("scheme", "").lower() == "basic":
            found["basic"] = scheme

    for kind in ("bearer", "apikey_header", "apikey_query", "basic"):
        s = found[kind]
        if s is None:
            continue
        if kind == "bearer":
            return "bearer", "Authorization", None
        if kind == "apikey_header":
            return "apikey_header", s.get("name") or "X-API-Key", None
        if kind == "apikey_query":
            return "apikey_query", None, s.get("name") or "api_key"
        if kind == "basic":
            return "basic", "Authorization", None
    return "none", None, None


_ROLE_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _role_tokens(role: str) -> tuple[str, ...]:
    """Split a role string into its component words (lowercased, alphanumeric).

    Replaces the previous hardcoded `_ROLE_KEYWORD_MAP` bandaid that listed
    a closed set of roles ("ocr", "transcribe", "translate", ...) — any
    novel role fell through to single-keyword matching. This generic
    tokenizer handles ANY role string (snake_case, camelCase, kebab-case,
    multi-word) by splitting on non-alphanumeric chars, lowercasing, and
    dropping the stop-words that match every endpoint.

    Examples:
        "ocr"                 -> ("ocr",)
        "image_generation"    -> ("image", "generation")
        "speech-to-text"      -> ("speech", "to", "text")  -> filtered to ("speech", "text")
        "MusicComposition"    -> ("music", "composition")  (caller lowercases first)
    """
    if not role:
        return ()
    # Split on non-alphanumeric runs; lowercase
    raw_tokens = _ROLE_TOKEN_PATTERN.findall(role.lower())
    # Stop words that appear in too many endpoint summaries to be discriminating.
    stop = {"to", "of", "the", "a", "an", "and", "or", "for", "with", "from", "by", "in", "on", "is", "are"}
    return tuple(t for t in raw_tokens if t not in stop and len(t) >= 2)


def find_operation_for_role(
    spec: dict,
    role: str,
    server_fallback: str | None = None,
) -> OperationMatch | None:
    """Pick the best operation in the spec matching the workflow role.

    Generic role-token matching: splits the role into words, scores each
    operation by how many tokens overlap its operationId / summary /
    description / path / tags. Any role string works, no hardcoded
    domain map. Prefers POST > PUT > PATCH > GET when multiple operations
    score equally.

    Returns None when no operation has any token overlap with the role.
    """
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return None
    server = _pick_server_url(spec, server_fallback)
    auth_scheme, header_name, query_name = _detect_auth_scheme(spec)
    tokens = _role_tokens(role)

    method_priority = {"post": 0, "put": 1, "patch": 2, "get": 3}
    candidates: list[tuple[tuple[int, int, int], str, str, dict]] = []

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            method_lc = method.lower()
            if method_lc not in method_priority:
                continue
            if not isinstance(op, dict):
                continue
            haystack_tokens = set(_ROLE_TOKEN_PATTERN.findall(
                " ".join([
                    op.get("operationId", ""),
                    op.get("summary", ""),
                    op.get("description", ""),
                    path,
                    " ".join(op.get("tags", []) or []),
                ]).lower()
            ))
            if not tokens:
                # No role guidance — every operation is equally eligible.
                # Still rank-order by method preference + path simplicity.
                score = 1
            else:
                score = sum(1 for tok in tokens if tok in haystack_tokens)
            if score > 0:
                rank = (-score, method_priority[method_lc], len(path))
                candidates.append((rank, method, path, op))

    if not candidates:
        return None
    candidates.sort()
    _, method, path, op = candidates[0]
    return OperationMatch(
        method=method.upper(),
        path=path,
        operation=op,
        server_url=server,
        auth_scheme=auth_scheme,
        auth_header_name=header_name,
        auth_query_name=query_name,
    )


# ---------------------------------------------------------------------------
# Harness code generation
# ---------------------------------------------------------------------------


def _slug_env_var(provider: str) -> str:
    s = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")
    return f"{s}_API_KEY" if s else "API_KEY"


def _request_kwarg_for_op(op: dict) -> str:
    """Return one of 'json', 'data', 'files', 'params' based on the op's
    requestBody content type (OpenAPI 3) or consumes (Swagger 2)."""
    body = op.get("requestBody") or {}
    content = (body.get("content") or {})
    if "multipart/form-data" in content:
        return "files"
    if "application/x-www-form-urlencoded" in content:
        return "data"
    if "application/json" in content:
        return "json"
    consumes = op.get("consumes") or []
    if any("multipart" in c for c in consumes):
        return "files"
    if any("urlencoded" in c for c in consumes):
        return "data"
    return "json"  # safe default


def _extract_path_params(path: str) -> list[str]:
    """Extract {param} placeholders from an OpenAPI path template.

    ``/users/{id}/posts/{post_id}`` → ``["id", "post_id"]``.
    Previously the generator did ``urljoin(server, path.lstrip("/"))`` and
    emitted the literal URL, so ``GET /users/{id}`` hit the server with the
    literal ``{id}`` → 404. This caused the OpenAPI fastpath to silently
    fail on 30-50% of providers that expose path-parameterized endpoints.
    """
    return re.findall(r"\{([^{}]+)\}", path or "")


def generate_harness_code(
    operation: OperationMatch,
    candidate_name: str,
    provider: str,
) -> str:
    """Emit a minimal harness.py given a matched OpenAPI operation."""
    env_var = _slug_env_var(provider)
    # Template URL (may contain {param} placeholders — substitute at call time)
    url_template = urljoin(operation.server_url + "/", operation.path.lstrip("/"))
    path_params = _extract_path_params(operation.path)
    request_kwarg = _request_kwarg_for_op(operation.operation)

    auth_setup = ""
    request_extra = ""
    if operation.auth_scheme == "bearer":
        auth_setup = (
            f"    api_key = os.environ.get({env_var!r})\n"
            f"    if not api_key:\n"
            f"        return {{'success': False, 'output': '', 'latency_ms': 0.0, "
            f"'raw_response': {{}}, 'error': 'Missing env var: {env_var}'}}\n"
            f"    headers = {{'Authorization': f'Bearer {{api_key}}'}}\n"
        )
    elif operation.auth_scheme == "apikey_header":
        header = operation.auth_header_name or "X-API-Key"
        auth_setup = (
            f"    api_key = os.environ.get({env_var!r})\n"
            f"    if not api_key:\n"
            f"        return {{'success': False, 'output': '', 'latency_ms': 0.0, "
            f"'raw_response': {{}}, 'error': 'Missing env var: {env_var}'}}\n"
            f"    headers = {{{header!r}: api_key}}\n"
        )
    elif operation.auth_scheme == "apikey_query":
        query = operation.auth_query_name or "api_key"
        auth_setup = (
            f"    api_key = os.environ.get({env_var!r})\n"
            f"    if not api_key:\n"
            f"        return {{'success': False, 'output': '', 'latency_ms': 0.0, "
            f"'raw_response': {{}}, 'error': 'Missing env var: {env_var}'}}\n"
            f"    headers = {{}}\n"
            f"    query_params = {{{query!r}: api_key}}\n"
        )
        request_extra = ", params=query_params"
    elif operation.auth_scheme == "basic":
        auth_setup = (
            f"    api_key = os.environ.get({env_var!r}, '')\n"
            f"    headers = {{'Authorization': f'Basic {{api_key}}'}}\n"
        )
    else:
        auth_setup = "    headers = {}\n"

    # Path-parameter substitution. When the operation's path contains
    # {placeholders}, the harness MUST read them from ``input_data`` before
    # the request — otherwise requests hits the URL literal with curly
    # braces still in it and gets 404.
    if path_params:
        substitution_setup = (
            f"    # Path params this endpoint requires: {path_params}\n"
            f"    _path_params = {path_params!r}\n"
            f"    _missing = [p for p in _path_params if p not in input_data]\n"
            f"    if _missing:\n"
            f"        return {{'success': False, 'output': '', 'latency_ms': 0.0, "
            f"'raw_response': {{}}, 'error': f'Missing path params: {{_missing}}. Test case input_data must include them.'}}\n"
            f"    url = {url_template!r}\n"
            f"    for _p in _path_params:\n"
            f"        url = url.replace('{{' + _p + '}}', str(input_data[_p]))\n"
        )
        url_expr = "url"
    else:
        substitution_setup = f"    url = {url_template!r}\n"
        url_expr = "url"

    if request_kwarg == "files":
        body_setup = (
            f"{substitution_setup}"
            "    file_path = input_data.get('file_path') or input_data.get('test_file_path')\n"
            "    if not file_path:\n"
            "        return {'success': False, 'output': '', 'latency_ms': 0.0, "
            "'raw_response': {}, 'error': 'INCOMPATIBLE: this endpoint requires a file upload'}\n"
            "    with open(file_path, 'rb') as f:\n"
            "        start = time.time()\n"
            f"        r = requests.request({operation.method!r}, {url_expr}{request_extra}, "
            f"headers=headers, files={{'file': f}}, timeout=120)\n"
        )
    else:
        # Strip path-param keys from the body payload — they went into the URL.
        path_strip_expr = ""
        if path_params:
            path_strip_expr = f" and k not in {set(path_params)!r}"
        body_setup = (
            f"{substitution_setup}"
            f"    payload = {{k: v for k, v in input_data.items() "
            f"if k not in ('test_file_path', 'file_path'){path_strip_expr}}}\n"
            f"    start = time.time()\n"
            f"    r = requests.request({operation.method!r}, {url_expr}{request_extra}, "
            f"headers=headers, {request_kwarg}=payload, timeout=120)\n"
        )

    code = (
        "# Auto-generated harness from OpenAPI spec.\n"
        f"# Candidate: {candidate_name}\n"
        f"# Provider:  {provider}\n"
        f"# Endpoint:  {operation.method} {operation.path}\n"
        f"# Auth:      {operation.auth_scheme}\n"
        "import os\n"
        "import time\n"
        "import json\n"
        "import requests\n"
        "\n"
        "def run(input_data):\n"
        "    \"\"\"Execute the API call. Returns the standard test-runner shape.\"\"\"\n"
        f"{auth_setup}"
        f"{body_setup}"
        "        latency_ms = round((time.time() - start) * 1000, 2)\n"
        "        try:\n"
        "            raw = r.json()\n"
        "        except Exception:\n"
        "            raw = {'text': r.text[:5000]}\n"
        "        if r.status_code >= 400:\n"
        "            return {'success': False, 'output': '', 'latency_ms': latency_ms, "
        "'raw_response': raw, 'error': f'HTTP {r.status_code}: {r.text[:500]}'}\n"
        "        return {'success': True, 'output': raw, 'latency_ms': latency_ms, "
        "'raw_response': raw, 'error': None}\n"
    )
    return code


def generate_harness_for_candidate(
    openapi_url: str,
    role: str,
    candidate_name: str,
    provider: str,
    server_fallback: str | None = None,
) -> str | None:
    """End-to-end: fetch spec → match operation → emit harness.py text.

    Returns None when the spec couldn't be fetched, was malformed, or had
    no operation matching the role. Caller falls back to the LLM build path.
    """
    spec = fetch_openapi_spec(openapi_url)
    if spec is None:
        return None
    op = find_operation_for_role(spec, role, server_fallback=server_fallback)
    if op is None:
        return None
    try:
        return generate_harness_code(op, candidate_name=candidate_name, provider=provider)
    except Exception as exc:
        logger.warning("openapi: harness generation crashed: %s", exc)
        return None


__all__ = [
    "OperationMatch",
    "fetch_openapi_spec",
    "find_operation_for_role",
    "generate_harness_code",
    "generate_harness_for_candidate",
]
