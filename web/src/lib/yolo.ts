import type { Yolo } from "@/types";

/** Seconds left from the last chat payload. The header counts down between refreshes. */
export function yoloRemaining(yolo: Yolo | null | undefined, fetchedAt: number, now: number): number {
  if (!yolo?.on) return 0;
  const base = Math.max(0, Number(yolo.remaining_seconds) || 0);
  const elapsed = Math.max(0, (now - fetchedAt) / 1000);
  return Math.max(0, base - elapsed);
}

/** `1h 12m`, `12m`, or `45s`. */
export function yoloClock(seconds: number): string {
  const left = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(left / 3600);
  const minutes = Math.floor((left % 3600) / 60);
  const secs = left % 60;
  if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  if (minutes > 0) return `${minutes}m`;
  return `${secs}s`;
}
