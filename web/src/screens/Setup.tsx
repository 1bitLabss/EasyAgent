import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { api, queryClient } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApp } from "@/store";
import type { Bot, Chat, Endpoint } from "@/types";

type LocalServer = { kind: string; base_url: string; label: string; models: string[] };

function Note({ children }: { children: ReactNode }) {
  return <p className="text-xs text-muted">{children}</p>;
}

function SetupWizard() {
  const openBot = useApp((state) => state.openBot);
  const setNudge = useApp((state) => state.setNudge);
  const [step, setStep] = useState(1);
  const [picked, setPicked] = useState<LocalServer | null>(null);
  const [manual, setManual] = useState(false);
  const [address, setAddress] = useState("");
  const [probing, setProbing] = useState(false);
  const [probeError, setProbeError] = useState("");
  const [model, setModel] = useState("");
  const [botName, setBotName] = useState("Ada");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const servers = useQuery({
    queryKey: ["local-servers"],
    queryFn: () => api<{ servers: LocalServer[] }>("/api/local-servers"),
  });

  function pick(server: LocalServer) {
    setPicked(server);
    setModel(server.models[0] || "");
    setProbeError("");
    setStep(2);
  }

  async function checkAddress() {
    setProbing(true);
    setProbeError("");
    try {
      const result = await api<{ ok: boolean; models?: string[] }>("/api/endpoints/probe", {
        method: "POST",
        json: { base_url: address.trim() },
      });
      if (!result.ok) {
        setProbeError("Nothing answered there.");
        return;
      }
      pick({ kind: "manual", base_url: address.trim(), label: address.trim(), models: result.models || [] });
    } catch {
      setProbeError("Nothing answered there.");
    } finally {
      setProbing(false);
    }
  }

  async function finishLater() {
    await api("/api/setup", { method: "PUT", json: { onboarded: true } });
    await queryClient.invalidateQueries({ queryKey: ["setup"] });
  }

  async function create() {
    if (!picked) return;
    setBusy(true);
    setError("");
    try {
      const endpoint = await api<Endpoint>("/api/endpoints", {
        method: "POST",
        json: { name: picked.label, base_url: picked.base_url, api_key: null, model: model || null },
      });
      const bot = await api<Bot>("/api/bots", {
        method: "POST",
        json: { name: botName.trim() || "Ada", endpoint_id: endpoint.id, model: null, context_tokens: 24000 },
      });
      await api("/api/setup", { method: "PUT", json: { onboarded: true } });
      const chat = await api<Chat>(`/api/bots/${bot.id}/ongoing`);
      await queryClient.invalidateQueries({ queryKey: ["bots"] });
      await queryClient.invalidateQueries({ queryKey: ["chats", bot.id] });
      await queryClient.invalidateQueries({ queryKey: ["setup"] });
      openBot(bot.id, chat.id);
      setNudge("Ask it anything.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Something went wrong. Try again.");
      setBusy(false);
    }
  }

  const title = step === 1 ? "Find your model server" : step === 2 ? "Pick a model" : "Name your assistant";

  return (
    <div className="safe-overlay fixed inset-0 z-50 flex items-center justify-center bg-background p-4">
      <div className="w-full max-w-sm space-y-4 rounded-lg border border-border bg-card p-5">
        <div>
          <p className="text-xs uppercase tracking-wide text-muted">Step {step} of 3</p>
          <h1 className="text-xl font-semibold">{title}</h1>
        </div>

        {step === 1 ? (
          <div className="space-y-3">
            {servers.isLoading ? <p data-testid="setup-looking" className="text-sm text-muted">Looking…</p> : null}
            {(servers.data?.servers || []).map((server) => (
              <button
                key={server.base_url}
                type="button"
                data-testid="local-server-row"
                className="block w-full rounded-lg border border-border px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10"
                onClick={() => pick(server)}
              >
                <span className="font-medium">{server.label}</span>
                <span className="text-muted"> · {server.models.length} {server.models.length === 1 ? "model" : "models"}</span>
              </button>
            ))}
            {!servers.isLoading && !(servers.data?.servers || []).length && !manual ? (
              <Note>No model server answered on this computer.</Note>
            ) : null}
            {(servers.data?.servers || []).length > 0 && !manual ? (
              <button type="button" className="text-sm text-muted underline" onClick={() => setManual(true)}>
                My server is elsewhere
              </button>
            ) : null}
            {!servers.isLoading && (!(servers.data?.servers || []).length || manual) ? (
              <div className="space-y-2">
                <label className="block text-sm">Model server address
                  <Input
                    value={address}
                    onChange={(event) => setAddress(event.target.value)}
                    placeholder="http://localhost:8080/v1"
                    className="mt-1"
                  />
                </label>
                {probeError ? <p className="text-sm text-danger" role="alert">{probeError}</p> : null}
                <Button type="button" disabled={!address.trim() || probing} onClick={() => void checkAddress()}>
                  {probing ? "Checking…" : "Check"}
                </Button>
                <Note>Local servers need no key. Add one later under Connections.</Note>
              </div>
            ) : null}
          </div>
        ) : null}

        {step === 2 && picked ? (
          <div className="space-y-3">
            <Note>From {picked.label}.</Note>
            {picked.models.length ? (
              <label className="block text-sm">Model
                <select
                  data-testid="setup-model"
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  className="mt-1 h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
                >
                  {picked.models.map((id) => <option key={id} value={id}>{id}</option>)}
                </select>
              </label>
            ) : (
              <label className="block text-sm">Model name
                <Input value={model} onChange={(event) => setModel(event.target.value)} className="mt-1" />
              </label>
            )}
            <div className="flex items-center gap-3">
              <Button type="button" onClick={() => setStep(3)}>Next</Button>
              <button type="button" className="text-sm text-muted underline" onClick={() => setStep(1)}>Back</button>
            </div>
          </div>
        ) : null}

        {step === 3 && picked ? (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <label className="block text-sm">Name
              <Input value={botName} maxLength={80} onChange={(event) => setBotName(event.target.value)} className="mt-1" />
            </label>
            {error ? <p className="text-sm text-danger" role="alert">{error}</p> : null}
            <Button type="submit" disabled={busy}>{busy ? "Creating…" : "Create"}</Button>
          </form>
        ) : null}

        <div className="border-t border-border pt-3">
          <button type="button" data-testid="setup-later" className="text-sm text-muted underline" onClick={() => void finishLater()}>
            Set up later
          </button>
        </div>
      </div>
    </div>
  );
}

export function SetupGate() {
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const setup = useQuery({ queryKey: ["setup"], queryFn: () => api<{ onboarded: boolean }>("/api/setup") });
  if (!setup.isSuccess || setup.data.onboarded) return null;
  if (!bots.isSuccess || (bots.data || []).length > 0) return null;
  return <SetupWizard />;
}
