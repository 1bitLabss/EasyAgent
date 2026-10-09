export type Endpoint = {
  id: string;
  name: string;
  base_url: string;
  has_api_key: boolean;
  model: string | null;
  max_parallel: number;
};

export type Bot = {
  id: string;
  name: string;
  endpoint_id: string;
  endpoint_name: string | null;
  endpoint_base_url: string | null;
  model: string | null;
  context_tokens: number;
  face_color: string;
  face_color_set: boolean;
  check_enabled: boolean;
  learn_paused: boolean;
  learn_manual: boolean;
  safety_mode?: string;
  safety_unlocks?: string[];
};

export type Run = {
  id: string;
  status: "idle" | "running" | "stopped" | "error";
  started_at: string | null;
  last_activity_at: string | null;
  current_step: string;
  reason: string;
};

export type Attachment = {
  id: string;
  name?: string;
  media_type?: string;
  path?: string;
  excerpt?: string;
};

export type ChatMessage = {
  id?: string;
  role: string;
  content: string;
  thinking?: string;
  thought_seconds?: number;
  error?: boolean;
  choices?: string[];
  reaction?: string;
  reaction_by?: string;
  live?: boolean;
  attachment?: Attachment;
  skills_saved?: string[];
  check?: string;
  lesson?: string;
  speaker_name?: string;
  speaker?: string;
  created_at?: string;
};

export type ChatSummary = {
  id: string;
  bot_id: string;
  title: string;
  updated_at?: string;
  message_count?: number;
};

export type Chat = ChatSummary & {
  summary?: string;
  messages: ChatMessage[];
  message_count?: number;
  window_start?: number;
  fresh_from?: number;
  context?: Record<string, number>;
  run?: Run;
};

export type Room = {
  id: string;
  name: string;
  bot_ids?: string[];
  messages?: ChatMessage[];
  context?: Record<string, number>;
  updated_at?: string;
};

export type Computer = {
  id: string;
  name: string;
  kind: "linux" | "windows" | string;
  host: string;
  port?: number | null;
  has_sign_in?: boolean;
};

export type Project = {
  id: string;
  name: string;
  kind?: string;
  bot_id?: string;
  bot_ids?: string[];
  files?: { id: string; name: string }[];
};

export type Skill = { name: string; description: string; body: string };

export type Schedule = {
  id: string;
  prompt: string;
  kind: string;
  every_minutes?: number | null;
  cron?: string | null;
  paused?: boolean;
};

export type Job = {
  id?: string;
  status?: string;
  prompt?: string;
  result?: string;
  error?: string;
  started_at?: string;
  finished_at?: string;
};

export type Unread = {
  total: number;
  chats: { bot_id: string; chat_id: string; unread: number }[];
  rooms: { room_id: string; unread: number }[];
  busy?: string[];
};

export type NoteFile = {
  name: string;
  title: string;
  blurb?: string;
  entries: number;
  chars?: number;
  added?: number;
  removed?: number;
  changed?: string;
};

export type PruneRow = {
  chat_id: string;
  message_id: string;
  created_at?: string;
  preview?: string;
};

export type Learning = {
  paused: boolean;
  manual: boolean;
  waiting: { id: string; name?: string; status?: string; trigger?: string; reason?: string }[];
  promoted: { id: string; name?: string; status?: string; reason?: string }[];
  rejected: { id: string; name?: string; status?: string; reason?: string }[];
  skills: { name: string; origin?: string; uses?: number; passes?: number; fails?: number; archived?: boolean }[];
  ledger?: { id: string; kind?: string; key?: string; created_at?: string; rolled?: boolean }[];
  notes?: NoteFile[];
  last_night?: { at?: string; summary?: string; changes?: { file: string; added?: number; removed?: number }[] };
  prune?: {
    pruning: boolean;
    keep_forever: boolean;
    retain_days: number;
    pending: PruneRow[];
  };
  question?: string;
  habits?: { id: string; title?: string; text?: string; hour?: string; status?: string }[];
  dreams?: { id: string; title?: string; text?: string }[];
  calibration?: number | null;
};

export type MemoryTopic = { name: string; title?: string; count?: number };
export type MemoryLine = { id: string; text: string };
