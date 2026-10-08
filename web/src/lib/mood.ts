export type Mood = "glad" | "sad";

type Hit = { botId: string; mood: Mood; until: number };

const HOLD_MS = 600;
let hit: Hit | null = null;
const listeners = new Set<() => void>();

export function moodForBadge(badge: string): Mood | null {
  if (badge === "checked") return "glad";
  if (badge === "revised") return "sad";
  return null;
}

export function moodForReaction(emoji: string): Mood {
  return emoji === "👎" ? "sad" : "glad";
}

export function faceMood(botId: string): Mood | null {
  if (!hit || hit.botId !== botId || hit.until <= Date.now()) return null;
  return hit.mood;
}

export function subscribeMood(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function pokeFace(botId: string, mood: Mood) {
  if (!botId) return;
  const until = Date.now() + HOLD_MS;
  hit = { botId, mood, until };
  listeners.forEach((listener) => listener());
  window.setTimeout(() => {
    if (hit && hit.until === until) {
      hit = null;
      listeners.forEach((listener) => listener());
    }
  }, HOLD_MS + 20);
}
