export function localWhen(value: string | undefined, zone?: string): string {
  const raw = (value || "").trim();
  if (!raw) return "undated";
  const parsed = Date.parse(raw);
  if (Number.isNaN(parsed)) return raw;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: zone,
  }).format(new Date(parsed));
}
