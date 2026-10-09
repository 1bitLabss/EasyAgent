export function pickOpenBot(ids: string[], saved: string | null): string | null {
  if (!ids.length) return null;
  if (saved && ids.includes(saved)) return saved;
  return ids[0];
}
