/**
 * Voice replies. When a bot is known the server synthesizes the reply and we
 * play the mp3 it hands back. Older servers and offline runs have no /tts, so
 * we fall back to the browser's own speechSynthesis. One message at a time:
 * starting a message cancels whatever was speaking, and anything that ends the
 * moment (chat switch, Stop, a new message) calls stopSpeaking().
 */

import { useEffect, useSyncExternalStore } from "react";

const READ_ALOUD_KEY = "easyagent.readAloud";
const TOKEN_KEY = "easyagent.token";

let speakingKey = "";
const listeners = new Set<(key: string) => void>();

/** Bots whose /tts answered 404: stop asking, use the browser voice. */
const serverVoiceMissing = new Set<string>();

/** Bots whose server voice is proven (voices probe or a good /tts POST). */
const serverVoiceKnown = new Set<string>();

/** Bots we already probed this session: at most one voices request each. */
const voiceProbed = new Set<string>();

const capabilityListeners = new Set<() => void>();

let currentAudio: HTMLAudioElement | null = null;
let currentUrl = "";

function notify() {
  for (const fn of listeners) fn(speakingKey);
}

function finish(key: string) {
  if (speakingKey === key) {
    speakingKey = "";
    notify();
  }
}

/** Some in-app webviews ship no speechSynthesis at all; then we render nothing. */
export function speechAvailable(): boolean {
  return (
    typeof window !== "undefined" &&
    !!window.speechSynthesis &&
    typeof window.SpeechSynthesisUtterance === "function"
  );
}

/** Voice exists if the browser can speak or this bot's server voice is proven. */
export function canUseVoice(botId = ""): boolean {
  return speechAvailable() || (!!botId && serverVoiceKnown.has(botId));
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  let token = "";
  try {
    token = localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    /* a private window can refuse storage */
  }
  if (token) headers["X-EasyAgent-Token"] = token;
  return headers;
}

function notifyCapability(): void {
  for (const fn of capabilityListeners) fn();
}

/**
 * Ask once per session whether this bot has a server voice, so a webview
 * without speechSynthesis still learns it can speak. A good /tts POST counts
 * as proof too (see speakWithServer); a 404 joins serverVoiceMissing for good.
 */
export function probeServerVoice(botId: string): void {
  if (!botId || speechAvailable()) return;
  if (voiceProbed.has(botId) || serverVoiceKnown.has(botId) || serverVoiceMissing.has(botId)) return;
  voiceProbed.add(botId);
  void fetch(`/api/bots/${botId}/tts/voices`, { headers: authHeaders() })
    .then((response) => {
      if (response.ok) {
        serverVoiceKnown.add(botId);
        notifyCapability();
      } else if (response.status === 404) {
        serverVoiceMissing.add(botId);
      }
    })
    .catch(() => {
      /* offline or unreachable: stay quiet, the probe already spent its one shot */
    });
}

/** Reactive "can this bot speak" for components: browser voice or proven server
 * voice. Probes the server when the browser has no voice of its own. */
export function useVoiceAvailable(botId: string | null | undefined): boolean {
  const available = useSyncExternalStore(
    (onChange) => {
      capabilityListeners.add(onChange);
      return () => capabilityListeners.delete(onChange);
    },
    () => canUseVoice(botId || ""),
    () => speechAvailable(),
  );
  useEffect(() => {
    probeServerVoice(botId || "");
  }, [botId]);
  return available;
}

function stopAudio() {
  if (!currentAudio) return;
  const audio = currentAudio;
  const url = currentUrl;
  currentAudio = null;
  currentUrl = "";
  try {
    audio.pause();
    audio.src = "";
  } catch {
    /* the sound is over as far as we care */
  }
  if (url) {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* the browser will collect it */
    }
  }
}

function speakWithBrowser(text: string, key: string): void {
  if (!speechAvailable()) {
    finish(key);
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new window.SpeechSynthesisUtterance(text);
  utterance.onend = () => finish(key);
  utterance.onerror = () => finish(key);
  window.speechSynthesis.speak(utterance);
}

/** Ask the server for an mp3 of text. True means the server path handled it. */
async function speakWithServer(botId: string, text: string, key: string): Promise<boolean> {
  try {
    const response = await fetch(`/api/bots/${botId}/tts`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      if (response.status === 404) serverVoiceMissing.add(botId);
      return false;
    }
    serverVoiceKnown.add(botId); // a good /tts is proof enough; skip the probe
    voiceProbed.add(botId);
    const blob = await response.blob();
    if (speakingKey !== key) return true; // stopped while the server was synthesizing
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;
    currentUrl = url;
    audio.onended = () => {
      if (currentAudio === audio) {
        currentAudio = null;
        currentUrl = "";
      }
      try {
        URL.revokeObjectURL(url);
      } catch {
        /* already gone */
      }
      finish(key);
    };
    audio.onerror = () => {
      stopAudio();
      if (speakingKey === key) speakWithBrowser(text, key);
    };
    try {
      await audio.play();
    } catch {
      stopAudio();
      return false;
    }
    return true;
  } catch {
    return false;
  }
}

/** Speak `text` under `key`, cancelling any other utterance first. */
export function speakText(text: string, key: string, botId = ""): void {
  if (!text.trim()) return;
  stopAudio();
  speakingKey = key;
  notify();
  if (!botId || serverVoiceMissing.has(botId)) {
    speakWithBrowser(text, key);
    return;
  }
  void speakWithServer(botId, text, key).then((handled) => {
    if (!handled && speakingKey === key) speakWithBrowser(text, key);
  });
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
  stopAudio();
  if (speechAvailable()) window.speechSynthesis.cancel();
}

/** Tests start each case without memory of a server that lacked or had /tts. */
export function resetSpeakForTests(): void {
  serverVoiceMissing.clear();
  serverVoiceKnown.clear();
  voiceProbed.clear();
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
