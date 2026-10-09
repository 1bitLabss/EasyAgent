import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { api, queryClient, saveToken } from "@/api";
import { Face } from "@/components/Face";
import { ThinkingBox } from "@/components/ThinkingBox";
import { Markdown } from "@/components/Markdown";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { contextNote } from "@/lib/run";
import { useApp } from "@/store";
import type { Bot, Computer, Endpoint, Project, Room } from "@/types";

function Page({ kicker, title, lede, children }: { kicker: string; title: string; lede: string; children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto px-6 py-5">
      <p className="text-xs uppercase tracking-wide text-muted">{kicker}</p>
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="mt-1 max-w-2xl text-sm text-muted">{lede}</p>
      <div className="mt-5 space-y-4">{children}</div>
    </div>
  );
}

export function ConnectionsScreen() {
  const askConfirm = useApp((state) => state.askConfirm);
  const endpoints = useQuery({ queryKey: ["endpoints"], queryFn: () => api<Endpoint[]>("/api/endpoints") });
  const [editing, setEditing] = useState<Endpoint | null | undefined>(undefined);
  const [error, setError] = useState("");
  return (
    <Page kicker="Connections" title="Where a bot sends messages" lede="A connection is a server that can chat. It is not a conversation. Chats stay on this computer.">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">Saved connections</h2>
        <Button variant="ghost" size="sm" onClick={() => { setEditing(null); setError(""); }}>Add</Button>
      </div>
      {editing !== undefined ? (
        <form className="space-y-2 rounded-lg border border-border bg-card p-4" onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const body = {
            name: String(data.get("name") || ""),
            base_url: String(data.get("base_url") || ""),
            api_key: String(data.get("api_key") || ""),
            model: String(data.get("model") || ""),
            clear_api_key: data.get("clear_api_key") === "on",
            max_parallel: Number(data.get("max_parallel") || 1),
          };
          const request = editing
            ? api(`/api/endpoints/${editing.id}`, { method: "PATCH", json: body })
            : api("/api/endpoints", { method: "POST", json: body });
          void request.then(() => {
            setEditing(undefined);
            return queryClient.invalidateQueries({ queryKey: ["endpoints"] });
          }).catch((reason: Error) => setError(reason.message));
        }}>
          <label className="block text-sm">Name <Input name="name" required maxLength={80} defaultValue={editing?.name || ""} className="mt-1" /></label>
          <label className="block text-sm">Address <Input name="base_url" required type="url" defaultValue={editing?.base_url || ""} placeholder="http://localhost:8080/v1" className="mt-1" /></label>
          <label className="block text-sm">Key <span className="text-muted">optional</span>
            <Input name="api_key" type="password" autoComplete="off" placeholder={editing?.has_api_key ? "Saved. Leave blank to keep it." : "only if the server asks"} className="mt-1" />
          </label>
          {editing?.has_api_key ? <label className="flex items-center gap-2 text-sm"><input name="clear_api_key" type="checkbox" /> Clear the saved key</label> : null}
          <label className="block text-sm">Model name <span className="text-muted">optional</span>
            <Input name="model" maxLength={120} defaultValue={editing?.model || ""} className="mt-1" />
          </label>
          <label className="block text-sm">At once <span className="text-muted">1–32</span>
            <Input name="max_parallel" type="number" min={1} max={32} required defaultValue={editing?.max_parallel || 1} className="mt-1" />
          </label>
          <p className="text-xs text-muted">EasyAgent sends each message to this address. Leave the key blank to keep the one already saved. At once is how many replies may use this connection together. Extra chats wait in line.</p>
          {error ? <p className="text-sm text-danger" role="alert">{error}</p> : null}
          <div className="flex gap-2">
            <Button type="submit">Save</Button>
            <Button type="button" variant="ghost" onClick={() => setEditing(undefined)}>Cancel</Button>
          </div>
        </form>
      ) : null}
      {(endpoints.data || []).length === 0 ? <p className="text-sm text-muted">No connections yet. Add the address of a server that can chat.</p> : (
        <ul className="space-y-2">
          {(endpoints.data || []).map((endpoint) => (
            <li key={endpoint.id} className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-2">
              <button type="button" className="min-w-0 text-left" onClick={() => setEditing(endpoint)}>
                <strong className="block">{endpoint.name}</strong>
                <span className="block truncate text-xs text-muted">{endpoint.base_url} · {endpoint.max_parallel} at a time</span>
              </button>
              <Button variant="ghost" size="sm" onClick={() => askConfirm({
                title: `Remove ${endpoint.name}?`,
                copy: "This deletes only this connection. Bots and chats stay until you point a bot somewhere else.",
                name: endpoint.name,
                submit: "Remove connection",
                run: async () => {
                  await api(`/api/endpoints/${endpoint.id}`, { method: "DELETE", json: { confirm_name: endpoint.name } });
                  await queryClient.invalidateQueries({ queryKey: ["endpoints"] });
                },
              })}>Remove</Button>
            </li>
          ))}
        </ul>
      )}
    </Page>
  );
}

export function RoomsScreen() {
  const roomId = useApp((state) => state.roomId);
  const setRoom = useApp((state) => state.setRoom);
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: () => api<Room[]>("/api/rooms") });
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const room = useQuery({
    queryKey: ["room", roomId],
    enabled: Boolean(roomId),
    queryFn: () => api<Room>(`/api/rooms/${roomId}`),
  });
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const [draft, setDraft] = useState("");
  if (!roomId || !room.data) {
    return (
      <Page kicker="Rooms" title="A separate conversation" lede="A room is not a private chat. Each bot you add replies here, and their own chats stay as they were.">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">Rooms</h2>
          <Button variant="ghost" size="sm" onClick={() => setAdding((open) => !open)}>Add</Button>
        </div>
        {adding ? (
          <form className="space-y-2" onSubmit={(event) => {
            event.preventDefault();
            const name = String(new FormData(event.currentTarget).get("name") || "");
            void api<Room>("/api/rooms", { method: "POST", json: { name } }).then((created) => {
              setAdding(false);
              setRoom(created.id);
              return queryClient.invalidateQueries({ queryKey: ["rooms"] });
            }).catch((reason: Error) => setError(reason.message));
          }}>
            <Input name="name" required maxLength={80} placeholder="Desk" />
            <p className="text-xs text-muted">A room has its own transcript. The bots you add keep their private chats.</p>
            {error ? <p className="text-sm text-danger">{error}</p> : null}
            <Button type="submit" size="sm">Create room</Button>
          </form>
        ) : null}
        {(rooms.data || []).length === 0 ? <p className="text-sm text-muted">No rooms yet. Create one, then add two or more bots.</p> : (
          <ul>{(rooms.data || []).map((item) => <li key={item.id}><button type="button" className="text-sm underline" onClick={() => setRoom(item.id)}>{item.name}</button></li>)}</ul>
        )}
      </Page>
    );
  }
  const open = room.data;
  const members = (open.bot_ids || []).map((id) => (bots.data || []).find((bot) => bot.id === id)).filter(Boolean) as Bot[];
  return (
    <section className="flex h-full min-h-0 flex-col">
      <header className="border-b border-border px-4 py-3">
        <p className="text-xs uppercase tracking-wide text-muted">Room</p>
        <h1 className="text-xl font-semibold">{open.name}</h1>
        <Button variant="ghost" size="sm" onClick={() => setRoom(null)}>All rooms</Button>
        <ul className="mt-2 flex flex-wrap gap-2">
          {members.map((bot) => (
            <li key={bot.id} className="flex items-center gap-1 text-sm">
              <Face color={bot.face_color} tiny /> {bot.name}
              <button type="button" className="text-xs underline" onClick={() => void api(`/api/rooms/${open.id}/bots/${bot.id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["room", open.id] }))}>Remove</button>
            </li>
          ))}
        </ul>
        <form className="mt-2 flex gap-2" onSubmit={(event) => {
          event.preventDefault();
          const bot_id = String(new FormData(event.currentTarget).get("bot_id") || "");
          if (!bot_id) return;
          void api(`/api/rooms/${open.id}/bots`, { method: "POST", json: { bot_id } }).then(() => queryClient.invalidateQueries({ queryKey: ["room", open.id] })).catch((reason: Error) => setError(reason.message));
        }}>
          <select name="bot_id" className="h-9 rounded-md border border-border bg-card px-2 text-sm">
            <option value="">Add a bot</option>
            {(bots.data || []).filter((bot) => !(open.bot_ids || []).includes(bot.id)).map((bot) => <option key={bot.id} value={bot.id}>{bot.name}</option>)}
          </select>
          <Button type="submit" size="sm">Add</Button>
        </form>
        {error ? <p className="text-sm text-danger">{error}</p> : null}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {(open.messages || []).length === 0 ? <p className="text-sm text-muted">Nothing in this room yet. Add the bots, then send. Each one replies here, in order. This does not write their private chats.</p> : (
          <ol className="space-y-3">
            {(open.messages || []).map((message, index) => {
              const speaker = (bots.data || []).find((bot) => bot.id === message.speaker);
              const last = index === (open.messages || []).length - 1;
              return (
                <li key={message.id || index} className="rounded-lg bg-card px-3 py-2">
                  <p className="mb-1 flex items-center gap-2 text-xs text-muted">
                    {speaker ? <Face color={speaker.face_color} tiny /> : null}
                    {message.speaker_name || (message.role === "user" ? "You" : "Reply")}
                  </p>
                  {message.role !== "user" ? <ThinkingBox text={message.thinking} seconds={message.thought_seconds} /> : null}
                  {message.role === "user" ? <p className="whitespace-pre-wrap">{message.content}</p> : <Markdown text={message.content || ""} />}
                  {last && (message.choices || []).length > 1 ? (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {(message.choices || []).map((choice) => (
                        <Button key={choice} size="sm" variant="outline" onClick={() => {
                          setDraft(choice);
                          void api<Room>(`/api/rooms/${open.id}/messages`, { method: "POST", json: { content: choice } }).then((next) => {
                            queryClient.setQueryData(["room", open.id], next);
                            setDraft("");
                          }).catch((reason: Error) => setError(reason.message));
                        }}>{choice}</Button>
                      ))}
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ol>
        )}
      </div>
      <form className="border-t border-border p-3" onSubmit={(event) => {
        event.preventDefault();
        const content = draft.trim();
        if (!content) return;
        setDraft("");
        void api<Room>(`/api/rooms/${open.id}/messages`, { method: "POST", json: { content } }).then((next) => {
          queryClient.setQueryData(["room", open.id], next);
          void api(`/api/rooms/${open.id}/read`, { method: "POST" });
        }).catch((reason: Error) => setError(reason.message));
      }}>
        <p className="mb-2 text-xs text-muted">{contextNote(open.context, "room")}</p>
        <Textarea value={draft} rows={3} placeholder="Message the room. Enter to send. Each bot replies in turn." onChange={(event) => setDraft(event.target.value)} />
        <Button className="mt-2" type="submit">Send</Button>
      </form>
    </section>
  );
}

export function ProjectsScreen() {
  const projectId = useApp((state) => state.projectId);
  const setProject = useApp((state) => state.setProject);
  const askConfirm = useApp((state) => state.askConfirm);
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/api/projects") });
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const project = useQuery({
    queryKey: ["project", projectId],
    enabled: Boolean(projectId),
    queryFn: () => api<Project>(`/api/projects/${projectId}`),
  });
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  if (!projectId || !project.data) {
    return (
      <Page kicker="Projects" title="A named pile of files" lede="A group project is not a room and not a chat. You choose which bots can use the files. The files stay on this computer.">
        <div className="flex items-center justify-between">
          <h2 className="font-medium">Group projects</h2>
          <Button variant="ghost" size="sm" onClick={() => setAdding((value) => !value)}>Add</Button>
        </div>
        {adding ? (
          <form className="flex gap-2" onSubmit={(event) => {
            event.preventDefault();
            const name = String(new FormData(event.currentTarget).get("name") || "");
            void api<Project>("/api/projects", { method: "POST", json: { name } }).then((created) => {
              setProject(created.id);
              return queryClient.invalidateQueries({ queryKey: ["projects"] });
            }).catch((reason: Error) => setError(reason.message));
          }}>
            <Input name="name" required maxLength={80} placeholder="Shop manual" />
            <Button type="submit">Create project</Button>
          </form>
        ) : null}
        {error ? <p className="text-sm text-danger">{error}</p> : null}
        <p className="text-xs text-muted">Create it, then open it to upload files and choose the bots. Rooms stay as they are.</p>
        {(projects.data || []).length === 0 ? <p className="text-sm text-muted">No group projects yet. Create one, then choose which bots can use it.</p> : (
          <ul>{(projects.data || []).map((item) => <li key={item.id}><button type="button" className="text-sm underline" onClick={() => setProject(item.id)}>{item.name}</button></li>)}</ul>
        )}
      </Page>
    );
  }
  const open = project.data;
  return (
    <div className="h-full overflow-y-auto px-6 py-5">
      <p className="text-xs uppercase tracking-wide text-muted">Group project</p>
      <h1 className="text-2xl font-semibold">{open.name}</h1>
      <p className="mt-1 max-w-2xl text-sm text-muted">These files stay on this computer. A bot you add here can list and read them. Removing a bot from this project does not remove it from a room.</p>
      <Button variant="ghost" size="sm" onClick={() => setProject(null)}>All projects</Button>
      {error ? <p className="mt-2 text-sm text-danger">{error}</p> : null}
      <section className="mt-4 space-y-2">
        <h2 className="font-medium">Files</h2>
        <form onSubmit={(event) => {
          event.preventDefault();
          const input = event.currentTarget.elements.namedItem("files") as HTMLInputElement;
          const body = new FormData();
          for (const file of input.files || []) body.append("files", file);
          void api(`/api/projects/${open.id}/files`, { method: "POST", body }).then(() => queryClient.invalidateQueries({ queryKey: ["project", open.id] })).catch((reason: Error) => setError(reason.message));
        }}>
          <input name="files" type="file" multiple />
          <Button className="ml-2" size="sm" type="submit">Upload</Button>
        </form>
        {(open.files || []).length === 0 ? <p className="text-sm text-muted">No files yet. Upload one or more. A chat is not changed.</p> : (
          <ul className="text-sm">{(open.files || []).map((file) => (
            <li key={file.id} className="flex justify-between"><span>{file.name}</span><button type="button" className="text-xs underline" onClick={() => void api(`/api/projects/${open.id}/files/${file.id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["project", open.id] }))}>Remove</button></li>
          ))}</ul>
        )}
      </section>
      <section className="mt-6 space-y-2">
        <h2 className="font-medium">Bots who can use this</h2>
        <form className="flex gap-2" onSubmit={(event) => {
          event.preventDefault();
          const bot_id = String(new FormData(event.currentTarget).get("bot_id") || "");
          void api(`/api/projects/${open.id}/bots`, { method: "POST", json: { bot_id } }).then(() => queryClient.invalidateQueries({ queryKey: ["project", open.id] })).catch((reason: Error) => setError(reason.message));
        }}>
          <select name="bot_id" className="h-9 rounded-md border border-border bg-card px-2">
            {(bots.data || []).filter((bot) => !(open.bot_ids || []).includes(bot.id)).map((bot) => <option key={bot.id} value={bot.id}>{bot.name}</option>)}
          </select>
          <Button type="submit" size="sm">Add bot</Button>
        </form>
        {(open.bot_ids || []).length === 0 ? <p className="text-sm text-muted">No bots yet. A bot that is not added cannot read these files.</p> : (
          <ul className="text-sm">{(open.bot_ids || []).map((id) => {
            const bot = (bots.data || []).find((item) => item.id === id);
            return <li key={id} className="flex justify-between"><span>{bot?.name || id}</span><button type="button" className="text-xs underline" onClick={() => void api(`/api/projects/${open.id}/bots/${id}`, { method: "DELETE" }).then(() => queryClient.invalidateQueries({ queryKey: ["project", open.id] }))}>Remove</button></li>;
          })}</ul>
        )}
      </section>
      <section className="mt-6">
        <h2 className="font-medium">Remove this project</h2>
        <p className="text-xs text-muted">You have to type the project's name. Rooms and chats stay.</p>
        <Button className="mt-2" variant="danger" onClick={() => askConfirm({
          title: `Remove ${open.name}?`,
          copy: "You have to type the project's name. Rooms and chats stay.",
          name: open.name,
          submit: "Remove project",
          run: async () => {
            await api(`/api/projects/${open.id}`, { method: "DELETE", json: { confirm_name: open.name } });
            setProject(null);
            await queryClient.invalidateQueries({ queryKey: ["projects"] });
          },
        })}>Remove project</Button>
      </section>
    </div>
  );
}

export function ComputersScreen() {
  const askConfirm = useApp((state) => state.askConfirm);
  const computers = useQuery({ queryKey: ["computers"], queryFn: () => api<Computer[]>("/api/computers") });
  const [form, setForm] = useState<Partial<Computer> | null>(null);
  const [pending, setPending] = useState<Partial<Computer> | null>(null);
  const [error, setError] = useState("");
  return (
    <Page kicker="Computers" title="Machines a bot can use" lede="A saved computer is a Linux or Windows machine, not a chat. The sign-in prompt is not written into a transcript.">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">Saved computers</h2>
        <Button variant="ghost" size="sm" onClick={() => { setForm({ kind: "linux" }); setError(""); }}>Add</Button>
      </div>
      {form ? (
        <form className="space-y-2 rounded-lg border border-border bg-card p-4" onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const port = String(data.get("port") || "");
          setPending({
            id: form.id,
            name: String(data.get("name") || ""),
            kind: String(data.get("kind") || "linux"),
            host: String(data.get("host") || ""),
            port: port ? Number(port) : null,
          });
        }}>
          <label className="block text-sm">Name <Input name="name" required maxLength={80} defaultValue={form.name || ""} className="mt-1" /></label>
          <label className="block text-sm">Kind
            <select name="kind" defaultValue={form.kind || "linux"} className="mt-1 h-9 w-full rounded-md border border-border bg-card px-2">
              <option value="linux">Linux computer</option>
              <option value="windows">Windows computer</option>
            </select>
          </label>
          <label className="block text-sm">Host <Input name="host" required maxLength={253} defaultValue={form.host || ""} className="mt-1" /></label>
          <label className="block text-sm">Port <span className="text-muted">optional</span>
            <Input name="port" type="number" min={1} max={65535} defaultValue={form.port || ""} className="mt-1" />
          </label>
          <p className="text-xs text-muted">Saving opens a sign-in prompt for the username and password. That prompt is not written into the chat.</p>
          {error ? <p className="text-sm text-danger">{error}</p> : null}
          <div className="flex gap-2">
            <Button type="submit">Save</Button>
            <Button type="button" variant="ghost" onClick={() => setForm(null)}>Cancel</Button>
          </div>
        </form>
      ) : null}
      {(computers.data || []).length === 0 ? <p className="text-sm text-muted">No computers yet. Add a Linux or Windows computer the bot can use.</p> : (
        <ul className="space-y-2">
          {(computers.data || []).map((computer) => (
            <li key={computer.id} className="flex items-center justify-between gap-2">
              <button type="button" className="text-left" onClick={() => setForm(computer)}>
                <strong className="block">{computer.name}</strong>
                <span className="text-xs text-muted">{computer.kind === "windows" ? "Windows" : "Linux"} · {computer.host} · {computer.has_sign_in ? "Sign-in saved" : "No sign-in"}</span>
              </button>
              <Button variant="ghost" size="sm" onClick={() => askConfirm({
                title: `Remove ${computer.name}?`,
                copy: "This deletes only this saved computer. Chats, bots, and other computers stay.",
                name: computer.name,
                submit: "Remove computer",
                run: async () => {
                  await api(`/api/computers/${computer.id}`, { method: "DELETE", json: { confirm_name: computer.name } });
                  await queryClient.invalidateQueries({ queryKey: ["computers"] });
                },
              })}>Remove</Button>
            </li>
          ))}
        </ul>
      )}
      <SignIn open={Boolean(pending)} kind={pending?.kind || "linux"} editing={Boolean(pending?.id)} onClose={() => setPending(null)} onSave={(user, password, key) => {
        if (!pending?.name || !pending.host) return;
        const payload: Record<string, unknown> = { name: pending.name, kind: pending.kind, host: pending.host, user, password, key: pending.kind === "linux" ? key : "" };
        if (pending.port) payload.port = pending.port;
        const request = pending.id
          ? api(`/api/computers/${pending.id}`, { method: "PATCH", json: payload })
          : api("/api/computers", { method: "POST", json: payload });
        void request.then(() => {
          setPending(null);
          setForm(null);
          return queryClient.invalidateQueries({ queryKey: ["computers"] });
        }).catch((reason: Error) => setError(reason.message));
      }} />
    </Page>
  );
}

function SignIn({ open, kind, editing, onClose, onSave }: { open: boolean; kind: string; editing: boolean; onClose: () => void; onSave: (user: string, password: string, key: string) => void }) {
  const [error, setError] = useState("");
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent>
        <DialogTitle>Username and password</DialogTitle>
        <DialogDescription>This prompt only receives the sign-in. It is not written into the chat.</DialogDescription>
        <form className="mt-3 space-y-2" onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const user = String(data.get("user") || "");
          const password = String(data.get("password") || "");
          const key = String(data.get("key") || "");
          if (!editing && !user.trim()) { setError("Type the username."); return; }
          if (!editing && kind === "windows" && !password) { setError("Type the password."); return; }
          if (!editing && kind === "linux" && !password && !key) { setError("Type a password or paste a key."); return; }
          setError("");
          onSave(user, password, key);
        }}>
          <label className="block text-sm">Username <Input name="user" maxLength={80} autoComplete="off" className="mt-1" /></label>
          <label className="block text-sm">Password <span className="text-muted">{kind === "linux" ? "or a key" : "required"}</span>
            <Input name="password" type="password" autoComplete="off" className="mt-1" />
          </label>
          {kind === "linux" ? <label className="block text-sm">Key <span className="text-muted">Linux, if you sign in with a key</span><Textarea name="key" rows={4} className="mt-1" /></label> : null}
          <p className="text-xs text-muted">Leave these blank when you are only changing the name or host, and the saved sign-in stays.</p>
          {error ? <p className="text-sm text-danger">{error}</p> : null}
          <Button type="submit">Save sign-in</Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function DirectionScreen() {
  const direction = useQuery({ queryKey: ["direction"], queryFn: () => api<{ text: string }>("/api/direction") });
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  return (
    <Page kicker="Direction" title="What every bot is told" lede="This note is included in every request. Saving it does not change stored chats.">
      <Textarea key={direction.data?.text || ""} id="direction-text" rows={16} defaultValue={direction.data?.text || ""} />
      <Button onClick={() => {
        const text = (document.getElementById("direction-text") as HTMLTextAreaElement | null)?.value || "";
        void api("/api/direction", { method: "PUT", json: { text } }).then(() => setNote("Saved. Chats were not changed.")).catch((reason: Error) => setError(reason.message));
      }}>Save direction</Button>
      {note ? <p className="text-sm">{note}</p> : null}
      {error ? <p className="text-sm text-danger">{error}</p> : null}
    </Page>
  );
}

export function AboutScreen() {
  const theme = useApp((state) => state.theme);
  const health = useQuery({ queryKey: ["health"], queryFn: () => api<{ version?: string }>("/api/health") });
  const [login, setLogin] = useState(false);
  const [desktop, setDesktop] = useState(false);
  const [note, setNote] = useState("");
  const version = health.data?.version;
  useEffect(() => {
    const tauri = (window as Window & { __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: object) => Promise<unknown> } }).__TAURI_INTERNALS__;
    if (!tauri?.invoke) return;
    setDesktop(true);
    void tauri.invoke("open_at_login_enabled").then((enabled) => setLogin(Boolean(enabled))).catch(() => undefined);
  }, []);
  return (
    <div className="flex h-full flex-col items-start overflow-y-auto px-6 py-8">
      <img src="/static/mascot.svg" alt="" width={168} height={168} />
      <h1 className="mt-3 text-3xl font-semibold">EasyAgent</h1>
      <p className="tagline mt-1 text-lg">AI agents, made easy.</p>
      <p className="mt-3 max-w-lg text-sm text-muted">{version ? `Version ${version}. ` : ""}A local harness for any OpenAI-compatible model. Chats stay on this computer. EasyAgent does not send telemetry.</p>
      <p className="mt-3 max-w-lg text-sm text-muted">The earlier page is still here at /classic. Set EASYAGENT_UI=classic to make that the page at /.</p>
      {desktop ? (
        <label className="mt-4 flex items-center gap-3 text-sm">
          <Switch checked={login} onCheckedChange={(on) => {
            const tauri = (window as Window & { __TAURI_INTERNALS__?: { invoke?: (cmd: string, args?: object) => Promise<unknown> } }).__TAURI_INTERNALS__;
            setLogin(on);
            void tauri?.invoke?.("set_open_at_login", { enabled: on }).catch((reason: Error) => setNote(reason.message));
          }} />
          Open EasyAgent when I sign in
        </label>
      ) : <p className="mt-4 text-sm text-muted">Open at login is a setting in the desktop app. This browser tab stays as it is.</p>}
      {note ? <p className="mt-2 text-sm text-danger">{note}</p> : null}
      <p className="mt-4 text-xs text-muted">Theme is {theme}. Ctrl or Cmd K switches bots. Ctrl or Cmd N starts a new chat.</p>
    </div>
  );
}

export function TokenGate() {
  const needed = useApp((state) => state.tokenNeeded);
  const setNeeded = useApp((state) => state.setTokenNeeded);
  const [error, setError] = useState("");
  if (!needed) return null;
  return (
    <div className="safe-overlay fixed inset-0 z-50 flex items-center justify-center bg-background p-4">
      <form className="w-full max-w-sm space-y-3 rounded-lg border border-border bg-card p-5" onSubmit={(event) => {
        event.preventDefault();
        const token = String(new FormData(event.currentTarget).get("token") || "");
        saveToken(token);
        setNeeded(false);
        void queryClient.invalidateQueries().catch((reason: Error) => setError(reason.message));
      }}>
        <img src="/static/mascot.svg" alt="" width={96} height={96} />
        <p className="tagline">AI agents, made easy.</p>
        <p className="text-xs uppercase tracking-wide text-muted">Shared token</p>
        <h1 className="text-xl font-semibold">Type the shared token.</h1>
        <p className="text-sm text-muted">Chats stay on the computer running EasyAgent. This phone does not keep a copy.</p>
        <label className="block text-sm">Token <Input name="token" type="password" autoComplete="off" required className="mt-1" /></label>
        {error ? <p className="text-sm text-danger">{error}</p> : null}
        <Button type="submit">Open</Button>
      </form>
    </div>
  );
}
