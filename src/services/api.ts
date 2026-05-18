import type {
  AgentModes,
  Plan,
  RunStateOut,
  SSEEventData,
  SelectCandidatesRequest,
  SelectCandidatesResponse,
} from "@/types/pipeline";

// VITE_API_BASE override lets the frontend point at a different backend
// without rebuilding (e.g. staging vs prod, or a remote dev backend during
// local UI work). Falls back to the dev-proxy mount /pzapi when unset.
const API_BASE: string = (import.meta as { env?: { VITE_API_BASE?: string } }).env?.VITE_API_BASE ?? "/pzapi";

export async function createRun(
  text: string,
  fileIds: string[] = [],
  agentModes?: AgentModes,
  plan: Plan = "free"
): Promise<{ run_id: string; trace_id: string }> {
  const res = await fetch(`${API_BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      file_ids: fileIds,
      agent_modes: agentModes ?? {
        agent1: "mock",
        agent2: "mock",
        agent3: "mock",
        agent4: "mock",
        agent5: "mock",
      },
      plan,
    }),
  });
  if (!res.ok) throw new Error(`Failed to create run: ${res.status}`);
  return res.json();
}

// Phase 2: poll the run state to refresh QuotaBadge after gates trigger.
// Cheap GET; the QuotaBadge calls this on a slow interval (e.g. every 5s
// while the pipeline is running) to keep "credits remaining" current.
export async function getRunState(runId: string): Promise<RunStateOut> {
  const res = await fetch(`${API_BASE}/runs/${runId}`);
  if (!res.ok) throw new Error(`Failed to get run state: ${res.status}`);
  return res.json();
}

export async function sendMessage(
  runId: string,
  message: string,
  fileIds: string[] = []
): Promise<{
  is_clear: boolean;
  assistant_message: string;
  clarifying_questions: string[];
  pipeline_started: boolean;
}> {
  const res = await fetch(`${API_BASE}/runs/${runId}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, file_ids: fileIds }),
  });
  if (!res.ok) throw new Error(`Chat failed: ${res.status}`);
  return res.json();
}

export async function uploadFiles(
  runId: string,
  files: File[]
): Promise<{ file_ids: string[]; filenames: string[] }> {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  const res = await fetch(`${API_BASE}/runs/${runId}/files`, {
    method: "POST",
    body: formData,
  });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}

// Phase 6: submit per-scope candidate picks + user-added providers.
export async function selectCandidates(
  runId: string,
  request: SelectCandidatesRequest
): Promise<SelectCandidatesResponse> {
  const res = await fetch(`${API_BASE}/runs/${runId}/select-candidates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`Selection failed (${res.status}): ${detail}`);
  }
  return res.json();
}

export async function cancelRun(runId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/runs/${runId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Cancel failed: ${res.status}`);
}

/** Status of the SSE connection — surfaced via onStatusChange so the
 * UI can render a "reconnecting…" banner instead of silently going dead.
 */
export type SSEConnectionStatus = "connecting" | "open" | "reconnecting" | "closed";

export function subscribeToEvents(
  runId: string,
  onEvent: (event: SSEEventData) => void,
  onError?: (error: Event) => void,
  onStatusChange?: (status: SSEConnectionStatus) => void,
): () => void {
  // Auto-reconnect with exponential backoff. Without this, a single network
  // blip / server restart silently kills the event stream and the user sees
  // a stuck-in-loading UI with no recovery affordance. The reconnect respects
  // the standard SSE Last-Event-ID semantics so the backend can resume from
  // the right offset (when supported).
  let eventSource: EventSource | null = null;
  let manuallyClosed = false;
  let reconnectAttempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let lastEventId: string | null = null;

  const setStatus = (s: SSEConnectionStatus) => {
    try {
      onStatusChange?.(s);
    } catch {
      // never let a status callback break the subscription
    }
  };

  const handleMessage = (e: MessageEvent) => {
    if (e.lastEventId) {
      lastEventId = e.lastEventId;
    }
    try {
      const parsed = JSON.parse(e.data) as SSEEventData;
      onEvent(parsed);
    } catch {
      // ignore malformed events
    }
  };

  const scheduleReconnect = () => {
    if (manuallyClosed) return;
    setStatus("reconnecting");
    // Exponential backoff capped at 30s (1s, 2s, 4s, 8s, 16s, 30s, 30s, …).
    const delayMs = Math.min(30_000, 1_000 * Math.pow(2, reconnectAttempt));
    reconnectAttempt += 1;
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delayMs);
  };

  const connect = () => {
    if (manuallyClosed) return;
    setStatus("connecting");
    const url = lastEventId
      ? `${API_BASE}/runs/${runId}/events?last_event_id=${encodeURIComponent(lastEventId)}`
      : `${API_BASE}/runs/${runId}/events`;
    eventSource = new EventSource(url);

  // Listen to all named event types
  const eventTypes = [
    "pipeline_started",
    "agent_started",
    "agent_completed",
    "candidates_found",
    "candidates_verified",
    "test_cases_ready",
    "harness_started",
    "harness_completed",
    "harness_failed",
    "test_execution_started",
    "test_result",
    "candidate_results_ready",
    "pipeline_completed",
    "pipeline_failed",
    "pipeline_cancelled",
    "cost_update",
    "agent_activity",
    "agent_thinking",
    "candidates_selected",
    "report_generating",
    "agent_blocked",       // Phase 2: emitted when billing gate denies an agent
    "workflow_blueprint",  // Phase 3: emitted after Agent 1 with the blueprint payload
    "selection_required",  // Phase 6: pipeline paused, awaiting user candidate picks
    "candidate_verified",  // Per-candidate selected-candidate verification result (per scope)
    "candidate_rejected",  // Per-candidate selected-candidate rejection (per scope)
    "scope_verified_complete", // Per-scope summary (verified + rejected counts)
    "test_data_sufficiency",   // Modality-aware verdict per file-requiring scope: READY / AUGMENT / SYNTHESIZE / REQUEST_MORE / DEGRADE
    "coverage_gap",            // Backend warns when Agent 2 found 0 candidates or scopes have no coverage
    "evaluation_report",       // Final structured EvaluationReport assembled at pipeline_completed time
    "done",
  ];

    for (const type of eventTypes) {
      eventSource.addEventListener(type, handleMessage);
    }

    // Also listen to generic messages
    eventSource.onmessage = handleMessage;

    eventSource.onopen = () => {
      reconnectAttempt = 0; // reset backoff on success
      setStatus("open");
    };

    eventSource.onerror = (e) => {
      onError?.(e);
      try {
        eventSource?.close();
      } catch {
        // ignore close errors
      }
      eventSource = null;
      scheduleReconnect();
    };
  };

  connect();

  // Return cleanup function — manualClose stops auto-reconnect.
  return () => {
    manuallyClosed = true;
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    try {
      eventSource?.close();
    } catch {
      // ignore close errors
    }
    setStatus("closed");
  };
}


/** Fetch the persisted EvaluationReport for a completed (or in-flight) run.
 *
 * Backend prefers the on-disk artifact at runs/<trace>/evaluation_report.json,
 * falling back to on-demand assembly if the file isn't there yet. Either path
 * returns the same shape, so callers don't branch.
 */
export async function getEvaluationReport(runId: string): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_BASE}/runs/${runId}/report`);
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`Report fetch failed (${res.status}): ${detail}`);
  }
  return res.json();
}
