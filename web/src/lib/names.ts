/** Trim, collapse inner spaces, and ignore letter case. An empty stored name never matches. */
export function foldName(value: string): string {
  return String(value || "").trim().replace(/\s+/g, " ").toLowerCase();
}

export function namesMatch(typed: string, stored: string): boolean {
  const right = foldName(stored);
  return Boolean(right) && foldName(typed) === right;
}
