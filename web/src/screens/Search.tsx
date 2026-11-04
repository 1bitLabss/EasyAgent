import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/api";
import { Input } from "@/components/ui/input";
import { useApp } from "@/store";
import type { ChatSearchHit } from "@/types";

export function SearchScreen() {
  const openBot = useApp((state) => state.openBot);
  const setJumpTo = useApp((state) => state.setJumpTo);
  const [query, setQuery] = useState("");
  const trimmed = query.trim();
  const search = useQuery({
    queryKey: ["chat-search", trimmed],
    enabled: trimmed.length >= 2,
    queryFn: () =>
      api<{ results: ChatSearchHit[] }>(`/api/chat-search?q=${encodeURIComponent(trimmed)}&limit=50`),
  });
  const results = search.data?.results || [];

  function open(hit: ChatSearchHit) {
    openBot(hit.bot_id, hit.chat_id);
    setJumpTo(hit.message_id);
  }

  return (
    <div className="h-full overflow-y-auto px-6 py-5">
      <p className="text-xs uppercase tracking-wide text-muted">Search</p>
      <h1 className="text-2xl font-semibold tracking-tight">Find what was said</h1>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        Searches every chat on every bot. Open a result and the chat lands on that message.
      </p>
      <form className="mt-5 max-w-xl" onSubmit={(event) => event.preventDefault()}>
        <Input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Words from any chat"
          maxLength={200}
          autoFocus
          data-testid="chat-search-input"
        />
      </form>
      {trimmed.length < 2 ? <p className="mt-4 text-sm text-muted">Type two or more letters.</p> : null}
      {search.isError ? <p className="mt-4 text-sm text-danger" role="alert">{(search.error as Error).message}</p> : null}
      {trimmed.length >= 2 && search.data && !results.length ? <p className="mt-4 text-sm text-muted">Nothing matched.</p> : null}
      <ul className="mt-4 max-w-2xl space-y-2">
        {results.map((hit) => (
          <li key={`${hit.bot_id}:${hit.chat_id}:${hit.message_id}`}>
            <button
              type="button"
              data-testid="chat-search-row"
              className="w-full rounded-md border border-border px-3 py-2 text-left text-sm hover:bg-black/5 dark:hover:bg-white/10"
              onClick={() => open(hit)}
            >
              <span className="block text-xs text-muted">
                {hit.bot_name} · {hit.chat_title}
              </span>
              <span className="mt-1 block line-clamp-3">{hit.text}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
