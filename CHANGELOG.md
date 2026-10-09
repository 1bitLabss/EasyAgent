# Changelog

## 0.3.5

- The reaction picker (thumbs up, thumbs down, heart, eyes) stays out of the way until you hover a message, focus it from the keyboard, or tap it. A reaction that was actually saved stays on the message as a small pill that names who placed it.

## 0.3.4

- A long chat no longer remounts its rows while it sits idle. The page draws every message the server already sent (the server still pages at 80, and Earlier messages still loads the rest) instead of guessing a 72px row and sliding that window on every scroll. The scroller opts out of overflow anchoring, and sticking to the bottom only runs when a new message arrives.

## 0.3.3

- The desktop window opens the page the local server is serving, including when `EASYAGENT_PORT` is not the default. The page bundled into the app only redirects there, so a UI fix reaches Windows without a new installer. The installer version is 0.3.3 so the desktop installers workflow can build that shell.
- The tray asks `/api/unread` every 15 seconds. It no longer opens a new connection every 2 seconds.
- An open chat checks for an approval card every 5 seconds while a reply is running. It stops when that run is idle, and it pauses while the window is hidden.

## 0.3.2

- A finished reply stops the chat transcript poll. The page no longer asks for `?window=80` every couple of seconds after the answer is on screen, and it does not mark the chat read again on every refresh.
- Unread is one request per window, at least eight seconds apart, and a hidden window does not ask. Sidebar tiles read that same result.
- The bubbles stay mounted when a refresh repeats the same transcript, so the text does not flash and the typing box keeps focus.

## 0.3.1

- A risky tool call waits for you. The chat shows an approval card with the exact command or path, the rule that fired, and Approve once, Deny, and, when it is safe to offer, Always allow this exact command for this bot. Deny and an expired card are final. The bot does not retry that action.
- Careful is the default for each bot. Normal allows replacing a file inside the workspace. Advanced asks you to type the bot's name, and a blocked action stays blocked unless you unlock that one rule. An unlocked rule still waits for a yes.
- Wiping a disk, deleting a profile root, turning off the firewall or Defender, dumping credentials, reading browser cookies or saved passwords, piping a download into a shell, a fork bomb, and editing the guardrails are blocked even with approval.
- An approved delete on this computer goes to EasyAgent Trash. An overwrite keeps a snapshot first. Settings lists the audit log and can restore either one.
- Pages, files, and tool output are marked as data. A command copied out of that text waits for you. A lesson or a nightly proposal that would weaken the guardrails is rejected.
- This is the guardrail release that was planned as 0.2.4. The phone app had already shipped as 0.3.0, so the version moves forward to 0.3.1 instead of back.

## 0.3.0

- EasyAgent installs on an iPhone or an Android phone from the page you already use. The icon is the locked mascot face. On a phone the faces sit in a bar at the bottom, the message box stays above the keyboard, and the Thinking box still stops scrolling at a short height.
- On an iPhone, the Camera app reads the pairing code and Safari opens EasyAgent. Add to Home Screen from that page keeps the pairing token, including the separate storage the home-screen icon uses. The page stays under the notch, the keyboard does not cover the message box, and tapping the box does not zoom the page.
- The server listens on this computer only, until you turn on Phone access in Settings. It then also listens on the home network. Settings shows a QR code. Scanning it once stores a pairing token on the phone, and you can revoke that phone later. An address outside the home network is refused. The API does not answer a phone that has no token.
- Windows Defender Firewall needs an inbound rule for TCP port 44721 from the local subnet. Settings explains that and can add the rule after you agree. `EASYAGENT_FIREWALL=1` is the same agreement from the launcher.
- A Tauri v2 iOS and Android shell is documented in desktop/MOBILE.md. It is not required for the home-screen app. It loads this same page and pairs the same way.

## 0.2.3

- Learning can propose a candidate on llama.cpp even when the address and the model name do not contain the word llama. EasyAgent asks the server what it is, and a grammar error counts too. A learning proposal, the checker, the nightly pass, the rolling summary, and eval grading do not send the tool list. If a schema is rejected, the call is tried again as plain JSON, and a JSON object is still read when it sits inside a think block or a code fence. A correction, a thumbs-down, and the nightly pass each leave a candidate waiting, and a check that does not pass still rejects it.
- The nightly pass tells the model today's date and the timezone, and it shows the pipe-row format with an example. It does not send tools. An explicit promise such as "I will check the boiler tomorrow" is recorded. When a chat is using the only slot, the nightly pass and the other background calls step aside and try again later instead of waiting until they time out.
- A memory line is scrubbed with the same secret scrub as a lesson and a note.
- Thinking text is kept as the model streamed it. Newlines and spaces stay, and a word is not split by an inserted space. The reply's words are sent as they arrive.
- The bot's name sits above the conversation. An empty thinking box is not shown, and a retry stays in the one box that is already open. A turn with no words does not leave an empty bubble. Reactions stay visible, and the tooltip says you or the bot's name.
- A note title is scrubbed before it is shortened, so a cut key does not leave a fragment such as sk-proj. A prediction records what the bot expected. The person's own message is not filed as the expectation, and a reaction with no words is not a prediction.
- Rolling back the last change consumes that snapshot. Another press says there is nothing left when the ledger has already been used, and the button stays off. A waiting candidate has Reject. Rejecting it does not install the skill.
- The notes snapshot time is shown on your clock. Bot colors stay off the colors used for waiting, reconnecting, thinking, a tool, and a halt. Waiting and reconnecting are different colors. A new run clears the previous stop reason.

## 0.2.2

- Thinking is a box above the reply. It stays open and scrolls on its own while the model is reasoning, then folds to one line that you can open again. A check note stays in that same box.
- When you already have a bot, EasyAgent opens that bot's chat. It remembers the last one you used, and otherwise opens the first. Add a bot is only there when you have none.
- The list of transcripts that can be removed shows the time on your clock.
- A secret with hyphens, such as an sk-test key, is scrubbed from the eight notes, the rolling summary, and lessons. The same scrubber catches sk-proj, sk-ant, ghp_, github_pat_, Slack xox tokens, AKIA keys, and Bearer tokens.
- A revised check, a correction, a thumbs-down, and the nightly pass can propose a learning candidate. It stays a candidate until the replay or the check passes.
- A finished answer that mentions a later check is kept. Only a reply that is just an announcement is asked to call the tool.
- A turn that only reacts, or reacts and says a short word, finishes without an empty-reply error.
- A reaction the bot placed is labeled as the bot's. A reaction you placed stays yours.
- The nightly notes and the rolling summary wait several minutes for a local model, and they still use the normal retry budget.
- Each bot gets a different face color when another bot already has that color.
- Eval tasks that only checked exact wording now grade the meaning. Checks that a tool ran, a file was written, or a marker was returned stay exact.

## 0.2.1

- A version tag publishes a GitHub Release and attaches the Windows `.exe` and `.msi`, the macOS `.dmg`, and the Linux `.deb`, AppImage, and `.rpm`. The installers are taken from `desktop/target`, which is where the cargo workspace writes them. The notes on that release are this section. The macOS supervisor build no longer calls the Linux-only `prctl` death signal. About reads the version from the app.

## 0.2.0

- Unsigned installers for Windows (NSIS and MSI), macOS (one universal disk image), and Linux (deb, AppImage, and rpm). The desktop window loads this same local page. Closing the window hides it, and Quit in the tray is what exits.

## Changed

- While a bot is idle, overnight by default, it updates eight notes in its own folder: mistakes, promises, unknowns, predictions, habits, a playbook, a map of the setup, and dreams. It uses the model that bot is already connected to. A running chat skips the pass. Notes you wrote are left alone. Secrets are not written. Digested transcripts older than 30 days can be removed after the notes and the search index have been checked, and the bot's settings show what would go. Pruning can be turned off, or transcripts can be kept forever. The Learning panel lists the eight files and what changed last night, and can roll a snapshot back.
- Each bot has one conversation that stays open. The page opens that chat, loads older messages when you scroll up, and does not ask you to start a new chat. Start fresh, in the bot's settings, clears only what the model sees. Delete chat still removes one transcript. A long history stays inside the token budget: a rolling summary, the recent turns, and a few earlier passages found with BM25, or with embeddings when the connection supports them. Nothing already saved is deleted to make room.
- Bot names in the top pill, the rail tooltip, and the empty-state tagline use Silkscreen. The rest of the page stays Inter. A passed check or a thumbs-up bounces the face for a moment. A failed check, a tool error, or a thumbs-down flashes the x eyes. Reduced motion skips the bounce and the flicker.
- A bot in the rail and in the name pill is only its screen face, in that bot's color. The home mark stays the full mascot. How many messages fit is on the bot's settings, not under the chat.
- The chat is a narrow rail of screen faces and an open conversation. Bot replies sit in soft gray bubbles, yours sit in black, and a tool or a thought is a short gray line you can open. Settings, connections, and the other screens slide in from the right. On Windows the desktop window draws its own min, max, and close. On Mac the traffic lights stay the system's.
- The page is a React app. The earlier page is still at /classic, and `EASYAGENT_UI=classic` serves it at /. The desktop window loads the same page. Closing the window hides it, and the bots keep running. Quit is in the tray. The window is a single instance, remembers its size, and can open when you sign in. A notice appears when a bot finishes a reply or asks a question. The updater is present and off. Installers for Windows, Mac, and Linux are unsigned until a signing key is added.
- Typing a name to confirm a removal ignores extra spaces and letter case. The name still has to be typed. A chat list has Delete chat, which removes only that transcript. The bot and its other chats stay. The model still cannot delete a chat.
- A bot can learn from a hard turn, but a proposal stays a candidate until a check passes and a replay does not do worse. The idle pass does not run while a chat is active, and Stop ends it. Skills and memory you wrote are left alone. A learned skill that keeps failing is archived. The Learning panel shows what is waiting, what was promoted or rejected, and can roll back the last change. There is no second model.
- A drafted reply can be checked by the same model that wrote it, before it is kept. The check is on for each bot unless you turn it off in that bot's settings. A short reply with no tools is skipped. A failed check is sent back at most twice, then the chat shows checked or revised after check. Stop ends it at once. The call uses that bot's connection and waits in its At once line. There is no second model.
- An eval suite scores saved tasks against one connection. `python -m easyagent.evals run --connection <name>` uses a temporary bot and does not open your chats. `--mock` runs without a model. Rubrics are graded by that same connection and the bot's model. `--judge-model` can name a model for an offline run. There is no second connection. `compare` names tasks that passed before and fail now.
- The mascot is locked. A test pins the SHA-256 of the grid and of the two reference photos, and it fails if one of them changes. A change needs maintainer approval.
- The mascot is the measured trace of the original character, cell for cell. The face is that head without the arms and feet, and the fill inside the outline takes the bot's color. A smaller face keeps the antenna, the white line, the screen, the eyes, and the smile.
- The mascot is traced from the approved character. The pixel drawing keeps the rounded monitor, the face screen, the boxed 1, the waving arm, and the 101. A small face is used in the sidebar and the phone header, where the full drawing would be too small to read.

## Fixed

- Deleting the chat that is open leaves the pane on the next chat, or on No chat open when that bot has no chats left.
- A flaky model server does not stop the run on the first missed connection. Connect errors, a timeout before the first token, 502, 503, 504, and a busy or unavailable slot are retried with a pause that grows from 1 second to 15 seconds, for up to 3 minutes (`EASYAGENT_MODEL_RETRY_SECONDS`). The run stays up, and the face and the step show reconnecting in amber. The connection slot is free during that pause. A stream that drops after text has started is tried once more, and text already shown is replaced. Stop still stops at once. When the window runs out, the chat says Stopped and how long it retried.
- A running turn is visibly alive. The step shows a pulsing dot, a shimmer across the words, trailing dots, and the elapsed time ticking each second. Waiting or queued is amber, thinking is purple, a tool is blue, and a stop or an error is a steady red. The bot's row in the sidebar pulses while any of its chats is running. Reduced motion uses a slow fade.
- A connection has a limit of how many replies can use it at once. The default is 1, and you can change it on that connection. Extra chats wait in line. The waiting chat says Queued, names the connection and who is using it, and shows how long it has waited. It starts on its own when a slot is free. Stop while it is waiting leaves the line. A server that drops the connection is retried for the retry window before the chat says Stopped.
- A reply stays in the chat it belongs to. Switching bots, switching chats, or adding a bot does not stop another chat's run and does not draw that run into the chat on screen. Each run has its own cancel flag, working folder, and environment. Stop stops only that chat and always says why. A stop is never only "Stopped." An empty assistant line is an error you can retry. Reading a chat file retries when Windows denies the read.
- Adding a bot selects that bot, creates its first chat, and opens the message box so you can type without clicking New.
- A running turn stays visible. The chat shows a pulse, how long it has been going, and the current step in plain words, including after a reload. A turn that stops on its own says Stopped and the reason, with Retry and Continue. A quiet minute says it is still waiting on the model.
- Saving the chat retries when Windows denies the replace, and a live thinking update that still cannot be saved does not end the reply. Thinking is not written on every chunk. Sentences in the thinking text stay apart, one paragraph per step.
- On Windows the shell is PowerShell. HTTP and JSON use Invoke-RestMethod, ConvertTo-Json, and ConvertFrom-Json, and a multi-line body is a script file. A command that prints nothing says so, with its exit code.
- The chat budget is counted in tokens and defaults to 24,000 (configurable per bot and with `EASYAGENT_CONTEXT_TOKENS`). The whole recent chat is sent until it no longer fits, with no 8-turn or 2,000-character-per-message cap, and only older turns beyond the budget are summarized. The footer reports the real numbers: messages saved, how many the reply sees in full, how many were summarized, and tokens used of the budget.
- The system prompt names the bot's own files: data folder, chats folder and this chat's file, `notes/MEMORY.md` and `notes/USER.md`, memory topics, skills, and the per-OS app folder. `MEMORY.md` and `USER.md` are created on the first turn when missing. A new `history` tool lists, searches, and reads the bot's own past chats and memory.
- A failed request names the connection and the exact address it used, and a stored error line (which may name an old address) is no longer replayed to the model. Each request reads the bot's connection as saved at that moment.

## Added

- Each bot has a screen-face. The color comes from the bot, and you can change it in that bot's settings. The face looks, blinks, and talks while a reply is running. The full-body mascot is the mark on an empty screen. The tagline is AI agents, made easy.
- Each bot keeps its own chats on this computer, and the model only receives a recent stretch of that chat plus a short summary of older turns.
- You can save a connection, with an optional model name and an optional key, and open it later to change the server it uses, the model name, or the key. Leaving the key blank keeps the one already saved.
- You can add a bot from the page, using a connection that already exists or creating one in the same step.
- You can rename a bot, point it at another connection, and change how much of the chat it sees. The saved chats stay as they were.
- Removing a bot requires typing its name, and there is no control that deletes every chat.
- A room is a separate transcript where each bot you add can reply. One bot's failure stays on its turn, and the others still reply.
- A schedule runs a saved prompt on a timer and appends the result to that bot's job log. It does not write a chat.
- A phone on the same network opens this page with a shared token. The messages stay on the computer that runs EasyAgent.
- Away from home, the computer dials out to a relay. The phone uses the relay and the token, the relay does not keep transcripts, and the phone says EasyAgent is offline when the computer is disconnected.
- A bot can ask for a web search that runs on this computer. If the search fails, the chat keeps your message and shows the error.
- One bot can ask one other existing bot to do a single task. The asking chat gets a short result, the other bot keeps the full reply in its own chat, and a failure does not wipe either transcript.
- Replies you have not opened are counted in the window title until you open that chat or room, and a desktop tray icon shows the same number when a tray is available.
- Direction is included in every request, and a bot can save a reusable skill from a reply.
- The bot can list, read, and write files on this computer, and run a command here. A printed tool call is not the answer. An error from a tool stays in the chat.
- You can save a Linux computer or a Windows computer from the page. The bot can run a command there, and saving one does not change chats.
- The page lists the tools in plain words, and skills you add in markdown for the bot to use.
- You can put one emoji reaction on a message. Tapping that same emoji again removes it, and the message text stays.
- When a bot needs a decision, it asks one short question and shows the choices to tap. The one you pick is saved as your reply, and you can type when none of them fit.
- A bot keeps memory lines you can change or drop from the page. Changing one line leaves the others, and it does not rewrite a skill.
- You can attach one file or picture in a chat. The bot can read it, and it can hand a file back. The chat keeps the file.
- You can ask to be told when a new message arrives or a job fails. That notice fires once, without watching the page.
- A night pass can propose a skill or a memory line. It does not install the proposal. A proposal with no concrete counterexample is dropped.
- When you send a message, EasyAgent scans saved chats for a few earlier stretches that share its words and adds them to the prompt, along with the summary and the recent tail. A message with no overlap adds nothing, and the full transcript stays on disk.
- A bot project is a named pile of files on that bot's settings. Only that bot can list and read them. The files stay on this computer, and uploading or removing one does not rewrite a chat.
- A group project is its own screen, not a room and not a chat. You name it, upload files, and choose which bots can use them. A bot that was not added cannot read them. Removing a bot from the project leaves rooms as they were.

## Updated

- This is version 0.1.0. The README is the public guide: how to run it, how to connect a model, and how to build the desktop window.
- A reaction is a signal. The emoji and which message go to the bot on the next turn, and a reaction on the latest reply gets one short answer. The bot can put one emoji back on a person's message, and the page shows it.
- EasyAgent can also open in a native window on Windows, Linux, and macOS. The window uses the same page and the same local server. If that server is already running it attaches; if not, it starts one and stops that one on quit. The browser path is unchanged.
- An empty or failed web search stays a tool result. The chat does not reply with "The search found nothing." EasyAgent tries one other query, then the model answers in its own words.
- A file with no folder named is written under this computer's app data: `%LOCALAPPDATA%\EasyAgent` on Windows, `~/Library/Application Support/EasyAgent` on macOS, and `~/.local/share/EasyAgent` on Linux. Shell advice matches that computer. A new bot starts with one example memory line and one example skill. EasyAgent is MIT licensed.
- Thinking is shown, then the reply. A stop or a dropped connection that has only Thinking says Stopped, including an older live reply left behind a newer message. Reasoning with no answer asks once more, and that turn is not saved as a blank success.
- Stop ends the reply that is still coming in. Thinking and any reply already on screen stay, the message box unlocks, and the chat says Stopped when nothing had arrived. A stream that closes midway is that same stop. Sending again still starts the new message.
- A quiet stretch while the model is thinking does not drop the stream. EasyAgent waits as long as that connection stays open.
- Send stays available while a reply is streaming. Sending again stops the model request and any command from that turn, then starts the new message. If the connection to the model drops, the error stays in the chat and Send is ready again.
- The goal note is the last sentence that names what was built and where it is. A read-back such as "the file reads back cleanly" is not saved. If the model never says that finished sentence, the goal file is left alone.
- The goal note is one sentence of what finished looks like. A line that only says the file is being written is not saved. A lasting fact the model states, such as a command that failed or a path that worked, is one memory line. Inter also loads from a public font file when the local font is missing. On Windows the tray icon shows the unread count without a DISPLAY variable, clears that count when the chat is read, and keeps updating while the window is minimized.
- A page of only gradients stays open until it has an image that returns 200, a CSS url() that returns 200, or an inline svg. The closer names the title and the path. A latest-release answer has to cite the current hit, and an older announcement is searched once more.
- A current version, release, or price is looked up before the answer. A choice ends with the model's recommendation or one question. An image or CSS address that does not return 200 keeps the page turn open. The goal note is one short sentence.
- A tool failure goes back to the model and the turn keeps going. The reply is the model's own words. An unknown computer means this computer. A finish with no answer is not the end. A plain chat with no tool call is one call. Goal, plan, and thinking notes are only sentences the model wrote.
- A saved computer keeps the username, password, and key in an encrypted vault. The page asks for them in a sign-in prompt that is not stored, and they are not written into the chat.
- Sending a message shows your own line in the chat immediately, then a Thinking row while the model works, and the reply fills in as it arrives.
- A failed send stays in the chat with the error.
- The page is a paper layout, with Inter for the interface and the chat and JetBrains Mono for logs, paths, and skills.
- The main screen is the conversation, with a thin list of bots and their chats beside it. A bot's settings, and the screens for connections, rooms, and saved computers, open on their own.
- After a tool runs, the chat shows a short line that it happened and then the bot's answer. A long listing, file bytes, and command output stay out of the message. The model still receives a bounded slice, and a question about one file is a yes or no about that file.
- The conversation shows your messages in bubbles on the right and the bot's messages in bubbles on the left, with the message field along the bottom. A bot's settings stay on that bot's own screen.
- One send keeps going until the reply answers the ask, for as many steps as the task takes. Each tool runs, the result comes back to the model, and the model continues. Saying it will check does not end the turn. A turn stops only when it is stuck: the same tool with the same arguments again, or a step that makes no progress. The chat then says it is stuck and what is still undone.
- An empty reply, or a reply that only says it will look, does not end the turn. EasyAgent runs the write, list, read, command, or web search the request needs, then asks the model again. The chat shows a short line for what ran. A file write names the file and the path. A news question gets the story, not the raw page. If a tool fails, the chat says which tool and why.
- A sentence that claims a file was created is not a write. EasyAgent runs the write tool, then checks that the file is on disk and not empty before the chat names it. If that check fails, the chat says the file was not written. A news search that comes back empty fetches the CNN page, and if that is empty too the chat says the search found nothing.
- A tool call written as a tool_call tag, or as a native tool object beside a sentence, runs. A tag that is cut off is sent back once. A timeout, a dropped connection, or a busy server is retried for the retry window. The chat does not keep the raw tag.
- A write uses the folder and the filename only. Words that are not a path stay the text of the file. The folder is created if it is missing. The chat names the file only after it is on disk, and a failed write says why.
- A command on this computer or over SSH shows its useful lines in the chat, under the short line that it ran. A search, a read, and a write do the same with what came back. If you say you see nothing, or that the file is missing, the bot shows the result it already has or tries that action again. The raw command text stays out of the chat. For top, the bubble names the busiest process and does not paste the process table.
- Memory is plain text files on this computer, not one list. Each bot has a small index that only points at topic files, and each topic file is one subject. A new line is filed with the topic it belongs with, or a new topic file is made. On a turn the bot reads the index, then only the topic files that matter. Existing lines are moved into a topic file.
- A request for a file, a page, or a picture stays open until that file is on disk and not empty. A failed write says the path and the error, and the same turn tries the write again. The chat does not end on a bare "The file was not written." Listing a folder or running a command does not finish a request for a picture.
- When a turn writes a file and the file is on disk, the chat shows it. A picture appears as the image. A text file shows a short preview, and that preview opens the file that was written. A failed write shows the error and no preview.
- The model is sent the native tools on every reply. Every tool call in one reply runs, and calls that do not depend on each other run together. A fence or a tag still runs when the model writes the tool as a sentence.
- A smashed Windows path keeps every folder up to the filename. Words that are not a path stay the text of the file.
- A request for a picture stays open when an unrelated command fails. The picture is still written.
- The page and the offline font are read from the files on disk. A Windows path check does not turn the page into a 404 or the font into a 503.
- The tray does not start when there is no display. The window title still shows the unread count.
- A long task is not cut off because time passed or because many steps ran. The turn stops when the same tool repeats with nearly the same arguments and nothing new happened, when finish is called, when a question only you can answer is asked, or when a real blocker is named.
- A request that needs a file, a command, or a judgment opens a contract, a plan, and an evidence ledger. Those are put back in front of the model on every step. A quick fact does not. The turn ends when finish is called, when a question only you can answer is asked, or when a real blocker is named. "The file was not written." is not a finish. A file is proven only from the bytes read back. The reply says which contract lines are proven.
- A request to read several notes and write one summary reads each note and writes that file. The summary path stays a file. A different path, or a paste of the path list, does not prove it.
- On Windows, ls is not run. A landing page is proven when the file is the page that was asked for. A generic sample does not prove that page. The bubble shows a short preview and a link, not the HTML source. A failed command is not left in the reply.
- A command Windows rejects does not end a page request. The turn keeps going until the page is on disk, with a short preview and the path.
- A page with no folder named is written under this computer's app data. The chat uses the name written on that file, says where it is, and that you can open it. Proven, C1, and Evidence stay out of the bubble.
- On Windows, pwd is not run. A rejected command does not end a page. The turn keeps going until the page is on disk, and the chat uses the name in that file.
- A question the model can decide does not end a simple page. The turn keeps going until the page is on disk, and the chat uses the name in that file.
- A tool result, including a write, comes back into the next model call. A reply with no tool call ends the turn. If this turn wrote a file and that file was not read back or run, a ready sentence stays hidden, the turn continues at most twice, and the answer is the file on disk. If the file on disk is still the same kind of short page — a hero and one stack of blocks, even when those blocks are not identical — reading it back does not finish the turn: the nudge includes what is on disk, and the page has to be written again. A sentence with no write is the reply. The same loop is every task.
- A repeated call that does not change the file does not end a short page. The turn keeps going and says to write the page again. A second write of a different page is progress, even at the same path. The chat uses the name on the file written after the turn kept going, says where it is, and that you can open it.
- The reply after a page is written is the heading on the file, where it is, and that you can open it. The tool log is not the reply. A shape check does not end the turn with Stuck. A later write that is shorter does not replace a fuller page already written.
- A ready sentence after one short page does not end the turn. A title, a welcome, and one stack of similar blocks stays open until the file on disk is a page with another topic. The reply is that later page.
- A task that builds or changes something states the goal, the thinking, and a short plan in the chat before any write, and saves them as markdown next to standing notes. A lookup runs when a page's contents would change the result. A question the model can decide does not stop the turn. A question only you know still shows choices. A quick chat skips this. A landing page is not done while the file is a header, a paragraph, and a stack of cards, or while it has no modern styling and no real picture in the file. The reply is the name on that file, where it is, and that you can open it.
- The goal, the thinking, the plan, and the check show in the chat as they happen. The check describes the file on disk. A styled page with a picture in the file is not a plain page. If the model server stays down after the retry window and that file is already the page, the reply is the page, not the timeout.
- A build is said in sentences: what was understood, what comes next, what the file actually has, and the step after that. Those sentences are not labeled Goal, Thinking, Plan, or Check. A CSS gradient is not a picture. When the file has no picture, a drawn picture is put in the file and the file is checked again. The turn does not stop to ask what the gap is. The tool log is not the reply. The last sentence says what the page is, where it is, and that you can open it.
- The sentences in the chat are the model's own words as it works. The harness does not write them, and a search title is not pasted into the chat. The last sentence uses the name in the page title, says where the file is, and that you can open it. A heading that already ends with a period does not get a second one.
- A page build keeps the model's running account as the turn goes: what the page needs, what was just written, what is missing, and the next change, including the line being changed. The harness does not replace that account with a status line. The last line still uses the name in the title, the path, and that you can open it.
- When a turn learns a fact that would change a later turn, the loop writes that short lesson into the bot's markdown memory. An older line that is still true stays. An outdated line the model names is replaced. Chatter is not saved. The chat says so in the model's own words.
- A command that fails, including a Unix command Windows does not recognize, comes back to the model. The turn keeps going. The chat stays the model's own words. The last sentence names the page from the title, says where the file is, and that you can open it.
- A page account keeps going while the model is still looking at the file. The rest of the page is in front of the model, not only the hero. A look-ahead does not end the turn. A memory note is written only when the model marks a lesson. Nothing lasting means no note.
- A sentence that says the model will read or check does not end the turn. The read result goes back. The next sentence is what the model found and what it does next. The closer is not pasted over that plan.
- A finish fence is not part of the chat. The last sentence uses the name in the title. A heading is not that name.
- When the model returns reasoning, on its own channel or in a think block, the chat shows it as a collapsible Thinking section above the reply. It streams as it arrives. It is not part of the answer. A reply with no separate reasoning shows nothing extra.
- The page name is the title, trimmed at a subtitle separator. A heading is not the name. The harness closer is added only when the model's own last words do not already name that title and the path.
- The chat bubble is the model's own words. A line that only says a tool ran is not the reply, and that line is not sent back as the assistant's text. The tool result comes back as a tool message. The last text after a tool call stays in the bubble. After a read comes back, the model says what it found before the turn ends. An in-page link with no matching id is a dead link. Goal, plan, and check notes are kept per chat, and the goal is a sentence the model wrote. Replies prefer plain sentences.
- A wrap-up does not end the turn while a placeholder link is still in the file, or while a write still changes the file. A placeholder link is an href of "#" or an empty href, and it is something to fix, the same as a dead link. A paragraph that reports a check result stays. A plan to check is finished when that same paragraph already says what it found. A paragraph is dropped only when it repeats an earlier one and adds nothing. When the last line only says to open the file, or is a short done-line with no title, that line is replaced by the one sentence that names the title and the path.
- A cited HTML tag in prose or in a code span stays in the reply. The page shows it as text.
- Set EASYAGENT_DEBUG_RAW=1 to save the first raw model chunks in data/debug-raw.txt. Nothing is added when the model sends no reasoning.
- Research and a decision get one breaker, the same model, and at most two defect rounds. "Looks fine" and "consider adding more detail" are not approval. A file or a command is checked from the tool output. The night pass may propose a playbook edit and does not install it. A proposal with no concrete counterexample is dropped.
