// Adapted from Hermes desktop (MIT, Nous Research):
// apps/desktop/src/store/composer-input-history.ts

type Browse = { cursor: number; draft: string };

const sessions = new Map<string, Browse>();

export function deriveUserHistory(messages: readonly { role?: string; content?: string }[]): string[] {
  const out: string[] = [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const row = messages[index];
    if (!row || row.role !== "user") continue;
    const text = (row.content || "").trim();
    if (text) out.push(text);
  }
  return out;
}

export function isBrowsing(sessionId: string): boolean {
  const state = sessions.get(sessionId);
  return Boolean(state && state.cursor >= 0);
}

export function browseBackward(sessionId: string, current: string, history: readonly string[]): string | null {
  if (!history.length) return null;
  const state = sessions.get(sessionId);
  let cursor = 0;
  let draft = current;
  if (!state || state.cursor < 0) {
    cursor = 0;
    draft = current;
  } else if (state.cursor < history.length - 1) {
    cursor = state.cursor + 1;
    draft = state.draft;
  } else {
    return null;
  }
  sessions.set(sessionId, { cursor, draft });
  return history[cursor] || null;
}

export function browseForward(sessionId: string, history: readonly string[]): { text: string; returned: boolean } | null {
  const state = sessions.get(sessionId);
  if (!state || state.cursor < 0) return null;
  if (state.cursor === 0) {
    sessions.delete(sessionId);
    return { text: state.draft, returned: true };
  }
  const cursor = state.cursor - 1;
  sessions.set(sessionId, { ...state, cursor });
  return { text: history[cursor] || "", returned: false };
}

export function resetBrowse(sessionId: string) {
  sessions.delete(sessionId);
}

export const DRAFT_PERSIST_MS = 400;

export function draftStorageKey(botId: string, chatId: string): string {
  return `easyagent.draft.${botId}.${chatId}`;
}

export function readDraft(botId: string, chatId: string): string {
  if (!botId || !chatId) return "";
  try {
    return localStorage.getItem(draftStorageKey(botId, chatId)) || "";
  } catch {
    return "";
  }
}

export function writeDraft(botId: string, chatId: string, text: string) {
  if (!botId || !chatId) return;
  try {
    if (!text) localStorage.removeItem(draftStorageKey(botId, chatId));
    else localStorage.setItem(draftStorageKey(botId, chatId), text);
  } catch {
    /* a private window can refuse storage; the draft still sits in the box */
  }
}
