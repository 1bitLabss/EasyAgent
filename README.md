# EasyAgent

![EasyAgent mascot](assets/mascot.svg)

The pixel drawing is traced from [mascot-original.jpg](assets/mascot-original.jpg). The banner with the wordmark is [banner.jpg](assets/banner.jpg). Both are the reference for the character.

**AI agents, made easy.**

EasyAgent is a free local agent harness for any OpenAI-compatible model. You run it on your own computer. It gives you multiple bots, real tools, markdown memory, and a goal, plan, build, and check loop. It does not include a model, an account, or a cloud copy of your chats.

Version 0.3.12. [MIT license](LICENSE). Copyright Nathan / 1bitLabs.

I built this for myself, and I'm sharing it free. It is the harness I wanted on my own machine: a few bots, the model I already run, and the files on that computer.

## Quick start

You need Python 3.11 or newer. The same steps work on Windows, macOS, and Linux. From a checkout of this repo:

**Windows** (Command Prompt or PowerShell):

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m easyagent
```

**macOS and Linux**:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m easyagent
```

Open http://127.0.0.1:44721

The page is a React app. The built files are in `easyagent/ui`, so `pip install` and `python -m easyagent` serve it without Node. The earlier page is still at http://127.0.0.1:44721/classic. `EASYAGENT_UI=classic` serves that page at `/`.

To work on the page, start EasyAgent, then in `web/`:

```bash
npm install
npm run dev
```

That opens a dev server on http://127.0.0.1:44731 and proxies `/api`, `/static`, and `/classic` to port 44721. `npm test` runs the Vitest checks. `npm run build` writes `easyagent/ui` again. `npm run test:e2e` runs the browser smoke tests against a temporary data folder and a mock model. EasyAgent does not send telemetry.

- Port: `44721`. Override with `EASYAGENT_PORT`.
- Chats, bots, memory, and connections: a `data` folder in the directory where you started the process (`./data`). Override with `EASYAGENT_DATA`.
- Files the bot writes when you did not name a folder: this computer's app-data folder.
  - Windows: `%LOCALAPPDATA%\EasyAgent`
  - macOS: `~/Library/Application Support/EasyAgent`
  - Linux: `$XDG_DATA_HOME/EasyAgent`, or `~/.local/share/EasyAgent` when `XDG_DATA_HOME` is unset
- Bind address: `127.0.0.1`. Phone access in Settings also listens on this computer's LAN address. It is off until you turn it on. An address outside the home network is refused.

`pip install -r requirements.txt` installs the server. On Windows it also installs the tray dependencies. The tray is optional. `EASYAGENT_TRAY=0` skips it.

## Release check

```bash
python -m easyagent selftest all --data COPY --port PORT
```

Point `--data` at a copy of the data folder, never the live one, and pick a spare `--port`. The gate starts a server there, talks only to bots it creates (names start with `selftest-`), and prints a PASS/FAIL table with per-check timings. A JSON report is written to `selftest-all.json` in the current directory, or to `--report`. Any FAIL exits nonzero. The server, those bots, and the temp files are removed when it finishes. The long-chat probe uses headless Edge or Chromium when one is installed. Containment is a report. `--consent` does not apply ACL or profile changes.

## Connect a model

EasyAgent does not download or run a model. A connection is the base URL of a server that already speaks `POST /v1/chat/completions`. llama.cpp, Ollama, vLLM, and hosted OpenAI-compatible APIs all fit. Saving a connection does not send a message.

1. In the left rail, open **Connections** and choose **Add**.
2. Name it. The name is only a label.
3. Set the address to your server, for example `http://localhost:8080/v1`.
4. Leave the key blank unless that server asks for one. A saved key is not shown again.
5. Set **Model name** to the id that server expects, for example `your-model`, or leave it blank when the server has one model.
6. Leave **At once** at `1` unless that server can answer more than one request at a time.
7. Save, then add a bot and pick that connection.

EasyAgent calls `{address}/chat/completions`. The address should already include `/v1` when the server uses that prefix. A model typed on the bot replaces the connection's model for that bot only. **How much chat it sees** is that bot's token budget. The default is 24,000 tokens, or `EASYAGENT_CONTEXT_TOKENS` when that is set. The range is 512 to 1,000,000. Keep it under the model's context window.

## How it works

### Bots and chats

A bot is a name, a connection, an optional model, and a token budget. Each bot has one ongoing conversation. The full transcript stays on disk. See [One chat that never ends](#one-chat-that-never-ends).

Adding a bot selects it, opens its conversation, and puts the cursor in the message box. Switching bots does not stop a reply that is already running, and it does not draw that reply into the chat on screen.

### One chat that never ends

Each bot opens straight into its one conversation. There is no chat list and no New chat button. The transcript stays on this computer and grows without a hard stop. The model is never told the context is full, and a long chat does not end.

On every message the request is built from four pieces. The summary, the recent turns, and the retrieved passages stay inside the bot's token budget:

1. The bot's direction, memory, and skills.
2. A rolling summary of older turns. While the bot is idle, its own connection rewrites that summary. The summary is capped. A sentence is kept only when it cites a message id that is really in the transcript, and a fact that is not in those messages is dropped.
3. The most recent turns, in full, until the budget is used.
4. A few passages from the whole on-disk history that match the new message. Search is BM25 across every saved chat. If that bot's connection answers `POST /embeddings`, those vectors can rerank the passages. If it does not, or it does not answer in time, BM25 is the whole search. The passages are labeled `From earlier:` and include the time they were said. Search waits only a fraction of a second. The summary refresh does not hold up the reply.

The page shows the latest messages and loads older ones when you scroll up, so a long conversation stays quick.

Start fresh, in the bot's settings, clears only what the model will see next. It does not delete the transcript. Delete chat still removes one transcript when you ask it to.

Chats already on disk are not deleted. The newest one becomes the ongoing conversation. The others stay in the history search.

A room is a separate transcript. You add existing bots, and each one replies in the order you added them. One bot's failure stays on its turn. Removing a bot from a room does not delete the bot or its private chats.

One bot can ask one other existing bot to do a single task. The asking chat gets a short result. The full reply is a new chat on the bot that did the work. A bot cannot ask itself.

### Connections and the queue

Each connection has a limit, **At once**, for how many replies may use it together. The default is 1. You can set it from 1 to 32. Extra chats wait in a first-in line. The waiting chat says it is queued, names the connection and who is using it, and shows how long it has waited. It starts on its own when a slot is free. Stop while it is waiting leaves the line.

A model server that refuses the connection, times out before the first token, returns 502, 503, or 504, or says it is busy, is retried with a growing pause for up to 3 minutes. The chat stays open and shows that it is reconnecting. The slot is free during that pause, and it is held only during a model request, not while a tool is running. Stop still stops at once. A stream that drops after the reply has started is tried once more from the start of that reply. Set `EASYAGENT_MODEL_RETRY_SECONDS` to change the 3 minutes.

A failed request names the connection and the address it used. An old error line in the transcript is not sent back to the model, so a previous address cannot leak into a later turn.

### Tools

The model can use tools on this computer. The chat shows a short line that something ran, then the model's own answer. Raw listings, file bytes, and command output stay out of the bubble. The model still receives a bounded slice.

- List, read, and write files.
- Run a command. On Windows the shell is PowerShell. On Linux and macOS it is the normal shell. `ls` and `pwd` are not used on Windows.
- Search the public web from this computer, through DuckDuckGo. An empty or failed search is a tool result. EasyAgent tries one other query, then the model writes the answer. The chat does not reply with "The search found nothing."
- Read or search this bot's own saved chats and memory.
- Read and file memory.
- List or read a project you attached to that bot.
- Run a command on a saved Linux computer (SSH) or a saved Windows computer.
- Ask you to pick one of a few choices.
- Finish a turn.

A printed tool call is not the answer. The call runs, and the result goes back to the model. One send keeps going while the work is still unfinished. The turn stops when it is stuck: the same tool with the same arguments and nothing new, a real blocker, a question only you can answer, or finish. A risky call does not run until you approve it. See [Safety](#safety).

Saved computer passwords and keys are sealed in an encrypted vault. The page asks for them in a sign-in prompt. They are not written into the chat.

### Memory notes

Memory is markdown and plain text on this computer, not a hidden list.

- `notes/MEMORY.md` and `notes/USER.md` are created on the first turn when they are missing.
- Topic files hold related lines. A small index only points at those files.
- A new bot starts with one example memory line, and one example skill if you have no skills yet. Both say they are examples. Delete them when you do not want the sample.
- You can change or drop one line from the bot's settings. The other lines stay. Saving a line does not rewrite a chat.
- `data/DIRECTION.md` is included in every request. Edit it from the Direction panel.
- A skill is a markdown note under `data/skills/`. The bot can save one from a reply. The Skills panel lists them.

A night pass can propose a skill or a memory line from a recent chat. It does not install the proposal. A proposal with no concrete counterexample is dropped. An older memory line is not rewritten by that pass.

### What your bot keeps track of

While a bot is idle, and only then, it reads that day's conversation and the tool results already in the chat. It updates eight markdown notes in its own folder. The pass uses the model that bot is already connected to. There is no extra model. A running chat, or a message in the last little while, skips the pass. The idle window defaults to overnight (02:00–05:00) and can be changed. Every pass is appended to `notes/nightly-log.md`.

The notes live next to `MEMORY.md` and `USER.md`. Those two stay yours. Text you write in the eight files, outside the bot's auto block, is never rewritten. Each file has a size cap. A later pass merges duplicates. Every bot entry names the message id and the date it came from. The Learning panel lists all eight and what changed last night. Roll back one snapshot from that history.

| File | What it is for |
| --- | --- |
| `MISTAKES.md` | What went wrong, the cause, and the fix that worked. A similar task can see the matching lines. |
| `PROMISES.md` | Commitments such as "I'll check later." Open ones are raised at a natural pause, and closed when the chat shows they are done. |
| `UNKNOWNS.md` | Open questions and assumptions. A later tool result in the chat can close one. What is left is a single question, not a pile of them. |
| `PREDICTIONS.md` | Before a real task, the expected outcome and a confidence. Afterwards it scores itself. The running score decides how many times a reply is checked. |
| `HABITS.md` | Repeated patterns. A routine is only a suggestion until you approve it. Approval adds a schedule. The pass never adds one. |
| `PLAYBOOK.md` | Multi-step recipes made only from skills the replay promoted. A step that did not work is left out. |
| `WORLD.md` | Machines, addresses, models, and when they were last seen. Passwords, keys, and tokens are not written. |
| `DREAMS.md` | Ideas that might help. The best one can be mentioned. None of them run on their own. |

Only the lines that match the current message are sent, and they share the same token budget as the rest of the chat. The eight files are not pasted in whole.

Digested raw messages older than a window (30 days unless you change it) can be removed after the note and the search index for them are on disk and have been read back. The bot's settings show what would be removed before it goes. Keep forever, or turn pruning off, and nothing is removed. A message that was not digested is never removed.

### The goal loop

When you ask for something to be built or changed, the turn stays open. The model says what it understood and what it is doing next, in its own sentences. Those sentences are not labeled Goal, Thinking, Plan, or Check. A promise to look, a search title, or a short "done" line does not finish the turn.

The loop is: understand the ask, do the work with tools, read the result back, and keep going while the result is still short of the ask. A page with no real picture stays open. A placeholder link stays open. The last sentence that names what was built and where the file is becomes that chat's goal note. If the model never says that sentence, the goal file is left alone.

When the model returns reasoning on its own channel, or in a think block, the chat shows it as a collapsible Thinking section above the reply. EasyAgent does not invent reasoning. Thinking is not sent back as the next prompt.

### Run states

While a turn is active, the chat shows a colored dot, a shimmer on the step, trailing dots, and the elapsed time ticking each second.

| State | Color | What you see |
| --- | --- | --- |
| Waiting on the model, or queued | Amber | The step, and how long it has waited |
| Thinking | Purple | The model's step |
| Running a tool | Blue | Looking, searching, remembering, connecting, or running a command |
| Stopped or error | Red, steady | The reason. No pulse |
| Done | | The indicator goes away |

The bot's row in the sidebar pulses while any of its chats has an active run, including when you are looking at a different chat. If you prefer reduced motion, the pulse and shimmer become a slow fade.

Stop ends that chat's run and says why: you pressed Stop, the reply stopped before it finished, the server restarted, a new message was sent in that chat, or the stream failed. Sending again in the same chat stops the current run, then starts the new message. Sending in a different chat does not.

### Reactions

You can put one emoji on a message: 👍, 👎, ❤️, or 👀. Tapping that same emoji again removes it. The message text stays. The emoji and which message go to the bot on the next turn. A reaction on the latest reply can get one short answer. The bot can put one of those four emoji back on a message you sent.

## Safety

EasyAgent does not do something dangerous or irreversible until you say yes, in the chat, for that exact action. The rules are in [SAFETY.md](SAFETY.md).

A fixed rules list decides first. If a command does not match a rule, the same model this bot is already connected to reviews it. There is no second model. If that review cannot run, the action waits.

- Allowed without a card: reading, listing, search, a harmless command, and writing a new file in the bot's workspace. The workspace is the app-data folder, this bot's workspace folder, the working folder, any folder you named, and the system temp folder.
- Asked, with an approval card: deleting, moving, or renaming a file; replacing a file you did not just create and did not ask to replace; writing outside the workspace; installing or removing software; changing a service, a scheduled task, or a startup entry; a destructive git command; posting, uploading, sending a message, or spending money; a change on a remote computer; downloading a program.
- Blocked, even if you would have approved it: wiping a disk, deleting the disk root or a user profile, turning off the firewall or Defender, dumping credentials, reading browser cookies or saved passwords, piping a download into a shell, a fork bomb, and editing EasyAgent's own guardrails. Advanced can unlock one of those rules. It still waits for a yes. It is never automatic.

The card names the bot, shows the exact command or path, names the tier and the rule, and says why in one sentence. Approve once runs it. Deny stops it. Always allow is only for that exact command on that bot, and it is not offered for a blocked rule. If the card expires, that is a denial. A denial is final. The bot does not retry it, reword it, or reach the same result another way.

An approved delete on this computer goes to EasyAgent Trash. Settings can restore it. Before an approved overwrite, EasyAgent keeps a snapshot and Settings can restore that too. A delete on a remote computer is not moved to Trash.

Careful is the default. Normal also allows replacing a file inside the workspace, and it still keeps a snapshot. Advanced asks you to type the bot's name.

Text from a web page, a file, or a tool is marked untrusted. The bot is told that instructions inside those markers are data. A tool call that copies a command out of that text waits for you.

A lesson, a nightly proposal, a playbook, or a note cannot turn the guardrails down. A lesson that tries is rejected. One turn also stops after 48 tool calls or 20 minutes of tool time.

## Day to day

1. Start `python -m easyagent` and open http://127.0.0.1:44721.
2. Add a connection, then a bot. The first chat is already open.
3. Type in the box at the bottom and send. Your line shows immediately. The run indicator takes over until the reply is in.
4. Open another chat or another bot whenever you want. A run you left behind keeps going. Its bot row stays lit.
5. Press Stop on the chat that is running when you want that one to end. The text already on screen stays.
6. React from the message itself when a reply is useful or wrong. You do not have to send a new message for that.
7. For a page or a file, name the folder when you care where it goes. Otherwise it lands in the app-data folder above. The last line of a finished page names the title, the path, and that you can open it.
8. Open the bot's settings for its name, connection, token budget, memory, schedules, and skills. Connections, rooms, and saved computers are their own screens.
9. A schedule runs a saved prompt on a timer and appends the result to that bot's job log. It does not write a chat. A missed slot runs once on the next start. Older missed minutes are not replayed.
10. Tell me when arms one notice for a new message, or for a job that failed. It fires once.

Removing a bot asks you to type its name. Only that bot's folder is deleted. There is no control that wipes every chat.

The window title shows an unread count, `(3) EasyAgent`, until you open those chats or rooms. Your own messages are not counted. A tray icon shows the same number when a tray is available. On Windows the number is drawn on the icon. Set `EASYAGENT_TRAY=0` to skip the icon. The tab title still works.

## Desktop app

The desktop app is a Tauri 2 window around the same page. It is not a second product. Chats and tools still belong to the Python server. If something is already answering `GET /api/health` on port 44721, the window attaches and leaves that server running when you quit. If nothing is listening, the window starts `python -m easyagent` (`python3` on Linux and macOS) with `EASYAGENT_TRAY=0` and stops that process when the window exits. If the port is open but the response is not EasyAgent, the window stays on the startup page and does not launch another server.

Install the Python package from the repo root first (`pip install -r requirements.txt`), so the window can start the server. Rust 1.90 or newer and Node.js are required. `desktop/rust-toolchain.toml` pins Rust 1.90.0 when rustup is installed. `EASYAGENT_PYTHON` picks the interpreter. `EASYAGENT_CWD`, when it is an existing directory, is the server's working directory. Use the same directory, or the same `EASYAGENT_DATA`, when the window and a terminal should share chats.

Signing keys are not in this repo. The installers this build produces are unsigned. Windows gets an NSIS installer and an MSI. macOS gets a `.dmg`; `npm run build -- --target universal-apple-darwin` from `desktop/` makes that disk image for both Apple silicon and Intel. Linux gets a `.deb` and an AppImage.

The window is one instance. Closing it hides it, and the bots keep running. Quit is in the tray. A server the window started stops on Quit. A server that was already running is left alone. The window remembers its size. The tray icon carries the unread count. A notice appears when a bot finishes a reply or asks a question. In the desktop app, About has "Open EasyAgent when I sign in."

The updater plugin is wired and off. There is no signing key, `createUpdaterArtifacts` is false, and the window does not check for updates. `EASYAGENT_UPDATES=1` still does not install anything. [desktop/BUILD.md](desktop/BUILD.md) has the build commands and the notes on signing.

### Linux

Install the webview libraries, then the app:

```bash
sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev patchelf build-essential pkg-config libssl-dev file
cd desktop
npm install
npm run dev
```

`npm run dev` opens the window against the local server. To build installers:

```bash
npm run build
```

That writes a `.deb` and an AppImage under `desktop/src-tauri/target/release/bundle/`. An `.rpm` is produced when the rpm tools are installed. Linux has no single code-signing system. This build does not sign the AppImage.

### Windows

Install the Visual Studio C++ build tools, the WebView2 runtime (current Windows 11 already includes it), Rust, and Node.js. From `desktop/`, in a terminal where `python` is on `PATH`:

```bat
npm install
npm run dev
npm run build
```

The NSIS and MSI installers land under `desktop\src-tauri\target\release\bundle\`. They are unsigned.

### macOS

Install the Xcode command line tools, Rust, and Node.js. From `desktop/`:

```bash
npm install
npm run dev
npm run build
```

`npm run build` produces a `.app` and a `.dmg` under `desktop/src-tauri/target/release/bundle/`. That build is unsigned and not notarized.

`npm test` in `desktop/` runs `cargo test -p easyagent-supervisor`. It checks attach versus start, the health JSON, and the tray title. It does not need a display.

## Phone on the home Wi-Fi

The page is an installable app. On the computer, open **Phone** in the You menu, or the Phone access block in Settings, and turn it on. Settings shows a QR code for `http://<lan-ip>:44721/?pair=<token>`. Scan it once. The phone stores the token and the code changes. Revoke a phone from that same screen. `http://127.0.0.1:44721` on the computer does not ask for a token.

The server listens on `127.0.0.1` until Phone access is on. It then also listens on the LAN address. A request from outside the home network is refused, with or without a token. A LAN request without the pairing token is refused. The token is not accepted in the URL as a way to call the API. `EASYAGENT_TOKEN`, when set, is an extra token for the home network and only while Phone access is on. It is not printed.

iPhone: open the Camera app and point it at the code. Safari opens EasyAgent. From that page, Share, then Add to Home Screen. The icon is the mascot face. The home-screen app opens full screen, under the notch, and keeps the pairing token. Android: Chrome can add the page to the home screen. Chrome's Install app button needs a secure page, and this address is `http` on purpose. The manifest and the service worker are already on the page. The icon is painted from the locked face. It is not a new drawing.

On a small screen the faces sit in a bar along the bottom, the message box stays above the keyboard, and the Thinking box keeps its scroll cap. Tap targets are at least 44 pixels. A finished reply can notify the phone after you allow it in Phone access.

Windows Defender Firewall, Advanced settings, Inbound Rules: allow TCP port 44721 from the local subnet. Settings has **Add the Windows Firewall rule**, and it adds that rule only after you press it. Starting with `EASYAGENT_FIREWALL=1` is the same consent from the launcher. On any other system the button does not change a firewall.

A Tauri v2 iOS and Android shell is scaffolded in [desktop/MOBILE.md](desktop/MOBILE.md). It loads this same page and pairs the same way. The home-screen app does not need it.

## Phone and relay

Away from home, the computer dials out to the relay in `Dockerfile`. The phone has the relay URL and the token. The relay stores no transcripts. `docker-compose.yml` is that service with no volume. On your computer, set `EASYAGENT_RELAY_URL` to the relay's public URL and use the same token. If this computer is disconnected, the phone says EasyAgent is offline.

A local stand-in, not a public host:

```bash
export EASYAGENT_TOKEN='replace-this-with-a-long-random-string'
python -m easyagent.relay
```

That listens on port `44731`. Point `EASYAGENT_RELAY_URL` at `http://127.0.0.1:44731` and open that address in a second browser.

## What is stored

| Path | Contents |
| --- | --- |
| `data/endpoints.json` | Connection names, base URLs, optional model ids, and the at-once limit. API keys are not stored in this file |
| `data/secrets.db` | Encrypted API keys. The master key is in the OS keychain, or derived from a passphrase file outside this folder |
| `data/phone.json` | Phone access on or off, the current pairing code, and a hash for each paired phone |
| `data/bots/<id>/bot.json` | Bot name, connection, optional model, token budget |
| `data/bots/<id>/chats/<id>.json` | Full transcript, summary, and how far the summary covers |
| `data/bots/<id>/chats/<id>/files/` | A file or picture attached in that chat |
| `data/bots/<id>/notes/MEMORY.md` | Standing memory in markdown |
| `data/bots/<id>/notes/USER.md` | Notes about the person, in markdown |
| `data/bots/<id>/notes/MISTAKES.md` | Mistakes, causes, and fixes the bot keeps |
| `data/bots/<id>/notes/PROMISES.md` | Open and closed commitments |
| `data/bots/<id>/notes/UNKNOWNS.md` | Open questions and assumptions |
| `data/bots/<id>/notes/PREDICTIONS.md` | Expected outcomes and the calibration score |
| `data/bots/<id>/notes/HABITS.md` | Repeated patterns and suggested routines |
| `data/bots/<id>/notes/PLAYBOOK.md` | Recipes from promoted skills |
| `data/bots/<id>/notes/WORLD.md` | Machines, connections, and last seen. No secrets |
| `data/bots/<id>/notes/DREAMS.md` | Ideas. None of them run on their own |
| `data/bots/<id>/notes/nightly-log.md` | One entry for every idle pass |
| `data/bots/<id>/notes/retention.json` | Idle window, how long to keep digested transcripts, and whether pruning is on |
| `data/bots/<id>/memory/` | Topic files. The index only names them |
| `data/rooms/<id>.json` | One room: member bots and its own transcript |
| `data/bots/<id>/schedules.json` | That bot's schedules |
| `data/bots/<id>/job-log.json` | Results appended when a schedule fires |
| `data/skills/*.md` | Skills |
| `data/DIRECTION.md` | Read on every turn |
| `data/unread.json` | How far you have read. Not a transcript |
| `data/computers.json` | Saved computers. Passwords and keys are sealed |

`data/` is local. Do not commit it. Deleting `unread.json` only makes existing replies look unread again.

## How EasyAgent checks its work

After a reply is drafted, EasyAgent asks the same model to grade it. EasyAgent uses your connected model to review and learn — no extra model needed.

The grade is JSON: did the reply answer the request, did a tool that ran succeed, and does the reply claim a file line, a command output, or a search result that is not in the tool results. A short reply that did not use a tool is left alone.

If the grade fails, the problems go back to that same model. That happens at most twice. The chat shows a small badge, checked or revised after check, and the problems sit in the collapsible Thinking section. Stop ends the check at once. The call waits in that connection's At once line.

Each bot has the check on unless you turn it off in that bot's settings. There is no second model and no judge connection. A grade does not promote a file, a command, or a skill. Those still depend on what actually ran.

## How EasyAgent learns

EasyAgent uses your connected model to review and learn — no extra model needed.

After a long turn, a turn that recovered from a failure, or a thumbs-up or thumbs-down, the model may propose a skill or a short lesson. The proposal is a candidate. It names when it applies, the steps, the pitfalls, the scope, and a check: a command and the exit code you expect, a file that should exist, or a pattern in the output. On a llama.cpp server that accepts it, the proposal is constrained to that JSON shape.

A candidate is not a live skill. A linter checks that the tools it names exist, that the command parses, and that the text stays under a size cap. While the bot is idle, EasyAgent replays the task that produced the candidate, with the candidate and without it, in a temporary folder. The default is three runs each. It promotes the candidate only when the replay does not do worse and the check passes. Otherwise it drops the candidate and keeps the reason. This does not run while a chat is active, it waits in that connection's At once line, and Stop ends it. You can pause it per bot, or hold a passing candidate until you approve it.

Each skill keeps a count of uses, passes, and fails, from checks, tool results, and your reactions. The model and connection that wrote it are stored with the date. Skills you wrote stay ahead of learned ones. A learned skill that keeps failing is archived. Every change to a skill or a memory file has a backup, and the Learning panel can roll the last one back.

Notes taken during a chat wait in an inbox. The idle pass merges them into a learned skill, and it stops when the skill would pass its size cap. A lesson from a failed check is shown on the next similar try. If that try fails, the lesson expires. If it passes, the lesson is kept as a note on a learned skill. The chat shows a short line, Learned: ….

A skill or a memory line you wrote is never edited by this pass. Each bot learns on its own. A note picked up in a room, or from another bot, stays aside until you approve it or a check proves it.

Some agents let the model rewrite its own notes in the background, and nothing checks whether the change helped. Those notes can grow without a limit, contradict themselves, or lock in a fix for a failure that does not repeat. EasyAgent keeps a proposal as a candidate until a command, a file, or a replay says the change holds. A note that keeps failing is archived. A note you wrote is left alone.

## Running evals

EasyAgent uses your connected model to review and learn — no extra model needed. A grade is a proposal. File checks, command results, and the other machine checks decide whether a task passed.

```bash
python -m easyagent.evals run --connection "Home server"
python -m easyagent.evals run --mock
python -m easyagent.evals compare evals/results/one.json evals/results/two.json
```

`--connection` is the saved connection name. The runner copies that connection into a temporary folder, makes a temporary bot, and runs each task through the same tool loop as a chat. It does not open your chats or write into `data/`. Tasks live in `evals/tasks/`. Each one has a prompt and checks: a file exists or contains text, the reply matches, a tool was called, a tool result was not invented, a step limit, or a rubric.

The rubric is graded by the same connection, using that bot's model, and it waits in that connection's At once line. `--judge-model name` is an optional model name for an offline run. It does not add a connection, and the app has no separate judge setting. `--tasks id,id` runs a subset. `--mock` uses a scripted model so the runner can be tested without a server. Results go to `evals/results/<timestamp>.json` with the pass rate, each category, and the average steps, tokens, and time. `compare` names tasks that passed in the first file and fail in the second.

## Tests

```bash
python -m pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how changes are reviewed.

## License

[MIT](LICENSE). Copyright (c) 2026 Nathan / 1bitLabs.

I built this for myself, and I'm sharing it free. There is no hosted service behind it. You run the process, you pick the model, and the chats stay on your computer.
