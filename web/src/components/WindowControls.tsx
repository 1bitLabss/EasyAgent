import { Minus, Square, X } from "lucide-react";
import { desktopOs, windowCommand } from "@/lib/desktop";

export function WindowControls() {
  if (desktopOs() !== "windows") return null;
  return (
    <div className="absolute right-0 top-0 z-20 flex">
      <button type="button" className="flex h-9 w-11 items-center justify-center text-muted hover:bg-black/5 dark:hover:bg-white/10" aria-label="Minimize" onClick={() => void windowCommand("minimize")}>
        <Minus className="h-3.5 w-3.5" />
      </button>
      <button type="button" className="flex h-9 w-11 items-center justify-center text-muted hover:bg-black/5 dark:hover:bg-white/10" aria-label="Maximize" onClick={() => void windowCommand("toggle_maximize")}>
        <Square className="h-3 w-3" />
      </button>
      <button type="button" className="flex h-9 w-11 items-center justify-center text-muted hover:bg-[#e81123] hover:text-white" aria-label="Close" onClick={() => void windowCommand("close")}>
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
