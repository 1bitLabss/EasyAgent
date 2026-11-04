# Grokbot Experience Checklist — EasyAgent audit lens

What makes Grok feel like Grok (experience, not features), and EasyAgent's status as of v0.3.49 (verified against the live app where possible):

## Speed & liveness
- [~] **First token fast, streams smoothly** — EasyAgent streams in batches (~30 paints/s) ✅; but local models make TTFT slow — Grok hides this with instant acknowledgment. EasyAgent's "Model is reading the prompt… Ns" is honest but static; Grok-style would animate it.
- [x] **Send = Stop in one button** — already done (0.3.45).
- [~] **No dead ends** — errors become actionable cards (Retry, Connection settings) ✅; but "held" messages after failures still feel like a dead end vs Grok just retrying invisibly.

## Minimal chrome
- [x] One endless chat, no chat list ✅ (matches Grok exactly).
- [~] Single composer ✅ but the `+` / mic / mode chips add weight; Grok's box is nearly bare.
- [ ] **Settings are conversational** — in Grok you ask the bot to change things. EasyAgent has screens for everything; a "just tell me to do it" path exists (routines via chat) but isn't the primary surface.
- [x] Icon rail, generous whitespace ✅.

## Personality
- [~] Mascot + pixel faces are charming ✅ but the *voice* of the app (status lines, empty states) is deliberately plain. Grok is witty; EasyAgent is deadpan. This is a brand choice — worth asking Nathan which way he wants to lean.
- [x] Reactions (👍👎❤️👀) ✅ — very Grok.

## Friction removal (the big Grok trait: zero setup)
- [ ] **First-run to first-reply in <60s** — today: add connection (needs a URL!) → add bot → chat. Grok: open and talk. A "first run" wizard or a bundled free default model would close this.
- [x] Attachments work in composer ✅ (though model can't see them yet — parity bug).
- [ ] **Voice that just works** — Web Speech API is Chrome-only and silent-fails elsewhere; Grok's voice is native-feeling everywhere.
- [~] Phone access via QR exists ✅ but is opt-in and buried in Settings; Grok's cross-device is seamless.

## Trust signals
- [x] Approvals as cards, audit trail, honesty receipts ✅ (stronger than Grok).
- [ ] **Cost/usage visibility** — Hermes has account usage tracking; EasyAgent shows none (tokens, time per reply).

## The one-line summary
EasyAgent already matches Grok's *structure* (one chat, one box, instant feel). The gap is (1) first-run friction, (2) liveliness of waiting states, (3) voice that works everywhere, (4) personality decision pending, (5) usage visibility.
