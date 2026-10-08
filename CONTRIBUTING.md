# Contributing

Nathan reviews pull requests. A change should be small, focused, and green.

## Run it locally

Python 3.11 or newer. From a checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m easyagent
```

On Windows, create the venv with `py -m venv .venv`, activate `.venv\Scripts\activate`, and use `python -m easyagent`.

Open http://127.0.0.1:44721. The default port is 44721. Chats are written under `./data` in the directory where you started the process, or `EASYAGENT_DATA` if you set it. Files the bot writes when no folder was named go to the operating system's app-data folder. See the README.

The desktop window is optional. From `desktop/`, with Rust 1.90+ and Node.js installed:

```bash
npm install
npm run dev
```

## Tests

From the repo root, with the venv active:

```bash
python -m pytest
```

That is the check a pull request needs. Do not commit with a failing suite.

The desktop supervisor tests do not need a display:

```bash
cd desktop
npm test
```

`pytest` already checks that `GET /api/health` and `GET /api/unread` still have the shape the window reads. A full `npm run build` needs the webview libraries in [desktop/BUILD.md](desktop/BUILD.md). You do not need that for an ordinary Python change.

## Review and learning

EasyAgent uses your connected model to review and learn — no extra model needed. The checker, eval rubrics, and later lesson or skill proposals all use the model that bot is already connected to. A model grade is a proposal. Commands, files, tests, and replay decide what is kept. Do not add a second helper model or a judge-connection setting. The settings screen has the per-bot check toggle and no other model control. `--judge-model` on an eval run only names a model on that same connection. A skill proposal stays in `skills/_candidates` until a check and a replay agree. Skills and memory you wrote are not auto-edited.

## Mascot

The mascot is locked. The grid in `easyagent/mascot.py`, `assets/mascot-original.jpg`, and `assets/banner.jpg` stay as they are. A change needs maintainer approval. A test pins their SHA-256 hashes and fails if one of them moves.

## Code style

Match the file you are editing. Python uses four-space indentation and type hints where the surrounding code uses them. The page speaks in plain sentences. Do not reformat unrelated lines, and do not add a formatter or a new framework in a drive-by change.

A behavior change needs a test that fails without the change. Prefer extending a test that already covers that path.

## Pull requests

- Keep the change small and focused. One bug, or one feature, per request.
- `python -m pytest` is green.
- Say what changed and how you tested it.
- Nathan reviews the request. He may ask for a smaller diff.

## Do not include secrets or personal data

Never commit:

- API keys, tokens, passwords, private keys, or `.env` files
- `data/`, chat logs, memory files, or transcripts
- LAN addresses, home directory names, or machine names from your own setup
- `.venv/`, `__pycache__/`, `*.log`, `node_modules/`, or `src-tauri/target/`

Use placeholders in examples and tests: `http://localhost:8080/v1` and `your-model`. A test can prove that a key is stored and not returned. It should not contain a real key.
