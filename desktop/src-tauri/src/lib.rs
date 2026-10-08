//! Native window around the FastAPI app. The page is still http://127.0.0.1:44721.

use std::sync::Mutex;
use std::time::Duration;

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Manager, RunEvent};

use easyagent_supervisor as host;

struct OwnedServer(Mutex<Option<std::process::Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let port = host::port_from_env(std::env::var("EASYAGENT_PORT").ok().as_deref());
            let window = app
                .get_webview_window("main")
                .expect("the main window is declared in tauri.conf.json");
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
            install_tray(app, port)?;
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
    std::thread::spawn(move || loop {
        let count = host::http_get(port, "/api/unread", Duration::from_millis(700))
            .ok()
            .filter(|response| response.status == 200)
            .and_then(|response| host::unread_total(&response.body))
            .unwrap_or(0);
        let title = host::title_for(count);
        let (pixels, width, height) = host::draw_icon(count);
        let _ = paint.set_tooltip(Some(title));
        let _ = paint.set_icon(Some(Image::new_owned(pixels, width, height)));
        std::thread::sleep(Duration::from_secs(2));
    });
    Ok(())
}

fn focus(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}
