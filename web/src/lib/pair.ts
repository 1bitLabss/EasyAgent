const MANIFEST_TOKEN = /^[A-Za-z0-9_-]{16,128}$/;

export function readPairToken(href: string): string {
  try {
    const url = new URL(href);
    return url.searchParams.get("pair") || url.searchParams.get("token") || "";
  } catch {
    return "";
  }
}

export function isIos(userAgent: string, platform = "", maxTouchPoints = 0): boolean {
  if (/iPad|iPhone|iPod/.test(userAgent)) return true;
  return platform === "MacIntel" && maxTouchPoints > 1;
}

export function keepPairInAddress(ios: boolean, standalone: boolean): boolean {
  return ios && !standalone;
}

export function addressAfterPair(href: string, keep: boolean): string | null {
  if (!readPairToken(href) || keep) return null;
  const url = new URL(href);
  url.searchParams.delete("pair");
  url.searchParams.delete("token");
  return url.pathname + url.search + url.hash;
}

export function manifestHref(token: string): string | null {
  if (!MANIFEST_TOKEN.test(token)) return null;
  return `/manifest.webmanifest?pair=${encodeURIComponent(token)}`;
}
