# EasyAgent

EasyAgent is a free local agent harness for any OpenAI-compatible model. You run it on your own computer. It gives you multiple bots, real tools, markdown memory, and a goal, plan, build, and check loop. It does not include a model, an account, or a cloud copy of your chats.

Version 0.1.0. [MIT license](LICENSE). Copyright Nathan / 1bitLabs.

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

- Port: `44721`. Override with `EASYAGENT_PORT`.
- Chats, bots, memory, and connections: a `data` folder in the directory where you started the process (`./data`). Override with `EASYAGENT_DATA`.
- Files the bot writes when you did not name a folder: this computer's app-data folder.
  - Windows: `%LOCALAPPDATA%\EasyAgent`
  - macOS: `~/Library/Application Support/EasyAgent`
  - Linux: `$XDG_DATA_HOME/EasyAgent`, or `~/.local/share/EasyAgent` when `XDG_DATA_HOME` is unset
- Bind address: `0.0.0.0` (override with `EASYAGENT_HOST`). Another machine is refused until you set `EASYAGENT_TOKEN`. Set `EASYAGENT_HOST=127.0.0.1` when this computer should be the only client.

`pip install -r requirements.txt` installs the server. On Windows it also installs the tray dependencies. The tray is optional. `EASYAGENT_TRAY=0` skips it.

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

A bot is a name, a connection, an optional model, and a token budget. Each bot has its own chats. The full transcript stays on disk. The model receives a recent stretch of that chat plus a short summary of older turns, not the whole history, once the budget is full.

Adding a bot selects it, creates its first chat, and puts the cursor in the message box. Switching bots or chats does not stop a reply that is already running in another chat, and it does not draw that reply into the chat on screen.

A room is a separate transcript. You add existing bots, and each one replies in the order you added them. One bot's failure stays on its turn. Removing a bot from a room does not delete the bot or its private chats.

One bot can ask one other existing bot to do a single task. The asking chat gets a short result. The full reply is a new chat on the bot that did the work. A bot cannot ask itself.

### Connections and the queue

Each connection has a limit, **At once**, for how many replies may use it together. The default is 1. You can set it from 1 to 32. Extra chats wait in a first-in line. The waiting chat says it is queued, names the connection and who is using it, and shows how long it has waited. It starts on its own when a slot is free. Stop while it is waiting leaves the line.

A server that disconnects without a response, or resets the connection, is tried once more before the chat says Stopped. The slot is held only during a model request, not while a tool is running.

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

A printed tool call is not the answer. The call runs, and the result goes back to the model. One send keeps going while the work is still unfinished. The turn stops when it is stuck: the same tool with the same arguments and nothing new, a real blocker, a question only you can answer, or finish.

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

Signing keys are not in this repo. The installers this build produces are unsigned. Tauri's updater is not configured. The window does not check for updates. [desktop/BUILD.md](desktop/BUILD.md) has the same commands and the notes on signing.

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

## Phone and relay

A phone on the same network uses this same page. Transcripts stay in `./data` on the computer that runs EasyAgent.

```bash
export EASYAGENT_TOKEN='replace-this-with-a-long-random-string'
python -m easyagent
```

Leave `EASYAGENT_HOST` unset so the process listens on `0.0.0.0`. The log prints the phone URL. Open that URL on the phone and type the token. `http://127.0.0.1:44721` on the computer itself does not ask for it. If `EASYAGENT_TOKEN` is unset, a phone is refused. Do not put the token in the URL.

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
| `data/endpoints.json` | Connection names, base URLs, optional keys, optional model ids, and the at-once limit |
| `data/bots/<id>/bot.json` | Bot name, connection, optional model, token budget |
| `data/bots/<id>/chats/<id>.json` | Full transcript, summary, and how far the summary covers |
| `data/bots/<id>/chats/<id>/files/` | A file or picture attached in that chat |
| `data/bots/<id>/notes/MEMORY.md` | Standing memory in markdown |
| `data/bots/<id>/notes/USER.md` | Notes about the person, in markdown |
| `data/bots/<id>/memory/` | Topic files. The index only names them |
| `data/rooms/<id>.json` | One room: member bots and its own transcript |
| `data/bots/<id>/schedules.json` | That bot's schedules |
| `data/bots/<id>/job-log.json` | Results appended when a schedule fires |
| `data/skills/*.md` | Skills |
| `data/DIRECTION.md` | Read on every turn |
| `data/unread.json` | How far you have read. Not a transcript |
| `data/computers.json` | Saved computers. Passwords and keys are sealed |

`data/` is local. Do not commit it. Deleting `unread.json` only makes existing replies look unread again.

## Tests

```bash
python -m pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how changes are reviewed.

## License

[MIT](LICENSE). Copyright (c) 2026 Nathan / 1bitLabs.

I built this for myself, and I'm sharing it free. There is no hosted service behind it. You run the process, you pick the model, and the chats stay on your computer.
