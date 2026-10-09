# EasyAgent on a phone

The installable app for home Wi-Fi is the page itself. On the computer, open Settings, turn on Phone access, and scan the QR code. iPhone: Safari, Share, Add to Home Screen. Android: Chrome, Install app or Add to Home screen when the page offers it. The icon is the locked mascot face. Chats stay on the computer.

Android Chrome only offers Install app for a secure page. This home address is `http://` on the LAN, which is what the QR uses. Add to Home Screen still works. The manifest and the service worker are already on the page for a secure context.

## Tauri v2 shell (scaffold)

This is not required for the home-screen app. It is the same UI in a Tauri window, pairing the same way: the webview opens `http://<lan-ip>:44721/?pair=<token>` from the QR. The token is stored by that page. The phone does not run the Python server and does not keep the transcripts.

`desktop/src-tauri/src/mobile.rs` is compiled only for `ios` and `android`. Desktop `cargo test` does not build it. The tray, single-instance, autostart, and updater plugins in `src/lib.rs` are desktop-only. A mobile build should leave those plugins out.

From `desktop/`, after the Android SDK or Xcode is installed:

```bash
npm install
npx tauri android init
npx tauri ios init
npx tauri android dev
npx tauri ios dev
npx tauri android build
npx tauri ios build
```

`android init` and `ios init` write `src-tauri/gen/`. Those folders are local build output. Point the webview at the computer's LAN address from Settings, including `?pair=`. Do not bundle a second copy of the chats on the phone.

Rust 1.90 or newer, the Tauri 2 CLI already in `desktop/package.json`, and a phone on the same Wi-Fi as Phone access. The Windows Firewall rule, when you are pairing from a Windows computer, is the inbound TCP 44721 rule described in Settings.
