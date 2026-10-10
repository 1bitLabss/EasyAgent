import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { api, queryClient } from "@/api";
import { ApprovalCard } from "@/components/ApprovalCard";
import { FACE_PALETTE, Face } from "@/components/Face";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { pokeFace } from "@/lib/mood";
import { contextNote } from "@/lib/run";
import { localWhen } from "@/lib/time";
import { windowChat } from "@/lib/window";
import { PhoneAccess } from "@/screens/Phone";
import { useApp } from "@/store";
import type { Bot, Chat, ChatSummary, Endpoint, Job, Learning, MemoryLine, MemoryTopic, Project, Schedule, Skill } from "@/types";

function Note({ children }: { children: ReactNode }) {
  return <p className="text-xs text-muted">{children}</p>;
}

function Block({ id, title, children }: { id?: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="space-y-3 rounded-lg border border-border bg-card p-4">
      <h2 className="text-base font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function BrowserBlock({ onError }: { onError: (text: string) => void }) {
  const install = useQuery({
    queryKey: ["browser-install"],
    queryFn: () => api<{ installed?: boolean; label?: string }>("/api/browser/install"),
  });
  const [busy, setBusy] = useState(false);
  const label = install.data?.label || "Install browser (~700 MB)";

  async function installBrowser() {
    setBusy(true);
    try {
      await api("/api/browser/install", { method: "POST" });
      await queryClient.invalidateQueries({ queryKey: ["browser-install"] });
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "The browser could not be installed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Block id="browser" title="Browser">
      <Note>Each bot has its own Chromium profile. It is not your browser. Chromium is not downloaded until you press the button or a bot opens a page.</Note>
      <p data-testid="browser-install">{install.data?.installed ? "Browser installed." : "Browser not installed."}</p>
      <Button type="button" disabled={busy || install.data?.installed} onClick={() => void installBrowser()}>{label}</Button>
    </Block>
  );
}

type ConnectorTool = { name: string; permission: string; description?: string };
type ConnectorRow = {
  id: string;
  name: string;
  transport: string;
  command: string[];
  url: string;
  enabled: boolean;
  secret_names: string[];
  tools: ConnectorTool[];
  starter: string;
  package: string;
  version: string;
};

function ConnectorsBlock({ botId, onError }: { botId: string; onError: (text: string) => void }) {
  const rows = useQuery({
    queryKey: ["connectors", botId],
    queryFn: () => api<ConnectorRow[]>(`/api/bots/${botId}/connectors`),
  });
  const [name, setName] = useState("");
  const [transport, setTransport] = useState("stdio");
  const [command, setCommand] = useState("");
  const [url, setUrl] = useState("");
  const [pkg, setPkg] = useState("");
  const [version, setVersion] = useState("");
  const [envName, setEnvName] = useState("");
  const [envValue, setEnvValue] = useState("");
  const [note, setNote] = useState("");

  async function addStarter(starter: string) {
    setNote("");
    try {
      await api(`/api/bots/${botId}/connectors/starter`, { method: "POST", json: { starter, name: starter } });
      setNote("Review the card before this server is installed.");
      await queryClient.invalidateQueries({ queryKey: ["approvals", botId] });
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "That connector was not added.");
    }
  }

  async function addCustom(event: FormEvent) {
    event.preventDefault();
    setNote("");
    const secrets = envName.trim() && envValue ? { [envName.trim()]: envValue } : {};
    try {
      await api(`/api/bots/${botId}/connectors`, {
        method: "POST",
        json: {
          name,
          transport,
          command: transport === "stdio" ? command.split(" ").filter(Boolean) : [],
          url: transport === "http" ? url : "",
          secrets,
          package: pkg,
          version,
        },
      });
      setEnvValue("");
      setNote("Review the card before this server is installed. The secret is not saved until you approve.");
      await queryClient.invalidateQueries({ queryKey: ["approvals", botId] });
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "That connector was not added.");
    }
  }

  async function setPermission(row: ConnectorRow, tool: string, permission: string) {
    const tools: Record<string, string> = {};
    for (const item of row.tools) tools[item.name] = item.name === tool ? permission : item.permission;
    await api(`/api/bots/${botId}/connectors/${row.id}`, { method: "POST", json: { tools } });
    await queryClient.invalidateQueries({ queryKey: ["connectors", botId] });
  }

  async function remove(id: string) {
    await api(`/api/bots/${botId}/connectors/${id}`, { method: "DELETE" });
    await queryClient.invalidateQueries({ queryKey: ["connectors", botId] });
  }

  return (
    <Block id="connectors" title="Connectors">
      <Note>Each bot has its own MCP servers. A write, delete, or send waits for you. Secrets stay in the secrets database and are handed only to that server. Nothing is installed until you approve the review card.</Note>
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={() => void addStarter("filesystem")}>Filesystem</Button>
        <Button type="button" variant="outline" onClick={() => void addStarter("fetch")}>Fetch</Button>
        <Button type="button" variant="outline" onClick={() => void addStarter("git")}>Git</Button>
        <Button type="button" variant="outline" onClick={() => void addStarter("sqlite")}>SQLite</Button>
      </div>
      <Note>Filesystem is this bot's workspace. It does not follow the server's working directory.</Note>
      <form className="space-y-2" onSubmit={(event) => void addCustom(event)}>
        <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Name" maxLength={80} />
        <select className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm" value={transport} onChange={(event) => setTransport(event.target.value)}>
          <option value="stdio">stdio</option>
          <option value="http">streamable HTTP</option>
        </select>
        {transport === "stdio" ? (
          <Input value={command} onChange={(event) => setCommand(event.target.value)} placeholder="Command, with a pinned version" />
        ) : (
          <Input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/mcp" />
        )}
        <div className="flex gap-2">
          <Input value={pkg} onChange={(event) => setPkg(event.target.value)} placeholder="Package" />
          <Input value={version} onChange={(event) => setVersion(event.target.value)} placeholder="1.2.3" />
        </div>
        <div className="flex gap-2">
          <Input value={envName} onChange={(event) => setEnvName(event.target.value)} placeholder="Env name" autoComplete="off" />
          <Input value={envValue} onChange={(event) => setEnvValue(event.target.value)} placeholder="Secret value" type="password" autoComplete="off" />
        </div>
        <Button type="submit">Review connector</Button>
      </form>
      {note ? <Note>{note}</Note> : null}
      <ul className="space-y-3">
        {(rows.data || []).map((row) => (
          <li key={row.id} className="rounded-md border border-border p-3 text-sm">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-medium">{row.name}</p>
                <p className="text-xs text-muted">{row.package || row.starter || row.transport} {row.version}</p>
              </div>
              <Button type="button" size="sm" variant="danger" onClick={() => void remove(row.id).catch((reason: Error) => onError(reason.message))}>Remove</Button>
            </div>
            <ul className="mt-2 space-y-1">
              {row.tools.map((tool) => (
                <li key={tool.name} className="flex items-center justify-between gap-2">
                  <span>{tool.name}</span>
                  <select aria-label={`${tool.name} permission`} className="h-8 rounded-md border border-border bg-card px-2" value={tool.permission} onChange={(event) => void setPermission(row, tool.name, event.target.value)}>
                    <option value="allow">Allow</option>
                    <option value="ask">Ask</option>
                    <option value="block">Block</option>
                  </select>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </Block>
  );
}

function SearchBlock({ onError }: { onError: (text: string) => void }) {
  const setup = useQuery({
    queryKey: ["search-setup"],
    queryFn: () => api<{ provider?: string; searxng_url?: string; has_brave_key?: boolean; has_tavily_key?: boolean }>("/api/search-setup"),
  });
  const [provider, setProvider] = useState("duckduckgo");
  const [url, setUrl] = useState("");
  const [brave, setBrave] = useState("");
  const [tavily, setTavily] = useState("");

  useEffect(() => {
    if (!setup.data) return;
    setProvider(setup.data.provider || "duckduckgo");
    setUrl(setup.data.searxng_url || "");
  }, [setup.data]);

  async function save() {
    await api("/api/search-setup", {
      method: "PUT",
      json: { provider, searxng_url: url, brave_key: brave || null, tavily_key: tavily || null },
    });
    setBrave("");
    setTavily("");
    await queryClient.invalidateQueries({ queryKey: ["search-setup"] });
  }

  return (
    <Block id="search" title="Web search">
      <Note>DuckDuckGo is the default. A Brave or Tavily key is stored in the encrypted secrets database, not in a settings file. A research answer has to cite the pages it fetched.</Note>
      <label className="block text-sm">
        Provider
        <select className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1" value={provider} onChange={(event) => setProvider(event.target.value)}>
          <option value="duckduckgo">DuckDuckGo</option>
          <option value="searxng">SearXNG</option>
          <option value="brave">Brave</option>
          <option value="tavily">Tavily</option>
        </select>
      </label>
      <Input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="SearXNG address (http or https)" />
      <Input value={brave} onChange={(event) => setBrave(event.target.value)} placeholder={setup.data?.has_brave_key ? "Brave key saved" : "Brave key"} type="password" />
      <Input value={tavily} onChange={(event) => setTavily(event.target.value)} placeholder={setup.data?.has_tavily_key ? "Tavily key saved" : "Tavily key"} type="password" />
      <Button type="button" onClick={() => void save().catch((reason: Error) => onError(reason.message))}>Save search</Button>
    </Block>
  );
}

function ContainmentBlock({ botId, botName, onError }: { botId: string; botName: string; onError: (text: string) => void }) {
  const status = useQuery({
    queryKey: ["sandbox", botId],
    queryFn: () => api<{ label?: string; status?: string; undo?: string }>(`/api/bots/${botId}/sandbox`),
  });
  const label = status.data?.label || "off";

  async function act(action: "setup" | "undo") {
    await api(`/api/bots/${botId}/sandbox`, { method: "POST", json: { action } });
    await queryClient.invalidateQueries({ queryKey: ["sandbox", botId] });
    await queryClient.invalidateQueries({ queryKey: ["approvals", botId] });
  }

  return (
    <Block id="containment" title="OS containment">
      <Note>Tool commands stay on the file guards until you turn this on once. The approval card explains the AppContainer profile and the folder grants. Credentials, Vault, and Protect are not changed. Remove it here, or with python -m easyagent contain --undo.</Note>
      <p data-testid="sandbox-status">Status: {label}</p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" onClick={() => void act("setup").catch((reason: Error) => onError(reason.message))}>Turn on OS containment</Button>
        <Button type="button" variant="outline" onClick={() => void act("undo").catch((reason: Error) => onError(reason.message))}>Remove containment</Button>
      </div>
      <ApprovalCard botId={botId} botName={botName} active />
    </Block>
  );
}

type HonestySettings = {
  receipts: boolean;
  pushback: boolean;
  excuse: boolean;
  loop: boolean;
  tripwires: boolean;
  stall: boolean;
  stall_minutes: number;
};

function HonestyBlock({ botId, onError }: { botId: string; onError: (text: string) => void }) {
  const honesty = useQuery({
    queryKey: ["honesty", botId],
    queryFn: () => api<HonestySettings>(`/api/bots/${botId}/honesty`),
  });
  const data = honesty.data;

  async function save(patch: Partial<HonestySettings>) {
    try {
      await api(`/api/bots/${botId}/honesty`, { method: "POST", json: patch });
      await queryClient.invalidateQueries({ queryKey: ["honesty", botId] });
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Could not save honesty settings.");
    }
  }

  if (honesty.isError) {
    return (
      <Block id="honesty" title="Honesty">
        <Note>Honesty settings could not be loaded.</Note>
      </Block>
    );
  }
  if (!data) {
    return (
      <Block id="honesty" title="Honesty">
        <Note>Loading honesty settings.</Note>
      </Block>
    );
  }

  const row = (key: "receipts" | "pushback" | "excuse" | "loop" | "tripwires" | "stall", label: string) => (
    <label key={key} className="flex items-center justify-between gap-3 text-sm">
      <span>{label}</span>
      <Switch checked={Boolean(data[key])} onCheckedChange={(on) => void save({ [key]: on })} />
    </label>
  );

  return (
    <Block id="honesty" title="Honesty">
      <Note>These checks run in the harness. A claim of done needs a tool result from after the last change. A report that it is still broken starts a fresh look, without the previous explanation.</Note>
      {row("receipts", "Receipts for done, fixed, created, and deleted")}
      {row("pushback", "Fresh investigation when it is still broken")}
      {row("excuse", "Re-check a pre-existing or unrelated claim")}
      {row("loop", "Stop a third identical failure")}
      {row("tripwires", "Mistake tripwires before a matching tool")}
      {row("stall", "Stall watchdog")}
      <label className="block text-sm">Minutes with no progress
        <Input
          type="number"
          min={1}
          max={120}
          defaultValue={data.stall_minutes}
          key={data.stall_minutes}
          className="mt-1"
          onBlur={(event) => {
            const next = Number(event.target.value);
            if (!Number.isFinite(next) || next === data.stall_minutes) return;
            void save({ stall_minutes: next });
          }}
        />
      </label>
    </Block>
  );
}

function SafetyBlock({ botId, botName, mode, unlocks, onError }: { botId: string; botName: string; mode: string; unlocks: string[]; onError: (text: string) => void }) {
  const audit = useQuery({ queryKey: ["audit", botId], queryFn: () => api<{ at?: string; decision?: string; rule?: string; why?: string; detail?: string }[]>(`/api/bots/${botId}/audit`) });
  const trash = useQuery({ queryKey: ["trash", botId], queryFn: () => api<{ id: string; kind?: string; names?: string[]; name?: string; from?: string }[]>(`/api/bots/${botId}/trash`) });
  const [nextMode, setNextMode] = useState(mode);
  const [confirm, setConfirm] = useState("");
  const [unlock, setUnlock] = useState(unlocks.join(", "));

  useEffect(() => {
    setNextMode(mode);
    setUnlock(unlocks.join(", "));
    setConfirm("");
  }, [botId, mode, unlocks]);

  async function save() {
    const list = unlock.split(",").map((item) => item.trim()).filter(Boolean);
    await api(`/api/bots/${botId}/safety`, {
      method: "POST",
      json: { mode: nextMode, confirm_name: confirm, unlocks: nextMode === "advanced" ? list : [] },
    });
    await queryClient.invalidateQueries({ queryKey: ["bots"] });
  }

  return (
    <Block id="safety" title="Safety">
      <Note>Careful is the default. A risky action waits for you in the chat. Normal also allows replacing a file inside this bot's workspace. Advanced asks you to type the bot's name, and a blocked action stays blocked unless you unlock that one rule. An unlocked rule still waits for a yes. It is never automatic.</Note>
      <label className="block text-sm">Mode
        <select className="mt-1 h-9 w-full rounded-md border border-border bg-card px-2" value={nextMode} onChange={(event) => setNextMode(event.target.value)}>
          <option value="careful">Careful</option>
          <option value="normal">Normal</option>
          <option value="advanced">Advanced</option>
        </select>
      </label>
      {nextMode === "advanced" ? (
        <>
          <label className="block text-sm">Type {botName} to turn on Advanced
            <Input className="mt-1" value={confirm} onChange={(event) => setConfirm(event.target.value)} />
          </label>
          <label className="block text-sm">Unlocked block rules, comma separated
            <Input className="mt-1" value={unlock} onChange={(event) => setUnlock(event.target.value)} />
          </label>
        </>
      ) : null}
      <Button type="button" onClick={() => void save().catch((reason: Error) => onError(reason.message))}>Save safety</Button>
      <h3 className="text-sm font-medium">Audit log</h3>
      <ul className="space-y-2 text-sm" data-testid="audit-log">
        {(audit.data || []).length ? (audit.data || []).slice().reverse().map((row, index) => (
          <li key={`${row.at}-${index}`} className="rounded-lg bg-black/5 p-2 dark:bg-white/10">
            <span className="font-medium">{row.decision}</span>
            <span className="text-muted"> · {row.rule}</span>
            <p>{row.why}</p>
            {row.detail ? <pre className="mt-1 whitespace-pre-wrap text-xs text-muted">{row.detail}</pre> : null}
          </li>
        )) : <li className="text-muted">No safety decisions yet.</li>}
      </ul>
      <h3 className="text-sm font-medium">Trash</h3>
      <ul className="space-y-2 text-sm">
        {(trash.data || []).length ? (trash.data || []).slice().reverse().map((item) => (
          <li key={item.id} className="flex items-center justify-between gap-2">
            <span>{item.kind === "snapshot" ? item.name : (item.names || []).join(", ") || item.id}</span>
            <Button size="sm" variant="outline" onClick={() => void api(`/api/bots/${botId}/trash/${item.id}/restore`, { method: "POST" }).then(() => queryClient.invalidateQueries({ queryKey: ["trash", botId] })).catch((reason: Error) => onError(reason.message))}>Restore</Button>
          </li>
        )) : <li className="text-muted">Trash is empty.</li>}
      </ul>
    </Block>
  );
}

export function SettingsScreen() {
  const botId = useApp((state) => state.botId);
  const chatId = useApp((state) => state.chatId);
  const focus = useApp((state) => state.focus);
  const setScreen = useApp((state) => state.setScreen);
  const askConfirm = useApp((state) => state.askConfirm);
  const selectChat = useApp((state) => state.selectChat);
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const endpoints = useQuery({ queryKey: ["endpoints"], queryFn: () => api<Endpoint[]>("/api/endpoints") });
  const bot = (bots.data || []).find((item) => item.id === botId);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [face, setFace] = useState<string | null>(null);
  const [topic, setTopic] = useState("");
  const [projectId, setProjectId] = useState("");

  useEffect(() => {
    setFace(null);
    setNote("");
    setTopic("");
    setProjectId("");
  }, [botId]);

  useEffect(() => {
    if (!focus) return;
    document.getElementById(focus)?.scrollIntoView({ block: "start" });
  }, [focus, botId]);

  const chat = useQuery({
    queryKey: ["chat", botId, chatId],
    enabled: Boolean(botId && chatId),
    queryFn: () => api<Chat>(`/api/bots/${botId}/chats/${chatId}?window=80`),
  });
  const chats = useQuery({
    queryKey: ["chats", botId],
    enabled: Boolean(botId),
    queryFn: () => api<ChatSummary[]>(`/api/bots/${botId}/chats`),
  });
  const learning = useQuery({ queryKey: ["learning", botId], enabled: Boolean(botId), queryFn: () => api<Learning>(`/api/bots/${botId}/learning`) });
  const memory = useQuery({ queryKey: ["memory-index", botId], enabled: Boolean(botId), queryFn: () => api<{ topics: MemoryTopic[] }>(`/api/bots/${botId}/memory/index`) });
  const lines = useQuery({
    queryKey: ["memory-topic", botId, topic],
    enabled: Boolean(botId && topic),
    queryFn: () => api<{ title: string; lines: MemoryLine[] }>(`/api/bots/${botId}/memory/topics/${encodeURIComponent(topic)}`),
  });
  const projects = useQuery({ queryKey: ["bot-projects", botId], enabled: Boolean(botId), queryFn: () => api<Project[]>(`/api/bots/${botId}/projects`) });
  const project = (projects.data || []).find((item) => item.id === projectId);
  const schedules = useQuery({ queryKey: ["schedules", botId], enabled: Boolean(botId), queryFn: () => api<Schedule[]>(`/api/bots/${botId}/schedules`) });
  const jobs = useQuery({ queryKey: ["jobs", botId], enabled: Boolean(botId), queryFn: () => api<Job[]>(`/api/bots/${botId}/jobs`) });
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api<Skill[]>("/api/skills") });
  const proposals = useQuery({ queryKey: ["proposals"], queryFn: () => api<{ id: string; kind?: string; name?: string; text?: string }[]>("/api/proposals") });
  const watches = useQuery({ queryKey: ["watches"], queryFn: () => api<{ kind?: string; id?: string }[]>("/api/watches") });

  if (!bot || !botId) {
    return (
      <div className="h-full space-y-4 overflow-y-auto p-6">
        <PhoneAccess />
        <section>
          <h1 className="text-2xl font-semibold">Pick a bot first.</h1>
          <p className="mt-2 text-sm text-muted">The rest of Settings belongs to one bot. Chats are not changed here.</p>
        </section>
      </div>
    );
  }

  async function save(form: FormData) {
    setNote("");
    setError("");
    const context = Number(form.get("context_tokens"));
    if (!Number.isInteger(context) || context < 512 || context > 1000000) {
      setNote("How much chat it sees must be a whole number of tokens from 512 to 1000000. Chats were not changed.");
      return;
    }
    const body: Record<string, unknown> = {
      name: String(form.get("name") || ""),
      endpoint_id: String(form.get("endpoint_id") || ""),
      model: String(form.get("model") || "") || null,
      context_tokens: context,
      check_enabled: form.get("check_enabled") === "on",
    };
    if (face) body.face_color = face;
    const saved = await api<Bot>(`/api/bots/${botId}`, { method: "PATCH", json: body });
    setFace(null);
    await queryClient.invalidateQueries({ queryKey: ["bots"] });
    setNote(`Saved ${saved.name}. Chats were not rewritten.`);
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 pt-5">
        <PhoneAccess />
      </div>
      <header className="px-6 py-5">
        <p className="text-xs uppercase tracking-wide text-muted">Bot settings</p>
        <h1 className="text-2xl font-semibold">{bot.name}</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">This screen is for the bot, not the conversation. Saving here does not rewrite its chats. Changing one memory line leaves the others.</p>
        {chat.data?.context ? <p className="mt-2 text-xs text-muted">{contextNote(chat.data.context, "chat")}</p> : null}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={!chatId}
            onClick={() => {
              if (!botId || !chatId) return;
              void api<Chat>(`/api/bots/${botId}/chats/${chatId}/fresh`, { method: "POST" })
                .then((next) => {
                  queryClient.setQueryData(["chat", botId, chatId], windowChat(next));
                  setNote("The next reply starts fresh. Every message is still in this chat.");
                })
                .catch((reason: Error) => setError(reason.message));
            }}
          >
            Start fresh
          </Button>
          <Note>Start fresh clears only what the bot sees next. It does not delete the transcript.</Note>
        </div>
        <div className="mt-4 max-w-2xl space-y-2 rounded-lg border border-border bg-card p-4">
          <h2 className="text-base font-semibold">What can be removed</h2>
          <Note>Digested messages older than the window can be removed after the notes and the search index already have them. Undigested messages stay. Pruning can be turned off, or transcripts can be kept forever.</Note>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span>Remove old digested transcripts</span>
            <Switch
              checked={learning.data?.prune?.pruning !== false}
              onCheckedChange={(on) => {
                void api(`/api/bots/${botId}/notes/retention`, { method: "PUT", json: { pruning: on } })
                  .then(() => queryClient.invalidateQueries({ queryKey: ["learning", botId] }))
                  .catch((reason: Error) => setError(reason.message));
              }}
            />
          </label>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span>Keep transcripts forever</span>
            <Switch
              checked={Boolean(learning.data?.prune?.keep_forever)}
              onCheckedChange={(on) => {
                void api(`/api/bots/${botId}/notes/retention`, { method: "PUT", json: { keep_forever: on } })
                  .then(() => queryClient.invalidateQueries({ queryKey: ["learning", botId] }))
                  .catch((reason: Error) => setError(reason.message));
              }}
            />
          </label>
          <label className="block text-sm">Keep digested transcripts for
            <Input
              className="mt-1 max-w-[8rem]"
              type="number"
              min={1}
              max={3650}
              value={learning.data?.prune?.retain_days ?? 30}
              onChange={(event) => {
                const retain_days = Number(event.target.value);
                if (!Number.isInteger(retain_days) || retain_days < 1) return;
                void api(`/api/bots/${botId}/notes/retention`, { method: "PUT", json: { retain_days } })
                  .then(() => queryClient.invalidateQueries({ queryKey: ["learning", botId] }))
                  .catch((reason: Error) => setError(reason.message));
              }}
            />
            <span className="ml-2 text-muted">days (30 by default)</span>
          </label>
          {(learning.data?.prune?.pending || []).length === 0 ? <Note>Nothing is waiting to be removed.</Note> : (
            <ul className="space-y-1 text-sm">
              {(learning.data?.prune?.pending || []).map((row) => (
                <li key={`${row.chat_id}:${row.message_id}`}>
                  {localWhen(row.created_at)} — {row.preview || row.message_id}
                  {learning.data?.prune?.pruning === false || learning.data?.prune?.keep_forever ? " (staying)" : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
        <Button className="mt-2" variant="ghost" size="sm" onClick={() => setScreen("chat")}>Back to the chat</Button>
        {note ? <p className="mt-2 text-sm" role="status">{note}</p> : null}
        {error ? <p className="mt-2 text-sm text-danger" role="alert">{error}</p> : null}
      </header>
      <div className="space-y-4 px-6 pb-10">
        <Block title="Saved transcripts">
          <Note>This bot opens into one conversation. Older transcripts stay on this computer until you delete one.</Note>
          {(chats.data || []).length === 0 ? <Note>No transcript yet.</Note> : (
            <ul className="space-y-1">
              {(chats.data || []).map((item) => (
                <li key={item.id} className="flex items-center justify-between gap-2 text-sm">
                  <span className="min-w-0 truncate">{item.id === chatId ? "This conversation" : (item.title || "Saved transcript")}</span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => askConfirm({
                      title: "Delete chat",
                      copy: "This removes only this transcript. The bot and its other chats stay.",
                      submit: "Delete chat",
                      run: async () => {
                        await api(`/api/bots/${botId}/chats/${item.id}`, { method: "DELETE" });
                        queryClient.removeQueries({ queryKey: ["chat", botId, item.id] });
                        await queryClient.invalidateQueries({ queryKey: ["chats", botId] });
                        if (useApp.getState().chatId === item.id) {
                          const next = await api<Chat>(`/api/bots/${botId}/ongoing`);
                          queryClient.setQueryData(["ongoing", botId], next);
                          queryClient.setQueryData(["chat", botId, next.id], windowChat(next));
                          selectChat(next.id);
                        }
                      },
                    })}
                  >
                    Delete chat
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </Block>
        <form className="space-y-3 rounded-lg border border-border bg-card p-4" onSubmit={(event) => { event.preventDefault(); void save(new FormData(event.currentTarget)).catch((reason: Error) => setError(reason.message)); }}>
          <label className="block text-sm">Name
            <Input name="name" required maxLength={80} defaultValue={bot.name} className="mt-1" />
          </label>
          <label className="block text-sm">Connection
            <select name="endpoint_id" className="mt-1 h-9 w-full rounded-md border border-border bg-card px-2" defaultValue={bot.endpoint_id}>
              {(endpoints.data || []).map((endpoint) => <option key={endpoint.id} value={endpoint.id}>{endpoint.name}</option>)}
            </select>
          </label>
          <label className="block text-sm">Model name <span className="text-muted">optional</span>
            <Input name="model" maxLength={120} defaultValue={bot.model || ""} placeholder="uses the connection" className="mt-1" />
          </label>
          <label className="block text-sm">How much chat it sees <span className="text-muted">tokens (default 24000)</span>
            <Input name="context_tokens" type="number" min={512} max={1000000} defaultValue={bot.context_tokens} className="mt-1" />
          </label>
          <Note>Older messages stay saved. This number is only how much of the recent chat is sent with the next reply.</Note>
          <label className="flex items-center gap-2 text-sm">
            <input name="check_enabled" type="checkbox" defaultChecked={bot.check_enabled} />
            Check replies before sending
          </label>
          <Note>EasyAgent uses your connected model to review and learn — no extra model needed.</Note>
          <fieldset>
            <legend className="text-sm">Face color</legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {FACE_PALETTE.map((color) => (
                <button key={color} type="button" className={`rounded-md p-1 ${(face || bot.face_color) === color ? "ring-2 ring-foreground" : ""}`} onClick={() => setFace(color)} aria-label={color}>
                  <Face color={color} />
                </button>
              ))}
            </div>
          </fieldset>
          <Button type="submit">Save</Button>
        </form>

        <SafetyBlock botId={bot.id} botName={bot.name} mode={bot.safety_mode || "careful"} unlocks={bot.safety_unlocks || []} onError={setError} />

        <HonestyBlock botId={bot.id} onError={setError} />

        <ContainmentBlock botId={bot.id} botName={bot.name} onError={setError} />

        <BrowserBlock onError={setError} />

        <ConnectorsBlock botId={bot.id} onError={setError} />

        <SearchBlock onError={setError} />

        <Block id="learning" title="Learning">
          <Note>After a hard turn, this bot can propose a skill. The proposal stays a candidate until a check passes and a replay does not do worse. Skills and memory you wrote are left alone.</Note>
          <h3 className="text-sm font-medium">What changed last night</h3>
          {learning.data?.last_night?.summary ? <Note>{learning.data.last_night.summary}</Note> : <Note>No nightly pass yet. These notes are written only while this bot is idle.</Note>}
          <ul className="space-y-1 text-sm">
            {(learning.data?.notes || []).map((file) => (
              <li key={file.name}>
                <span className="font-medium">{file.title}</span>
                <span className="text-muted"> · {file.name} · {file.entries} {file.entries === 1 ? "entry" : "entries"}</span>
                {file.changed ? <span className="text-muted"> · changed last night</span> : null}
              </li>
            ))}
          </ul>
          {learning.data?.question ? <Note>{learning.data.question}</Note> : null}
          {(learning.data?.habits || []).length ? (
            <ul className="space-y-1 text-sm">
              {(learning.data?.habits || []).map((habit) => (
                <li key={habit.id} className="flex items-center justify-between gap-2">
                  <span>{habit.title}: {habit.text} Suggested only.</span>
                  <Button size="sm" variant="outline" onClick={() => void api(`/api/bots/${botId}/notes/habits/${habit.id}/approve`, { method: "POST" }).then(() => queryClient.invalidateQueries({ queryKey: ["learning", botId] })).then(() => queryClient.invalidateQueries({ queryKey: ["schedules", botId] })).catch((reason: Error) => setError(reason.message))}>Approve</Button>
                </li>
              ))}
            </ul>
          ) : null}
          {(learning.data?.dreams || []).length ? (
            <ul className="space-y-1 text-sm">
              {(learning.data?.dreams || []).map((dream) => (
                <li key={dream.id}>{dream.title} — an idea only. It does not run.</li>
              ))}
            </ul>
          ) : null}
          {(learning.data?.ledger || []).some((row) => row.kind === "notes") ? (
            <ul className="space-y-1 text-sm">
              {(learning.data?.ledger || []).filter((row) => row.kind === "notes").map((row) => (
                <li key={row.id} className="flex items-center justify-between gap-2">
                  <span>Notes snapshot {localWhen(row.created_at) || row.id}</span>
                  <Button size="sm" variant="ghost" onClick={() => void api(`/api/bots/${botId}/learning/rollback/${row.id}`, { method: "POST" }).then(() => { setNote("Rolled back that notes snapshot."); return queryClient.invalidateQueries({ queryKey: ["learning", botId] }); }).catch((reason: Error) => setError(reason.message))}>Roll back</Button>
                </li>
              ))}
            </ul>
          ) : null}
          <label className="flex items-center justify-between gap-3 text-sm">
            <span>Pause learning while this bot is idle</span>
            <Switch checked={Boolean(learning.data?.paused)} onCheckedChange={(on) => void api(`/api/bots/${botId}/learning/pause`, { method: "POST", json: { on } }).then(() => queryClient.invalidateQueries({ queryKey: ["learning", botId] })).then(() => queryClient.invalidateQueries({ queryKey: ["bots"] }))} />
          </label>
          <label className="flex items-center justify-between gap-3 text-sm">
            <span>Hold candidates until I approve them</span>
            <Switch checked={Boolean(learning.data?.manual)} onCheckedChange={(on) => void api(`/api/bots/${botId}/learning/manual`, { method: "POST", json: { on } }).then(() => queryClient.invalidateQueries({ queryKey: ["learning", botId] }))} />
          </label>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" disabled={(learning.data?.ledger || []).filter((row) => !row.rolled).length === 0} onClick={() => void api(`/api/bots/${botId}/learning/rollback`, { method: "POST" }).then(() => { setNote("Rolled back the last change."); return queryClient.invalidateQueries({ queryKey: ["learning", botId] }); }).catch((reason: Error) => setError(reason.message || "There is nothing to roll back."))}>Roll back the last change</Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => void api(`/api/bots/${botId}/learning/stop`, { method: "POST" }).catch((reason: Error) => setError(reason.message))}>Stop</Button>
          </div>
          {(["waiting", "promoted", "rejected"] as const).map((key) => (
            <div key={key}>
              <h3 className="text-sm font-medium capitalize">{key}</h3>
              {(learning.data?.[key] || []).length === 0 ? <Note>{key === "waiting" ? "No candidates waiting." : key === "promoted" ? "Nothing promoted yet." : "Nothing rejected."}</Note> : (
                <ul className="space-y-1 text-sm">
                  {(learning.data?.[key] || []).map((row) => (
                    <li key={row.id} className="flex items-center justify-between gap-2">
                      <span>{row.name || row.id}{row.reason ? ` — ${row.reason}` : ""}</span>
                      {key === "waiting" ? (
                        <span className="flex gap-1">
                          <Button size="sm" variant="outline" onClick={() => void api(`/api/bots/${botId}/learning/approve/${row.id}`, { method: "POST" }).then(() => { pokeFace(botId, "glad"); return queryClient.invalidateQueries({ queryKey: ["learning", botId] }); }).catch((reason: Error) => setError(reason.message))}>Approve</Button>
                          <Button size="sm" variant="ghost" onClick={() => void api(`/api/bots/${botId}/learning/reject/${row.id}`, { method: "POST" }).then(() => { setNote("Rejected that candidate."); return queryClient.invalidateQueries({ queryKey: ["learning", botId] }); }).catch((reason: Error) => setError(reason.message))}>Reject</Button>
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          <h3 className="text-sm font-medium">Skill results</h3>
          {(learning.data?.skills || []).length === 0 ? <Note>No skill results yet.</Note> : (
            <ul className="space-y-1 text-sm">
              {(learning.data?.skills || []).map((skill) => (
                <li key={skill.name}>{skill.name} · {skill.origin || "learned"}{skill.archived ? " · archived" : ""} · {skill.passes || 0} passed, {skill.fails || 0} failed</li>
              ))}
            </ul>
          )}
        </Block>

        <Block id="memory" title="Memory">
          <Note>Memory is files on this computer. The index only points at topic files. Open a topic to read its lines. Change or drop one line. The other lines stay, and a chat is not rewritten.</Note>
          {(memory.data?.topics || []).length === 0 ? <Note>No topic files. A new line starts one. The index only points at those files.</Note> : (
            <ul className="space-y-1">
              {(memory.data?.topics || []).map((item) => (
                <li key={item.name}><button type="button" className="text-sm underline" onClick={() => setTopic(item.name)}>{item.title || item.name} · {item.count || 0}</button></li>
              ))}
            </ul>
          )}
          {topic ? (
            <div>
              <Button variant="ghost" size="sm" onClick={() => setTopic("")}>Index</Button>
              <h3 className="text-sm font-medium">{lines.data?.title || topic}</h3>
              {(lines.data?.lines || []).length === 0 ? <Note>This topic file has no lines.</Note> : (
                <ul className="space-y-2">
                  {(lines.data?.lines || []).map((row) => (
                    <li key={row.id} className="flex items-start justify-between gap-2 text-sm">
                      <span>{row.text}</span>
                      <span className="flex gap-1">
                        <button type="button" className="text-xs underline" onClick={() => {
                          const next = window.prompt("A line", row.text);
                          if (!next) return;
                          void api(`/api/bots/${botId}/memory/${row.id}`, { method: "PATCH", json: { text: next } }).then(() => queryClient.invalidateQueries({ queryKey: ["memory-topic", botId, topic] }));
                        }}>Change</button>
                        <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/memory/${row.id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["memory-topic", botId, topic] })).then(() => queryClient.invalidateQueries({ queryKey: ["memory-index", botId] }))}>Drop</button>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}
          <form className="flex gap-2" onSubmit={(event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const text = String(new FormData(form).get("text") || "");
            void api(`/api/bots/${botId}/memory`, { method: "POST", json: { text, topic: topic || null } }).then(() => {
              form.reset();
              return Promise.all([
                queryClient.invalidateQueries({ queryKey: ["memory-index", botId] }),
                queryClient.invalidateQueries({ queryKey: ["memory-topic", botId, topic] }),
              ]);
            }).catch((reason: Error) => setError(reason.message));
          }}>
            <Input name="text" required maxLength={500} placeholder="one line this bot should remember" />
            <Button type="submit">Remember</Button>
          </form>
        </Block>

        <Block title="Projects">
          <Note>A project is a named pile of files for this bot only. The files stay on this computer. Uploading or removing one does not change a chat.</Note>
          <form className="flex gap-2" onSubmit={(event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const name = String(new FormData(form).get("name") || "");
            void api<Project>(`/api/bots/${botId}/projects`, { method: "POST", json: { name } }).then((created) => {
              form.reset();
              setProjectId(created.id);
              return queryClient.invalidateQueries({ queryKey: ["bot-projects", botId] });
            }).catch((reason: Error) => setError(reason.message));
          }}>
            <Input name="name" required maxLength={80} placeholder="Kiln notes" />
            <Button type="submit">Create project</Button>
          </form>
          {(projects.data || []).length === 0 ? <Note>No projects for this bot. Create one, then upload files.</Note> : (
            <ul className="space-y-1 text-sm">
              {(projects.data || []).map((item) => (
                <li key={item.id}><button type="button" className="underline" onClick={() => setProjectId(item.id)}>{item.name}</button></li>
              ))}
            </ul>
          )}
          {project ? (
            <div className="space-y-2">
              <h3 className="font-medium">{project.name}</h3>
              <form onSubmit={(event) => {
                event.preventDefault();
                const input = event.currentTarget.elements.namedItem("files") as HTMLInputElement;
                const body = new FormData();
                for (const file of input.files || []) body.append("files", file);
                void api(`/api/bots/${botId}/projects/${project.id}/files`, { method: "POST", body }).then(() => queryClient.invalidateQueries({ queryKey: ["bot-projects", botId] })).catch((reason: Error) => setError(reason.message));
              }}>
                <input name="files" type="file" multiple />
                <Button className="ml-2" type="submit" size="sm">Upload</Button>
              </form>
              {(project.files || []).length === 0 ? <Note>No files in this project yet.</Note> : (
                <ul className="text-sm">
                  {(project.files || []).map((file) => (
                    <li key={file.id} className="flex justify-between gap-2">
                      <span>{file.name}</span>
                      <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/projects/${project.id}/files/${file.id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["bot-projects", botId] }))}>Remove</button>
                    </li>
                  ))}
                </ul>
              )}
              <Button type="button" variant="danger" size="sm" onClick={() => askConfirm({
                title: `Remove ${project.name}?`,
                copy: "You have to type the project's name. Chats stay.",
                name: project.name,
                submit: "Remove project",
                run: async () => {
                  await api(`/api/bots/${botId}/projects/${project.id}`, { method: "DELETE", json: { confirm_name: project.name } });
                  setProjectId("");
                  await queryClient.invalidateQueries({ queryKey: ["bot-projects", botId] });
                },
              })}>Remove project</Button>
            </div>
          ) : null}
        </Block>

        <Block title="Routines">
          <Note>A routine runs a saved prompt on your clock and posts the result into this bot's chat. The unread dot lights up. Quiet mode stays in the log when the reply is nothing new. A live chat keeps the model; the routine waits.</Note>
          <RoutineForm botId={botId} onError={setError} />
          {(schedules.data || []).length === 0 ? <Note>No routines yet. Add one, or ask in the chat and confirm the card.</Note> : (
            <ul className="space-y-3 text-sm">
              {(schedules.data || []).map((item) => (
                <li key={item.id} className="space-y-1">
                  <div className="flex items-start justify-between gap-2">
                    <span>
                      <strong>{item.name || item.prompt}</strong>
                      {item.paused ? " · paused" : ""}
                      {item.quiet ? " · quiet" : ""}
                    </span>
                    <span className="flex shrink-0 gap-2">
                      <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/schedules/${item.id}/run`, { method: "POST" }).then(() => queryClient.invalidateQueries({ queryKey: ["schedules", botId] })).then(() => queryClient.invalidateQueries({ queryKey: ["jobs", botId] })).then(() => queryClient.invalidateQueries({ queryKey: ["unread"] })).catch((reason: Error) => setError(reason.message))}>Run now</button>
                      <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/schedules/${item.id}/pause`, { method: "POST", json: { paused: !item.paused } }).then(() => queryClient.invalidateQueries({ queryKey: ["schedules", botId] }))}>{item.paused ? "Resume" : "Pause"}</button>
                      <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/schedules/${item.id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["schedules", botId] })).then(() => queryClient.invalidateQueries({ queryKey: ["routine-trash", botId] }))}>Delete</button>
                    </span>
                  </div>
                  <p className="text-xs text-muted">{item.preview || item.label}</p>
                </li>
              ))}
            </ul>
          )}
          <RoutineTrash botId={botId} onError={setError} />
          <h3 className="text-sm font-medium">Job log</h3>
          {(jobs.data || []).length === 0 ? <Note>Nothing has fired yet.</Note> : (
            <ul className="space-y-1 text-xs text-muted">
              {(jobs.data || []).slice(0, 12).map((job, index) => (
                <li key={job.id || index}>{job.status || "done"} · {job.error || job.result || job.prompt || ""}</li>
              ))}
            </ul>
          )}
        </Block>

        <Block title="Tell me when">
          <form className="flex items-end gap-2" onSubmit={(event) => {
            event.preventDefault();
            const kind = String(new FormData(event.currentTarget).get("kind") || "message");
            void api("/api/watches", { method: "POST", json: { kind } }).then(() => queryClient.invalidateQueries({ queryKey: ["watches"] })).catch((reason: Error) => setError(reason.message));
          }}>
            <label className="text-sm">When
              <select name="kind" className="mt-1 block h-9 rounded-md border border-border bg-card px-2">
                <option value="message">A new message</option>
                <option value="job_failed">A job that failed</option>
              </select>
            </label>
            <Button type="submit" size="sm">Tell me</Button>
          </form>
          <Note>One notice, from this computer. It fires once. You do not have to leave this page open or watch for it.</Note>
          <Note>{(watches.data || []).length ? `${watches.data?.length} waiting.` : "None waiting."}</Note>
        </Block>

        <Block id="skills" title="Skills">
          <Note>A skill is a markdown note any bot can follow. Saving one does not change a chat.</Note>
          <SkillForm onError={setError} />
          {(skills.data || []).length === 0 ? <Note>No skills yet. Add one in markdown. The bot can use it on the next reply.</Note> : (
            <ul className="space-y-1 text-sm">{(skills.data || []).map((skill) => <li key={skill.name}><strong>{skill.name}</strong> {skill.description}</li>)}</ul>
          )}
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">Proposals</h3>
            <Button type="button" variant="ghost" size="sm" onClick={() => void api("/api/night", { method: "POST" }).then(() => queryClient.invalidateQueries({ queryKey: ["proposals"] })).catch((reason: Error) => setError(reason.message))}>Run night pass</Button>
          </div>
          <Note>A night pass can propose a skill or a memory line. It does not install it.</Note>
          {(proposals.data || []).length === 0 ? <Note>No proposals.</Note> : (
            <ul className="text-sm">{(proposals.data || []).map((item) => <li key={item.id}>{item.kind}: {item.name || item.text}</li>)}</ul>
          )}
        </Block>

        <Block title="What it can do">
          <ul className="list-disc space-y-1 pl-4 text-sm">
            <li>Files on this computer. List a folder, read a file, or write a file.</li>
            <li>A command on this computer.</li>
            <li>Web search on this computer.</li>
            <li>A command on a Linux computer you saved.</li>
            <li>A command on a Windows computer you saved.</li>
            <li>A question with choices you can tap. You can still type.</li>
            <li>A file or a picture in the chat.</li>
            <li>Files in a project this bot can use. List them, or read one.</li>
            <li>Memory files. Read the index, then one topic file.</li>
          </ul>
        </Block>

        <Block title="Delete this bot">
          <Note>You have to type the bot's name. That deletes this bot and its chats. Other bots stay.</Note>
          <Button variant="danger" onClick={() => askConfirm({
            title: `Remove ${bot.name}?`,
            copy: "You have to type the bot's name. That deletes this bot and its chats. Other bots stay.",
            name: bot.name,
            submit: "Remove bot",
            run: async () => {
              await api(`/api/bots/${botId}`, { method: "DELETE", json: { confirm_name: bot.name } });
              useApp.setState({ botId: null, chatId: null, screen: "chat" });
              await queryClient.invalidateQueries({ queryKey: ["bots"] });
            },
          })}>Remove bot</Button>
        </Block>
      </div>
    </div>
  );
}

function RoutineForm({ botId, onError }: { botId: string; onError: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [when, setWhen] = useState("weekdays");
  const templates = useQuery({ queryKey: ["routine-templates"], queryFn: () => api<{ id: string; name: string; blurb: string }[]>("/api/routine-templates") });
  if (!open) return <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(true)}>Add</Button>;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {(templates.data || []).map((item) => (
          <Button key={item.id} type="button" size="sm" variant="outline" onClick={() => {
            void api<Schedule>(`/api/bots/${botId}/schedules`, { method: "POST", json: { preset: item.id } }).then(() => {
              setOpen(false);
              return queryClient.invalidateQueries({ queryKey: ["schedules", botId] });
            }).catch((reason: Error) => onError(reason.message));
          }}>{item.name}</Button>
        ))}
      </div>
      <form className="space-y-2" onSubmit={(event) => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        const name = String(data.get("name") || "").trim();
        const prompt = String(data.get("prompt") || "").trim();
        const timezone = String(data.get("timezone") || "").trim();
        const quiet = data.get("quiet") === "on";
        const body: Record<string, unknown> = { name, prompt, quiet };
        if (timezone) body.timezone = timezone;
        if (when === "weekdays") body.weekdays = String(data.get("time") || "8:00 AM");
        else if (when === "daily") body.daily = String(data.get("time") || "8:00 AM");
        else if (when === "interval") {
          body.kind = "interval";
          body.every_minutes = Number(data.get("every_minutes") || 60);
        } else {
          body.kind = "cron";
          body.cron = String(data.get("cron") || "");
        }
        void api<Schedule>(`/api/bots/${botId}/schedules`, { method: "POST", json: body }).then(() => {
          setOpen(false);
          return queryClient.invalidateQueries({ queryKey: ["schedules", botId] });
        }).catch((reason: Error) => onError(reason.message));
      }}>
        <Input name="name" required maxLength={80} placeholder="Morning briefing" />
        <Textarea name="prompt" required maxLength={4000} rows={2} placeholder="What should this bot do on its own?" />
        <select className="h-9 rounded-md border border-border bg-card px-2" value={when} onChange={(event) => setWhen(event.target.value)}>
          <option value="weekdays">Weekdays</option>
          <option value="daily">Every day</option>
          <option value="interval">Every N minutes</option>
          <option value="cron">Cron</option>
        </select>
        {when === "weekdays" || when === "daily" ? <Input name="time" defaultValue="8:00 AM" placeholder="8:00 AM" /> : null}
        {when === "interval" ? <Input name="every_minutes" type="number" min={5} max={10080} defaultValue={60} /> : null}
        {when === "cron" ? <Input name="cron" maxLength={80} placeholder="0 8 * * 1-5" /> : null}
        <Input name="timezone" placeholder="Local time, or America/Chicago" />
        <label className="flex items-center gap-2 text-sm">
          <input name="quiet" type="checkbox" />
          Quiet. “Nothing new” does not post or light the unread dot.
        </label>
        <Note>The line under a saved routine is the next run in your local time. A gap under 5 minutes is refused.</Note>
        <div className="flex gap-2">
          <Button type="submit" size="sm">Save routine</Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
        </div>
      </form>
    </div>
  );
}

function RoutineTrash({ botId, onError }: { botId: string; onError: (message: string) => void }) {
  const trash = useQuery({ queryKey: ["routine-trash", botId], enabled: Boolean(botId), queryFn: () => api<Schedule[]>(`/api/bots/${botId}/schedules/trash`) });
  const rows = trash.data || [];
  if (!rows.length) return null;
  return (
    <div className="space-y-1">
      <h3 className="text-sm font-medium">Trash</h3>
      <ul className="space-y-1 text-sm">
        {rows.map((item) => (
          <li key={item.id} className="flex items-center justify-between gap-2">
            <span>{item.name || item.prompt}</span>
            <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/schedules/${item.id}/restore`, { method: "POST" }).then(() => queryClient.invalidateQueries({ queryKey: ["schedules", botId] })).then(() => queryClient.invalidateQueries({ queryKey: ["routine-trash", botId] })).catch((reason: Error) => onError(reason.message))}>Restore</button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SkillForm({ onError }: { onError: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  if (!open) return <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(true)}>Add</Button>;
  return (
    <form className="space-y-2" onSubmit={(event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      void api("/api/skills", {
        method: "POST",
        json: { name: String(data.get("name") || ""), description: String(data.get("description") || ""), body: String(data.get("body") || "") },
      }).then(() => {
        setOpen(false);
        return queryClient.invalidateQueries({ queryKey: ["skills"] });
      }).catch((reason: Error) => onError(reason.message));
    }}>
      <Input name="name" required maxLength={48} placeholder="desk-notes" />
      <Input name="description" maxLength={200} placeholder="one plain line" />
      <Textarea name="body" rows={5} placeholder="Write the steps in markdown." />
      <Note>Add one in markdown. The bot can use it on the next reply.</Note>
      <div className="flex gap-2">
        <Button type="submit" size="sm">Save skill</Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </form>
  );
}
