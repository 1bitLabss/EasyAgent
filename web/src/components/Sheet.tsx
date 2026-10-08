import { X } from "lucide-react";
import type { ReactNode } from "react";

export function Sheet({ title, onClose, children }: { title?: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className="pointer-events-none fixed inset-0 z-30 flex justify-end">
      <aside className="pointer-events-auto flex h-full w-full max-w-[440px] flex-col bg-background shadow-[-16px_0_40px_rgba(0,0,0,0.06)]">
        <header className="flex items-center justify-between px-5 py-4">
          {title ? <h2 className="text-base font-semibold">{title}</h2> : <span />}
          <button type="button" className="rounded-full p-1.5 text-muted hover:bg-black/5 dark:hover:bg-white/10" aria-label="Close" onClick={onClose}>
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </aside>
    </div>
  );
}
