import { useEffect, useState, type ReactNode } from "react";

/** A one-second clock that re-renders only itself. */
export function Elapsed({ children }: { children: (now: number) => ReactNode }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return children(now);
}
