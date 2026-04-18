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

function AudioPathsBlock({ paths }: { paths?: AudioPath[] }) {
  if (!paths || paths.length === 0) return null;
  // Split: "conversation" role(s) render first and wide (the whole call
  // is what most users want to hear); per-turn caller/agent clips
  // render below at the normal compact size.
  const featured = paths.filter((p) => p.role === "conversation");
  const perTurn = paths.filter((p) => p.role !== "conversation");
  return (
    <div className="mt-2 rounded border border-zinc-800 bg-zinc-900/40 p-2">
      <div className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1">
        Audio ({paths.length})
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
  pros?: string[];
  cons?: string[];
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

function CandidateRow({ c }: { c: CandidateReport }) {
  const passPct = typeof c.pass_rate === "number" ? c.pass_rate * 100 : 0;
  const monthly = c.monthly_cost_projection_usd;
  return (
    <div className="rounded bg-zinc-900/70 border border-zinc-800 p-3 space-y-2">
      <div className="flex items-baseline justify-between gap-4 flex-wrap">
        <div>
          <span className="text-xs text-zinc-500 mr-2">#{c.rank ?? "?"}</span>
          <span className="text-sm font-medium text-zinc-100">{c.name}</span>
          {c.provider && (
            <span className="text-xs text-zinc-500 ml-2">· {c.provider}</span>
          )}
        </div>
        <div className="text-xs text-zinc-400">
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
        </div>
      </div>
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
                <AudioPathsBlock paths={ev.audio_paths} />
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
                <AudioPathsBlock paths={ev.audio_paths} />
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
