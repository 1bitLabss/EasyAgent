export type VisibleViewport = { height: number; offsetTop: number };

declare global {
  interface Window {
    __easyagentApplyViewport?: (vv: VisibleViewport) => void;
  }
}

function frameOf(target: HTMLElement): HTMLElement | null {
  if (target.classList.contains("app-frame")) return target;
  return target.querySelector(".app-frame");
}

export function applyVisualViewport(vv: VisibleViewport | null, target: HTMLElement) {
  if (!vv || !Number.isFinite(vv.height) || vv.height <= 0) return;
  const offset = Number.isFinite(vv.offsetTop) ? Math.max(0, Math.round(vv.offsetTop)) : 0;
  const height = Math.round(vv.height);
  target.style.setProperty("--app-height", `${height}px`);
  target.style.setProperty("--vv-offset", `${offset}px`);
  const frame = frameOf(target);
  if (!frame || frame === target) return;
  frame.style.setProperty("--app-height", `${height}px`);
  frame.style.setProperty("--vv-offset", `${offset}px`);
  frame.style.top = `${offset}px`;
  frame.style.height = `${height}px`;
}

export function installVisualViewport(target: HTMLElement = document.documentElement) {
  let pinned: VisibleViewport | null = null;
  const read = (): VisibleViewport | null => {
    if (pinned) return pinned;
    const vv = window.visualViewport;
    if (!vv) return { height: window.innerHeight, offsetTop: 0 };
    return { height: vv.height, offsetTop: vv.offsetTop };
  };
  const apply = () => applyVisualViewport(read(), target);
  apply();
  const vv = window.visualViewport;
  vv?.addEventListener("resize", apply);
  vv?.addEventListener("scroll", apply);
  const onFocus = () => {
    apply();
    window.setTimeout(apply, 60);
    window.setTimeout(apply, 320);
  };
  window.addEventListener("focusin", onFocus);
  const onScroll = () => {
    if (window.scrollX || window.scrollY) window.scrollTo(0, 0);
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.__easyagentApplyViewport = (next: VisibleViewport) => {
    pinned = next;
    applyVisualViewport(next, target);
  };
  return () => {
    pinned = null;
    vv?.removeEventListener("resize", apply);
    vv?.removeEventListener("scroll", apply);
    window.removeEventListener("focusin", onFocus);
    window.removeEventListener("scroll", onScroll);
    delete window.__easyagentApplyViewport;
  };
}
