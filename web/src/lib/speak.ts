/**
 * Voice replies over the browser's own speechSynthesis. One utterance at a
 * time: starting a message cancels whatever was speaking, and anything that
 * ends the moment (chat switch, Stop, a new message) calls stopSpeaking().
 */

const READ_ALOUD_KEY = "easyagent.readAloud";

let speakingKey = "";
const listeners = new Set<(key: string) => void>();

function notify() {
  for (const fn of listeners) fn(speakingKey);
}

/** Some in-app webviews ship no speechSynthesis at all; then we render nothing. */
export function speechAvailable(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.speechSynthesis &&
    typeof window.SpeechSynthesisUtterance === "function"
  );
}

/** Speak `text` under `key`, cancelling any other utterance first. */
export function speakText(text: string, key: string): void {
  if (!speechAvailable() || !text.trim()) return;
  window.speechSynthesis.cancel();
  speakingKey = key;
  notify();
  const utterance = new window.SpeechSynthesisUtterance(text);
  const done = () => {
    if (speakingKey === key) {
      speakingKey = "";
      notify();
    }
  };
  utterance.onend = done;
  utterance.onerror = done;
  window.speechSynthesis.speak(utterance);
}

/** Which message (if any) is speaking right now. */
export function speakingKeyOf(): string {
  return speakingKey;
}

/** Watch for start/stop so every row's button can stay honest. */
export function onSpeakingChange(fn: (key: string) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function stopSpeaking(): void {
  const was = speakingKey !== "";
  speakingKey = "";
  if (was) notify();
  if (speechAvailable()) window.speechSynthesis.cancel();
}

/** "Read replies aloud" lives in localStorage, same as the theme choice. */
export function readAloudEnabled(): boolean {
  try {
    return localStorage.getItem(READ_ALOUD_KEY) === "on";
  } catch {
    return false;
  }
}

export function setReadAloudEnabled(on: boolean): void {
  try {
    if (on) localStorage.setItem(READ_ALOUD_KEY, "on");
    else localStorage.removeItem(READ_ALOUD_KEY);
  } catch {
    /* the choice still applies for this visit */
  }
}
