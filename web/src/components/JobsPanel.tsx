import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api";
import { useApp } from "@/store";

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

function when(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" });
}

export function JobsPanel() {
  const setJobs = useApp((state) => state.setJobs);
  const highlight = useApp((state) => state.highlightJob);
  const openBot = useApp((state) => state.openBot);
  const queryClient = useQueryClient();
  const jobs = useQuery({
    queryKey: ["routines"],
    queryFn: () => api<JobRow[]>("/api/routines"),
    refetchInterval: 4000,
  });
  const rows = jobs.data || [];
  const busy = rows.filter((row) => row.state === "running" || row.state === "queued").length;

  async function pause(row: JobRow) {
    await api(`/api/bots/${row.bot_id}/schedules/${row.id}/pause`, {
      method: "POST",
      json: { paused: row.state !== "paused" && row.state !== "done" },
    });
    await queryClient.invalidateQueries({ queryKey: ["routines"] });
  }

  return (
    <aside className="flex h-full w-[360px] shrink-0 flex-col border-l border-border bg-background" data-testid="jobs-panel">
      <header className="flex items-center justify-between px-3 py-3">
        <h2 className="text-sm font-semibold">Jobs {busy ? <span data-testid="jobs-badge">{busy}</span> : null}</h2>
        <button type="button" className="text-sm text-muted" onClick={() => setJobs(false)} aria-label="Close jobs">Close</button>
      </header>
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
              <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${row.bot_id}/schedules/${row.id}/run`, { method: "POST" })}>Run now</button>
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
