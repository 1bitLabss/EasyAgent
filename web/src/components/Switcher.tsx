import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/api";
import { Face } from "@/components/Face";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useApp } from "@/store";
import type { Bot } from "@/types";

export function Switcher() {
  const open = useApp((state) => state.switcher);
  const setSwitcher = useApp((state) => state.setSwitcher);
  const selectBot = useApp((state) => state.selectBot);
  const bots = useQuery({ queryKey: ["bots"], queryFn: () => api<Bot[]>("/api/bots") });
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const rows = (bots.data || []).filter((bot) => bot.name.toLowerCase().includes(query.trim().toLowerCase()));

  useEffect(() => {
    setQuery("");
    setIndex(0);
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={setSwitcher}>
      <DialogContent>
        <DialogTitle>Switch bot</DialogTitle>
        <Input
          autoFocus
          className="mt-3"
          placeholder="Bot name"
          value={query}
          onChange={(event) => { setQuery(event.target.value); setIndex(0); }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setIndex((value) => Math.min(value + 1, Math.max(rows.length - 1, 0)));
            }
            if (event.key === "ArrowUp") {
              event.preventDefault();
              setIndex((value) => Math.max(value - 1, 0));
            }
            if (event.key === "Enter" && rows[index]) {
              selectBot(rows[index].id);
              setSwitcher(false);
            }
          }}
        />
        <ul className="mt-2 max-h-64 overflow-auto">
          {rows.map((bot, row) => (
            <li key={bot.id}>
              <button
                type="button"
                className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left ${row === index ? "bg-black/5 dark:bg-white/10" : ""}`}
                onMouseEnter={() => setIndex(row)}
                onClick={() => { selectBot(bot.id); setSwitcher(false); }}
              >
                <Face color={bot.face_color} tiny />
                {bot.name}
              </button>
            </li>
          ))}
          {rows.length === 0 ? <li className="px-2 py-2 text-sm text-muted">No bot with that name.</li> : null}
        </ul>
        <p className="mt-3 text-xs text-muted">Ctrl or Cmd K switches bots. Ctrl or Cmd N starts a new chat.</p>
      </DialogContent>
    </Dialog>
  );
}
