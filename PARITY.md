# EasyAgent ↔ Hermes Parity Matrix
*Compiled 2026-11-03 from full source inventories of both codebases (Hermes @ hermes-agent, EasyAgent @ v0.3.49).*

## Legend
✅ parity · 🟡 partial (exists, weaker) · ❌ missing · ⭐ EasyAgent is stronger

## Core agent loop
| Capability | Hermes | EasyAgent |
|---|---|---|
| Terminal + background processes | ✅ | ✅ (terminal; polling via tool) |
| File tools (read/write/patch/search) | ✅ + docx/pdf extraction | ✅ (no office/pdf extraction 🟡) |
| Web search / fetch / research | ✅ + x_search | ✅ research with citations |
| Persistent Python kernel | ✅ execute_code (state survives) | 🟡 python tool is per-call, no persistent kernel |
| Browser automation | ✅ CDP + cloud + profiles + dialogs | ✅ Playwright per-bot 🟡 no dialog handling |
| Browser secrets vault | ✅ browser_vault | 🟡 vault exists, browser never fills from it |
| Todo / plan | ✅ todo_list | ✅ plan (checklist, compaction-immune) |
| Clarify questions | ✅ batched multi-question | 🟡 question = single choice list |
| Subagents / delegation | ✅ parallel + worktrees + schemas | ✅ ask_bot / delegate / helper 🟡 no parallel fan-out UI |
| MCP | ✅ consume + export as server | ✅ consume (per-tool permissions ⭐) · ❌ export |

## Memory & knowledge
| Capability | Hermes | EasyAgent |
|---|---|---|
| Curated memory | ✅ MEMORY.md/USER.md | ✅ same + direction + topic files ⭐ |
| Long-chat context | ✅ compression+checkpoints | ✅ rolling summary w/ citation-checked facts ⭐ |
| Search past chats | ✅ FTS5 + **user-facing search** | 🟡 BM25+embeddings bot-side only; **no user search UI** |
| Skills | ✅ + hub install + linter | ✅ + verified-candidate pipeline ⭐ |
| Background learning | ✅ review+capture | ✅ night pass, proposals must survive replay ⭐ |
| Profiles isolation | ✅ | 🟡 bots ≈ profiles; global direction shared |

## Media & voice
| Capability | Hermes | EasyAgent |
|---|---|---|
| Model sees images | ✅ vision_analyze + region crop | ❌ **attachments are decorative** |
| Image/video generation | ✅ fal/Kling/xAI | ❌ |
| TTS | ✅ Edge free + 4 providers | ❌ |
| STT / voice mode | ✅ whisper.cpp + wake word + voice channels | 🟡 browser Web Speech (Chrome-only) |
| File delivery in chat | ✅ MEDIA: any type | 🟡 bot file previews only |

## Automation & platforms
| Capability | Hermes | EasyAgent |
|---|---|---|
| Cron / routines | ✅ + blueprints | ✅ timezone-aware, catch-up ⭐ |
| Watches / "tell me when" | 🟡 via cron | ✅ dedicated notify ⭐ |
| Messaging platforms | ✅ 20+ (Telegram, Discord, WhatsApp, Slack, Signal, Matrix, Email, SMS, Teams…) | 🟡 Telegram + Discord + webhook |
| Remote machines | ❌ (local/backends) | ✅ SSH + WinRM sealed creds ⭐ |
| Rooms / multi-bot | 🟡 bot-mode + kanban | ✅ first-class rooms ⭐ |
| Kanban multi-agent board | ✅ 15 tools | ❌ |

## Safety & trust
| Capability | Hermes | EasyAgent |
|---|---|---|
| Approvals | ✅ smart + floors + yolo | ✅ cards + action-class grants ⭐ |
| OS-level containment | 🟡 sandbox options | ✅ bubblewrap/Landlock/sandbox-exec/AppContainer ⭐ |
| Script analysis | ❌ | ✅ scriptgate proves scripts statement-by-statement ⭐ |
| Honesty checks | 🟡 background review | ✅ deterministic receipts ⭐ |
| Evals | ✅ | ✅ machine-checked + compare ⭐ |
| Usage/cost tracking | ✅ account usage | ❌ |

## Desktop & UX
| Capability | Hermes | EasyAgent |
|---|---|---|
| Desktop shell | ✅ Electron + panes + preview + themes | ✅ Tauri + tray 🟡 no preview pane |
| GUI computer control | ✅ computer_use (cua-driver) | ❌ |
| GUI tour / onboarding | ✅ gui_tour, setup_beat | ❌ **no first-run wizard** |
| Themes | ✅ accent/mode/layout | 🟡 light/dark only |
| Phone/mobile | ✅ gateway platforms | ✅ LAN pairing + relay ⭐ simpler |
| Deferred tool catalog | ✅ tool_search (context economy) | 🟡 small-prompt culture instead (<8k chars) |

## The honest summary
EasyAgent **already beats Hermes** on: containment, script gating, verified learning, honesty receipts, evals, rooms, remote computers, routines UX.
Hermes leads on: **media (vision/TTS/generation), platform breadth, desktop GUI control, usage tracking, user-facing chat search, first-run experience.**

## Recommended build order (each = one shippable release)
1. **0.3.50 — "The bot can see"**: attachments reach the model (text inline + path for read_file) + per-connection vision toggle (image_url parts). Biggest replace-Hermes unlock. ~2-3 days.
2. **0.3.51 — "Talk to me"**: TTS via edge-tts (free, no key) + audio reply cards + STT fallback (server-side whisper.cpp option). Grokbot voice feel. ~2 days.
3. **0.3.52 — "Find anything"**: user-facing chat search (reuse BM25 index, add search UI + jump-to-message) + token/time usage line per reply and per day. ~2 days.
4. **0.3.53 — "First minute"**: first-run wizard (detect Ollama/llama.cpp automatically → create connection+bot → first reply in <60s) + animated waiting states. The Grok friction-killer. ~2-3 days.
5. **0.3.54+ backlog**: persistent python kernel, parallel delegation UI, browser→vault fill, WhatsApp/Slack bridges, image generation (fal), computer_use (biggest single feature, do last), MCP export, preview pane.
