/** The message box grows with the draft, then scrolls. Eight lines is the cap. */
export const COMPOSER_MAX_LINES = 8;

export function composerMaxPx(linePx: number, pad: number): number {
  const line = Number.isFinite(linePx) && linePx > 0 ? linePx : 23;
  const extra = Number.isFinite(pad) && pad > 0 ? pad : 0;
  return Math.ceil(line * COMPOSER_MAX_LINES + extra);
}

export function fitComposer(box: HTMLTextAreaElement) {
  const style = window.getComputedStyle(box);
  const font = parseFloat(style.fontSize) || 16;
  const parsed = parseFloat(style.lineHeight);
  const line = Number.isFinite(parsed) ? parsed : font * 1.45;
  const pad = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
  const border = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
  const max = composerMaxPx(line, pad + border);
  box.style.height = "auto";
  const full = box.scrollHeight;
  box.style.height = `${Math.min(full, max)}px`;
  box.style.overflowY = full > max + 1 ? "auto" : "hidden";
}

export function resetComposer(box: HTMLTextAreaElement) {
  box.style.height = "";
  box.style.overflowY = "hidden";
}
