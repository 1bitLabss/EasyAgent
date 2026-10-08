//! Native window around the FastAPI app. The page is still http://127.0.0.1:44721.

use std::sync::Mutex;
use std::time::Duration;

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent};
use tauri_plugin_autostart::MacosLauncher;
use tauri_plugin_notification::NotificationExt;

use easyagent_supervisor as host;

struct OwnedServer(Mutex<Option<std::process::Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
#[tauri::command]
fn set_open_at_login(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    use tauri_plugin_autostart::ManagerExt;
    let launch = app.autolaunch();
    if enabled {
        launch.enable()
    } else {
        launch.disable()
    }
    .map_err(|err| err.to_string())
}

#[tauri::command]
fn open_at_login_enabled(app: tauri::AppHandle) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    app.autolaunch().is_enabled().map_err(|err| err.to_string())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus(app);
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![set_open_at_login, open_at_login_enabled])
        .setup(|app| {
            let port = host::port_from_env(std::env::var("EASYAGENT_PORT").ok().as_deref());
            let window = app
                .get_webview_window("main")
                .expect("the main window is declared in tauri.conf.json");
            #[cfg(target_os = "windows")]
            {
                let _ = window.set_decorations(false);
            }
            let owned = match ensure_server(&window, port) {
                Ok(child) => child,
                Err(message) => {
                    let _ = window.set_title(&message);
                    show_status(&window, &message);
                    app.manage(OwnedServer(Mutex::new(None)));
                    return Ok(());
                }
            };
            app.manage(OwnedServer(Mutex::new(owned)));
            let watch = window.clone();
            window.on_window_event(move |event| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = watch.hide();
                }
            });
            install_tray(app, port)?;
            consider_updates(app.handle());
            let url = host::app_url(port)
                .parse::<tauri::Url>()
                .expect("the local url is valid");
            window
                .navigate(url)
                .map_err(|err| format!("Could not open the EasyAgent page. {err}"))?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("EasyAgent desktop failed to start")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app.try_state::<OwnedServer>() {
                    if let Some(mut child) = state.0.lock().ok().and_then(|mut slot| slot.take()) {
                        host::terminate_child(&mut child);
                    }
                }
            }
        });
}

fn ensure_server(window: &tauri::WebviewWindow, port: u16) -> Result<Option<std::process::Child>, String> {
    match host::decide(host::probe_port(port)).map_err(str::to_string)? {
        host::Launch::Attach => Ok(None),
        host::Launch::Spawn => {
            show_status(window, "Starting EasyAgent…");
            let mut child = host::spawn_server(port).map_err(|err| {
                format!(
                    "Could not start EasyAgent ({err}). Install it with pip, or set EASYAGENT_PYTHON to the interpreter that has the easyagent package."
                )
            })?;
            if host::wait_until_up(port, Duration::from_secs(25)) {
                Ok(Some(child))
            } else {
                host::terminate_child(&mut child);
                Err(format!(
                    "EasyAgent did not answer {} . If another program is using that port, stop it or set EASYAGENT_PORT.",
                    host::health_url(port)
                ))
            }
        }
    }
}

fn show_status(window: &tauri::WebviewWindow, message: &str) {
    let script = format!(
        "var node = document.getElementById('status'); if (node) node.textContent = {};",
        serde_json::to_string(message).unwrap_or_else(|_| "\"EasyAgent did not start.\"".into())
    );
    let window = window.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(200));
        let _ = window.eval(&script);
    });
}

fn install_tray(app: &tauri::App, port: u16) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open EasyAgent", true, None::<&str>)?;
    let browser = MenuItem::with_id(app, "browser", "Open in browser", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &browser, &quit])?;
    let (pixels, width, height) = host::draw_icon(0);
    let tray = TrayIconBuilder::with_id("easyagent")
        .tooltip("EasyAgent")
        .icon(Image::new_owned(pixels, width, height))
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id().as_ref() {
            "open" => focus(app),
            "browser" => {
                let (program, args) = host::browser_command(&host::app_url(port));
                let _ = std::process::Command::new(program).args(args).spawn();
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                focus(tray.app_handle());
            }
        })
        .build(app)?;

    let paint = tray.clone();
    let notices = app.handle().clone();
    std::thread::spawn(move || {
        let mut seen: Vec<host::UnreadChat> = Vec::new();
        let mut primed = false;
        loop {
            let response = host::http_get(port, "/api/unread", Duration::from_millis(700))
                .ok()
                .filter(|response| response.status == 200);
            let count = response.as_ref().and_then(|response| host::unread_total(&response.body)).unwrap_or(0);
            let chats = response.map(|response| host::unread_chats(&response.body)).unwrap_or_default();
            if primed {
                for chat in &chats {
                    let before = seen.iter().find(|item| item.bot_id == chat.bot_id && item.chat_id == chat.chat_id).map(|item| item.unread).unwrap_or(0);
                    if chat.unread > before {
                        let path = format!("/api/bots/{}/chats/{}", chat.bot_id, chat.chat_id);
                        let choices = host::http_get(port, &path, Duration::from_millis(700))
                            .ok()
                            .filter(|item| item.status == 200)
                            .map(|item| host::choice_count(&item.body))
                            .unwrap_or(0);
                        let _ = notices.notification().builder().title("EasyAgent").body(host::notice_line(choices)).show();
                    }
                }
            }
            seen = chats;
            primed = true;
            let title = host::title_for(count);
            let (pixels, width, height) = host::draw_icon(count);
            let _ = paint.set_tooltip(Some(title));
            let _ = paint.set_icon(Some(Image::new_owned(pixels, width, height)));
            std::thread::sleep(Duration::from_secs(2));
        }
    });
    Ok(())
}

/// The updater plugin is registered, and this is the only place that would ask it.
/// It does nothing unless EASYAGENT_UPDATES=1, and even then there is no signing key.
fn consider_updates(app: &tauri::AppHandle) {
    if !host::updates_requested(std::env::var("EASYAGENT_UPDATES").ok().as_deref()) {
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_updater::UpdaterExt;
        match app.updater() {
            Ok(updater) => {
                if let Err(err) = updater.check().await {
                    eprintln!("EasyAgent updates are not signed yet ({err}). No update was installed.");
                }
            }
            Err(err) => eprintln!("EasyAgent updates are not signed yet ({err}). No update was installed."),
        }
    });
}

fn focus(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}
