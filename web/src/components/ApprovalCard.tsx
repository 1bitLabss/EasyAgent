import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, queryClient } from "@/api";
import { Button } from "@/components/ui/button";
import { approvalPollMs } from "@/lib/window";

export type Approval = {
  id: string;
  bot_id: string;
  tier: string;
  rule: string;
  why: string;
  detail: string;
  offer_always: boolean;
  action_class?: string;
  action_tool?: string;
  class_label?: string;
  proposal?: {
    kind?: string;
    url?: string;
    element?: string;
    values?: string;
    hint?: string;
    server?: string;
    tool?: string;
    command?: string[] | string;
    package?: string;
    version?: string;
    env?: string[];
    diff?: string;
    session?: boolean;
    exec_always?: boolean;
  } | null;
};

export const NEVER_ALWAYS = new Set([
  "root-delete", "disk-format", "fork-bomb", "remote-script", "firewall-off", "credential-dump",
  "browser-store", "guardrail-edit", "browser-pay", "browser-login", "browser-post", "browser-settings",
  "browser-submit", "browser-secret", "mcp-ask", "mcp-install", "bridge-send", "own-server",
  "bot-isolation", "data-dir",
]);

export function ApprovalCard({ botId, botName, active }: { botId: string; botName: string; active: boolean }) {
  const seen = useRef<string>("");
  const [hidden, setHidden] = useState(() => typeof document !== "undefined" && document.hidden);
  useEffect(() => {
    const onVis = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  const cards = useQuery({
    queryKey: ["approvals", botId],
    enabled: Boolean(botId),
    refetchInterval: approvalPollMs(active, hidden),
    queryFn: () => api<Approval[]>(`/api/bots/${botId}/approvals`),
  });
  const card = (cards.data || [])[0];

  useEffect(() => {
    if (!card || card.id === seen.current) return;
    seen.current = card.id;
    if (typeof Notification === "undefined") return;
    if (Notification.permission === "default") {
      void Notification.requestPermission();
    }
    if (Notification.permission !== "granted" || document.visibilityState === "visible") return;
    try {
      new Notification("EasyAgent", { body: `${botName} is waiting for approval.` });
    } catch {
      /* the card is still on the page */
    }
  }, [card, botName]);

  if (!card) return null;

  async function decide(decision: "approve" | "deny" | "always" | "done" | "takeover" | "session" | "exec-always" | "class") {
    await api(`/api/bots/${botId}/approvals/${card.id}`, { method: "POST", json: { decision } });
    await queryClient.invalidateQueries({ queryKey: ["approvals", botId] });
    await queryClient.invalidateQueries({ queryKey: ["audit", botId] });
    await queryClient.invalidateQueries({ queryKey: ["bots"] });
  }

  // A lasting grant is never offered for a blocked rule, a bridge send, a browser
  // action, a connector, or EasyAgent's own server. The server refuses those too.
  const lasting = card.tier !== "block" && !NEVER_ALWAYS.has(card.rule);
  const takeover = card.proposal?.kind === "takeover";
  const install = card.proposal?.kind === "mcp-install" || card.rule === "mcp-install";
  const connector = card.proposal?.kind === "mcp" || card.rule === "mcp-ask";
  const heading = card.rule === "routine"
    ? "Confirm this routine"
    : takeover
      ? "Use the browser window"
      : install
        ? "Review this connector"
        : connector
          ? "Approve this connector"
          : "Approve this action";
  const command = Array.isArray(card.proposal?.command) ? card.proposal?.command.join(" ") : card.proposal?.command;

  return (
    <section className="mx-auto mb-3 w-full max-w-[760px] rounded-2xl border border-border bg-card p-4 text-left shadow-sm" data-testid="approval-card">
      <p className="text-xs font-medium uppercase tracking-wide text-muted">{botName}</p>
      <h2 className="mt-1 text-base font-semibold">{heading}</h2>
      <p className="mt-2 text-sm">{card.why}</p>
      {card.proposal?.hint ? <p className="mt-2 text-sm" data-testid={card.proposal.exec_always ? "exec-always-hint" : "approval-hint"}>{card.proposal.hint}</p> : null}
      {install ? (
        <dl className="mt-2 space-y-1 text-sm" data-testid="connector-review">
          <div><dt className="inline text-muted">Command </dt><dd className="inline">{command || "—"}</dd></div>
          <div><dt className="inline text-muted">Package </dt><dd className="inline">{card.proposal?.package || "—"}</dd></div>
          <div><dt className="inline text-muted">Version </dt><dd className="inline">{card.proposal?.version || "—"}</dd></div>
          <div><dt className="inline text-muted">Env </dt><dd className="inline">{(card.proposal?.env || []).join(", ") || "(none)"}</dd></div>
        </dl>
      ) : null}
      {connector ? <p className="mt-2 text-sm">{card.proposal?.server} · {card.proposal?.tool}</p> : null}
      {card.proposal?.kind === "script-diff" && card.proposal.diff ? (
        <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-xl bg-black/5 p-3 text-xs dark:bg-white/10" data-testid="script-diff">{card.proposal.diff}</pre>
      ) : null}
      <p className="mt-2 text-xs text-muted">Tier {card.tier}. Rule {card.rule}.{card.action_class ? ` Class ${card.action_class}.` : ""} If you do not answer, the card expires and that counts as a denial.</p>
      <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-xl bg-black/5 p-3 text-xs dark:bg-white/10">{card.detail}</pre>
      <div className="mt-3 flex flex-wrap gap-2">
        {takeover ? (
          <>
            <Button type="button" onClick={() => void decide("takeover")}>Open the window</Button>
            <Button type="button" onClick={() => void decide("done")}>Done</Button>
            <Button type="button" variant="danger" onClick={() => void decide("deny")}>Deny</Button>
          </>
        ) : lasting && card.proposal?.kind === "script-diff" && card.proposal.session ? (
          <>
            <Button type="button" data-testid="approve-once" onClick={() => void decide("approve")}>Allow once</Button>
            {lasting && card.class_label ? (
              <Button type="button" variant="outline" data-testid="approve-class" onClick={() => void decide("class")}>{card.class_label}</Button>
            ) : null}
            <Button type="button" variant="outline" data-testid="approve-session" onClick={() => void decide("session")}>Allow for this session</Button>
            {card.proposal.exec_always ? (
              <Button type="button" variant="outline" data-testid="approve-exec-always" onClick={() => void decide("exec-always")}>Always allow</Button>
            ) : null}
            <Button type="button" variant="danger" onClick={() => void decide("deny")}>Deny</Button>
          </>
        ) : (
          <>
            <Button type="button" onClick={() => void decide("approve")}>Approve once</Button>
            {lasting && card.class_label ? (
              <Button type="button" variant="outline" data-testid="approve-class" onClick={() => void decide("class")}>{card.class_label}</Button>
            ) : null}
            <Button type="button" variant="danger" onClick={() => void decide("deny")}>Deny</Button>
            {lasting && card.offer_always ? (
              <Button type="button" variant="outline" onClick={() => void decide("always")}>Always allow this command for this bot</Button>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
