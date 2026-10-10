const $ = (id) => document.getElementById(id);

// One stream per chat, keyed by bot id and chat id. Each entry has its own
// AbortController, live buffer, and run id. Switching bots or chats does not
// abort these, and an event is painted only when that chat is the open view.
const streams = new Map();

const state = {
  endpoints: [],
  bots: [],
  chats: [],
  rooms: [],
  botId: null,
  chatId: null,
  chat: null,
  roomId: null,
  room: null,
  view: null,
  nav: 0,
  drafts: {},
  sending: false,
  loadingChats: false,
  scheduleTimer: null,
  connectionId: null,
  computerId: null,
  computers: [],
  skills: [],
  live: null,
  roomLive: null,
  screen: "chat",
  groupProjects: [],
  groupProjectId: null,
  groupProject: null,
  botProjects: [],
  botProjectId: null,
  memoryTopic: null,
  memoryBotId: null,
  busyBots: new Set(),
  pendingFace: null,
};

function streamKey(botId, chatId) {
  return `${botId || ""}:${chatId || ""}`;
}

function streamFor(botId, chatId) {
  return streams.get(streamKey(botId, chatId)) || null;
}

function isVisible(stream) {
  return Boolean(
    stream && state.view === "bot" && state.botId === stream.botId && state.chatId === stream.chatId
  );
}

function visibleStream() {
  if (state.view !== "bot" || !state.botId || !state.chatId) return null;
  return streamFor(state.botId, state.chatId);
}

function eventMatches(event, stream) {
  if (!event || !stream) return false;
  if (event.bot_id && event.bot_id !== stream.botId) return false;
  if (event.chat_id && event.chat_id !== stream.chatId) return false;
  const runId = event.run_id || (event.run && event.run.id) || "";
  if (runId) {
    if (!stream.runId) stream.runId = runId;
    else if (runId !== stream.runId) return false;
  }
  return true;
}

function draftKey() {
  if (!state.botId) return null;
  return state.chatId || `new:${state.botId}`;
}

function stashDraft() {
  const key = draftKey();
  if (!key) return;
  state.drafts[key] = $("draft").value;
}

function restoreDraft() {
  const key = draftKey();
  $("draft").value = key ? state.drafts[key] || "" : "";
}

function sharedToken() {
  try {
    return sessionStorage.getItem("easyagent.token") || "";
  } catch {
    return "";
  }
}

function captureTokenFromUrl() {
  const url = new URL(location.href);
  const token = url.searchParams.get("token");
  if (!token) return;
  try {
    sessionStorage.setItem("easyagent.token", token);
  } catch {
    /* the form can still take it */
  }
  url.searchParams.delete("token");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  const token = sharedToken();
  if (token) headers["X-EasyAgent-Token"] = token;
  const response = await fetch(path, {
    ...options,
    headers,
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (response.ok) showOffline(false);
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : response.statusText;
    const error = new Error(message || "Request failed");
    error.status = response.status;
    error.offline = response.status === 503 && Boolean(data && data.offline);
    if (error.offline) showOffline(true, message);
    throw error;
  }
  return data;
}

function showOffline(on, message) {
  const node = $("offline-banner");
  if (!node) return;
  if (message) node.textContent = message;
  show(node, on);
}

function mascotImg(className) {
  return el("img", {
    class: className || "mascot",
    src: "/static/mascot.svg",
    alt: "",
    width: "168",
    height: "168",
  });
}

function offlineStage(message) {
  showOffline(true, message);
  $("empty-stage").replaceChildren(
    mascotImg(),
    el("p", { class: "eyebrow" }, ["Offline"]),
    el("h1", {}, ["The computer at home is not connected."]),
    el("p", { class: "tagline" }, ["AI agents, made easy."]),
    el("p", { class: "lede" }, [message || "Chats stay on that computer. Reload when it is back."]),
  );
}

function formatCount(value) {
  const number = Number(value) || 0;
  return number.toLocaleString("en-US");
}

// What the reply really sees: how many saved messages are sent in full,
// how many were folded into a summary, and the token budget they fit in.
function contextNote(ctx, where) {
  const total = ctx.transcript_messages || 0;
  const chars = ctx.transcript_chars || 0;
  const shown = ctx.model_messages || 0;
  const folded = ctx.compacted_messages || 0;
  const used = ctx.context_tokens || 0;
  const limit = ctx.max_context_tokens || 0;
  const saved = `${total} message${total === 1 ? "" : "s"} saved (${formatCount(chars)} characters, about ${formatCount(ctx.transcript_tokens || 0)} tokens).`;
  if (!total) return limit ? `No messages yet. Each reply can use up to ${formatCount(limit)} tokens of this ${where}.` : "";
  const budget = limit ? ` (about ${formatCount(used)} of ${formatCount(limit)} tokens)` : "";
  if (!folded) return `${saved} The next reply sees all of them in full${budget}. Nothing compacted.`;
  return `${saved} The next reply sees the newest ${shown} in full and a summary of the ${folded} older ones${budget}.`;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(node, on) {
  node.hidden = !on;
}

function localWhen(value) {
  const raw = String(value || "").trim();
  if (!raw) return "undated";
  const parsed = Date.parse(raw);
  if (Number.isNaN(parsed)) return raw;
  return new Date(parsed).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formError(id, message) {
  const node = $(id);
  node.textContent = message || "";
  show(node, Boolean(message));
}

function rememberSelection() {
  try {
    localStorage.setItem("easyagent.view", state.view || "");
    if (state.botId) localStorage.setItem("easyagent.bot", state.botId);
    else localStorage.removeItem("easyagent.bot");
    if (state.chatId) localStorage.setItem("easyagent.chat", state.chatId);
    else localStorage.removeItem("easyagent.chat");
    if (state.roomId) localStorage.setItem("easyagent.room", state.roomId);
    else localStorage.removeItem("easyagent.room");
  } catch {
    /* storage is optional */
  }
}

function savedSelection() {
  try {
    return {
      view: localStorage.getItem("easyagent.view"),
      botId: localStorage.getItem("easyagent.bot"),
      chatId: localStorage.getItem("easyagent.chat"),
      roomId: localStorage.getItem("easyagent.room"),
    };
  } catch {
    return { view: null, botId: null, chatId: null, roomId: null };
  }
}

function renderEndpoints() {
  const list = $("endpoint-list");
  list.replaceChildren();
  show($("endpoint-empty"), state.endpoints.length === 0);
  for (const endpoint of state.endpoints) {
    const button = el("button", {
      type: "button",
      class: "entity",
      title: endpoint.base_url,
      onclick: () => openConnectionForm(endpoint),
    }, [
      el("strong", {}, [endpoint.name]),
      el("small", {}, [endpoint.base_url]),
      el("small", {}, [endpoint.has_api_key ? "Key saved" : "No key"]),
      el("small", {}, [endpoint.model ? endpoint.model : "No model name"]),
      el("small", {}, [Number(endpoint.max_parallel) > 1 ? `${Number(endpoint.max_parallel)} at a time` : "1 at a time"]),
    ]);
    const edit = el("button", {
      type: "button",
      class: "endpoint-remove",
      onclick: () => openConnectionForm(endpoint),
    }, ["Edit"]);
    const remove = el("button", {
      type: "button",
      class: "endpoint-remove",
      onclick: () => confirmRemoveEndpoint(endpoint),
    }, ["Remove"]);
    list.append(el("li", { class: "endpoint-row" }, [button, edit, remove]));
  }
  const botFormOpen = $("bot-form") && !$("bot-form").hidden;
  if (!botFormOpen) fillEndpointSelect($("bot-endpoint"), null, true);
  fillEndpointSelect($("settings-endpoint"), currentBot() && currentBot().endpoint_id, false);
}

let pendingComputer = null;

function syncSigninKind() {
  const linux = $("computer-kind").value !== "windows";
  show($("signin-key-label"), linux);
  const hint = $("signin-password-hint");
  if (hint) hint.textContent = linux ? "or a key" : "required";
}

function clearSignin() {
  for (const id of ["signin-user", "signin-password", "signin-key"]) {
    const node = $(id);
    if (node) node.value = "";
  }
}

function renderComputers() {
  const list = $("computer-list");
  if (!list) return;
  list.replaceChildren();
  show($("computer-empty"), state.computers.length === 0);
  for (const computer of state.computers) {
    const kind = computer.kind === "windows" ? "Windows" : "Linux";
    const button = el("button", {
      type: "button",
      class: "entity",
      onclick: () => openComputerForm(computer),
    }, [
      el("strong", {}, [computer.name]),
      el("small", {}, [`${kind} · ${computer.host}`]),
      el("small", {}, [computer.has_sign_in ? "Sign-in saved" : "No sign-in"]),
    ]);
    const edit = el("button", { type: "button", class: "endpoint-remove", onclick: () => openComputerForm(computer) }, ["Edit"]);
    const remove = el("button", { type: "button", class: "endpoint-remove", onclick: () => confirmRemoveComputer(computer) }, ["Remove"]);
    list.append(el("li", { class: "endpoint-row" }, [button, edit, remove]));
  }
}

function openComputerForm(computer) {
  const form = $("computer-form");
  show(form, true);
  formError("computer-error", "");
  if (!computer) {
    state.computerId = null;
    form.reset();
    $("computer-kind").value = "linux";
    syncSigninKind();
    $("computer-name").focus();
    return;
  }
  state.computerId = computer.id;
  $("computer-name").value = computer.name || "";
  $("computer-kind").value = computer.kind === "windows" ? "windows" : "linux";
  $("computer-host").value = computer.host || "";
  $("computer-port").value = computer.port || "";
  $("computer-name").focus();
}

function renderSkills() {
  const list = $("skill-list");
  if (!list) return;
  list.replaceChildren();
  show($("skill-empty"), state.skills.length === 0);
  for (const skill of state.skills) {
    list.append(el("li", {}, [
      el("div", { class: "entity" }, [
        el("strong", {}, [skill.name]),
        el("small", {}, [skill.description || "No description"]),
      ]),
    ]));
  }
}

async function refreshComputers() {
  state.computers = await api("/api/computers");
  renderComputers();
}

async function refreshSkills() {
  state.skills = await api("/api/skills");
  renderSkills();
}

function memoryLine(botId, row) {
  const text = el("p", {}, [row.text || ""]);
  const edit = el("button", {
    type: "button",
    class: "text-btn",
    onclick: () => {
      const input = el("input", { value: row.text || "", maxlength: "500" });
      text.replaceWith(input);
      input.focus();
      let saving = false;
      const save = async () => {
        if (saving) return;
        saving = true;
        formError("memory-error", "");
        try {
          await api(`/api/bots/${botId}/memory/${row.id}`, {
            method: "PATCH",
            body: JSON.stringify({ text: input.value }),
          });
          await refreshMemory(botId);
        } catch (error) {
          saving = false;
          formError("memory-error", error.message);
        }
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          save();
        }
      });
      input.addEventListener("blur", save);
    },
  }, ["Change"]);
  const drop = el("button", {
    type: "button",
    class: "text-btn",
    onclick: async () => {
      formError("memory-error", "");
      try {
        await api(`/api/bots/${botId}/memory/${row.id}`, { method: "DELETE" });
        await refreshMemory(botId);
      } catch (error) {
        formError("memory-error", error.message);
      }
    },
  }, ["Drop"]);
  const children = [text, edit, drop];
  if (row.also) children.splice(1, 0, el("small", { class: "mono" }, [`also ${row.also}`]));
  return el("li", { class: "memory-row" }, children);
}

async function openMemoryTopic(name) {
  state.memoryTopic = name;
  await refreshMemory(state.botId);
}

async function refreshMemory(botId) {
  const indexList = $("memory-index-list");
  if (!indexList || !botId) return;
  if (state.memoryBotId !== botId) {
    state.memoryTopic = null;
    state.memoryBotId = botId;
  }
  let index = { topics: [] };
  try {
    index = await api(`/api/bots/${botId}/memory/index`);
  } catch (error) {
    formError("memory-error", error.message);
    return;
  }
  const topics = index.topics || [];
  indexList.replaceChildren();
  show($("memory-empty"), topics.length === 0);
  for (const topic of topics) {
    const count = topic.count === 1 ? "1 line" : `${topic.count || 0} lines`;
    const selected = topic.name === state.memoryTopic;
    indexList.append(el("li", {}, [
      el("button", {
        type: "button",
        class: selected ? "entity is-selected" : "entity",
        onclick: () => openMemoryTopic(topic.name),
      }, [
        el("strong", {}, [topic.title || topic.name]),
        el("small", { class: "mono" }, [topic.name]),
        el("small", {}, [count]),
      ]),
    ]));
  }
  const open = Boolean(state.memoryTopic);
  show($("memory-topic"), open);
  const list = $("memory-list");
  if (!open || !list) return;
  let topic;
  try {
    topic = await api(`/api/bots/${botId}/memory/topics/${encodeURIComponent(state.memoryTopic)}`);
  } catch (error) {
    state.memoryTopic = null;
    show($("memory-topic"), false);
    formError("memory-error", error.message);
    return;
  }
  $("memory-topic-title").textContent = topic.title || topic.name;
  const rows = topic.lines || [];
  list.replaceChildren();
  show($("memory-topic-empty"), rows.length === 0);
  for (const row of rows) list.append(memoryLine(botId, row));
}

async function refreshWatches() {
  try {
    renderWatches(await api("/api/watches"));
  } catch {
    /* the notice state is separate from the chat */
  }
}

function renderWatches(rows) {
  const note = $("watch-note");
  if (!note) return;
  const armed = (rows || []).filter((item) => item.armed);
  if (!armed.length) {
    note.textContent = "Nothing is waiting for a notice.";
    return;
  }
  note.textContent = armed.map((item) => (
    item.kind === "job_failed" ? "Waiting for a failed job. One notice." : "Waiting for a new message. One notice."
  )).join(" ");
}

async function refreshProposals() {
  const list = $("proposal-list");
  if (!list) return;
  const rows = await api("/api/proposals");
  list.replaceChildren();
  show($("proposal-empty"), rows.length === 0);
  for (const row of rows) {
    const label = row.kind === "skill" ? `Skill ${row.name || ""}`.trim() : "Memory";
    list.append(el("li", {}, [
      el("div", { class: "entity" }, [
        el("strong", {}, [label]),
        el("small", {}, [row.text || ""]),
        el("small", {}, ["Not installed"]),
      ]),
    ]));
  }
}

function confirmRemoveComputer(computer) {
  openConfirm({
    kicker: "Remove computer",
    title: `Remove ${computer.name}?`,
    copy: "This deletes only this saved computer. Chats, bots, and other computers stay.",
    name: computer.name,
    submitLabel: "Remove computer",
    action: async () => {
      await api(`/api/computers/${computer.id}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm_name: computer.name }),
      });
      if (state.computerId === computer.id) state.computerId = null;
      await refreshComputers();
    },
  });
}

function openConnectionForm(endpoint) {
  const form = $("endpoint-form");
  show(form, true);
  formError("endpoint-error", "");
  $("endpoint-clear-key").checked = false;
  if (!endpoint) {
    state.connectionId = null;
    form.reset();
    $("endpoint-parallel").value = "1";
    $("endpoint-submit").textContent = "Save";
    show($("endpoint-clear-key-label"), false);
    $("endpoint-key").placeholder = "only if the server asks";
    $("endpoint-name").focus();
    return;
  }
  state.connectionId = endpoint.id;
  $("endpoint-name").value = endpoint.name || "";
  $("endpoint-url").value = endpoint.base_url || "";
  $("endpoint-model").value = endpoint.model || "";
  $("endpoint-parallel").value = String(endpoint.max_parallel || 1);
  $("endpoint-key").value = "";
  $("endpoint-key").placeholder = endpoint.has_api_key ? "Saved. Leave blank to keep it." : "only if the server asks";
  show($("endpoint-clear-key-label"), Boolean(endpoint.has_api_key));
  $("endpoint-submit").textContent = "Save";
  $("endpoint-name").focus();
}

function renderBots() {
  const list = $("bot-list");
  list.replaceChildren();
  show($("bot-empty"), state.bots.length === 0);
  for (const bot of state.bots) {
    const selected = state.view !== "room" && bot.id === state.botId;
    const kids = [
      makeFace(bot, { live: true, selected }),
      el("strong", {}, [bot.name]),
    ];
    list.append(el("li", {}, [
      el("button", {
        type: "button",
        class: `entity bot-btn${selected ? " is-selected" : ""}${botIsLive(bot.id) ? " is-live" : ""}`,
        "data-bot-id": bot.id,
        "aria-current": selected ? "true" : null,
        onclick: () => selectBot(bot.id),
      }, kids),
    ]));
  }
}

function openSettings() {
  if (!state.botId) return;
  closePhoneList();
  state.view = "bot";
  state.screen = "settings";
  renderBots();
  renderStage();
  refreshMemory(state.botId);
}

function backToChat() {
  if (!state.botId) {
    state.screen = "chat";
    renderStage();
    return;
  }
  closePhoneList();
  state.view = "bot";
  state.screen = "chat";
  renderBots();
  renderStage();
}

function openPlainScreen(screen) {
  closePhoneList();
  state.screen = screen;
  if (state.view === "room") state.view = state.botId ? "bot" : null;
  renderBots();
  renderRooms();
  renderStage();
}

function currentBot() {
  return state.bots.find((bot) => bot.id === state.botId) || null;
}

function modelLabel(bot) {
  if (bot.model) return bot.model;
  const endpoint = state.endpoints.find((item) => item.id === bot.endpoint_id);
  if (endpoint && endpoint.model) return endpoint.model;
  return "No model name";
}

function fillEndpointSelect(select, selectedId, allowNew) {
  if (!select) return;
  const previous = selectedId || select.value;
  select.replaceChildren();
  select.disabled = false;
  if (allowNew) {
    select.append(el("option", { value: "__new__" }, ["New connection…"]));
  }
  if (!allowNew && state.endpoints.length === 0) {
    select.append(el("option", { value: "" }, ["Add a connection first"]));
    select.disabled = true;
    return;
  }
  if (selectedId && !state.endpoints.some((endpoint) => endpoint.id === selectedId)) {
    const missing = el("option", { value: "" }, ["Connection missing — pick one"]);
    missing.selected = true;
    select.append(missing);
  }
  for (const endpoint of state.endpoints) {
    const option = el("option", { value: endpoint.id }, [`${endpoint.name} · ${endpoint.base_url}`]);
    if (endpoint.id === previous) option.selected = true;
    select.append(option);
  }
  if (allowNew && state.endpoints.length === 0) select.value = "__new__";
  syncNewConnection();
}

function syncNewConnection() {
  const box = $("bot-new-connection");
  const select = $("bot-endpoint");
  if (!box || !select) return;
  show(box, select.value === "__new__");
}

function foldName(value) {
  return String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
}

function namesMatch(typed, stored) {
  const right = foldName(stored);
  return right.length > 0 && foldName(typed) === right;
}

function renderChatList() {
  const list = $("chat-list");
  list.replaceChildren();
  show($("chat-empty"), state.chats.length === 0);
  for (const chat of state.chats) {
    const selected = chat.id === state.chatId;
    const count = chat.message_count === 1 ? "1 message" : `${chat.message_count} messages`;
    list.append(el("li", { class: "chat-row" }, [
      el("button", {
        type: "button",
        class: selected ? "chat-item is-selected" : "chat-item",
        "aria-current": selected ? "true" : null,
        onclick: () => openChat(chat.id),
      }, [
        el("strong", {}, [chat.title || "New chat"]),
        el("small", {}, [count]),
      ]),
      el("button", {
        type: "button",
        class: "text-btn",
        onclick: () => confirmDeleteChat(chat),
      }, ["Delete chat"]),
    ]));
  }
}

function renderStage() {
  syncBotLive();
  const screen = state.screen || "chat";
  const showRoom = screen === "rooms" && state.view === "room" && state.room;
  const bot = state.view === "room" ? null : currentBot();
  const onChat = screen === "chat";
  show($("screen-chat"), onChat);
  show($("screen-settings"), screen === "settings" && Boolean(bot));
  show($("screen-connections"), screen === "connections");
  show($("screen-rooms"), screen === "rooms");
  show($("screen-computers"), screen === "computers");
  show($("screen-projects"), screen === "projects");
  show($("project-picker"), screen === "projects" && !state.groupProjectId);
  show($("project-open"), screen === "projects" && Boolean(state.groupProjectId));
  show($("screen-direction"), screen === "direction");
  show($("screen-about"), screen === "about");
  show($("room-picker"), screen === "rooms" && !showRoom);
  show($("room-stage"), Boolean(showRoom));
  show($("side-chats"), Boolean(state.botId) && state.view !== "room");
  show($("chat-head"), onChat && Boolean(bot));
  for (const id of ["nav-connections", "nav-rooms", "nav-projects", "nav-computers", "show-direction", "nav-about"]) {
    const node = $(id);
    if (!node) continue;
    const on = node.dataset.screen === screen;
    node.classList.toggle("is-selected", on);
    if (on) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  }
  if (showRoom) renderRoom();
  if (screen === "projects") renderOpenGroupProject();
  show($("empty-stage"), onChat && !bot);
  if ($("empty-title")) $("empty-title").textContent = state.bots.length ? "Pick a bot." : "Add a bot.";
  if (!bot) {
    if (onChat) {
      show($("thread-empty"), false);
      show($("thread"), false);
    }
    return;
  }
  $("bot-title").textContent = bot.name;
  if (onChat) mountChatFace(bot);
  if ($("settings-heading")) $("settings-heading").textContent = bot.name;
  renderAskChoices(bot);
  const model = modelLabel(bot);
  const endpoint = bot.endpoint_name ? `${bot.endpoint_name} · ${bot.endpoint_base_url}` : "No connection — pick one. Chats are still here.";
  const contextTokens = bot.context_tokens || 24000;
  const chats = state.chats.length;
  $("bot-meta").textContent = `${endpoint}. ${model}. Sees up to ${formatCount(contextTokens)} tokens of each chat. ${chats} saved chat${chats === 1 ? "" : "s"}.`;
  if (state.pendingFace && state.pendingFace.botId !== bot.id) state.pendingFace = null;
  if (state.renderedBotId !== bot.id) {
    fillEndpointSelect($("settings-endpoint"), bot.endpoint_id, false);
    $("settings-name").value = bot.name || "";
    $("settings-model").value = bot.model || "";
    $("settings-context").value = String(contextTokens);
    if ($("settings-check")) $("settings-check").checked = bot.check_enabled !== false;
    state.renderedBotId = bot.id;
  }
  paintFaceSwatches();
  if (screen === "settings" && bot && state.learnFor !== bot.id) {
    state.learnFor = bot.id;
    refreshLearning(bot.id);
  }
  if (screen !== "settings") state.learnFor = null;
  renderChatList();
  const chat = state.chat;
  const wrongBot = chat && chat.bot_id && chat.bot_id !== bot.id;
  if (!chat || wrongBot) {
    // Hide the previous bot's transcript. This only clears the screen, not the files.
    $("messages").replaceChildren();
    $("messages").dataset.botId = bot.id;
    $("summary-text").textContent = "";
    if (state.loadingChats) {
      $("thread-empty-title").textContent = `Opening ${bot.name}`;
      $("thread-empty-copy").textContent = "Loading this bot's saved chats. Nothing is deleted.";
    } else if (state.chats.length === 0) {
      $("thread-empty-title").textContent = `${bot.name} has no chats yet`;
      $("thread-empty-copy").textContent = "A new chat is stored for this bot only. Other bots keep theirs.";
    } else {
      $("thread-empty-title").textContent = "No chat open";
      $("thread-empty-copy").textContent = "Choose one of this bot's saved chats. They are still on disk.";
    }
    show($("thread-empty"), true);
    show($("thread"), false);
    return;
  }
  show($("thread-empty"), false);
  show($("thread"), true);
  renderThread();
}

function setProse(node, text) {
  node.replaceChildren();
  const source = String(text || "");
  const pattern = /```[^\n`]*\n([\s\S]*?)```|`([^`\n]+)`/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(source))) {
    if (match.index > last) node.append(document.createTextNode(source.slice(last, match.index)));
    const code = match[1] !== undefined ? match[1] : match[2];
    const span = el("code", {}, [code]);
    if (match[0].startsWith("```")) node.append(el("pre", { class: "prose-code" }, [span]));
    else node.append(span);
    last = match.index + match[0].length;
  }
  if (last < source.length) node.append(document.createTextNode(source.slice(last)));
  if (!node.childNodes.length) node.append(document.createTextNode(""));
}

function proseNode(text, attrs) {
  const node = el("div", attrs, []);
  setProse(node, text);
  return node;
}

function fillThinking(node, text) {
  node.replaceChildren();
  const parts = String(text || "").split(/\n{2,}/);
  for (const part of parts) {
    const trimmed = part.replace(/^\s+|\s+$/g, "");
    if (!trimmed) continue;
    const para = el("p", { class: "model-thinking-p" });
    para.textContent = trimmed;
    node.append(para);
  }
}

function thinkingBlock(text, options = {}) {
  if (!(text || "").trim()) return null;
  const body = el("div", { class: "model-thinking-body" });
  if (options.live) body.id = "live-thinking-body";
  fillThinking(body, text);
  if (!body.childNodes.length) return null;
  const details = el("details", { class: "model-thinking" }, [
    el("summary", {}, ["Thinking"]),
    body,
  ]);
  if (options.open) details.open = true;
  return details;
}

function formatElapsed(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  if (minutes <= 0) return `${rest}s`;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function currentRun() {
  if (state.live && state.live.run && state.live.run.status) return state.live.run;
  return (state.chat && state.chat.run) || null;
}

function runTone(step, status) {
  if (status === "stopped" || status === "error") return "halted";
  const text = String(step || "");
  if (text.startsWith("Model not answering")) return "reconnecting";
  if (text.startsWith("Queued:") || text.startsWith("Waiting") || text.startsWith("still waiting")) return "waiting";
  if (
    text.startsWith("Running") ||
    text === "Looking" ||
    text === "Remembering" ||
    text === "Connecting" ||
    text === "Searching" ||
    text.startsWith("Searching")
  ) return "tool";
  return "thinking";
}

function botIsLive(botId) {
  if (!botId) return false;
  for (const stream of streams.values()) {
    if (stream.botId !== botId || stream.replaced) continue;
    if (stream.sending) return true;
    const status = stream.live && stream.live.run && stream.live.run.status;
    if (status === "running") return true;
  }
  if (state.botId === botId) {
    const run = (state.chat && state.chat.run) || (state.live && state.live.run);
    if (run && run.status === "running") return true;
  }
  return Boolean(state.busyBots && state.busyBots.has(botId));
}

const FACE_PALETTE = ["#c4532a", "#1f8a4c", "#c43b7a", "#b86e12", "#0e7c86", "#3d6b4f", "#a34b2e", "#3a4f8a", "#6b4a2a", "#7a4e8a", "#2f6f5e", "#9a3d62"];
const FACE_FRAMES = ["eyes-mid", "eyes-left", "eyes-right", "eyes-up", "eyes-squint", "eyes-x", "mouth-smile", "mouth-flat", "mouth-open"];
let faceTemplate = null;
let faceSmallTemplate = null;

function faceMarkupOk(svg) {
  return FACE_FRAMES.every((name) => svg.querySelector("." + name));
}

async function fetchFace(url) {
  try {
    const response = await fetch(url);
    if (!response.ok) return null;
    const doc = new DOMParser().parseFromString(await response.text(), "image/svg+xml");
    const root = doc.documentElement;
    if (root && root.localName === "svg" && faceMarkupOk(root)) return root;
  } catch {
    return null;
  }
  return null;
}

async function loadFaceTemplate() {
  faceTemplate = await fetchFace("/static/face.svg?v=2");
  faceSmallTemplate = await fetchFace("/static/face-small.svg?v=1");
}

function applyFaceState(node, name) {
  node.classList.toggle("bot-live", name !== "idle" && name !== "halted");
  if (node.dataset.faceState === name) return;
  node.dataset.faceState = name;
  node.classList.remove("is-idle", "is-waiting", "is-reconnecting", "is-thinking", "is-tool", "is-talking", "is-halted");
  node.classList.add(`is-${name}`);
}

function faceStateFor(botId) {
  if (!botId) return "idle";
  let stream = null;
  for (const item of streams.values()) {
    if (item.botId !== botId || item.replaced) continue;
    const status = item.live && item.live.run && item.live.run.status;
    if (item.sending || status === "running") stream = item;
  }
  if (stream) {
    const live = stream.live || {};
    const run = live.run || {};
    const status = run.status || (stream.sending ? "running" : "");
    if (live.phase === "stopped" || live.phase === "error" || status === "stopped" || status === "error") return "halted";
    const tone = runTone(live.label || run.current_step || "", status);
    if (tone === "reconnecting") return "reconnecting";
    if ((live.text || "").length) return "talking";
    return tone;
  }
  if (state.botId === botId) {
    const run = (state.live && state.live.run) || (state.chat && state.chat.run);
    const live = state.live || {};
    if (live.phase === "stopped" || live.phase === "error") return "halted";
    if (run && run.status === "running") {
      const tone = runTone(live.label || run.current_step || "", run.status);
      if (tone === "reconnecting") return "reconnecting";
      if ((live.text || "").length) return "talking";
      return tone;
    }
  }
  if (state.busyBots && state.busyBots.has(botId)) return "thinking";
  return "idle";
}

function makeFace(bot, options = {}) {
  const wrap = document.createElement("span");
  wrap.className = "buddy-face";
  if (options.large) wrap.classList.add("is-large");
  if (options.tiny) wrap.classList.add("is-tiny");
  if (options.selected) wrap.classList.add("is-selected");
  wrap.style.setProperty("--face", (bot && bot.face_color) || "#5c4d9a");
  if (bot && bot.id) wrap.dataset.faceBot = bot.id;
  const halted = Boolean(options.halted);
  const live = Boolean(options.live) && !halted;
  if (live) wrap.dataset.faceLive = "1";
  applyFaceState(wrap, halted ? "halted" : live ? faceStateFor(bot && bot.id) : "idle");
  const template = options.large ? faceTemplate : (faceSmallTemplate || faceTemplate);
  if (template) wrap.append(document.importNode(template, true));
  wrap.setAttribute("aria-hidden", "true");
  return wrap;
}

function syncBotFaces() {
  document.querySelectorAll("[data-face-live]").forEach((node) => {
    const bot = state.bots.find((item) => item.id === node.dataset.faceBot);
    if (bot && bot.face_color) node.style.setProperty("--face", bot.face_color);
    applyFaceState(node, faceStateFor(node.dataset.faceBot));
  });
}

function syncBotLive() {
  const buttons = document.querySelectorAll("#bot-list .bot-btn");
  buttons.forEach((button) => {
    button.classList.toggle("is-live", botIsLive(button.dataset.botId));
  });
  syncBotFaces();
}

function whoLine(label, bot, options) {
  const kids = [];
  if (bot) kids.push(makeFace(bot, options || {}));
  kids.push(document.createTextNode(label));
  return el("p", { class: "who" }, kids);
}

function checkBadge(message) {
  if (!message || message.role === "user") return null;
  if (message.check === "revised") return el("span", { class: "check-badge" }, ["revised after check"]);
  if (message.check === "checked") return el("span", { class: "check-badge" }, ["checked"]);
  return null;
}

function learnNote(message) {
  const text = String((message && message.lesson) || "");
  if (!text.startsWith("Learned:")) return null;
  return el("p", { class: "learn-note" }, [text]);
}

function mountChatFace(bot) {
  const host = $("chat-face");
  if (!host || !bot) return;
  const face = host.querySelector("[data-face-live]");
  if (face && face.dataset.faceBot === bot.id) {
    face.style.setProperty("--face", bot.face_color || "#5c4d9a");
    face.classList.add("is-selected");
    return;
  }
  host.replaceChildren(makeFace(bot, { live: true, selected: true, large: true }));
}

function paintFaceSwatches() {
  const bot = currentBot();
  const host = $("face-swatches");
  if (!host || !bot) return;
  const pending = state.pendingFace && state.pendingFace.botId === bot.id ? state.pendingFace.color : "";
  const current = (pending || bot.face_color || "").toLowerCase();
  host.replaceChildren();
  for (const color of FACE_PALETTE) {
    const picked = color === current;
    host.append(el("button", {
      type: "button",
      class: picked ? "swatch is-selected" : "swatch",
      title: color,
      "aria-label": `Face color ${color}`,
      "aria-pressed": picked ? "true" : "false",
      onclick: () => {
        state.pendingFace = { botId: bot.id, color };
        paintFaceSwatches();
      },
    }, [makeFace({ id: bot.id, face_color: color }, { selected: picked })]));
  }
}

function describeRun(run, now) {
  const step = (state.live && state.live.label) || (run && run.current_step) || "Thinking";
  const started = Date.parse((run && run.started_at) || "");
  const elapsed = Number.isNaN(started) ? "" : formatElapsed((now - started) / 1000);
  const heard = state.live && state.live.heardAt;
  const server = Date.parse((run && run.last_activity_at) || "");
  const last = Math.max(heard || 0, Number.isNaN(server) ? 0 : server);
  let words = step;
  if (
    run &&
    run.status === "running" &&
    last &&
    !String(step).startsWith("Queued:") &&
    !String(step).startsWith("Model not answering")
  ) {
    const quiet = Math.floor((now - last) / 1000);
    if (quiet >= 60) words = `still waiting on the model (${quiet}s)`;
  }
  return { words, elapsed };
}

function liveFromRun(run, chat) {
  return {
    phase: "thinking",
    label: (run && run.current_step) || "Waiting on model",
    text: "",
    reasoning: "",
    run,
    heardAt: Date.parse((run && run.last_activity_at) || "") || Date.now(),
    reattached: Boolean(chat),
  };
}

let runClock = 0;
let runPoll = 0;

function stopRunWatch() {
  if (runClock) {
    clearInterval(runClock);
    runClock = 0;
  }
  if (runPoll) {
    clearInterval(runPoll);
    runPoll = 0;
  }
}

function paintRun(run) {
  const view = describeRun(run, Date.now());
  const step = $("run-step");
  const elapsed = $("run-elapsed");
  const row = $("run-indicator");
  if (step) step.textContent = view.words;
  if (elapsed) elapsed.textContent = view.elapsed;
  if (row && run) {
    const tone = runTone(view.words, run.status);
    row.classList.remove("is-waiting", "is-reconnecting", "is-thinking", "is-tool", "is-halted");
    row.classList.add(`is-${tone}`);
  }
  syncBotLive();
}

function startRunWatch() {
  const run = currentRun();
  if (!run || run.status !== "running") {
    stopRunWatch();
    return;
  }
  if (!runClock) {
    runClock = setInterval(() => {
      const active = currentRun();
      if (!active || active.status !== "running") {
        stopRunWatch();
        return;
      }
      if (!$("run-step")) {
        renderThread();
        return;
      }
      paintRun(active);
    }, 1000);
  }
  if (!runPoll) {
    runPoll = setInterval(async () => {
      const active = currentRun();
      if (!active || active.status !== "running" || state.sending || !state.botId || !state.chatId) return;
      if (state.view !== "bot") return;
      const botId = state.botId;
      const chatId = state.chatId;
      try {
        const chat = await api(`/api/bots/${botId}/chats/${chatId}`);
        if (state.botId !== botId || state.chatId !== chatId || state.sending) return;
        state.chat = chat;
        if (!chat.run || chat.run.status !== "running") {
          const stream = streamFor(botId, chatId);
          if (!stream || !stream.sending) state.live = null;
          renderThread();
          return;
        }
        const stream = streamFor(botId, chatId);
        if (stream && stream.sending && stream.live) {
          stream.live.run = chat.run;
          state.live = stream.live;
        } else if (!state.live) {
          state.live = liveFromRun(chat.run, chat);
        } else {
          state.live.run = chat.run;
        }
        renderThread();
      } catch {
        /* the indicator already on screen stays */
      }
    }, 2000);
  }
}

function replyBubble(who, text, reasoning, options = {}) {
  const main = el("div", { class: "message-main" }, []);
  const thought = thinkingBlock(reasoning, { open: true, live: Boolean(options.live) });
  if (thought) main.append(thought);
  if (text || options.forceBody) {
    const body = proseNode(text || "", { class: "body" });
    if (options.live) body.id = "live-reply-body";
    main.append(body);
  }
  return el("li", { class: options.className || "message assistant", id: options.id || null }, [
    whoLine(who, options.bot || null, { live: Boolean(options.live), halted: Boolean(options.halted) }),
    main,
  ]);
}

function renderLive(messages, bot) {
  const live = state.live;
  if (!live || live.phase === "error" || live.phase === "stopped") return;
  const reasoning = live.reasoning || "";
  const who = bot ? bot.name : "Reply";
  const showReply = live.phase === "reply" || Boolean(live.text) || Boolean((reasoning || "").trim());
  if (showReply) {
    messages.append(replyBubble(who, live.text || "", reasoning, {
      live: true,
      bot,
      forceBody: live.phase === "reply" || Boolean(live.text),
      id: "live-reply",
    }));
  }
  const statusLabel = live.label || "Thinking";
  const modelThought = Boolean((reasoning || "").trim()) && statusLabel === "Thinking";
  const running = currentRun() && currentRun().status === "running";
  if (live.phase === "thinking" && !modelThought && !running) {
    messages.append(el("li", { class: "message pending", id: "thinking-row" }, [
      el("p", { class: "who" }, [statusLabel]),
      el("p", { class: "body" }, ["…"]),
    ]));
  }
}

function renderRunIndicator(messages, bot) {
  const run = currentRun();
  if (!run || run.status !== "running") return;
  if (state.live && (state.live.phase === "stopped" || state.live.phase === "error")) return;
  const view = describeRun(run, Date.now());
  const tone = runTone(view.words, run.status);
  const who = bot ? bot.name : "Working";
  messages.append(el("li", {
    class: `message pending run-indicator is-${tone}`,
    id: "run-indicator",
    role: "status",
    "aria-live": "polite",
  }, [
    whoLine(who, bot, { live: true }),
    el("div", { class: "message-main run-main" }, [
      el("span", { class: "run-pulse", "aria-hidden": "true" }),
      el("p", { class: "body run-step" }, [
        el("span", { class: "run-step-text", id: "run-step" }, [view.words]),
        el("span", { class: "run-dots", "aria-hidden": "true" }),
      ]),
      el("p", { class: "run-elapsed", id: "run-elapsed" }, [view.elapsed]),
    ]),
  ]));
}

function renderStopped(messages) {
  if (state.sending && state.live && state.live.phase !== "stopped" && state.live.phase !== "error") return;
  const run = state.chat && state.chat.run;
  const liveStopped = state.live && (state.live.phase === "stopped" || state.live.phase === "error");
  if (!liveStopped && (!run || (run.status !== "stopped" && run.status !== "error"))) return;
  const last = ((state.chat && state.chat.messages) || []).slice(-1)[0];
  const already = last && String(last.content || "").trim().startsWith("Stopped");
  if (already && !liveStopped) return;
  const reason = (liveStopped && state.live.text) || (run && run.reason) || "the reply stopped before it finished.";
  const line = String(reason).startsWith("Stopped") ? reason : `Stopped: ${reason}`;
  if (already && String(last.content || "").trim() === line) return;
  const who = currentBot() ? currentBot().name : "Stopped";
  messages.append(el("li", {
    class: "message pending run-indicator is-halted",
    id: "stopped-row",
    role: "status",
  }, [
    whoLine(who, currentBot(), { halted: true }),
    el("div", { class: "message-main run-main" }, [
      el("span", { class: "run-pulse", "aria-hidden": "true" }),
      el("p", { class: "body run-step" }, [
        el("span", { class: "run-step-text" }, [line]),
      ]),
    ]),
  ]));
}

function resumeState() {
  const none = { retry: false, cont: false };
  if (state.sending && !(state.live && (state.live.phase === "stopped" || state.live.phase === "error"))) return none;
  if (state.live && state.live.phase !== "stopped" && state.live.phase !== "error") return none;
  const run = (state.chat && state.chat.run) || {};
  const last = ((state.chat && state.chat.messages) || []).slice(-1)[0];
  const liveStopped = Boolean(state.live && (state.live.phase === "stopped" || state.live.phase === "error"));
  const text = last ? String(last.content || "").trim() : "";
  const stoppedMsg = Boolean(last && last.role === "assistant" && (
    last.error || text === "Stopped." || text.startsWith("Stopped:")
  ));
  const waitingUser = Boolean(last && last.role === "user" && !state.live);
  const runBad = run.status === "stopped" || run.status === "error";
  return {
    retry: Boolean(liveStopped || stoppedMsg || waitingUser || runBad),
    cont: Boolean(liveStopped || stoppedMsg || runBad),
  };
}

const REACTIONS = ["👍", "👎", "❤️", "👀"];

function reactionControl(message, room) {
  const slot = el("div", { class: "react" });
  if (message.reaction) {
    slot.append(el("button", {
      type: "button",
      class: "reaction",
      title: "Remove this reaction",
      onclick: () => toggleReaction(message, message.reaction, room),
    }, [message.reaction]));
  }
  const menu = el("details", { class: "react-menu" });
  menu.append(el("summary", {}, ["React"]));
  const picks = el("span", { class: "react-picks" });
  for (const emoji of REACTIONS) {
    picks.append(el("button", {
      type: "button",
      class: "reaction-pick",
      onclick: () => toggleReaction(message, emoji, room),
    }, [emoji]));
  }
  menu.append(picks);
  slot.append(menu);
  return slot;
}

async function toggleReaction(message, emoji, room) {
  if (!message || !message.id || state.sending) return;
  const path = room
    ? `/api/rooms/${state.roomId}/messages/${message.id}/reaction`
    : `/api/bots/${state.botId}/chats/${state.chatId}/messages/${message.id}/reaction`;
  try {
    const data = await api(path, { method: "POST", body: JSON.stringify({ emoji }) });
    if (room) {
      if (state.view !== "room" || !state.room || state.room.id !== data.id) return;
      state.room = data;
      renderRoom();
      return;
    }
    if (state.view !== "bot" || !state.chat || state.chat.id !== data.id) return;
    state.chat = data;
    renderThread();
  } catch (error) {
    formError(room ? "room-error" : "thread-error", error.message);
  }
}

function choiceRow(choices) {
  const row = el("div", { class: "choices" });
  for (const choice of choices) {
    row.append(el("button", {
      type: "button",
      class: "choice",
      onclick: () => pickChoice(choice),
    }, [choice]));
  }
  return row;
}

function pickChoice(choice) {
  if (state.sending) return;
  if (state.view === "room") {
    $("room-draft").value = choice;
    sendRoom({ preventDefault() {} });
    return;
  }
  $("draft").value = choice;
  sendDraft({ preventDefault() {} });
}

function attachmentView(message, botId, chatId) {
  const att = message.attachment;
  if (!att || !att.id || !botId || !chatId) return null;
  const url = `/api/bots/${botId}/chats/${chatId}/files/${att.id}`;
  const made = Boolean(att.path);
  if ((att.media_type || "").startsWith("image/")) {
    const shot = el("img", { class: "shot", alt: att.name || "Picture", src: url });
    if (!made) return shot;
    return el("div", {}, [
      shot,
      el("a", { class: "file-link", href: url }, [att.path]),
    ]);
  }
  if (made && att.excerpt) {
    return el("div", {}, [
      el("pre", { class: "file-preview" }, [att.excerpt]),
      el("a", { class: "file-link", href: url }, [att.path]),
    ]);
  }
  return el("a", { class: "file-link", href: url }, [att.path || att.name || "File"]);
}

function scrollThread() {
  const scroller = $("thread-scroll");
  scroller.scrollTop = scroller.scrollHeight;
}

function syncStop() {
  const stop = $("stop");
  if (!stop) return;
  const stream = visibleStream();
  const run = state.view === "bot" ? currentRun() : null;
  const local = Boolean(stream && stream.control && stream.sending);
  const remote = Boolean(run && run.status === "running");
  stop.hidden = !(local || remote);
}

function renderThread() {
  syncBotLive();
  syncStop();
  const chat = state.chat;
  const bot = currentBot();
  if (!bot || (chat.bot_id && chat.bot_id !== bot.id)) {
    $("messages").replaceChildren();
    show($("thread"), false);
    show($("thread-empty"), true);
    return;
  }
  $("messages").dataset.botId = bot.id;
  const summary = (chat.summary || "").trim();
  $("summary-text").textContent = summary || "Nothing compacted yet. The recent tail still fits in the request.";
  const messages = $("messages");
  messages.replaceChildren();
  const transcript = chat.messages || [];
  transcript.forEach((message, index) => {
    const skills = message.skills_saved || [];
    const body = proseNode(message.content || "", { class: "body" });
    const failed = Boolean(message.error);
    const who = message.speaker_name || (failed ? "Error" : message.role === "user" ? "You" : (bot ? bot.name : "Reply"));
    const main = el("div", { class: "message-main" }, []);
    const thought = message.role === "assistant" && !failed ? thinkingBlock(message.thinking, { open: true }) : null;
    if (thought) main.append(thought);
    if ((message.content || "").length || !thought) main.append(body);
    if (skills.length) {
      body.append(el("span", { class: "skill-chip" }, [`Saved skill ${skills.join(", ")}`]));
    }
    const attached = attachmentView(message, bot.id, chat.id);
    if (attached) main.append(attached);
    const open = index === transcript.length - 1 && message.role === "assistant" && !failed
      && Array.isArray(message.choices) && message.choices.length > 1;
    if (open) main.append(choiceRow(message.choices));
    if (message.id) main.append(reactionControl(message, false));
    const speaker = message.role === "user" ? null : bot;
    const row = whoLine(who, speaker, { halted: failed });
    const badge = checkBadge(message);
    if (badge) row.append(badge);
    const learned = learnNote(message);
    if (learned) main.append(learned);
    const item = el("li", { class: `message ${message.role}${failed ? " error" : ""}` }, [
      row,
      main,
    ]);
    messages.append(item);
  });
  renderLive(messages, bot);
  renderRunIndicator(messages, bot);
  renderStopped(messages);
  $("context-note").textContent = contextNote(chat.context || {}, "chat");
  const resume = resumeState();
  show($("retry"), resume.retry);
  show($("continue"), resume.cont);
  startRunWatch();
  const lastMessage = transcript[transcript.length - 1];
  const asking = lastMessage && lastMessage.role === "assistant" && Array.isArray(lastMessage.choices) && lastMessage.choices.length > 1;
  $("draft").placeholder = asking
    ? "Or type an answer. Enter to send."
    : `Message ${currentBot() ? currentBot().name : ""}. Enter to send. Shift+Enter for a new line.`;
  const scroller = $("thread-scroll");
  scroller.scrollTop = scroller.scrollHeight;
}

async function refreshEndpoints() {
  state.endpoints = await api("/api/endpoints");
  renderEndpoints();
}

async function refreshBots() {
  state.bots = await api("/api/bots");
  renderBots();
}

async function refreshChats(botId) {
  state.chats = await api(`/api/bots/${botId}/chats`);
}

function closePhoneList() {
  $("rail").classList.remove("is-open");
  $("phone-menu").textContent = "Bots";
}

async function selectBot(botId, preferredChatId) {
  // Leave other chats' runs alone. This only clears the open view.
  state.live = null;
  state.sending = false;
  const ticket = ++state.nav;
  closePhoneList();
  state.view = "bot";
  state.screen = "chat";
  watchSchedules(false);
  const same = state.botId === botId;
  const previousChat = same ? state.chatId : null;
  if (!same) {
    stashDraft();
    // Drop the on-screen transcript only. Chat files are not written here.
    state.chat = null;
    state.chatId = null;
    state.chats = [];
    state.loadingChats = true;
  }
  state.botId = botId;
  refreshMemory(botId);
  refreshBotProjects(botId);
  refreshWatches();
  formError("thread-error", "");
  show($("settings-note"), false);
  if (!same) {
    renderBots();
    renderStage();
  }
  try {
    await refreshChats(botId);
  } catch (error) {
    if (ticket !== state.nav) return;
    state.loadingChats = false;
    state.chats = [];
    state.chat = null;
    state.chatId = null;
    renderBots();
    renderStage();
    $("bot-meta").textContent = error.message;
    return;
  }
  if (ticket !== state.nav) return;
  if (same && state.chat && !state.chatId) {
    renderBots();
    renderChatList();
    rememberSelection();
    return;
  }
  const stillThere = (id) => state.chats.some((chat) => chat.id === id);
  let next = null;
  if (preferredChatId && stillThere(preferredChatId)) next = preferredChatId;
  else if (previousChat && stillThere(previousChat)) next = previousChat;
  else if (state.chats[0]) next = state.chats[0].id;
  if (!next) state.loadingChats = false;
  renderBots();
  renderStage();
  rememberSelection();
  if (next) {
    try {
      await openChat(next);
    } catch (error) {
      state.chat = null;
      state.chatId = null;
      state.loadingChats = false;
      renderStage();
      $("bot-meta").textContent = error.message;
      return;
    }
    state.loadingChats = false;
    watchSchedules(true);
    return;
  }
  state.chatId = null;
  state.chat = null;
  renderStage();
  restoreDraft();
  watchSchedules(true);
}

function unreadTitle(total) {
  const n = Number(total) || 0;
  return n > 0 ? `(${n}) EasyAgent` : "EasyAgent";
}

function applyUnread(data) {
  document.title = unreadTitle(data && data.total);
  state.busyBots = new Set((data && data.busy) || []);
  syncBotLive();
}

async function refreshUnread() {
  try {
    applyUnread(await api("/api/unread"));
  } catch {
    /* leave the title; the transcript is separate */
  }
}

let unreadTimer = null;

function watchUnread() {
  if (unreadTimer) return;
  const tick = () => {
    if (document.hidden) return;
    refreshUnread();
  };
  tick();
  unreadTimer = setInterval(tick, 8000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) tick();
  });
}

async function markChatRead(botId, chatId, through) {
  if (!botId || !chatId) return;
  try {
    applyUnread(await api(`/api/bots/${botId}/chats/${chatId}/read`, {
      method: "POST",
      body: JSON.stringify(through == null ? {} : { through }),
    }));
  } catch {
    /* the transcript is already on screen */
  }
}

async function markRoomRead(roomId, through) {
  if (!roomId) return;
  try {
    applyUnread(await api(`/api/rooms/${roomId}/read`, {
      method: "POST",
      body: JSON.stringify(through == null ? {} : { through }),
    }));
  } catch {
    /* the room transcript is already on screen */
  }
}

let chatLoad = 0;

function adoptStream(botId, chat) {
  const stream = streamFor(botId, chat.id);
  if (stream && stream.sending && stream.live) {
    state.sending = true;
    state.live = stream.live;
    if (stream.live.run) chat.run = stream.live.run;
    if (stream.live.text || stream.live.reasoning) {
      chat.messages = (chat.messages || []).filter((item) => !item.live);
    }
    return;
  }
  state.sending = false;
  if (chat.run && chat.run.status === "running") {
    state.live = liveFromRun(chat.run, chat);
    return;
  }
  if (stream && stream.live && (stream.live.phase === "stopped" || stream.live.phase === "error")) {
    state.live = stream.live;
    return;
  }
  state.live = null;
}

async function openChat(chatId, errorMessage) {
  state.live = null;
  state.sending = false;
  if (!state.botId) return;
  state.view = "bot";
  state.screen = "chat";
  const botId = state.botId;
  const load = ++chatLoad;
  const draftAtStart = $("draft").value;
  stashDraft();
  const chat = await api(`/api/bots/${botId}/chats/${chatId}`);
  if (load !== chatLoad || state.botId !== botId || state.view !== "bot") return;
  // A click loads the chat. If the user types during that fetch, keep what they typed.
  const typedOver = $("draft").value !== draftAtStart;
  state.chatId = chat.id;
  state.chat = chat;
  adoptStream(botId, chat);
  formError("thread-error", errorMessage || "");
  rememberSelection();
  renderChatList();
  renderStage();
  if (typedOver) state.drafts[chat.id] = $("draft").value;
  else restoreDraft();
  await markChatRead(botId, chat.id, (chat.messages || []).length);
}

function startNewChat() {
  if (!state.botId) return;
  state.live = null;
  state.sending = false;
  state.view = "bot";
  state.screen = "chat";
  stashDraft();
  state.chatId = null;
  state.chat = {
    bot_id: state.botId,
    title: "New chat",
    summary: "",
    messages: [],
    context: {
      transcript_messages: 0,
      transcript_chars: 0,
      context_chars: 0,
      context_tokens: 0,
      max_context_tokens: (currentBot() && currentBot().context_tokens) || 24000,
    },
  };
  formError("thread-error", "");
  rememberSelection();
  renderChatList();
  renderStage();
  restoreDraft();
  $("draft").focus();
}

function renderAskChoices(bot) {
  const select = $("ask-bot");
  const button = $("ask-go");
  if (!select || !button) return;
  const previous = select.value;
  const others = state.bots.filter((item) => item.id !== bot.id);
  select.replaceChildren();
  if (others.length === 0) {
    select.append(el("option", { value: "" }, ["No other bot"]));
    select.disabled = true;
    button.disabled = true;
    return;
  }
  select.disabled = state.sending;
  button.disabled = state.sending;
  for (const other of others) {
    const option = el("option", { value: other.id }, [other.name]);
    if (other.id === previous) option.selected = true;
    select.append(option);
  }
}

async function askOtherBot() {
  if (state.sending || !state.botId) return;
  const botId = state.botId;
  const task = $("draft").value.trim();
  const childId = $("ask-bot").value;
  if (!childId) {
    formError("thread-error", "Pick another bot that already exists. No bot was created.");
    return;
  }
  if (!task) {
    formError("thread-error", "Write the one task in the message box, then ask.");
    return;
  }
  chatLoad += 1;
  state.sending = true;
  $("ask-go").disabled = true;
  $("send").disabled = true;
  formError("thread-error", "");
  let chatId = state.chatId;
  try {
    if (!chatId) {
      const created = await api(`/api/bots/${botId}/chats`, { method: "POST" });
      chatId = created.id;
      state.chatId = chatId;
    }
    const result = await api(`/api/bots/${botId}/chats/${chatId}/ask`, {
      method: "POST",
      body: JSON.stringify({ bot_id: childId, task }),
    });
    if (state.botId !== botId) return;
    delete state.drafts[`new:${botId}`];
    state.drafts[chatId] = "";
    $("draft").value = "";
    state.chat = result.chat;
    state.chatId = result.chat.id;
    await refreshChats(botId);
    if (state.botId !== botId) return;
    renderStage();
    rememberSelection();
    await markChatRead(botId, result.chat.id, (result.chat.messages || []).length);
  } catch (error) {
    if (state.botId !== botId) return;
    formError("thread-error", error.message);
  } finally {
    state.sending = false;
    $("send").disabled = false;
    if (currentBot()) renderAskChoices(currentBot());
  }
}

function showOutgoing(text, stream) {
  const now = new Date().toISOString();
  stream.live = {
    phase: "thinking",
    label: "Waiting on model",
    text: "",
    reasoning: "",
    heardAt: Date.now(),
    run: {
      id: stream.runId || "",
      status: "running",
      started_at: now,
      last_activity_at: now,
      current_step: "Waiting on model",
      reason: "",
    },
  };
  if (!isVisible(stream)) return;
  if (!state.chat || (state.chat.bot_id && state.chat.bot_id !== stream.botId)) {
    state.chat = { id: stream.chatId, bot_id: stream.botId, title: "New chat", messages: [], summary: "" };
  }
  state.chat.messages = [...(state.chat.messages || []), { role: "user", content: text }];
  state.live = stream.live;
  renderStage();
  scrollThread();
}

function noteActivity(stream) {
  const live = stream ? stream.live : state.live;
  if (!live) return;
  live.heardAt = Date.now();
}

function rememberLive(stream, live) {
  stream.live = live;
  if (isVisible(stream)) state.live = live;
}

function paintStream(stream, mode) {
  syncBotLive();
  if (!isVisible(stream)) return;
  state.live = stream.live;
  if (mode === "stage") renderStage();
  else if (mode === "run" && $("run-step")) paintRun((stream.live && stream.live.run) || { status: "running" });
  else renderThread();
  scrollThread();
}

function applyStreamEvent(event, stream) {
  if (!eventMatches(event, stream)) return;
  const live = stream.live || { phase: "thinking", label: "Waiting on model", text: "", reasoning: "" };
  if (event.type === "run" && event.run) {
    rememberLive(stream, {
      ...live,
      label: event.run.current_step || live.label || "Waiting on model",
      run: event.run,
      heardAt: Date.now(),
    });
    if (isVisible(stream) && state.chat) state.chat.run = event.run;
    paintStream(stream, $("run-step") ? "run" : "thread");
    return;
  }
  if (event.type === "stopped") {
    stream.live = null;
    stream.sending = false;
    if (event.chat) stream.savedChat = event.chat;
    if (!isVisible(stream)) {
      syncBotLive();
      return;
    }
    state.live = null;
    if (event.chat && event.chat.id === stream.chatId) {
      state.chat = event.chat;
      state.chatId = event.chat.id;
    }
    renderStage();
    scrollThread();
    return;
  }
  if (event.type === "status") {
    const run = event.run || live.run || null;
    if (run && event.text) run.current_step = event.text;
    rememberLive(stream, {
      phase: "thinking",
      label: event.text || "Thinking",
      text: live.text || "",
      reasoning: live.reasoning || "",
      run,
      heardAt: Date.now(),
    });
    paintStream(stream, $("run-step") ? "run" : "thread");
    return;
  }
  if (event.type === "replay") {
    const run = live.run || null;
    rememberLive(stream, {
      phase: "thinking",
      label: live.label || "Model not answering, retrying…",
      text: live.text || "",
      reasoning: event.text || "",
      run,
      heardAt: Date.now(),
    });
    paintStream(stream, $("run-step") ? "run" : "thread");
    return;
  }
  if (event.type === "replace") {
    rememberLive(stream, {
      phase: "thinking",
      label: "Searching",
      text: "",
      reasoning: live.reasoning || "",
      run: live.run,
      heardAt: Date.now(),
    });
    paintStream(stream, "thread");
    return;
  }
  if (event.type === "line") {
    rememberLive(stream, {
      phase: "thinking",
      label: live.label || "Thinking",
      text: (live.text || "") + (event.text || ""),
      reasoning: live.reasoning || "",
      run: live.run,
      heardAt: Date.now(),
    });
    paintStream(stream, "thread");
    return;
  }
  if (event.type === "thinking") {
    const reasoning = (live.reasoning || "") + (event.text || "");
    const label = !live.label || live.label === "Waiting on model" ? "Thinking" : live.label;
    rememberLive(stream, {
      phase: live.phase === "reply" ? "reply" : "thinking",
      label,
      text: live.text || "",
      reasoning,
      run: live.run,
      heardAt: Date.now(),
    });
    if (!isVisible(stream)) {
      syncBotLive();
      return;
    }
    const node = $("live-thinking-body");
    if (node) fillThinking(node, reasoning);
    else renderThread();
    if ($("run-step")) paintRun(currentRun() || { status: "running", current_step: label });
    scrollThread();
    syncBotLive();
    return;
  }
  if (event.type === "delta") {
    rememberLive(stream, {
      phase: "reply",
      text: (live.text || "") + (event.text || ""),
      reasoning: live.reasoning || "",
      run: live.run,
      heardAt: Date.now(),
    });
    if (!isVisible(stream)) {
      syncBotLive();
      return;
    }
    const body = $("live-reply-body");
    if (body) {
      $("thinking-row")?.remove();
      setProse(body, stream.live.text);
    } else {
      renderThread();
    }
    scrollThread();
    syncBotLive();
    return;
  }
  if (event.type === "error") {
    const detail = event.detail || "The reply failed.";
    stream.sending = false;
    stream.savedChat = event.chat || stream.savedChat;
    rememberLive(stream, { phase: "error", text: detail });
    if (!isVisible(stream)) {
      syncBotLive();
      return;
    }
    formError("thread-error", detail);
    if (event.chat && event.chat.id === stream.chatId) {
      state.live = null;
      stream.live = null;
      state.chat = event.chat;
      state.chatId = event.chat.id;
      renderStage();
    } else {
      state.live = stream.live;
      renderThread();
    }
    scrollThread();
    return;
  }
  if (event.type === "done" && event.chat) {
    stream.live = null;
    stream.sending = false;
    stream.savedChat = event.chat;
    if (!isVisible(stream)) {
      syncBotLive();
      return;
    }
    state.live = null;
    if (event.chat.id === stream.chatId) {
      state.chat = event.chat;
      state.chatId = event.chat.id;
    }
    renderStage();
    scrollThread();
  }
}

async function readEventStream(response, stream) {
  if (!response.body) throw new Error("The reply did not arrive.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let saw = false;
  let ended = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let split;
    while ((split = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const data = raw
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!data) continue;
      saw = true;
      const event = JSON.parse(data);
      if (event.type === "done" || event.type === "error" || event.type === "stopped") ended = event.type;
      applyStreamEvent(event, stream);
    }
  }
  if (!saw) throw new Error("The reply did not arrive.");
  return ended;
}

function beginFlight(botId, chatId) {
  const key = streamKey(botId, chatId);
  const previous = streams.get(key);
  if (previous) {
    previous.replaced = true;
    previous.sending = false;
    if (previous.control) {
      previous.control.abort();
      previous.control = null;
    }
  }
  const control = new AbortController();
  const stream = {
    botId,
    chatId,
    runId: "",
    control,
    live: null,
    sending: true,
    replaced: false,
  };
  streams.set(key, stream);
  if (isVisible(stream)) state.sending = true;
  syncStop();
  syncBotLive();
  return stream;
}

async function streamPost(path, body, stream) {
  const headers = { "Content-Type": "application/json", Accept: "text/event-stream" };
  const token = sharedToken();
  if (token) headers["X-EasyAgent-Token"] = token;
  const response = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(body || {}),
    signal: stream.control ? stream.control.signal : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      if (data && data.detail) detail = data.detail;
    } catch {
      /* the status line is enough */
    }
    const error = new Error(typeof detail === "string" ? detail : "The reply failed.");
    error.status = response.status;
    throw error;
  }
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    const data = await response.json();
    applyStreamEvent({ type: "done", chat: data.chat || data, bot_id: stream.botId, chat_id: stream.chatId }, stream);
    return "done";
  }
  return await readEventStream(response, stream);
}

async function settleOpenStream(ended, stream) {
  if (ended === "done" || ended === "error" || ended === "stopped") {
    stream.live = null;
    if (isVisible(stream)) state.live = null;
    return;
  }
  let chat = null;
  try {
    chat = await api(`/api/bots/${stream.botId}/chats/${stream.chatId}`);
  } catch {
    chat = null;
  }
  if (!chat) {
    rememberLive(stream, { phase: "stopped", text: "Stopped: the reply stopped before it finished." });
    if (isVisible(stream)) renderThread();
    return;
  }
  stream.savedChat = chat;
  if (!isVisible(stream)) return;
  state.chat = chat;
  state.chatId = chat.id;
  if (chat.run && chat.run.status === "running") {
    state.live = stream.live && stream.sending ? stream.live : liveFromRun(chat.run, chat);
    stream.live = state.live;
    return;
  }
  const last = (chat.messages || []).slice(-1)[0];
  const text = last ? String(last.content || "").trim() : "";
  const told = last && last.role === "assistant" && (last.error || text === "Stopped." || text.startsWith("Stopped:"));
  state.live = told ? null : { phase: "stopped", text: "Stopped: the reply stopped before it finished." };
  stream.live = state.live;
}

async function sendDraft(event) {
  event.preventDefault();
  if (!state.botId) return;
  const botId = state.botId;
  const text = $("draft").value.trim();
  const fileInput = $("attach-file");
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!text && !file) return;
  formError("thread-error", "");
  $("draft").value = "";
  delete state.drafts[`new:${botId}`];
  let chatId = state.chatId;
  let stream = null;
  try {
    if (!chatId) {
      const created = await api(`/api/bots/${botId}/chats`, { method: "POST" });
      chatId = created.id;
      if (state.botId === botId && !state.chatId) {
        state.chatId = chatId;
        if (state.chat) state.chat.id = chatId;
      }
    }
    stream = beginFlight(botId, chatId);
    chatLoad += 1;
    showOutgoing(text, stream);
    let attachmentId = null;
    if (file) {
      const body = new FormData();
      body.append("file", file);
      const headers = {};
      const token = sharedToken();
      if (token) headers["X-EasyAgent-Token"] = token;
      const response = await fetch(`/api/bots/${botId}/chats/${chatId}/files`, { method: "POST", headers, body });
      const uploaded = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(uploaded.detail || "That file was not kept.");
      }
      attachmentId = uploaded.id;
      fileInput.value = "";
    }
    if (stream.replaced) return;
    state.drafts[chatId] = "";
    const payload = { content: text };
    if (attachmentId) payload.attachment_id = attachmentId;
    const ended = await streamPost(`/api/bots/${botId}/chats/${chatId}/messages`, payload, stream);
    if (stream.replaced) return;
    await settleOpenStream(ended, stream);
    if (state.botId === botId) await refreshChats(botId);
    if (stream.replaced || !isVisible(stream)) return;
    renderStage();
    rememberSelection();
    if (state.chat) await markChatRead(botId, state.chat.id, (state.chat.messages || []).length);
  } catch (error) {
    if (!stream || stream.replaced || error.name === "AbortError") return;
    const detail = error.message || "The reply failed.";
    const shown = String(detail).startsWith("Stopped") ? detail : `Stopped: ${detail}`;
    rememberLive(stream, { phase: "stopped", text: shown });
    if (!isVisible(stream)) return;
    formError("thread-error", shown);
    renderThread();
    if (chatId) {
      try {
        const chat = await api(`/api/bots/${botId}/chats/${chatId}`);
        if (stream.replaced || !isVisible(stream)) return;
        const saved = chat.messages || [];
        if (!saved.some((item) => item.role === "user" && item.content === text)) {
          chat.messages = [...saved, { role: "user", content: text }];
        }
        state.chat = chat;
        const last = chat.messages[chat.messages.length - 1];
        state.live = last && (last.error || String(last.content || "").startsWith("Stopped")) ? null : stream.live;
        stream.live = state.live;
        renderStage();
      } catch {
        /* the error line above stays on screen */
      }
    }
  } finally {
    if (stream && !stream.replaced) {
      stream.sending = false;
      stream.control = null;
      if (isVisible(stream)) {
        state.sending = false;
        syncStop();
        if (state.view === "bot" && state.chat) renderThread();
        if (currentBot()) renderAskChoices(currentBot());
      }
    }
  }
}

async function retry(event) {
  event.preventDefault();
  if (!state.botId || !state.chatId) return;
  const botId = state.botId;
  const chatId = state.chatId;
  const stream = beginFlight(botId, chatId);
  chatLoad += 1;
  formError("thread-error", "");
  const now = new Date().toISOString();
  rememberLive(stream, {
    phase: "thinking",
    label: "Waiting on model",
    text: "",
    heardAt: Date.now(),
    run: { status: "running", started_at: now, last_activity_at: now, current_step: "Waiting on model", reason: "" },
  });
  if (isVisible(stream)) {
    renderThread();
    scrollThread();
  }
  try {
    const ended = await streamPost(`/api/bots/${botId}/chats/${chatId}/retry`, {}, stream);
    if (stream.replaced) return;
    await settleOpenStream(ended, stream);
    if (state.botId === botId) await refreshChats(botId);
    if (!isVisible(stream)) return;
    renderStage();
    await markChatRead(botId, chatId, (state.chat.messages || []).length);
  } catch (error) {
    if (stream.replaced || error.name === "AbortError") return;
    const detail = error.message || "The reply failed.";
    const shown = String(detail).startsWith("Stopped") ? detail : `Stopped: ${detail}`;
    rememberLive(stream, { phase: "stopped", text: shown });
    if (!isVisible(stream)) return;
    formError("thread-error", shown);
    renderThread();
  } finally {
    if (!stream.replaced) {
      stream.sending = false;
      stream.control = null;
      if (isVisible(stream)) {
        state.sending = false;
        syncStop();
        renderThread();
      }
    }
  }
}

async function continueRun(event) {
  event.preventDefault();
  if (state.sending || !state.botId || !state.chatId) return;
  $("draft").value = "Continue";
  await sendDraft(event);
}

async function stopFlight(event) {
  if (event) event.preventDefault();
  if (state.view !== "bot" || !state.botId || !state.chatId) return;
  const botId = state.botId;
  const chatId = state.chatId;
  const stream = streamFor(botId, chatId);
  formError("thread-error", "");
  if (stream && stream.control) {
    const control = stream.control;
    stream.control = null;
    stream.sending = false;
    stream.live = null;
    control.abort();
  }
  state.sending = false;
  state.live = null;
  syncStop();
  try {
    const chat = await api(`/api/bots/${botId}/chats/${chatId}/stop`, { method: "POST" });
    if (state.view !== "bot" || state.botId !== botId || state.chatId !== chatId) return;
    state.chat = chat;
    state.live = null;
    renderStage();
    scrollThread();
  } catch (error) {
    if (state.botId !== botId || state.chatId !== chatId) return;
    const shown = String(error.message || "").startsWith("Stopped")
      ? error.message
      : "Stopped: you pressed Stop";
    formError("thread-error", shown);
    renderThread();
  }
  if (currentBot()) renderAskChoices(currentBot());
}

function syncConfirmButton() {
  const typed = $("confirm-form").dataset.typed !== "0";
  $("confirm-go").disabled = typed && !namesMatch($("confirm-input").value, $("confirm-name").textContent);
}

function openConfirm({ kicker, title, copy, name, action, submitLabel, typed }) {
  const needsName = typed !== false;
  $("confirm-form").dataset.typed = needsName ? "1" : "0";
  $("confirm-kicker").textContent = kicker;
  $("confirm-title").textContent = title;
  $("confirm-copy").textContent = copy;
  $("confirm-name").textContent = name || "";
  if ($("confirm-type")) $("confirm-type").hidden = !needsName;
  $("confirm-input").value = "";
  $("confirm-go").textContent = submitLabel;
  formError("confirm-error", "");
  syncConfirmButton();
  $("confirm-form").onsubmit = async (event) => {
    event.preventDefault();
    if (needsName && !namesMatch($("confirm-input").value, name || "")) return;
    $("confirm-go").disabled = true;
    try {
      await action();
      $("confirm-dialog").close();
    } catch (error) {
      formError("confirm-error", error.message);
      syncConfirmButton();
    }
  };
  $("confirm-dialog").showModal();
  if (needsName) $("confirm-input").focus();
}

function confirmDeleteChat(chat) {
  const botId = state.botId;
  if (!botId || !chat) return;
  const chatId = chat.id;
  openConfirm({
    kicker: "Delete chat",
    title: `Delete ${chat.title || "this chat"}?`,
    copy: "This removes only this chat's transcript. The bot and its other chats stay.",
    typed: false,
    submitLabel: "Delete chat",
    action: async () => {
      await api(`/api/bots/${botId}/chats/${chatId}`, { method: "DELETE" });
      const wasOpen = state.chatId === chatId;
      await refreshChats(botId);
      if (wasOpen) {
        state.chat = null;
        state.chatId = null;
        const next = state.chats[0];
        if (next) await openChat(next.id);
        else renderStage();
      } else {
        renderChatList();
      }
      rememberSelection();
    },
  });
}

function confirmRemoveBot() {
  const bot = currentBot();
  if (!bot) return;
  const botId = bot.id;
  openConfirm({
    kicker: "Remove bot",
    title: `Remove ${bot.name}?`,
    copy: "This deletes only this bot and its chats from disk. Other bots, connections, skills, and direction stay.",
    name: bot.name,
    submitLabel: "Remove bot",
    action: async () => {
      await api(`/api/bots/${botId}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm_name: bot.name }),
      });
      if (state.botId === botId) {
        state.botId = null;
        state.chatId = null;
        state.chat = null;
        state.chats = [];
        state.renderedBotId = null;
      }
      await refreshBots();
      renderStage();
      rememberSelection();
    },
  });
}

function confirmRemoveEndpoint(endpoint) {
  openConfirm({
    kicker: "Remove connection",
    title: `Remove ${endpoint.name}?`,
    copy: "This deletes only the connection. Bots and their chats stay. A bot that used it will ask you to pick another connection.",
    name: endpoint.name,
    submitLabel: "Remove connection",
    action: async () => {
      await api(`/api/endpoints/${endpoint.id}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm_name: endpoint.name }),
      });
      await refreshEndpoints();
      await refreshBots();
      if (state.botId) renderStage();
    },
  });
}

async function saveSettings(event) {
  event.preventDefault();
  const bot = currentBot();
  if (!bot) return;
  const botId = bot.id;
  const name = $("settings-name").value.trim();
  const endpointId = $("settings-endpoint").value;
  const model = $("settings-model").value.trim();
  const contextTokens = Number($("settings-context").value);
  if (!name) {
    $("settings-note").textContent = "Type a name. Chats were not changed.";
    show($("settings-note"), true);
    return;
  }
  if (!endpointId) {
    $("settings-note").textContent = "Pick a connection. Chats were not changed.";
    show($("settings-note"), true);
    return;
  }
  if (!Number.isInteger(contextTokens) || contextTokens < 512 || contextTokens > 1000000) {
    $("settings-note").textContent = "How much chat it sees must be a whole number of tokens from 512 to 1000000. Chats were not changed.";
    show($("settings-note"), true);
    return;
  }
  const body = {
    name,
    endpoint_id: endpointId,
    model: model || null,
    context_tokens: contextTokens,
    check_enabled: $("settings-check") ? $("settings-check").checked : true,
  };
  if (state.pendingFace && state.pendingFace.botId === botId) body.face_color = state.pendingFace.color;
  try {
    await api(`/api/bots/${botId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    state.pendingFace = null;
    await refreshBots();
    if (state.botId !== botId) return;
    const count = state.chat ? (state.chat.messages || []).length : 0;
    if (state.chatId) {
      try {
        const chat = await api(`/api/bots/${botId}/chats/${state.chatId}`);
        if (state.botId === botId && state.chat && state.chat.id === chat.id) state.chat = chat;
      } catch {
        /* the save already landed; this form does not write the transcript */
      }
    }
    state.renderedBotId = null;
    renderStage();
    const still = state.chat ? (state.chat.messages || []).length : 0;
    $("settings-note").textContent = still === count
      ? "Saved. This bot's chats were not rewritten."
      : "Saved.";
    show($("settings-note"), true);
  } catch (error) {
    $("settings-note").textContent = error.message;
    show($("settings-note"), true);
  }
}

function closeInfo() {
  $("info-dialog").close();
}

async function showSkills() {
  $("info-title").textContent = "Skills";
  const body = $("info-body");
  body.replaceChildren(el("p", { class: "meta" }, [
    "Markdown files in the skills folder. The agent writes these when it learns something to reuse.",
  ]));
  try {
    const skills = await api("/api/skills");
    if (!skills.length) {
      body.append(el("p", {}, ["No skills yet. Ask the bot to remember a preference."]));
    }
    for (const skill of skills) {
      const card = el("div", { class: "skill-card" }, [
        el("strong", {}, [skill.name]),
        el("p", { class: "meta" }, [skill.description || "No description"]),
      ]);
      if (skill.body) card.append(el("pre", { class: "skill-body" }, [skill.body]));
      body.append(card);
    }
  } catch (error) {
    body.append(el("p", { class: "form-error" }, [error.message]));
  }
  $("info-dialog").showModal();
}

async function showDirection() {
  closePhoneList();
  state.screen = "direction";
  if (state.view === "room") state.view = state.botId ? "bot" : null;
  renderBots();
  renderRooms();
  renderStage();
  formError("direction-error", "");
  $("direction-note").textContent = "";
  try {
    const current = await api("/api/direction");
    $("direction-text").value = current.text;
  } catch (error) {
    formError("direction-error", error.message);
  }
}

function renderRooms() {
  const list = $("room-list");
  list.replaceChildren();
  show($("room-empty"), state.rooms.length === 0);
  for (const room of state.rooms) {
    const selected = state.view === "room" && room.id === state.roomId;
    const bots = room.bot_ids ? room.bot_ids.length : 0;
    const count = room.message_count === 1 ? "1 message" : `${room.message_count || 0} messages`;
    list.append(el("li", {}, [
      el("button", {
        type: "button",
        class: selected ? "entity is-selected" : "entity",
        "aria-current": selected ? "true" : null,
        onclick: () => selectRoom(room.id),
      }, [
        el("strong", {}, [room.name]),
        el("small", {}, [`${bots} bot${bots === 1 ? "" : "s"} · ${count}`]),
      ]),
    ]));
  }
}

async function refreshRooms() {
  state.rooms = await api("/api/rooms");
  renderRooms();
}

function renderRoom() {
  const room = state.room;
  if (!room) return;
  $("room-title").textContent = room.name || "Room";
  const names = (room.members || []).map((member) => member.name);
  $("room-meta").textContent = names.length
    ? `${names.join(", ")}. Replies stay in this room. Private chats are separate.`
    : "No bots in this room yet. Add two or more. Their private chats stay separate.";
  const members = $("room-members");
  members.replaceChildren();
  for (const member of room.members || []) {
    const memberBot = state.bots.find((item) => item.id === member.id);
    const chip = el("li", { class: "member-chip" }, [
      memberBot ? makeFace(memberBot, { tiny: true, selected: false }) : el("span"),
      el("span", {}, [member.name]),
      el("button", {
        type: "button",
        class: "text-btn",
        title: "Take this bot out of the room. The bot and this transcript stay.",
        onclick: () => removeFromRoom(member),
      }, ["Remove"]),
    ]);
    members.append(chip);
  }
  const select = $("room-add-bot");
  const present = new Set((room.members || []).map((member) => member.id));
  const available = state.bots.filter((bot) => !present.has(bot.id));
  select.replaceChildren();
  if (available.length === 0) {
    select.append(el("option", { value: "" }, [state.bots.length ? "All bots are in the room" : "Add a bot first"]));
    select.disabled = true;
  } else {
    select.disabled = false;
    for (const bot of available) select.append(el("option", { value: bot.id }, [bot.name]));
  }
  const messages = room.messages || [];
  show($("room-empty-thread"), messages.length === 0 && !state.roomLive);
  const list = $("room-messages");
  list.replaceChildren();
  const memberIndex = new Map((room.members || []).map((member, index) => [member.id, index]));
  for (const message of messages) {
    const error = Boolean(message.error);
    const mine = message.speaker === "user";
    const who = mine ? "You" : (message.speaker_name || "Bot");
    const speaker = memberIndex.has(message.speaker) ? ` speaker-${memberIndex.get(message.speaker) % 2}` : "";
    const main = el("div", { class: "message-main" }, [
      el("p", { class: "body" }, [message.content || ""]),
    ]);
    if (message.id) main.append(reactionControl(message, true));
    const speakerBot = mine ? null : state.bots.find((item) => item.id === message.speaker);
    const item = el("li", { class: `message ${mine ? "user" : "assistant"}${error ? " error" : ""}${speaker}` }, [
      whoLine(who, speakerBot, { halted: error, tiny: true }),
      main,
    ]);
    list.append(item);
  }
  if (state.roomLive === "thinking") {
    list.append(el("li", { class: "message pending" }, [
      el("p", { class: "who" }, ["Thinking"]),
      el("p", { class: "body" }, ["…"]),
    ]));
  } else if (typeof state.roomLive === "string" && state.roomLive) {
    list.append(el("li", { class: "message assistant error" }, [
      el("p", { class: "who" }, ["Error"]),
      el("p", { class: "body" }, [state.roomLive]),
    ]));
  }
  $("room-context").textContent = messages.length ? contextNote(room.context || {}, "room") : "";
  const scroller = $("room-scroll");
  scroller.scrollTop = scroller.scrollHeight;
}

async function selectRoom(roomId) {
  const ticket = ++state.nav;
  closePhoneList();
  watchSchedules(false);
  if (state.view === "bot") stashDraft();
  state.view = "room";
  state.screen = "rooms";
  state.roomLive = null;
  state.roomId = roomId;
  formError("room-error", "");
  renderRooms();
  renderBots();
  if (!state.room || state.room.id !== roomId) {
    const listed = state.rooms.find((room) => room.id === roomId);
    state.room = listed ? { ...listed, members: [], messages: [] } : { id: roomId, name: "Room", members: [], messages: [] };
  }
  renderStage();
  try {
    const room = await api(`/api/rooms/${roomId}`);
    if (ticket !== state.nav) return;
    state.room = room;
    state.roomId = room.id;
  } catch (error) {
    if (ticket !== state.nav) return;
    formError("room-error", error.message);
    return;
  }
  renderRooms();
  renderStage();
  rememberSelection();
  await markRoomRead(room.id, (room.messages || []).length);
}

async function removeFromRoom(member) {
  if (!state.roomId || state.sending) return;
  const roomId = state.roomId;
  formError("room-error", "");
  try {
    const room = await api(`/api/rooms/${roomId}/bots/${member.id}`, { method: "DELETE" });
    if (state.view !== "room" || state.roomId !== roomId) return;
    state.room = room;
    await refreshRooms();
    renderRoom();
  } catch (error) {
    formError("room-error", error.message);
  }
}

async function sendRoom(event) {
  event.preventDefault();
  if (state.sending || state.view !== "room" || !state.roomId) return;
  const roomId = state.roomId;
  const text = $("room-draft").value.trim();
  if (!text) return;
  state.sending = true;
  $("room-send").disabled = true;
  $("room-send").textContent = "Sending";
  formError("room-error", "");
  $("room-draft").value = "";
  if (!state.room) state.room = { id: roomId, messages: [], members: [] };
  state.room.messages = [...(state.room.messages || []), { speaker: "user", role: "user", content: text, speaker_name: "You" }];
  state.roomLive = "thinking";
  renderRoom();
  try {
    const room = await api(`/api/rooms/${roomId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content: text }),
    });
    if (state.view !== "room" || state.roomId !== roomId) return;
    state.roomLive = null;
    state.room = room;
    await refreshRooms();
    renderRoom();
    await markRoomRead(roomId, (room.messages || []).length);
  } catch (error) {
    if (state.view === "room" && state.roomId === roomId) {
      state.roomLive = null;
      try {
        state.room = await api(`/api/rooms/${roomId}`);
      } catch {
        /* keep the optimistic line */
      }
      const saved = (state.room && state.room.messages) || [];
      const last = saved[saved.length - 1];
      if (!last || !last.error) state.roomLive = error.message || "The reply failed.";
      renderRoom();
    }
  } finally {
    state.sending = false;
    $("room-send").disabled = false;
    $("room-send").textContent = "Send";
  }
}

function watchSchedules(on) {
  if (state.scheduleTimer) {
    clearInterval(state.scheduleTimer);
    state.scheduleTimer = null;
  }
  if (!on || state.view !== "bot" || !state.botId) return;
  const botId = state.botId;
  refreshSchedules(botId);
  state.scheduleTimer = setInterval(() => {
    if (state.view !== "bot" || state.botId !== botId) return;
    refreshSchedules(botId);
  }, 5000);
}

async function refreshLearning(botId) {
  if (!botId || state.botId !== botId) return;
  try {
    const panel = await api(`/api/bots/${botId}/learning`);
    if (state.botId !== botId) return;
    renderLearning(botId, panel);
  } catch (error) {
    if (state.botId === botId) formError("learn-error", error.message);
  }
}

function renderLearning(botId, panel) {
  const pause = $("learn-pause");
  const manual = $("learn-manual");
  if (pause) pause.checked = Boolean(panel.paused);
  if (manual) manual.checked = Boolean(panel.manual);
  fillLearn(botId, "learn-waiting", "learn-waiting-empty", panel.waiting || [], (item) => {
    const kids = [
      el("strong", {}, [item.name || "Candidate"]),
      el("small", {}, [item.status === "ready" ? "Waiting for approval." : "Waiting for a replay."]),
    ];
    if (item.reason) kids.push(el("small", {}, [item.reason]));
    if (item.status === "ready" && item.id) {
      kids.push(el("button", {
        type: "button",
        class: "text-btn",
        onclick: () => approveCandidate(botId, item.id),
      }, ["Approve"]));
    }
    return el("li", {}, kids);
  });
  fillLearn(botId, "learn-promoted", "learn-promoted-empty", panel.promoted || [], (item) => el("li", {}, [
    el("strong", {}, [item.name || "Skill"]),
    el("small", {}, [item.reason || "Promoted."]),
  ]));
  fillLearn(botId, "learn-rejected", "learn-rejected-empty", panel.rejected || [], (item) => el("li", {}, [
    el("strong", {}, [item.name || "Candidate"]),
    el("small", {}, [item.reason || "Rejected."]),
  ]));
  const night = $("learn-night");
  if (night) {
    night.textContent = (panel.last_night && panel.last_night.summary) || "No nightly pass yet. These notes are written only while this bot is idle.";
  }
  const noteList = $("learn-notes");
  if (noteList) {
    noteList.replaceChildren();
    for (const file of panel.notes || []) {
      const changed = file.changed ? " Changed last night." : "";
      noteList.append(el("li", {}, [
        el("strong", {}, [file.title || file.name]),
        el("small", {}, [`${file.name}. ${file.entries || 0} entries.${changed}`]),
      ]));
    }
  }
  const pruneList = $("learn-prune");
  const pruneEmpty = $("learn-prune-empty");
  const pending = (panel.prune && panel.prune.pending) || [];
  if (pruneList) {
    pruneList.replaceChildren();
    for (const row of pending) {
      const staying = panel.prune && (panel.prune.pruning === false || panel.prune.keep_forever) ? " Staying." : "";
      pruneList.append(el("li", {}, [`${localWhen(row.created_at)} — ${row.preview || row.message_id}${staying}`]));
    }
  }
  if (pruneEmpty) show(pruneEmpty, pending.length === 0);
  fillLearn(botId, "learn-skills", "learn-skills-empty", panel.skills || [], (item) => {
    const uses = item.uses || 0;
    const rate = item.rate == null ? "no uses yet" : `${Math.round(item.rate * 100)}% passed`;
    const where = item.archived ? "Archived." : (item.origin === "user" ? "You wrote this." : "Learned.");
    return el("li", {}, [
      el("strong", {}, [item.name || "Skill"]),
      el("small", {}, [`${where} ${uses} use${uses === 1 ? "" : "s"}, ${item.passes || 0} passed, ${item.fails || 0} failed. ${rate}`]),
    ]);
  });
}

function fillLearn(_botId, listId, emptyId, rows, render) {
  const list = $(listId);
  const empty = $(emptyId);
  if (!list) return;
  list.replaceChildren();
  if (empty) show(empty, rows.length === 0);
  for (const row of rows) list.append(render(row));
}

async function setLearnFlag(kind, on) {
  const bot = currentBot();
  if (!bot) return;
  try {
    const panel = await api(`/api/bots/${bot.id}/learning/${kind}`, {
      method: "POST",
      body: JSON.stringify({ on: Boolean(on) }),
    });
    renderLearning(bot.id, panel);
  } catch (error) {
    formError("learn-error", error.message);
    refreshLearning(bot.id);
  }
}

async function rollBackLearning() {
  const bot = currentBot();
  if (!bot) return;
  const status = $("learn-status");
  try {
    const panel = await api(`/api/bots/${bot.id}/learning/rollback`, { method: "POST", body: "{}" });
    renderLearning(bot.id, panel);
    if (status) {
      status.hidden = false;
      status.textContent = "Rolled back the last skill or memory change.";
    }
  } catch (error) {
    formError("learn-error", error.message);
  }
}

async function approveCandidate(botId, candidateId) {
  try {
    const panel = await api(`/api/bots/${botId}/learning/approve/${candidateId}`, { method: "POST", body: "{}" });
    if (state.botId === botId) renderLearning(botId, panel);
  } catch (error) {
    formError("learn-error", error.message);
  }
}

async function refreshSchedules(botId) {
  if (state.view !== "bot" || state.botId !== botId) return;
  try {
    const [schedules, jobs, trash] = await Promise.all([
      api(`/api/bots/${botId}/schedules`),
      api(`/api/bots/${botId}/jobs`),
      api(`/api/bots/${botId}/schedules/trash`),
    ]);
    if (state.view !== "bot" || state.botId !== botId) return;
    renderSchedules(schedules, jobs, trash || []);
  } catch (error) {
    if (state.view === "bot" && state.botId === botId) formError("schedule-error", error.message);
  }
}

function renderSchedules(schedules, jobs, trash) {
  const list = $("schedule-list");
  list.replaceChildren();
  show($("schedule-empty"), schedules.length === 0);
  for (const schedule of schedules) {
    const status = schedule.paused ? "Paused" : "On";
    list.append(el("li", { class: "schedule-row" }, [
      el("header", {}, [
        el("strong", {}, [schedule.name || schedule.label || schedule.kind]),
        el("span", { class: "schedule-actions" }, [
          el("button", {
            type: "button",
            class: "text-btn",
            onclick: () => runSchedule(schedule),
          }, ["Run now"]),
          el("button", {
            type: "button",
            class: "text-btn",
            onclick: () => setSchedulePaused(schedule, !schedule.paused),
          }, [schedule.paused ? "Resume" : "Pause"]),
          el("button", {
            type: "button",
            class: "text-btn",
            onclick: () => deleteSchedule(schedule),
          }, ["Delete"]),
        ]),
      ]),
      el("p", {}, [schedule.preview || schedule.prompt]),
      el("small", {}, [`${status}.${schedule.quiet ? " Quiet." : ""}`]),
    ]));
  }
  const bin = $("schedule-trash");
  if (bin) {
    bin.replaceChildren();
    for (const item of trash || []) {
      bin.append(el("li", { class: "schedule-row" }, [
        el("header", {}, [
          el("strong", {}, [item.name || item.prompt || "Routine"]),
          el("button", {
            type: "button",
            class: "text-btn",
            onclick: () => restoreSchedule(item),
          }, ["Restore"]),
        ]),
      ]));
    }
  }
  const log = $("job-log");
  log.replaceChildren();
  const recent = jobs.slice(-8).reverse();
  show($("job-empty"), recent.length === 0);
  for (const job of recent) {
    const failed = job.status === "error";
    log.append(el("li", { class: failed ? "job-row error" : "job-row" }, [
      el("header", {}, [
        el("strong", {}, [failed ? "Error" : "Ran"]),
        el("small", {}, [job.finished_at || job.started_at || ""]),
      ]),
      el("p", {}, [failed ? (job.error || "The endpoint failed.") : (job.output || "")]),
    ]));
  }
}

async function setSchedulePaused(schedule, paused) {
  if (!state.botId) return;
  const botId = state.botId;
  formError("schedule-error", "");
  try {
    await api(`/api/bots/${botId}/schedules/${schedule.id}/pause`, {
      method: "POST",
      body: JSON.stringify({ paused }),
    });
    await refreshSchedules(botId);
  } catch (error) {
    formError("schedule-error", error.message);
  }
}

async function runSchedule(schedule) {
  if (!state.botId) return;
  const botId = state.botId;
  formError("schedule-error", "");
  try {
    await api(`/api/bots/${botId}/schedules/${schedule.id}/run`, { method: "POST" });
    await refreshSchedules(botId);
  } catch (error) {
    formError("schedule-error", error.message);
  }
}

async function restoreSchedule(schedule) {
  if (!state.botId) return;
  const botId = state.botId;
  formError("schedule-error", "");
  try {
    await api(`/api/bots/${botId}/schedules/${schedule.id}/restore`, { method: "POST" });
    await refreshSchedules(botId);
  } catch (error) {
    formError("schedule-error", error.message);
  }
}

async function deleteSchedule(schedule) {
  if (!state.botId) return;
  const botId = state.botId;
  formError("schedule-error", "");
  try {
    await api(`/api/bots/${botId}/schedules/${schedule.id}`, { method: "DELETE" });
    await refreshSchedules(botId);
  } catch (error) {
    formError("schedule-error", error.message);
  }
}

function wire() {
  $("toggle-endpoint").addEventListener("click", () => {
    const form = $("endpoint-form");
    if (!form.hidden && !state.connectionId) {
      show(form, false);
      return;
    }
    openConnectionForm(null);
  });
  $("cancel-endpoint").addEventListener("click", () => {
    state.connectionId = null;
    show($("endpoint-form"), false);
  });
  $("endpoint-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    formError("endpoint-error", "");
    const payload = {
      name: $("endpoint-name").value,
      base_url: $("endpoint-url").value,
      model: $("endpoint-model").value,
      max_parallel: Number($("endpoint-parallel").value || 1),
    };
    const key = $("endpoint-key").value;
    if (key) payload.api_key = key;
    if ($("endpoint-clear-key").checked) payload.clear_api_key = true;
    const id = state.connectionId;
    try {
      if (id) {
        await api(`/api/endpoints/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        await api("/api/endpoints", { method: "POST", body: JSON.stringify(payload) });
      }
      state.connectionId = null;
      $("endpoint-form").reset();
      show($("endpoint-form"), false);
      await refreshEndpoints();
      await refreshBots();
      if (state.botId) {
        state.renderedBotId = null;
        renderStage();
      }
    } catch (error) {
      formError("endpoint-error", error.message);
    }
  });

  $("toggle-bot").addEventListener("click", () => {
    const form = $("bot-form");
    show(form, form.hidden);
    const prefer = state.endpoints[0] ? state.endpoints[0].id : "__new__";
    fillEndpointSelect($("bot-endpoint"), prefer, true);
    $("bot-submit").disabled = false;
    if (!form.hidden) $("bot-name").focus();
  });
  $("bot-endpoint").addEventListener("change", syncNewConnection);
  $("cancel-bot").addEventListener("click", () => show($("bot-form"), false));
  $("bot-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    formError("bot-error", "");
    let endpointId = $("bot-endpoint").value;
    try {
      if (endpointId === "__new__") {
        const created = await api("/api/endpoints", {
          method: "POST",
          body: JSON.stringify({
            name: $("bot-conn-name").value,
            base_url: $("bot-conn-url").value,
            api_key: $("bot-conn-key").value,
            model: $("bot-conn-model").value,
          }),
        });
        endpointId = created.id;
        await refreshEndpoints();
      }
      if (!endpointId) {
        formError("bot-error", "Pick a connection, or add the server address.");
        return;
      }
      const bot = await api("/api/bots", {
        method: "POST",
        body: JSON.stringify({
          name: $("bot-name").value,
          endpoint_id: endpointId,
          model: $("bot-model").value,
          context_tokens: $("bot-context").value === "" ? null : Number($("bot-context").value),
        }),
      });
      $("bot-form").reset();
      show($("bot-form"), false);
      show($("bot-new-connection"), false);
      await refreshBots();
      const chat = await api(`/api/bots/${bot.id}/chats`, { method: "POST" });
      await selectBot(bot.id, chat.id);
      $("draft").focus();
    } catch (error) {
      formError("bot-error", error.message);
    }
  });

  $("toggle-room").addEventListener("click", () => {
    const form = $("room-form");
    show(form, form.hidden);
    if (!form.hidden) $("room-name").focus();
  });
  $("cancel-room").addEventListener("click", () => show($("room-form"), false));
  $("room-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    formError("room-form-error", "");
    try {
      const room = await api("/api/rooms", {
        method: "POST",
        body: JSON.stringify({ name: $("room-name").value }),
      });
      $("room-form").reset();
      show($("room-form"), false);
      await refreshRooms();
      await selectRoom(room.id);
    } catch (error) {
      formError("room-form-error", error.message);
    }
  });
  $("room-add-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.roomId || state.sending) return;
    const botId = $("room-add-bot").value;
    if (!botId) return;
    const roomId = state.roomId;
    formError("room-error", "");
    try {
      const room = await api(`/api/rooms/${roomId}/bots`, {
        method: "POST",
        body: JSON.stringify({ bot_id: botId }),
      });
      if (state.view !== "room" || state.roomId !== roomId) return;
      state.room = room;
      await refreshRooms();
      renderRoom();
    } catch (error) {
      formError("room-error", error.message);
    }
  });
  $("room-composer").addEventListener("submit", sendRoom);
  $("room-draft").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("room-composer").requestSubmit();
    }
  });

  $("toggle-schedule").addEventListener("click", () => {
    const form = $("schedule-form");
    show(form, form.hidden);
    if (!form.hidden) $("schedule-prompt").focus();
  });
  $("cancel-schedule").addEventListener("click", () => show($("schedule-form"), false));
  $("schedule-kind").addEventListener("change", () => {
    const kind = $("schedule-kind").value;
    show($("schedule-time-label"), kind === "weekdays" || kind === "daily");
    show($("schedule-cron-label"), kind === "cron");
    show($("schedule-every-label"), kind === "interval");
  });
  $("schedule-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.botId) return;
    const botId = state.botId;
    formError("schedule-error", "");
    const kind = $("schedule-kind").value;
    const body = {
      name: $("schedule-name").value,
      prompt: $("schedule-prompt").value,
      quiet: $("schedule-quiet").checked,
    };
    const zone = $("schedule-zone").value.trim();
    if (zone) body.timezone = zone;
    if (kind === "weekdays") body.weekdays = $("schedule-time").value;
    else if (kind === "daily") body.daily = $("schedule-time").value;
    else if (kind === "cron") {
      body.kind = "cron";
      body.cron = $("schedule-cron").value;
    } else {
      body.kind = "interval";
      body.every_minutes = Number($("schedule-every").value);
    }
    try {
      await api(`/api/bots/${botId}/schedules`, { method: "POST", body: JSON.stringify(body) });
      $("schedule-prompt").value = "";
      show($("schedule-form"), false);
      await refreshSchedules(botId);
    } catch (error) {
      formError("schedule-error", error.message);
    }
  });

  $("new-chat").addEventListener("click", startNewChat);
  $("composer").addEventListener("submit", sendDraft);
  $("ask-go").addEventListener("click", askOtherBot);
  $("draft").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("retry").addEventListener("click", retry);
  $("continue").addEventListener("click", continueRun);
  $("stop").addEventListener("click", stopFlight);
  $("remove-bot").addEventListener("click", confirmRemoveBot);
  $("settings-form").addEventListener("submit", saveSettings);
  if ($("learn-pause")) $("learn-pause").addEventListener("change", () => setLearnFlag("pause", $("learn-pause").checked));
  if ($("learn-manual")) $("learn-manual").addEventListener("change", () => setLearnFlag("manual", $("learn-manual").checked));
  if ($("learn-rollback")) $("learn-rollback").addEventListener("click", rollBackLearning);
  $("show-skills").addEventListener("click", () => {
    if (!state.botId) {
      showSkills();
      return;
    }
    openSettings();
  });
  $("show-direction").addEventListener("click", showDirection);
  $("open-settings").addEventListener("click", openSettings);
  $("open-settings-chat").addEventListener("click", openSettings);
  $("close-settings").addEventListener("click", backToChat);
  $("rooms-back").addEventListener("click", () => openPlainScreen("rooms"));
  $("projects-back").addEventListener("click", () => {
    state.groupProjectId = null;
    state.groupProject = null;
    openPlainScreen("projects");
  });
  $("toggle-project").addEventListener("click", () => {
    const form = $("project-form");
    show(form, form.hidden);
    if (!form.hidden) $("project-name").focus();
  });
  $("cancel-project").addEventListener("click", () => show($("project-form"), false));
  $("project-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    formError("project-form-error", "");
    try {
      const project = await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({ name: $("project-name").value }),
      });
      $("project-name").value = "";
      show($("project-form"), false);
      state.groupProjects = await api("/api/projects");
      renderGroupProjectList();
      await openGroupProject(project.id);
    } catch (error) {
      formError("project-form-error", error.message);
    }
  });
  $("project-upload").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.groupProjectId) return;
    formError("project-error", "");
    try {
      state.groupProject = await postFiles(`/api/projects/${state.groupProjectId}/files`, $("project-files"));
      state.groupProjects = await api("/api/projects");
      renderGroupProjectList();
      renderStage();
    } catch (error) {
      formError("project-error", error.message);
    }
  });
  $("project-add-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.groupProjectId) return;
    const botId = $("project-add-bot").value;
    if (!botId) return;
    formError("project-error", "");
    try {
      state.groupProject = await api(`/api/projects/${state.groupProjectId}/bots`, {
        method: "POST",
        body: JSON.stringify({ bot_id: botId }),
      });
      state.groupProjects = await api("/api/projects");
      renderGroupProjectList();
      renderStage();
    } catch (error) {
      formError("project-error", error.message);
    }
  });
  $("project-remove").addEventListener("click", () => {
    const project = state.groupProject;
    if (!project) return;
    openConfirm({
      kicker: "Remove project",
      title: `Remove ${project.name}?`,
      copy: "This deletes the project and its files from this computer. Rooms and chats stay.",
      name: project.name,
      submitLabel: "Remove project",
      action: async () => {
        await api(`/api/projects/${project.id}`, {
          method: "DELETE",
          body: JSON.stringify({ confirm_name: project.name }),
        });
        state.groupProjectId = null;
        state.groupProject = null;
        await refreshGroupProjects();
        openPlainScreen("projects");
      },
    });
  });
  $("bot-project-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.botId) return;
    const botId = state.botId;
    formError("bot-project-error", "");
    try {
      const project = await api(`/api/bots/${botId}/projects`, {
        method: "POST",
        body: JSON.stringify({ name: $("bot-project-name").value }),
      });
      $("bot-project-name").value = "";
      await refreshBotProjects(botId);
      await openBotProject(project.id);
    } catch (error) {
      formError("bot-project-error", error.message);
    }
  });
  $("bot-project-upload").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.botId || !state.botProjectId) return;
    formError("bot-project-error", "");
    try {
      await postFiles(`/api/bots/${state.botId}/projects/${state.botProjectId}/files`, $("bot-project-files"));
      await refreshBotProjects(state.botId);
    } catch (error) {
      formError("bot-project-error", error.message);
    }
  });
  $("bot-project-remove").addEventListener("click", () => {
    const project = state.botProjects.find((item) => item.id === state.botProjectId);
    if (!project || !state.botId) return;
    const botId = state.botId;
    openConfirm({
      kicker: "Remove project",
      title: `Remove ${project.name}?`,
      copy: "This deletes the project and its files from this computer. This bot's chats stay.",
      name: project.name,
      submitLabel: "Remove project",
      action: async () => {
        await api(`/api/bots/${botId}/projects/${project.id}`, {
          method: "DELETE",
          body: JSON.stringify({ confirm_name: project.name }),
        });
        state.botProjectId = null;
        await refreshBotProjects(botId);
      },
    });
  });
  for (const id of ["nav-connections", "nav-rooms", "nav-projects", "nav-computers", "nav-about"]) {
    $(id).addEventListener("click", () => openPlainScreen($(id).dataset.screen));
  }
  $("save-direction").addEventListener("click", async () => {
    formError("direction-error", "");
    try {
      const saved = await api("/api/direction", {
        method: "PUT",
        body: JSON.stringify({ text: $("direction-text").value }),
      });
      $("direction-text").value = saved.text;
      $("direction-note").textContent = "Saved. Chats were not changed. The next turn will read this.";
    } catch (error) {
      formError("direction-error", error.message);
    }
  });
  $("info-close").addEventListener("click", closeInfo);
  $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close());
  $("confirm-input").addEventListener("input", syncConfirmButton);
  $("toggle-computer").addEventListener("click", () => {
    const form = $("computer-form");
    if (!form.hidden && !state.computerId) {
      show(form, false);
      return;
    }
    openComputerForm(null);
  });
  $("cancel-computer").addEventListener("click", () => {
    state.computerId = null;
    show($("computer-form"), false);
  });
  $("computer-kind").addEventListener("change", syncSigninKind);
  $("computer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    formError("computer-error", "");
    const name = $("computer-name").value.trim();
    const host = $("computer-host").value.trim();
    if (!name || !host) {
      formError("computer-error", "Type a name and a host. Chats were not changed.");
      return;
    }
    pendingComputer = {
      id: state.computerId,
      name,
      kind: $("computer-kind").value,
      host,
      port: $("computer-port").value === "" ? null : Number($("computer-port").value),
    };
    clearSignin();
    formError("signin-error", "");
    syncSigninKind();
    $("signin-dialog").showModal();
    $("signin-user").focus();
  });
  $("signin-cancel").addEventListener("click", () => {
    pendingComputer = null;
    clearSignin();
    $("signin-dialog").close();
  });
  $("signin-dialog").addEventListener("close", clearSignin);
  $("signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const pending = pendingComputer;
    if (!pending) return;
    formError("signin-error", "");
    const user = $("signin-user").value;
    const password = $("signin-password").value;
    const key = $("signin-key").value;
    const editing = Boolean(pending.id);
    if (!editing && !user.trim()) {
      formError("signin-error", "Type the username.");
      return;
    }
    if (!editing && pending.kind === "windows" && !password) {
      formError("signin-error", "Type the password.");
      return;
    }
    if (!editing && pending.kind === "linux" && !password && !key) {
      formError("signin-error", "Type a password or paste a key.");
      return;
    }
    clearSignin();
    pendingComputer = null;
    $("signin-dialog").close();
    const payload = {
      name: pending.name,
      kind: pending.kind,
      host: pending.host,
      user,
      password,
      key: pending.kind === "linux" ? key : "",
    };
    if (pending.port != null) payload.port = pending.port;
    try {
      if (pending.id) {
        await api(`/api/computers/${pending.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        await api("/api/computers", { method: "POST", body: JSON.stringify(payload) });
      }
      state.computerId = null;
      $("computer-form").reset();
      show($("computer-form"), false);
      await refreshComputers();
    } catch (error) {
      formError("computer-error", error.message);
    }
  });
  $("toggle-skill").addEventListener("click", () => {
    const form = $("skill-form");
    show(form, form.hidden);
    if (!form.hidden) $("skill-name").focus();
  });
  $("cancel-skill").addEventListener("click", () => show($("skill-form"), false));
  $("skill-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    formError("skill-error", "");
    try {
      await api("/api/skills", {
        method: "POST",
        body: JSON.stringify({
          name: $("skill-name").value,
          description: $("skill-description").value,
          body: $("skill-body").value,
        }),
      });
      $("skill-form").reset();
      show($("skill-form"), false);
      await refreshSkills();
    } catch (error) {
      formError("skill-error", error.message);
    }
  });
  syncSigninKind();

  $("memory-back").addEventListener("click", () => {
    state.memoryTopic = null;
    refreshMemory(state.botId);
  });
  $("memory-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.botId) return;
    formError("memory-error", "");
    try {
      const body = { text: $("memory-text").value };
      if (state.memoryTopic) body.topic = state.memoryTopic;
      await api(`/api/bots/${state.botId}/memory`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      $("memory-text").value = "";
      await refreshMemory(state.botId);
    } catch (error) {
      formError("memory-error", error.message);
    }
  });
  $("watch-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    formError("watch-error", "");
    try {
      const watch = await api("/api/watches", {
        method: "POST",
        body: JSON.stringify({ kind: $("watch-kind").value }),
      });
      renderWatches([watch]);
    } catch (error) {
      formError("watch-error", error.message);
    }
  });
  $("run-night").addEventListener("click", async () => {
    formError("proposal-error", "");
    $("run-night").disabled = true;
    try {
      await api("/api/night", { method: "POST", body: "{}" });
      await refreshProposals();
    } catch (error) {
      formError("proposal-error", error.message);
    } finally {
      $("run-night").disabled = false;
    }
  });

  $("phone-menu").addEventListener("click", () => {
    const rail = $("rail");
    const open = rail.classList.toggle("is-open");
    $("phone-menu").textContent = open ? "Close" : "Bots";
  });
  $("token-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    formError("token-error", "");
    try {
      sessionStorage.setItem("easyagent.token", $("token-input").value.trim());
    } catch {
      formError("token-error", "This browser blocked storage for the token.");
      return;
    }
    try {
      await api("/api/health");
    } catch (error) {
      sessionStorage.removeItem("easyagent.token");
      formError("token-error", error.status === 401 ? "That token was refused." : error.message);
      return;
    }
    $("token-input").value = "";
    show($("token-gate"), false);
    show($("app"), true);
    await loadApp();
  });
}

async function boot() {
  wire();
  captureTokenFromUrl();
  await loadFaceTemplate();
  try {
    await api("/api/health");
  } catch (error) {
    if (error.status === 401) {
      show($("app"), false);
      show($("token-gate"), true);
      $("token-input").focus();
      return;
    }
    show($("app"), true);
    if (error.offline) {
      offlineStage(error.message);
      return;
    }
    $("empty-stage").replaceChildren(
      mascotImg(),
      el("p", { class: "eyebrow" }, ["Could not load"]),
      el("h1", {}, ["The data directory did not load."]),
      el("p", { class: "tagline" }, ["AI agents, made easy."]),
      el("p", { class: "lede" }, [error.message]),
    );
    return;
  }
  await loadApp();
}

async function loadApp() {
  try {
    const health = await api("/api/health");
    const about = $("about-version");
    if (about && health && health.version) {
      about.textContent = `Version ${health.version}. A local harness for any OpenAI-compatible model. Chats stay on this computer.`;
    }
  } catch {
    // About stays without a number until /api/health answers.
  }
  watchUnread();
  try {
    await refreshEndpoints();
    await refreshBots();
    await refreshRooms();
    await refreshComputers();
    await refreshSkills();
    await refreshProposals();
    await refreshGroupProjects();
    const saved = savedSelection();
    if (saved.view === "room" && saved.roomId && state.rooms.some((room) => room.id === saved.roomId)) {
      await selectRoom(saved.roomId);
    } else if (saved.botId && state.bots.some((bot) => bot.id === saved.botId)) {
      await selectBot(saved.botId, saved.chatId);
    } else {
      renderStage();
    }
  } catch (error) {
    if (error.offline) {
      offlineStage(error.message);
      return;
    }
    $("empty-stage").replaceChildren(
      mascotImg(),
      el("p", { class: "eyebrow" }, ["Could not load"]),
      el("h1", {}, ["The data directory did not load."]),
      el("p", { class: "tagline" }, ["AI agents, made easy."]),
      el("p", { class: "lede" }, [error.message]),
    );
  }
}

async function postFiles(url, input) {
  const body = new FormData();
  for (const file of input.files || []) body.append("files", file);
  const headers = {};
  const token = sharedToken();
  if (token) headers["X-EasyAgent-Token"] = token;
  const response = await fetch(url, { method: "POST", headers, body });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : "That file was not kept.");
  }
  input.value = "";
  return data;
}

function fileRow(file, onRemove) {
  return el("li", { class: "schedule-row" }, [
    el("header", {}, [
      el("span", { class: "mono" }, [file.name]),
      el("button", { type: "button", class: "text-btn", onclick: onRemove }, ["Remove"]),
    ]),
  ]);
}

async function refreshBotProjects(botId) {
  const list = $("bot-project-list");
  if (!list || !botId) return;
  if (state.botId !== botId) return;
  state.botProjects = await api(`/api/bots/${botId}/projects`);
  if (state.botProjectId && !state.botProjects.some((item) => item.id === state.botProjectId)) {
    state.botProjectId = null;
  }
  renderBotProjects();
  if (state.botProjectId) await openBotProject(state.botProjectId);
}

function renderBotProjects() {
  const list = $("bot-project-list");
  if (!list) return;
  list.replaceChildren();
  show($("bot-project-empty"), state.botProjects.length === 0);
  for (const project of state.botProjects) {
    const count = (project.files || []).length;
    list.append(el("li", {}, [
      el("button", {
        type: "button",
        class: project.id === state.botProjectId ? "entity is-selected" : "entity",
        onclick: () => openBotProject(project.id),
      }, [
        el("strong", {}, [project.name]),
        el("small", {}, [count === 1 ? "1 file" : `${count} files`]),
      ]),
    ]));
  }
  if (!state.botProjectId) show($("bot-project-open"), false);
}

async function openBotProject(projectId) {
  if (!state.botId) return;
  const project = await api(`/api/bots/${state.botId}/projects/${projectId}`);
  if (state.botId !== project.bot_id) return;
  state.botProjectId = project.id;
  show($("bot-project-open"), true);
  $("bot-project-title").textContent = project.name;
  const files = $("bot-project-files-list");
  files.replaceChildren();
  show($("bot-project-files-empty"), (project.files || []).length === 0);
  for (const file of project.files || []) {
    files.append(fileRow(file, () => removeBotProjectFile(project.id, file.id)));
  }
  renderBotProjects();
}

async function removeBotProjectFile(projectId, fileId) {
  if (!state.botId) return;
  formError("bot-project-error", "");
  try {
    await api(`/api/bots/${state.botId}/projects/${projectId}/files/${fileId}`, { method: "DELETE" });
    await refreshBotProjects(state.botId);
  } catch (error) {
    formError("bot-project-error", error.message);
  }
}

async function refreshGroupProjects() {
  state.groupProjects = await api("/api/projects");
  if (state.groupProjectId && !state.groupProjects.some((item) => item.id === state.groupProjectId)) {
    state.groupProjectId = null;
    state.groupProject = null;
  }
  renderGroupProjectList();
  if (state.screen === "projects") renderStage();
}

function renderGroupProjectList() {
  const list = $("project-list");
  if (!list) return;
  list.replaceChildren();
  show($("project-empty"), state.groupProjects.length === 0);
  for (const project of state.groupProjects) {
    const count = (project.files || []).length;
    const bots = (project.bot_ids || []).length;
    list.append(el("li", {}, [
      el("button", {
        type: "button",
        class: "entity",
        onclick: () => openGroupProject(project.id),
      }, [
        el("strong", {}, [project.name]),
        el("small", {}, [`${count === 1 ? "1 file" : `${count} files`} · ${bots === 1 ? "1 bot" : `${bots} bots`}`]),
      ]),
    ]));
  }
}

async function openGroupProject(projectId) {
  const project = await api(`/api/projects/${projectId}`);
  state.groupProjectId = project.id;
  state.groupProject = project;
  state.screen = "projects";
  renderStage();
}

function renderOpenGroupProject() {
  const project = state.groupProject;
  if (!project || state.groupProjectId !== project.id) return;
  $("project-title").textContent = project.name;
  const files = $("project-files-list");
  files.replaceChildren();
  show($("project-files-empty"), (project.files || []).length === 0);
  for (const file of project.files || []) {
    files.append(fileRow(file, () => removeGroupProjectFile(project.id, file.id)));
  }
  const members = $("project-members");
  members.replaceChildren();
  const ids = new Set(project.bot_ids || []);
  show($("project-members-empty"), ids.size === 0);
  for (const bot of state.bots.filter((item) => ids.has(item.id))) {
    members.append(el("li", {}, [
      el("div", { class: "entity" }, [
        el("strong", {}, [bot.name]),
        el("button", {
          type: "button",
          class: "text-btn",
          onclick: () => removeGroupProjectBot(project.id, bot.id),
        }, ["Remove"]),
      ]),
    ]));
  }
  const select = $("project-add-bot");
  select.replaceChildren();
  const available = state.bots.filter((bot) => !ids.has(bot.id));
  if (available.length === 0) {
    select.append(el("option", { value: "" }, [state.bots.length ? "All bots are on this project" : "Add a bot first"]));
    select.disabled = true;
  } else {
    select.disabled = false;
    for (const bot of available) select.append(el("option", { value: bot.id }, [bot.name]));
  }
}

async function removeGroupProjectFile(projectId, fileId) {
  formError("project-error", "");
  try {
    state.groupProject = await api(`/api/projects/${projectId}/files/${fileId}`, { method: "DELETE" });
    await refreshGroupProjects();
    if (state.groupProjectId === projectId) renderStage();
  } catch (error) {
    formError("project-error", error.message);
  }
}

async function removeGroupProjectBot(projectId, botId) {
  formError("project-error", "");
  try {
    state.groupProject = await api(`/api/projects/${projectId}/bots/${botId}`, { method: "DELETE" });
    await refreshGroupProjects();
    if (state.groupProjectId === projectId) renderStage();
  } catch (error) {
    formError("project-error", error.message);
  }
}

boot();
