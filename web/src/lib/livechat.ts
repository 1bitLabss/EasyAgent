import { useQuery } from "@tanstack/react-query";
import { api, queryClient } from "@/api";
import { useApp } from "@/store";
import type { Unread } from "@/types";
import { unreadRefetchInterval } from "@/lib/window";

function chatStamp(unread: Unread, botId: string, chatId: string): string {
  const row = (unread.chats || []).find((item) => item.bot_id === botId && item.chat_id === chatId);
  const rev = (unread.revisions || []).find((item) => item.bot_id === botId && item.chat_id === chatId);
  return [row?.unread ?? 0, rev?.message_count ?? row?.message_count ?? "", rev?.updated_at ?? row?.updated_at ?? ""].join("|");
}

/** True when the open chat gained a reply, or its message count or updated time moved. */
export function openChatChanged(prev: Unread | undefined, next: Unread, botId: string, chatId: string): boolean {
  if (!prev || !botId || !chatId) return false;
  return chatStamp(prev, botId, chatId) !== chatStamp(next, botId, chatId);
}

export function noteUnread(next: Unread): void {
  const prev = queryClient.getQueryData<Unread>(["unread"]);
  const { botId, chatId } = useApp.getState();
  if (openChatChanged(prev, next, botId || "", chatId || "")) {
    void queryClient.invalidateQueries({ queryKey: ["chat", botId, chatId] });
  }
}

/** One unread poll for the window. A change in the open chat refetches that transcript. */
export function useUnreadWatch() {
  return useQuery({
    queryKey: ["unread"],
    queryFn: async () => {
      const next = await api<Unread>("/api/unread");
      noteUnread(next);
      return next;
    },
    refetchInterval: unreadRefetchInterval,
    notifyOnChangeProps: [],
  });
}
