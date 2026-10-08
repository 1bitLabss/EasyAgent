import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { api, queryClient } from "@/api";
import { FACE_PALETTE, Face } from "@/components/Face";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { pokeFace } from "@/lib/mood";
import { contextNote } from "@/lib/run";
import { windowChat } from "@/lib/window";
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
    return <section className="p-6"><h1 className="text-2xl font-semibold">Pick a bot first.</h1><p className="mt-2 text-sm text-muted">Settings belong to one bot. Chats are not changed here.</p></section>;
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
                  {row.created_at || "undated"} — {row.preview || row.message_id}
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
                  <span>Notes snapshot {row.created_at || row.id}</span>
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
            <Button type="button" variant="outline" size="sm" onClick={() => void api(`/api/bots/${botId}/learning/rollback`, { method: "POST" }).then(() => { setNote("Rolled back the last change."); return queryClient.invalidateQueries({ queryKey: ["learning", botId] }); }).catch((reason: Error) => setError(reason.message))}>Roll back the last change</Button>
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
                      {key === "waiting" ? <Button size="sm" variant="outline" onClick={() => void api(`/api/bots/${botId}/learning/approve/${row.id}`, { method: "POST" }).then(() => { pokeFace(botId, "glad"); return queryClient.invalidateQueries({ queryKey: ["learning", botId] }); })}>Approve</Button> : null}
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

        <Block title="Schedules">
          <Note>A schedule runs a saved prompt on a timer. It writes the job log, not a chat.</Note>
          <ScheduleForm botId={botId} onError={setError} />
          {(schedules.data || []).length === 0 ? <Note>No schedules. Add one to run a prompt on a timer. Chats are not part of the job.</Note> : (
            <ul className="space-y-2 text-sm">
              {(schedules.data || []).map((item) => (
                <li key={item.id} className="flex items-center justify-between gap-2">
                  <span>{item.prompt} · {item.kind === "cron" ? item.cron : `every ${item.every_minutes} min`}{item.paused ? " · paused" : ""}</span>
                  <span className="flex gap-2">
                    <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/schedules/${item.id}/pause`, { method: "POST", json: { paused: !item.paused } }).then(() => queryClient.invalidateQueries({ queryKey: ["schedules", botId] }))}>{item.paused ? "Resume" : "Pause"}</button>
                    <button type="button" className="text-xs underline" onClick={() => void api(`/api/bots/${botId}/schedules/${item.id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["schedules", botId] }))}>Remove</button>
                  </span>
                </li>
              ))}
            </ul>
          )}
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

function ScheduleForm({ botId, onError }: { botId: string; onError: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState("interval");
  if (!open) return <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(true)}>Add</Button>;
  return (
    <form className="space-y-2" onSubmit={(event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = new FormData(form);
      const body = kind === "cron"
        ? { prompt: String(data.get("prompt") || ""), kind, cron: String(data.get("cron") || "") }
        : { prompt: String(data.get("prompt") || ""), kind, every_minutes: Number(data.get("every_minutes") || 1) };
      void api(`/api/bots/${botId}/schedules`, { method: "POST", json: body }).then(() => {
        setOpen(false);
        return queryClient.invalidateQueries({ queryKey: ["schedules", botId] });
      }).catch((reason: Error) => onError(reason.message));
    }}>
      <Textarea name="prompt" required maxLength={4000} rows={2} placeholder="What should this bot do on its own?" />
      <select className="h-9 rounded-md border border-border bg-card px-2" value={kind} onChange={(event) => setKind(event.target.value)}>
        <option value="interval">Every N minutes</option>
        <option value="cron">Cron, local time</option>
      </select>
      {kind === "cron" ? <Input name="cron" maxLength={80} placeholder="*/15 * * * *" /> : <Input name="every_minutes" type="number" min={1} max={1440} defaultValue={1} />}
      <Note>Five fields are minute, hour, day, month, weekday, in local time. A stopped app runs a due slot once when it starts again, and does not run that slot twice.</Note>
      <div className="flex gap-2">
        <Button type="submit" size="sm">Save schedule</Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </form>
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
