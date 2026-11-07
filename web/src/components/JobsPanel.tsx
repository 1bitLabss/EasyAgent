import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/api";
import { formatElapsed, formatTokenCount } from "@/lib/run";
import { cn } from "@/lib/utils";
import { useApp } from "@/store";
import { Elapsed } from "./Elapsed";

/** Same breakpoint as the mobile layout rules in index.css. */
const NARROW_QUERY = "(max-width: 980px), (hover: none) and (pointer: coarse)";

function useNarrowLayout() {
  const [narrow, setNarrow] = useState(() => window.matchMedia?.(NARROW_QUERY).matches ?? false);
  useEffect(() => {
    if (!window.matchMedia) return;
    const query = window.matchMedia(NARROW_QUERY);
    const onChange = () => setNarrow(query.matches);
    query.addEventListener("change", onChange);
    onChange();
    return () => query.removeEventListener("change", onChange);
  }, []);
  return narrow;
}

export type JobRow = {
  id: string;
  bot_id: string;
  bot_name: string;
  name: string;
  describe?: string;
  next_run?: string | null;
  last_run_at?: string | null;
  last_status?: string;
  last_error?: string;
  paused?: boolean;
  running?: boolean;
  queued_for?: string;
  state?: string;
};

/** A child agent working right now, from the live registry. */
export type LiveChildRow = {
  child_id: string;
  name: string;
  task_excerpt: string;
  bot_id: string;
  chat_id: string | null;
  started_at: number;
};

/** Tokens stamped on finished replies today, summed on the server. */
export type UsageToday = {
  date: string;
  prompt_tokens: number;
  completion_tokens: number;
};

/** "1.2k in / 340 out" from the rollup. Empty when nothing is known yet. */
export function usageToday(data?: UsageToday | null): string {
  if (!data) return "";
  const prompt = Number(data.prompt_tokens) || 0;
  const completion = Number(data.completion_tokens) || 0;
  if (prompt <= 0 && completion <= 0) return "";
  return `${formatTokenCount(prompt)} in / ${formatTokenCount(completion)} out`;
}

function when(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" });
}

export function JobsPanel() {
  const narrow = useNarrowLayout();
  const setJobs = useApp((state) => state.setJobs);
  const highlight = useApp((state) => state.highlightJob);
  const openBot = useApp((state) => state.openBot);
  const queryClient = useQueryClient();
  const jobs = useQuery({
    queryKey: ["routines"],
    queryFn: () => api<JobRow[]>("/api/routines"),
    refetchInterval: 4000,
  });
  const children = useQuery({
    queryKey: ["children"],
    queryFn: () => api<LiveChildRow[]>("/api/children"),
    refetchInterval: 4000,
  });
  const usage = useQuery({
    queryKey: ["usage-today"],
    queryFn: () => api<UsageToday>("/api/usage/today"),
    refetchInterval: 30000,
  });
  const rows = jobs.data || [];
  const working = children.data || [];
  const busy = rows.filter((row) => row.state === "running" || row.state === "queued").length;

  async function pause(row: JobRow) {
    const pausing = row.state !== "paused" && row.state !== "done";
    queryClient.setQueryData<JobRow[]>(["routines"], (current) =>
      (current || []).map((item) =>
        item.id === row.id && item.bot_id === row.bot_id
          ? { ...item, state: pausing ? "paused" : item.last_run_at || item.last_status ? "ok" : "not run yet", paused: pausing, next_run: pausing ? null : item.next_run }
          : item,
      ),
    );
    await api(`/api/bots/${row.bot_id}/schedules/${row.id}/pause`, {
      method: "POST",
      json: { paused: pausing },
    });
    await queryClient.invalidateQueries({ queryKey: ["routines"] });
  }

  async function stop(row: JobRow) {
    await api(`/api/bots/${row.bot_id}/schedules/${row.id}/stop`, { method: "POST" });
    await queryClient.invalidateQueries({ queryKey: ["routines"] });
  }

  return (
    <aside
      className={cn(
        "flex h-full shrink-0 flex-col bg-background",
        narrow ? "jobs-panel w-full border-t border-border" : "w-[360px] border-l border-border",
      )}
      data-testid="jobs-panel"
    >
      <header className="flex items-center justify-between px-3 py-3">
        <h2 className="text-sm font-semibold">Jobs {busy ? <span data-testid="jobs-badge">{busy}</span> : null}</h2>
        <button type="button" className="text-sm text-muted" onClick={() => setJobs(false)} aria-label="Close jobs">Close</button>
      </header>
      <section className="border-b border-border px-3 pb-3" data-testid="working-now">
        <h3 className="mb-1 text-xs font-semibold uppercase text-muted">Working now</h3>
        {children.isLoading ? (
          <p className="text-sm text-muted">Checking who is working…</p>
        ) : working.length === 0 ? (
          <p className="text-sm text-muted" data-testid="working-now-empty">Nothing is working.</p>
        ) : (
          <ul className="space-y-2">
            {working.map((child) => (
              <li key={child.child_id} data-testid="working-child-row" data-child-id={child.child_id}>
                <button
                  type="button"
                  className="block w-full rounded-lg border border-border p-2 text-left text-sm"
                  disabled={!child.chat_id}
                  onClick={() => child.chat_id && void openBot(child.bot_id, child.chat_id)}
                >
                  <strong>{child.name}</strong>
                  <span className="mt-0.5 block text-xs text-muted">{child.task_excerpt}</span>
                  <span className="mt-1 block text-xs">
                    <Elapsed>
                      {(now) => formatElapsed(Math.max(0, (now - child.started_at * 1000) / 1000))}
                    </Elapsed>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
      {jobs.isLoading ? <p className="px-3 text-sm text-muted">Loading jobs…</p> : null}
      {jobs.isError ? <p className="px-3 text-sm text-danger" role="alert">{(jobs.error as Error).message}</p> : null}
      {!jobs.isLoading && rows.length === 0 ? (
        <p className="px-3 text-sm text-muted" data-testid="jobs-empty">No jobs yet. A job runs only while EasyAgent is open. Turn on Start at login if you want them waiting when you sign in.</p>
      ) : null}
      <ul className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 pb-3">
        {rows.map((row) => (
          <li
            key={`${row.bot_id}-${row.id}`}
            data-testid="job-row"
            data-highlight={highlight === row.id ? "true" : "false"}
            className="rounded-lg border border-border p-2 text-sm"
          >
            <button type="button" className="block w-full text-left" onClick={() => openBot(row.bot_id, null)}>
              <strong>{row.name}</strong>
              <span className="mt-0.5 block text-xs text-muted">{row.bot_name}</span>
              <span className="mt-0.5 block text-xs">{row.describe}</span>
              <span className="mt-1 block text-xs" data-testid="job-next">Next {when(row.next_run)}</span>
              <span className="block text-xs" data-testid="job-last">Last {when(row.last_run_at)}</span>
              <span className="mt-1 inline-block rounded-full bg-black/5 px-2 py-0.5 text-xs" data-testid="job-status">
                {row.state === "queued" ? `waiting for ${row.queued_for || "the connection"}` : row.state || "ok"}
              </span>
            </button>
            <div className="mt-2 flex gap-2">
              <button type="button" data-testid="job-pause" className="text-xs underline" onClick={() => void pause(row)}>
                {row.state === "paused" ? "Resume" : "Pause"}
              </button>
              <button
                type="button"
                className="text-xs underline"
                onClick={() => void api(`/api/bots/${row.bot_id}/schedules/${row.id}/run`, { method: "POST" }).then(() => queryClient.invalidateQueries({ queryKey: ["routines"] }))}
              >
                Run now
              </button>
              {row.state === "running" || row.state === "queued" ? (
                <button type="button" data-testid="job-stop" className="text-xs underline" onClick={() => void stop(row)}>
                  Stop
                </button>
              ) : null}
              <button
                type="button"
                data-testid="job-delete"
                className="text-xs underline"
                onClick={() => void api(`/api/bots/${row.bot_id}/schedules/${row.id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["routines"] }))}
              >
                Delete
              </button>
            </div>
          </li>
        ))}
      </ul>
      <footer className="space-y-2 border-t border-border px-3 py-3 text-xs text-muted">
        <p>Runs only while EasyAgent is open.</p>
        {usageToday(usage.data) ? <p data-testid="usage-today">Today: {usageToday(usage.data)}</p> : null}
        <button
          type="button"
          className="underline"
          onClick={() => void api("/api/routines/pause-all", { method: "POST" }).then(() => queryClient.invalidateQueries({ queryKey: ["routines"] }))}
        >
          Pause all
        </button>
      </footer>
    </aside>
  );
}
