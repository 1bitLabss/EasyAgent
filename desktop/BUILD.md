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

`cargo test -p easyagent-supervisor` does not need a display or WebKit. It checks attach versus start, the health JSON, the tray title, and the icon bitmap. `npm test` runs that same command. `npm run dev` opens the window. `npm run build` is `cargo tauri build` and writes installers under `desktop/src-tauri/target/release/bundle/`.

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

The installer lands under `src-tauri/target/release/bundle/` (NSIS and MSI).

That installer is unsigned. Signing it needs an Authenticode certificate and `signtool`, wired through Tauri's Windows bundle `signCommand` or certificate thumbprint. The certificate is not in this repo.

## macOS

Install the Xcode command line tools, Rust, and Node.js. From `desktop/`, `npm run build` produces a `.app` and a `.dmg`.

That build is unsigned and not notarized. Distributing it off your own Mac needs an Apple Developer ID Application certificate, the hardened runtime, `notarytool`, and stapling. Those secrets are not in this repo.

Tauri updater signing keys are not configured. The window does not check for updates.

## What the window starts

The README section **Desktop window** is the user-facing description. In short:

- `GET /api/health` returns `{"ok": true}`: attach. Quit does not stop that process.
- Nothing is listening: start one `python -m easyagent` with `EASYAGENT_TRAY=0` and `EASYAGENT_PORT` set, and stop that process when the window exits.
- Something else is listening: leave it alone and keep the startup page on screen.

## CI

`.github/workflows/desktop.yml` runs `cargo test -p easyagent-supervisor` only. A full `tauri build` needs the webview packages above. Signed installers need the Windows and macOS certificates named in those sections. If this forge does not run GitHub Actions, that file is still the command to keep.
