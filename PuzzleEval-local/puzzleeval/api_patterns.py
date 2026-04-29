"""Universal API patterns the Agent 5 harness builder snaps to.

The catalog is INJECTED INTO THE BUILDER'S SYSTEM PROMPT (as text) so the
model can read it once and skip re-deriving common integration shapes from
provider docs each time. Each pattern entry includes:

  - signature — how to recognize the pattern from docs
  - python skeleton — minimal working code to start from
  - common pitfalls — bugs we've seen on this pattern

Patterns are PRINCIPLE-BASED (not provider-specific). They cover the ~90%
of REST/HTTP API shapes the builder will encounter. Specialty auth flows
(AWS SigV4, mTLS, OAuth2 authorization_code with PKCE) are not here yet —
the builder falls back to web_search/web_fetch on those rare paths.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# The catalog itself
# ---------------------------------------------------------------------------

API_PATTERNS_CATALOG = """
## UNIVERSAL API PATTERNS (snap to these — don't reinvent)

The catalog below covers the ~90% of public APIs you will encounter. When
you see signatures matching a pattern, STOP RESEARCHING and use the
skeleton verbatim with the candidate's specific URL / fields swapped in.
Only depart from the skeleton when the docs explicitly require something
non-standard.

### Pattern 1 — REST + API key in Authorization header
Signature: docs say "Bearer <YOUR_API_KEY>" or "Authorization: Bearer ..."
Skeleton:
    import requests
    r = requests.post(URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()
Pitfalls: some APIs use "Token <key>" instead of "Bearer <key>". Read the docs literally.

### Pattern 2 — REST + API key in custom header
Signature: docs say "X-API-Key: <YOUR_KEY>" / "x-api-key" / vendor-specific header
Skeleton:
    r = requests.post(URL, headers={"X-API-Key": api_key, "Content-Type": "application/json"}, json=payload, timeout=60)
Pitfalls: case-sensitive on some servers. Send with the exact case the docs show.

### Pattern 3 — REST + API key in query string
Signature: docs say "?api_key=<KEY>" or "?key=<KEY>"
Skeleton:
    r = requests.post(URL, params={"api_key": api_key}, json=payload, timeout=60)
Pitfalls: NEVER log full URLs in this case — keys leak.

### Pattern 4 — Multipart file upload
Signature: docs show "multipart/form-data" or "files=" in cURL examples
Skeleton:
    with open(file_path, "rb") as f:
        r = requests.post(URL, headers={"Authorization": f"Bearer {api_key}"},
                          files={"file": (Path(file_path).name, f, mime_type)},
                          data=other_form_fields, timeout=120)
Pitfalls: don't pass `json=` AND `files=` together — most servers reject. Use `data=` for non-file fields alongside files.

### Pattern 5 — Base64 file in JSON body
Signature: docs show "image": "<base64 string>" or "document": {"data": "..."}
Skeleton:
    import base64
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    r = requests.post(URL, headers={"Authorization": f"Bearer {api_key}"},
                      json={"image": b64, "params": {...}}, timeout=120)
Pitfalls: some APIs want the data URI prefix (`data:image/png;base64,...`), some don't. Test with one example before assuming.

### Pattern 6 — Async polling (job_id then GET /jobs/{id})
Signature: docs say "returns a job_id, poll until status is 'completed'" OR you see two endpoints (POST + GET status)
Skeleton:
    import time
    submit = requests.post(SUBMIT_URL, headers=auth_headers, json=payload, timeout=60).json()
    job_id = submit["id"] or submit["job_id"]
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        status = requests.get(f"{STATUS_URL}/{job_id}", headers=auth_headers, timeout=30).json()
        if status.get("status") in ("completed", "succeeded", "done"):
            return status.get("result") or status
        if status.get("status") in ("failed", "error"):
            return {"success": False, "error": status.get("error", "job failed")}
        time.sleep(min(1.5, 0.5 * (1 + attempt_count * 0.3)))  # exponential backoff
    return {"success": False, "error": "polling timeout"}
Pitfalls: status field names vary widely (`status`, `state`, `phase`). Result field varies too. Check the actual endpoint response shape, not assumptions.

### Pattern 7 — SSE streaming
Signature: docs mention "text/event-stream" / "stream=true" / `data: ...` chunks
Skeleton:
    r = requests.post(URL, headers={"Authorization": f"Bearer {api_key}"},
                      json={"stream": True, **payload}, stream=True, timeout=300)
    chunks = []
    for line in r.iter_lines():
        if line and line.startswith(b"data: "):
            data = line[6:]
            if data == b"[DONE]":
                break
            chunks.append(json.loads(data))
    return {"success": True, "chunks": chunks}
Pitfalls: don't `.json()` the response — it's a stream, not a single body.

### Pattern 8 — OAuth 2.0 client credentials
Signature: docs say "POST /oauth/token, exchange client_id + client_secret for access_token"
Skeleton:
    token_resp = requests.post(TOKEN_URL,
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": csec},
        timeout=30).json()
    access_token = token_resp["access_token"]
    r = requests.post(API_URL, headers={"Authorization": f"Bearer {access_token}"}, json=payload, timeout=60)
Pitfalls: cache the access_token; don't fetch a new one on every call. Tokens typically last 1 hour.

### Pattern 9 — Service account JSON (Google / GCP-style)
Signature: docs reference a JSON key file with `private_key` / `client_email`
Skeleton: use the official SDK (e.g. `google-auth` + `google-cloud-X`); doing the JWT signing by hand is error-prone.
Pitfalls: the SDK reads `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json` from env. Set that env var; don't pass the path through your harness.

### Pattern 10 — Pagination (cursor / offset / link-header)
Signature: response includes `next_cursor` / `next_page_token` / `Link: <url>; rel="next"` / `?offset=N&limit=M`
For test harnesses you usually want only the FIRST page — don't paginate in tests unless the test specifically checks pagination behavior.

## DECISION RULE
Look at the candidate's atlas (or saved api_spec.txt). Match the auth + delivery pattern to one of the 10 above. Copy the skeleton. Substitute URL / fields. THAT'S YOUR FIRST DRAFT. Don't write a custom solution when a pattern matches.
"""


# ---------------------------------------------------------------------------
# Pivot prompt — invoked by the dead-end detector
# ---------------------------------------------------------------------------

STRUCTURED_PIVOT_PROMPT = """
## STRUCTURED PIVOT REQUIRED

You've hit the same class of error twice in a row. "Try again" is not an
option. You must now write down a structured pivot:

1. **Blocker statement.** One sentence: "I cannot make the harness work
   because <root cause>." Be specific. "The API returns 400" is not a
   root cause; "The API requires multipart/form-data but I'm sending
   application/json" is.

2. **Three fundamentally different approaches.** Not three variations of
   the same approach. Examples of fundamentally different:
   - Switch from REST to the official SDK (or vice versa).
   - Switch auth header location (header → query → body, or vice versa).
   - Switch the request body format (json → multipart → x-www-form-urlencoded).
   - Switch endpoint (the docs may show a different one for your input type).
   - Use the OpenAPI spec directly to generate the call shape.
   - Use a documented cURL example verbatim and translate it to Python.

3. **Pick the most promising approach** — not the closest to what you
   already have, the one whose evidence is strongest in the docs.

4. **Abandon the current line entirely.** Delete or comment-out the
   broken code; start the new approach in a fresh file or fresh function.
   Don't try to massage the broken approach into the new shape.

After this pivot, you have one more shot. If THIS pivot also fails, pivot
once more (different approach again). Hard ceiling: 3 pivots per harness.
After 3 unsuccessful pivots, signal HARNESS_FAILED with a clear
attempt_summary listing what you tried.

DO NOT signal HARNESS_FAILED before completing at least one full pivot.
"""


# ---------------------------------------------------------------------------
# Live-test battery — what to run before HARNESS_COMPLETE
# ---------------------------------------------------------------------------

LIVE_TEST_BATTERY_PROMPT = """
## LIVE TEST BATTERY (REQUIRED BEFORE HARNESS_COMPLETE)

Smoke test (structural + one happy-path call) is NOT enough to declare
HARNESS_COMPLETE. You must run the live battery first — the same
adversarial probes the test runner will run against the harness later.

For every test_file_path you have available (or every input_data variant
the candidate accepts), call your harness:

  1. **happy_path:** representative input. MUST return success=True.
  2. **minimal:** smallest valid input (empty string for text, smallest
     allowed file for files). MUST return either success=True or
     success=False with a clear error. MUST NOT crash.
  3. **boundary:** input near the documented size/length ceiling. MUST
     return success=True or graceful error. MUST NOT silently truncate.
  4. **invalid_credential:** call once with a deliberately wrong
     credential value. MUST return success=False (NEVER success=True).
     This is the silent-corruption trap — the test runner's adversarial
     probe will catch it later, but you should catch it now.

If ANY of these four fails (crash, wrong success value, missing fields):
- Read the actual error.
- Fix the harness.
- Re-run the WHOLE battery (not just the failing case).
Do not signal HARNESS_COMPLETE until all four pass cleanly.

The battery costs 4-5 API calls per harness. That's negligible vs the
cost of declaring HARNESS_COMPLETE on a broken harness and corrupting
every Agent 3 test result.
"""


__all__ = [
    "API_PATTERNS_CATALOG",
    "LIVE_TEST_BATTERY_PROMPT",
    "STRUCTURED_PIVOT_PROMPT",
]
