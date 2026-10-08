type Internals = {
  invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>;
};

function internals(): Internals | null {
  const value = (window as Window & { __TAURI_INTERNALS__?: Internals }).__TAURI_INTERNALS__;
  return value?.invoke ? value : null;
}

export function desktopOs(): "windows" | "macos" | "linux" | null {
  if (!internals()) return null;
  const ua = navigator.userAgent;
  if (/Windows/i.test(ua)) return "windows";
  if (/Mac OS|Macintosh/i.test(ua)) return "macos";
  return "linux";
}

export function windowCommand(command: "minimize" | "toggle_maximize" | "close") {
  return internals()?.invoke(`plugin:window|${command}`, { label: "main" });
}
