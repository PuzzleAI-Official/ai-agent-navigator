import type { AgentModes, SSEEventData } from "@/types/pipeline";

const API_BASE = "/pzapi";

export async function createRun(
  text: string,
  fileIds: string[] = [],
  agentModes?: AgentModes
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
    }),
  });
  if (!res.ok) throw new Error(`Failed to create run: ${res.status}`);
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

export async function cancelRun(runId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/runs/${runId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`Cancel failed: ${res.status}`);
}

export function subscribeToEvents(
  runId: string,
  onEvent: (event: SSEEventData) => void,
  onError?: (error: Event) => void
): () => void {
  const eventSource = new EventSource(`${API_BASE}/runs/${runId}/events`);

  const handleMessage = (e: MessageEvent) => {
    try {
      const parsed = JSON.parse(e.data) as SSEEventData;
      onEvent(parsed);
    } catch {
      // ignore malformed events
    }
  };

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
    "done",
  ];

  for (const type of eventTypes) {
    eventSource.addEventListener(type, handleMessage);
  }

  // Also listen to generic messages
  eventSource.onmessage = handleMessage;

  eventSource.onerror = (e) => {
    onError?.(e);
    eventSource.close();
  };

  // Return cleanup function
  return () => {
    eventSource.close();
  };
}
