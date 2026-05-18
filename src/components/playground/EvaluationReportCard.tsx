// ============================================================================
// EvaluationReportCard — structured final report summary
// ============================================================================
// Renders the `EvaluationReport` produced by the backend's new assembler
// (`puzzleeval/report.py`). Consumed via the `evaluation_report` SSE event
// OR the `GET /runs/{id}/report` endpoint.
//
// Shape (sourced from `assemble_report()`):
//   {
//     run_id, trace_id, user_summary, domain, monthly_volume,
//     total_cost_usd, candidate_count, test_count,
//     coverage: { blueprint_step_ids, covered_step_ids, missing_step_ids, coverage_percent },
//     winners_by_scope: { step_id -> candidate_name },
//     overall_winner: string | null,
//     candidate_reports: [{
//       name, provider, rank, overall_score, pass_rate, passed_count, total_count,
//       avg_latency_ms, cost_usd_per_call, monthly_cost_projection_usd,
//       auth_method, requirements, auth_env_vars, sandbox_used,
//       failure_evidence: [{ test_case_id, scenario, passed, score, reasoning_excerpt }],
//       success_evidence: [{ ... }],
//       pros: string[], cons: string[],
//     }, ...],
//     advisories: string[]
//   }
// ============================================================================

import type { ReactNode } from "react";

// Playable audio artifact role — matches backend AudioArtifact in pipeline.ts.
interface AudioPath {
  role: string;
  path: string;
  token?: string;
}

/**
 * Renders an HTML5 audio control for one captured caller/agent WAV. The
 * path comes in as an absolute filesystem path from the backend (under
 * runs/<trace_id>/harnesses/<slug>/voice/); we route through the backend's
 * static audio endpoint that accepts `?path=` and streams the file back.
 *
 * Playback is purely local — the backend serves the file, the browser
 * renders standard <audio controls>. No third-party media player needed.
 */
// Color + label mapping for every known artifact role. Kept in one
// place so adding a new role (say, a recording of a customer-side
// hand-off) is a single-line change.
const ROLE_STYLE: Record<string, { label: string; color: string }> = {
  caller: { label: "Caller", color: "text-blue-400" },
  agent: { label: "Agent", color: "text-emerald-400" },
  // The plugin-merged full-conversation clip. Highlighted so the user
  // notices the "hit play to hear the whole call" option without having
  // to click each turn individually.
  conversation: { label: "Full call", color: "text-violet-300" },
};

function AudioClip({
  role,
  path,
  featured = false,
}: AudioPath & { featured?: boolean }) {
  // Encode the full absolute path so special characters (backslashes on
  // Windows, colons, spaces) survive the URL transport.
  const audioUrl = `/pzapi/runs/audio?path=${encodeURIComponent(path)}`;
  const style = ROLE_STYLE[role] ?? {
    label: role.charAt(0).toUpperCase() + role.slice(1),
    color: "text-zinc-300",
  };
  return (
    <div
      className={
        featured
          ? "flex items-center gap-2 text-sm mb-2 rounded border border-violet-700/40 bg-violet-900/10 p-2"
          : "flex items-center gap-2 text-xs mt-1"
      }
    >
      <span className={`${style.color} font-mono w-16 shrink-0`}>
        {style.label}
      </span>
      <audio
        controls
        preload="none"
        src={audioUrl}
        className={featured ? "h-9 w-full" : "h-8 max-w-xs"}
      />
    </div>
  );
}

function AudioPathsBlock({
  paths,
  mergedAudioPath,
}: {
  paths?: AudioPath[];
  mergedAudioPath?: string | null;
}) {
  const normalized = [...(paths ?? [])];
  if (
    mergedAudioPath &&
    !normalized.some((p) => p.role === "conversation" && p.path === mergedAudioPath)
  ) {
    normalized.unshift({ role: "conversation", path: mergedAudioPath });
  }
  if (normalized.length === 0) return null;
  // Split: "conversation" role(s) render first and wide (the whole call
  // is what most users want to hear); per-turn caller/agent clips
  // render below at the normal compact size.
  const featured = normalized.filter((p) => p.role === "conversation");
  const perTurn = normalized.filter((p) => p.role !== "conversation");
  return (
    <div className="mt-2 rounded border border-zinc-800 bg-zinc-900/40 p-2">
      <div className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1">
        Audio ({normalized.length})
      </div>
      {featured.map((ap, i) => (
        <AudioClip key={`f${i}`} role={ap.role} path={ap.path} featured />
      ))}
      {perTurn.map((ap, i) => (
        <AudioClip key={`t${i}`} role={ap.role} path={ap.path} />
      ))}
    </div>
  );
}

// Per-criterion rubric score + reasoning, rendered as a progress bar with
// evidence turn indices highlighted. `critical` indicates this criterion
// tripped a critical-gate failure — rendered in red to match the backend's
// RubricVerdict.critical_failures signal.
function RubricScoreBar({
  name,
  score,
  reasoning,
  critical,
  evidenceIndices,
}: {
  name: string;
  score: number;
  reasoning: string;
  critical: boolean;
  evidenceIndices: number[];
}) {
  const pct = Math.max(0, Math.min(100, score * 100));
  const barColor = critical
    ? "bg-rose-500"
    : score >= 0.75
    ? "bg-emerald-500"
    : score >= 0.5
    ? "bg-amber-500"
    : "bg-zinc-600";
  return (
    <div className="mb-2">
      <div className="flex items-baseline justify-between text-[11px]">
        <span className={critical ? "text-rose-400 font-medium" : "text-zinc-300"}>
          {name}
          {critical && (
            <span className="ml-1 rounded bg-rose-500/20 px-1 py-px text-[9px] uppercase">
              critical fail
            </span>
          )}
        </span>
        <span className="text-zinc-500">{pct.toFixed(0)}%</span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded-full bg-zinc-800">
        <div
          className={`h-full rounded-full ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {reasoning && (
        <div className="mt-1 text-[10px] text-zinc-500 italic">
          {reasoning}
          {evidenceIndices.length > 0 && (
            <span className="ml-1 text-zinc-600">
              (turns {evidenceIndices.join(", ")})
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// Rubric verdict + transcript card for agentic conversational tests.
// Renders the conversation_summary headline, per-criterion bars (with
// critical failures in red), and an expandable transcript. Returns null
// when the test didn't produce a rubric verdict (non-conversational or
// scripted-mode tests).
function RubricBreakdownBlock({
  verdict,
  transcript,
}: {
  verdict?: {
    overall_score: number;
    passed: boolean;
    criterion_scores?: Array<{
      criterion_name: string;
      score: number;
      reasoning: string;
      evidence_turn_indices?: number[];
    }>;
    conversation_summary?: string;
    critical_failures?: string[];
  };
  transcript?: Array<{
    turn_index: number;
    role: string;
    text: string;
  }>;
}) {
  if (!verdict) return null;
  const critFailures = verdict.critical_failures ?? [];
  const scores = verdict.criterion_scores ?? [];
  const hasTranscript = transcript && transcript.length > 0;
  return (
    <div className="mt-2 rounded border border-zinc-800 bg-zinc-900/30 p-2">
      <div className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1">
        Rubric breakdown
      </div>
      {verdict.conversation_summary && (
        <div className="mb-2 text-[11px] text-zinc-300 italic">
          {verdict.conversation_summary}
        </div>
      )}
      {critFailures.length > 0 && (
        <div className="mb-2 rounded bg-rose-500/10 px-2 py-1 text-[10px] text-rose-300">
          Critical failure(s): {critFailures.join(", ")}
        </div>
      )}
      <div className="space-y-1">
        {scores.map((s) => (
          <RubricScoreBar
            key={s.criterion_name}
            name={s.criterion_name}
            score={s.score}
            reasoning={s.reasoning}
            critical={critFailures.includes(s.criterion_name)}
            evidenceIndices={s.evidence_turn_indices ?? []}
          />
        ))}
      </div>
      {hasTranscript && (
        <details className="mt-2 text-[10px]">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            Full transcript ({transcript!.length} turns)
          </summary>
          <div className="mt-1 space-y-1">
            {transcript!.map((t, i) => (
              <div key={i} className="rounded bg-zinc-900/50 p-1.5">
                <div
                  className={`text-[9px] uppercase ${
                    t.role === "user" ? "text-blue-400" : "text-emerald-400"
                  }`}
                >
                  Turn {t.turn_index} · {t.role === "user" ? "Caller" : "Agent"}
                </div>
                <div className="text-zinc-300">{t.text || "(empty)"}</div>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

interface EvidenceRow {
  test_case_id?: string;
  scenario?: string;
  passed?: boolean;
  score?: number;
  reasoning_excerpt?: string;
  // Populated by voice/audio plugins (voice_realtime caller + agent WAVs).
  // Each path is absolute under runs/<trace_id>/harnesses/<slug>/voice/.
  // Playable via the /runs/{id}/audio?path=... endpoint.
  audio_paths?: AudioPath[];
  // Stable pointer to the merged full-conversation recording. The backend
  // also mirrors this as audio_paths[{ role: "conversation" }].
  merged_audio_path?: string | null;
  judge_failed?: boolean;
  judge_failure_reason?: string | null;
  // Rubric verdict from the agentic conversational eval path. Mirrors
  // RubricVerdict shape — see src/types/pipeline.ts. Null/undefined for
  // non-conversational tests and for scripted-mode conversations.
  rubric_verdict?: {
    overall_score: number;
    passed: boolean;
    criterion_scores?: Array<{
      criterion_name: string;
      score: number;
      reasoning: string;
      evidence_turn_indices?: number[];
    }>;
    conversation_summary?: string;
    critical_failures?: string[];
  };
  // Per-turn transcript for conversational tests (agentic mode).
  // Renders inside the expandable rubric breakdown panel. Empty/absent
  // for non-conversational and scripted-mode tests.
  transcript?: Array<{
    turn_index: number;
    role: string;
    text: string;
    meta?: Record<string, unknown>;
  }>;
}

interface ForensicsEvent {
  // Free-form JSONL-decoded forensics events. Common fields per the
  // canonical taxonomy: t_ms, t_abs, kind, event, op, provider,
  // url_host, status_code, duration_ms, error_type, error.
  t_ms?: number;
  t_abs?: number;
  kind?: string;
  event?: string;
  [key: string]: unknown;
}

interface CandidateReport {
  name?: string;
  provider?: string;
  rank?: number;
  overall_score?: number;
  pass_rate?: number;
  passed_count?: number;
  total_count?: number;
  avg_latency_ms?: number | null;
  cost_usd_per_call?: number | null;
  monthly_cost_projection_usd?: number | null;
  auth_method?: string;
  requirements?: string[];
  auth_env_vars?: string[];
  sandbox_used?: boolean;
  failure_evidence?: EvidenceRow[];
  success_evidence?: EvidenceRow[];
  test_evidence?: EvidenceRow[];
  pros?: string[];
  cons?: string[];
  // Build-status disclosure (from report.py's failed-build surfacing).
  // When `build_succeeded === false`, the harness either failed
  // adversarial validation OR the build loop emitted FailedHarness.
  // The candidate row renders critical_failures + forensics_tail in
  // a dedicated <ForensicsBlock> so the user can see WHY it failed.
  build_succeeded?: boolean;
  abandoned?: boolean;
  abandon_reason?: string | null;
  critical_failures?: string[];
  forensics_tail?: ForensicsEvent[];
}

interface EfficiencySummary {
  migration_flags?: Record<string, boolean>;
  totals?: {
    candidates_attempted?: number;
    total_turns_observed?: number;
    total_cost_usd?: number;
    agent5_build_cost_usd?: number;
    agent5_test_cost_usd?: number;
  };
  turns_by_phase?: Record<string, number>;
  cost_by_phase_usd?: Record<string, number>;
  latency_by_phase_ms?: Record<string, number>;
  research?: Record<string, unknown>;
  blocked_fetches?: Record<string, unknown>;
  artifact_overhead?: Record<string, unknown>;
  failure_packets?: {
    count?: number;
    by_category?: Record<string, number>;
  };
  provider_health?: {
    status_counts?: Record<string, number>;
    failure_categories?: Record<string, number>;
  };
  abandoned_candidates?: {
    count?: number;
    by_reason?: Record<string, number>;
  };
  candidates?: Array<Record<string, unknown>>;
}

interface EvaluationReport {
  user_summary?: string;
  domain?: string;
  monthly_volume?: number | null;
  total_cost_usd?: number;
  candidate_count?: number;
  test_count?: number;
  coverage?: {
    blueprint_step_ids?: string[];
    covered_step_ids?: string[];
    missing_step_ids?: string[];
    coverage_percent?: number;
  };
  winners_by_scope?: Record<string, string>;
  overall_winner?: string | null;
  candidate_reports?: CandidateReport[];
  advisories?: string[];
  efficiency_summary?: EfficiencySummary | null;
}

interface Props {
  report: Record<string, unknown>;
}

export function EvaluationReportCard({ report }: Props): ReactNode {
  const r = report as EvaluationReport;
  const winners = r.winners_by_scope ?? {};
  const advisories = r.advisories ?? [];
  const candidates = r.candidate_reports ?? [];
  const coverage = r.coverage;
  const coveragePct = coverage?.coverage_percent ?? 0;

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4 space-y-4">
      {/* Header */}
      <div>
        <h3 className="text-lg font-semibold text-zinc-100">
          Evaluation Report
        </h3>
        <p className="text-sm text-zinc-400 mt-1">
          {r.user_summary || "Final evaluation report"}
        </p>
      </div>

      {/* Summary row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        <Metric
          label="Winner"
          value={r.overall_winner ?? "—"}
          emphasis={Boolean(r.overall_winner)}
        />
        <Metric
          label="Candidates"
          value={String(r.candidate_count ?? 0)}
        />
        <Metric
          label="Tests run"
          value={String(r.test_count ?? 0)}
        />
        <Metric
          label="Total cost"
          value={
            typeof r.total_cost_usd === "number"
              ? `$${r.total_cost_usd.toFixed(4)}`
              : "—"
          }
        />
      </div>

      {/* Coverage */}
      {coverage && coverage.blueprint_step_ids && coverage.blueprint_step_ids.length > 0 && (
        <div className="text-sm">
          <div className="text-zinc-400 mb-1">
            Scope coverage:{" "}
            <span className={coveragePct >= 1 ? "text-green-400" : "text-amber-400"}>
              {(coveragePct * 100).toFixed(0)}%
            </span>
          </div>
          {coverage.missing_step_ids && coverage.missing_step_ids.length > 0 && (
            <div className="text-xs text-amber-400">
              Missing: {coverage.missing_step_ids.join(", ")}
            </div>
          )}
        </div>
      )}

      {/* Per-scope winners */}
      {Object.keys(winners).length > 0 && (
        <div className="text-sm">
          <div className="text-zinc-400 mb-1">Per-scope winners:</div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(winners).map(([scope, name]) => (
              <div
                key={scope}
                className="text-xs bg-zinc-900 border border-zinc-800 rounded px-2 py-1"
              >
                <span className="text-zinc-500">{scope}:</span>{" "}
                <span className="text-zinc-100">{name}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Advisories */}
      {advisories.length > 0 && (
        <div className="text-sm bg-amber-950/30 border border-amber-900 rounded p-3 space-y-1">
          {advisories.map((a, i) => (
            <div key={i} className="text-xs text-amber-200">
              • {a}
            </div>
          ))}
        </div>
      )}

      <EfficiencySummaryBlock summary={r.efficiency_summary} />

      {/* Per-candidate detail (top 5 ranked) */}
      {candidates.length > 0 && (
        <div className="space-y-2">
          <div className="text-sm text-zinc-400">Ranked candidates:</div>
          {candidates.slice(0, 5).map((c) => (
            <CandidateRow key={c.name ?? String(c.rank ?? Math.random())} c={c} />
          ))}
        </div>
      )}
    </div>
  );
}

function countMapLabel(map?: Record<string, number>): string {
  const entries = Object.entries(map ?? {}).filter(([, v]) => Number(v) > 0);
  if (entries.length === 0) return "none";
  return entries.map(([k, v]) => `${k}: ${v}`).join(", ");
}

function numericField(obj: Record<string, unknown> | undefined, key: string): number {
  const value = obj?.[key];
  return typeof value === "number" ? value : 0;
}

function EfficiencySummaryBlock({
  summary,
}: {
  summary?: EfficiencySummary | null;
}) {
  if (!summary) return null;
  const phases = Object.keys(summary.turns_by_phase ?? {});
  const research = summary.research ?? {};
  const blocked = summary.blocked_fetches ?? {};
  const overhead = summary.artifact_overhead ?? {};
  const flags = summary.migration_flags ?? {};
  return (
    <details className="rounded border border-zinc-800 bg-zinc-900/40 p-3 text-xs">
      <summary className="cursor-pointer text-sm text-zinc-300">
        Build efficiency summary
      </summary>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-zinc-500">
            Phase rollup
          </div>
          <div className="space-y-1 text-zinc-300">
            {phases.length > 0 ? (
              phases.map((phase) => (
                <div key={phase} className="flex justify-between gap-2">
                  <span>{phase}</span>
                  <span className="font-mono text-zinc-400">
                    {summary.turns_by_phase?.[phase] ?? 0} turns / $
                    {(summary.cost_by_phase_usd?.[phase] ?? 0).toFixed(4)}
                  </span>
                </div>
              ))
            ) : (
              <div className="text-zinc-500">No per-phase data captured.</div>
            )}
          </div>
        </div>
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-zinc-500">
            Research and fetches
          </div>
          <div className="space-y-1 text-zinc-300">
            <div>research turns: {numericField(research, "turns")}</div>
            <div>web searches: {numericField(research, "web_search_results")}</div>
            <div>web fetches: {numericField(research, "web_fetch_results")}</div>
            <div>cache hit: {numericField(research, "cache_hit_pct").toFixed(1)}%</div>
            <div>blocked fetches: {numericField(blocked, "web_fetch_blocks")}</div>
            <div>empty fetches: {numericField(blocked, "empty_web_fetch_results")}</div>
          </div>
        </div>
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wide text-zinc-500">
            Recovery signals
          </div>
          <div className="space-y-1 text-zinc-300">
            <div>artifact-only turns: {numericField(overhead, "artifact_only_turns")}</div>
            <div>diagnostic turns: {numericField(overhead, "diagnostic_script_turns")}</div>
            <div>failure packets: {summary.failure_packets?.count ?? 0}</div>
            <div>abandoned: {summary.abandoned_candidates?.count ?? 0}</div>
            <div>packet categories: {countMapLabel(summary.failure_packets?.by_category)}</div>
            <div>abandon reasons: {countMapLabel(summary.abandoned_candidates?.by_reason)}</div>
          </div>
        </div>
      </div>
      {Object.keys(flags).length > 0 && (
        <div className="mt-3 border-t border-zinc-800 pt-2">
          <div className="mb-1 text-[10px] uppercase tracking-wide text-zinc-500">
            Migration flags
          </div>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(flags).map(([name, enabled]) => (
              <span
                key={name}
                className={
                  "rounded border px-1.5 py-0.5 font-mono text-[10px] " +
                  (enabled
                    ? "border-emerald-900/60 text-emerald-300"
                    : "border-amber-900/60 text-amber-300")
                }
              >
                {name.replace("PUZZLEEVAL_", "")}={enabled ? "1" : "0"}
              </span>
            ))}
          </div>
        </div>
      )}
    </details>
  );
}

function Metric({ label, value, emphasis = false }: { label: string; value: string; emphasis?: boolean }) {
  return (
    <div>
      <div className="text-xs text-zinc-500 uppercase tracking-wide">
        {label}
      </div>
      <div
        className={
          emphasis
            ? "text-base font-semibold text-green-400"
            : "text-base text-zinc-100"
        }
      >
        {value}
      </div>
    </div>
  );
}

function ForensicsBlock({
  criticalFailures,
  forensicsTail,
}: {
  criticalFailures?: string[];
  forensicsTail?: ForensicsEvent[];
}) {
  const failures = criticalFailures ?? [];
  const tail = forensicsTail ?? [];
  if (failures.length === 0 && tail.length === 0) {
    return null;
  }
  // Show last 20 events even though the backend captures up to 50 — the
  // "explore the full log" path is read_forensics from Agent 5; this UI
  // surface is for the operator's quick glance.
  const visibleTail = tail.slice(-20);
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-amber-400 hover:text-amber-300 font-medium">
        Forensics — {failures.length} critical issue
        {failures.length === 1 ? "" : "s"}
        {tail.length > 0 && (
          <> · last {visibleTail.length} of {tail.length} events</>
        )}
      </summary>
      <div className="mt-2 space-y-2 pl-3 border-l border-amber-900/40">
        {failures.length > 0 && (
          <div>
            <div className="text-zinc-300 mb-1">Critical failures:</div>
            <ul className="space-y-1">
              {failures.map((f, i) => (
                <li key={i} className="text-amber-300/90 font-mono text-[11px]">
                  • {f}
                </li>
              ))}
            </ul>
          </div>
        )}
        {visibleTail.length > 0 && (
          <div>
            <div className="text-zinc-300 mb-1">Last events:</div>
            <pre className="max-h-64 overflow-auto bg-zinc-950/80 border border-zinc-800 rounded p-2 text-[10px] leading-tight whitespace-pre-wrap break-all">
              {visibleTail
                .map((ev) => JSON.stringify(ev))
                .join("\n")}
            </pre>
          </div>
        )}
      </div>
    </details>
  );
}


function CandidateRow({ c }: { c: CandidateReport }) {
  const passPct = typeof c.pass_rate === "number" ? c.pass_rate * 100 : 0;
  const monthly = c.monthly_cost_projection_usd;
  const buildFailed = c.build_succeeded === false;
  const abandoned = c.abandoned === true;
  const evidenceRows = c.test_evidence?.length
    ? c.test_evidence
    : [...(c.failure_evidence ?? []), ...(c.success_evidence ?? [])];
  const callEvidence = evidenceRows.filter(
    (ev) =>
      Boolean(ev.merged_audio_path) ||
      Boolean(ev.audio_paths?.some((ap) => ap.role === "conversation"))
  );
  return (
    <div
      className={
        "rounded border p-3 space-y-2 " +
        (buildFailed
          ? abandoned
            ? "bg-zinc-900/60 border-zinc-700/70"
            : "bg-amber-950/20 border-amber-900/50"
          : "bg-zinc-900/70 border-zinc-800")
      }
    >
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <div>
          <span className="text-xs text-zinc-500 mr-2">#{c.rank ?? "?"}</span>
          <span className="text-sm font-medium text-zinc-100">{c.name}</span>
          {c.provider && (
            <span className="text-xs text-zinc-500 ml-2">· {c.provider}</span>
          )}
          {buildFailed && (
            <span className="ml-2 text-[10px] uppercase tracking-wider text-amber-400 font-semibold">
              {abandoned ? "abandoned" : "build failed validation"}
            </span>
          )}
        </div>
        <div className="text-xs text-zinc-400">
          {buildFailed ? (
            <span className="text-amber-400/90">
              {abandoned
                ? `abandoned${c.abandon_reason ? `: ${c.abandon_reason}` : ""} - see evidence`
                : "not tested - see forensics"}
            </span>
          ) : (
            <>
              {c.passed_count ?? 0}/{c.total_count ?? 0} passed ({passPct.toFixed(0)}%) ·{" "}
              {typeof c.overall_score === "number"
                ? `score ${(c.overall_score * 100).toFixed(0)}%`
                : "—"}
              {typeof monthly === "number" && (
                <> · est. ${monthly.toFixed(2)}/mo</>
              )}
              {c.sandbox_used && (
                <span className="ml-2 text-amber-400">[sandbox]</span>
              )}
            </>
          )}
        </div>
      </div>
      {buildFailed && (
        <ForensicsBlock
          criticalFailures={c.critical_failures}
          forensicsTail={c.forensics_tail}
        />
      )}
      {(c.pros?.length || c.cons?.length) && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
          {c.pros && c.pros.length > 0 && (
            <div>
              {c.pros.map((p, i) => (
                <div key={i} className="text-green-400">
                  + {p}
                </div>
              ))}
            </div>
          )}
          {c.cons && c.cons.length > 0 && (
            <div>
              {c.cons.map((p, i) => (
                <div key={i} className="text-zinc-400">
                  − {p}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {callEvidence.length > 0 && (
        <div className="rounded border border-zinc-800 bg-zinc-950/40 p-2 text-xs">
          <div className="mb-2 flex items-baseline justify-between gap-2">
            <div className="text-[10px] uppercase tracking-wide text-zinc-500">
              Full call recordings
            </div>
            <div className="text-[10px] text-zinc-600">
              {callEvidence.length} test{callEvidence.length === 1 ? "" : "s"}
            </div>
          </div>
          <div className="space-y-2">
            {callEvidence.map((ev, i) => (
              <div key={`${ev.test_case_id ?? "test"}-${i}`}>
                <div className="mb-1 flex items-center justify-between gap-2 text-zinc-300">
                  <span>{ev.scenario || ev.test_case_id || `Test ${i + 1}`}</span>
                  <span className={ev.passed ? "text-emerald-400" : "text-amber-400"}>
                    {ev.passed ? "passed" : "failed"}
                  </span>
                </div>
                <AudioPathsBlock
                  paths={(ev.audio_paths ?? []).filter((ap) => ap.role === "conversation")}
                  mergedAudioPath={ev.merged_audio_path}
                />
              </div>
            ))}
          </div>
        </div>
      )}
      {c.failure_evidence && c.failure_evidence.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            Failure evidence ({c.failure_evidence.length})
          </summary>
          <div className="mt-1 space-y-2 pl-3 border-l border-zinc-800">
            {c.failure_evidence.map((ev, i) => (
              <div key={i}>
                <div className="text-zinc-300">
                  {ev.scenario || ev.test_case_id}
                </div>
                {ev.reasoning_excerpt && (
                  <div className="text-zinc-500 italic">
                    {ev.reasoning_excerpt}
                  </div>
                )}
                {ev.judge_failed && (
                  <div className="text-amber-400">
                    Judge failed: {ev.judge_failure_reason || "rubric verdict unavailable"}
                  </div>
                )}
                <AudioPathsBlock
                  paths={ev.audio_paths}
                  mergedAudioPath={ev.merged_audio_path}
                />
                <RubricBreakdownBlock
                  verdict={ev.rubric_verdict}
                  transcript={ev.transcript}
                />
              </div>
            ))}
          </div>
        </details>
      )}
      {c.success_evidence && c.success_evidence.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            Success evidence ({c.success_evidence.length})
          </summary>
          <div className="mt-1 space-y-2 pl-3 border-l border-emerald-900/40">
            {c.success_evidence.map((ev, i) => (
              <div key={i}>
                <div className="text-zinc-300">
                  {ev.scenario || ev.test_case_id}
                </div>
                {ev.reasoning_excerpt && (
                  <div className="text-zinc-500 italic">
                    {ev.reasoning_excerpt}
                  </div>
                )}
                {ev.judge_failed && (
                  <div className="text-amber-400">
                    Judge failed: {ev.judge_failure_reason || "rubric verdict unavailable"}
                  </div>
                )}
                <AudioPathsBlock
                  paths={ev.audio_paths}
                  mergedAudioPath={ev.merged_audio_path}
                />
                <RubricBreakdownBlock
                  verdict={ev.rubric_verdict}
                  transcript={ev.transcript}
                />
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
