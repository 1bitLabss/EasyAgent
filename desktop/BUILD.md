# Building the EasyAgent desktop window

The desktop app is Tauri 2. It opens the existing EasyAgent page in the system webview and adds a tray icon. The FastAPI process is still the app.

Rust 1.90 or newer and Node.js are required. Tauri 2.12, which `cargo` resolves from the `2` requirement in this tree, does not compile on older rustc. `desktop/rust-toolchain.toml` pins 1.90.0 when rustup is installed. The Python package has to be installed as well when the window is the thing that starts the server: `pip install -r requirements.txt` from the repo root.

From `desktop/`:

```bash
npm install
cargo test -p easyagent-supervisor
npm run dev
npm run build
```

`cargo test -p easyagent-supervisor` does not need a display or WebKit. It checks attach versus start, the health JSON, the tray title, and the icon bitmap. `npm test` runs that same command. `npm run dev` opens the window. `npm run build` is `cargo tauri build` and writes installers under `desktop/target/release/bundle/`, because `desktop/` is the cargo workspace. On macOS, `npm run build -- --target universal-apple-darwin` builds one `.dmg` for both Apple silicon and Intel, under `desktop/target/universal-apple-darwin/release/bundle/dmg/`. The targets are NSIS and MSI on Windows, `.app` and `.dmg` on macOS, and `.deb`, AppImage, and `.rpm` on Linux.

`pytest` from the repo root includes `tests/test_desktop_contract.py`, which checks that `GET /api/health` and `GET /api/unread` still have the shape the window reads. That does not compile the window.

The window reads `EASYAGENT_PORT` (default 44721). Its security list allows `http://127.0.0.1` and `http://localhost` on any port, because the page is the one EasyAgent serves.

## Linux

```bash
sudo apt install libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev patchelf build-essential pkg-config libssl-dev file
```

`npm run build` produces a `.deb` and an AppImage, and an `.rpm` when the rpm tools are installed. Linux has no single code-signing system. Signing an AppImage with gpg is optional and is not done by this build.

## Windows

Install the Visual Studio C++ build tools, the WebView2 runtime (current Windows 11 already includes it), Rust, and Node.js. In `desktop/`, from a terminal where `python` is on `PATH`:

```bash
npm install
npm run build
```

The installer lands under `desktop/target/release/bundle/` (NSIS and MSI).

That installer is unsigned. Signing it needs an Authenticode certificate and `signtool`, wired through Tauri's Windows bundle `signCommand` or certificate thumbprint. The certificate is not in this repo.

## macOS

Install the Xcode command line tools, Rust, and Node.js. From `desktop/`, `npm run build` produces a `.app` and a `.dmg`.

That build is unsigned and not notarized. Distributing it off your own Mac needs an Apple Developer ID Application certificate, the hardened runtime, `notarytool`, and stapling. Those secrets are not in this repo.

Tauri updater signing keys are not configured. `createUpdaterArtifacts` is false, the endpoint list is empty, and the window does not check for updates. `EASYAGENT_UPDATES=1` only reaches the stub, which still refuses to install anything until a minisign public key is set in `src-tauri/tauri.conf.json`.

The window is a single instance. A second launch focuses the one that is already open. Closing the window hides it. The bots keep running. Quit, from the tray, is what exits. If this window started the server, Quit stops that process. If it attached to one that was already running, Quit leaves that process alone.

The window remembers its size. The tray icon shows the unread count. A native notice appears when a bot finishes a reply, or when that reply asks a question. About, in the desktop app, has "Open EasyAgent when I sign in."

## What the window starts

The README section **Desktop window** is the user-facing description. In short:

- `GET /api/health` returns `{"ok": true}`: attach. Quit does not stop that process.
- Nothing is listening: start one `python -m easyagent` with `EASYAGENT_TRAY=0` and `EASYAGENT_PORT` set, and stop that process when the window exits.
- Something else is listening: leave it alone and keep the startup page on screen.

## CI

`.github/workflows/desktop.yml` runs `cargo test -p easyagent-supervisor` on Ubuntu and `cargo check -p easyagent-supervisor` on macOS when the desktop crate changes. `.github/workflows/desktop-installers.yml` runs on a version tag (`v*`), builds the unsigned Windows, macOS, and Linux installers from `desktop/target`, and attaches them to a GitHub Release. The notes are the matching section of `CHANGELOG.md`. The macOS job targets `universal-apple-darwin`. Those jobs do not sign anything. Signed installers need the Windows and macOS certificates named above. If this forge does not run GitHub Actions, those files are still the commands to keep.
