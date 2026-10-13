fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["set_open_at_login", "open_at_login_enabled", "startup_state", "retry_start"]),
        ),
    )
    .expect("failed to run tauri-build");
}
