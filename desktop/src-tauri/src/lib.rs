//! Native window around the FastAPI app. The page is still http://127.0.0.1:44721.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
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

/// What the startup page shows. The page polls `startup_state`.
#[derive(Clone)]
enum Phase {
    Starting(String),
    Ready(String),
    Failed { message: String, log: String },
}

struct Startup {
    phase: Mutex<Phase>,
    busy: AtomicBool,
    port: u16,
    smoke: Option<PathBuf>,
}

/// How long a first start may take. The first start unpacks the runtime.
const START_TIMEOUT: Duration = Duration::from_secs(150);

#[cfg(any(target_os = "ios", target_os = "android"))]
mod mobile;

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

#[tauri::command]
fn startup_state(state: tauri::State<'_, Startup>) -> serde_json::Value {
    let phase = state.phase.lock().map(|phase| phase.clone()).unwrap_or(Phase::Starting(String::new()));
    match phase {
        Phase::Starting(message) => serde_json::json!({"state": "starting", "message": message}),
        Phase::Ready(url) => serde_json::json!({"state": "ready", "url": url}),
        Phase::Failed { message, log } => serde_json::json!({"state": "failed", "message": message, "log": log}),
    }
}

#[tauri::command]
fn retry_start(app: tauri::AppHandle) {
    begin_start(&app);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(any(target_os = "ios", target_os = "android"))]
    let _phone_shell = mobile::shell_note();
    tauri::Builder::default()
        // The D-Bus name on Linux comes from the identifier unless set here, and a
        // D-Bus name segment cannot start with a digit ("1bit"), which panicked at start.
        .plugin(
            tauri_plugin_single_instance::Builder::new()
                .callback(|app, _args, _cwd| {
                    focus(app);
                })
                .dbus_id("labs.onebit.easyagent")
                .build(),
        )
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, None))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            set_open_at_login,
            open_at_login_enabled,
            startup_state,
            retry_start
        ])
        .setup(|app| {
            let port = host::port_from_env(std::env::var("EASYAGENT_PORT").ok().as_deref());
            let smoke = smoke_target(std::env::args().collect());
            let window = app
                .get_webview_window("main")
                .expect("the main window is declared in tauri.conf.json");
            #[cfg(target_os = "windows")]
            {
                let _ = window.set_decorations(false);
            }
            app.manage(OwnedServer(Mutex::new(None)));
            app.manage(Startup {
                phase: Mutex::new(Phase::Starting("Starting EasyAgent…".into())),
                busy: AtomicBool::new(false),
                port,
                smoke,
            });
            let watch = window.clone();
            let handle = app.handle().clone();
            window.on_window_event(move |event| {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    let ready = handle
                        .try_state::<Startup>()
                        .and_then(|state| state.phase.lock().ok().map(|phase| matches!(*phase, Phase::Ready(_))))
                        .unwrap_or(false);
                    if ready {
                        api.prevent_close();
                        let _ = watch.hide();
                    }
                }
            });
            install_tray(app, port)?;
            consider_updates(app.handle());
            begin_start(app.handle());
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

/// `--smoke-test <file>`: start the server the way the window does, check the
/// page and its script, write the result to that file, and exit. Used by CI on
/// a freshly installed app.
fn smoke_target(args: Vec<String>) -> Option<PathBuf> {
    let index = args.iter().position(|arg| arg == "--smoke-test")?;
    args.get(index + 1).map(PathBuf::from)
}

fn set_phase(app: &tauri::AppHandle, phase: Phase) {
    if let Some(state) = app.try_state::<Startup>() {
        if let Ok(mut slot) = state.phase.lock() {
            *slot = phase;
        }
    }
}

/// Start (or attach to) the server off the main thread. The window shows the
/// bundled startup page meanwhile, and an error with the log path if it fails.
fn begin_start(app: &tauri::AppHandle) {
    let Some(state) = app.try_state::<Startup>() else {
        return;
    };
    if state.busy.swap(true, Ordering::SeqCst) {
        return;
    }
    let port = state.port;
    let smoke = state.smoke.clone();
    set_phase(app, Phase::Starting("Starting EasyAgent…".into()));
    let app = app.clone();
    std::thread::spawn(move || {
        let log = log_path(&app);
        let outcome = launch(&app, port, &log);
        let result = match outcome {
            Ok(child) => {
                if let Some(state) = app.try_state::<OwnedServer>() {
                    if let Ok(mut slot) = state.0.lock() {
                        if let Some(mut old) = slot.take() {
                            host::terminate_child(&mut old);
                        }
                        *slot = child;
                    }
                }
                let url = host::app_url(port);
                set_phase(&app, Phase::Ready(url.clone()));
                if let Some(window) = app.get_webview_window("main") {
                    let _ = open_server_page(&window, port);
                }
                Ok(())
            }
            Err(message) => {
                set_phase(
                    &app,
                    Phase::Failed { message: message.clone(), log: log.to_string_lossy().to_string() },
                );
                Err(message)
            }
        };
        if let Some(state) = app.try_state::<Startup>() {
            state.busy.store(false, Ordering::SeqCst);
        }
        if let Some(target) = smoke {
            finish_smoke(&app, port, &log, result, &target);
        }
    });
}

fn log_path(app: &tauri::AppHandle) -> PathBuf {
    app.path()
        .app_log_dir()
        .or_else(|_| app.path().app_local_data_dir().map(|dir| dir.join("logs")))
        .unwrap_or_else(|_| std::env::temp_dir().join("EasyAgent"))
        .join("server.log")
}

fn launch(app: &tauri::AppHandle, port: u16, log: &std::path::Path) -> Result<Option<std::process::Child>, String> {
    match host::decide(host::probe_port(port)).map_err(|err| {
        format!("{err} Something else is using port {port}. Stop it, or set EASYAGENT_PORT to a free port.")
    })? {
        host::Launch::Attach => return Ok(None),
        host::Launch::Spawn => {}
    }
    let env_python = std::env::var("EASYAGENT_PYTHON").ok();
    let bundled = if env_python.as_deref().map(str::trim).filter(|text| !text.is_empty()).is_some() {
        None
    } else {
        bundled_python(app)?
    };
    let (program, is_bundled) = host::choose_python(env_python.as_deref(), bundled.as_deref());
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|err| format!("Could not find the per-user app folder: {err}"))?;
    let (cwd, data) = host::data_plan(
        std::env::var("EASYAGENT_DATA").ok().as_deref(),
        std::env::var("EASYAGENT_CWD").ok().as_deref(),
        &app_data,
    );
    let spec = host::server_launch(port, program, is_bundled, cwd, data);
    set_phase(app, Phase::Starting("Starting EasyAgent…".into()));
    let mut child = host::spawn_logged(&spec, log).map_err(|err| {
        format!(
            "Could not start EasyAgent with {} ({err}). Set EASYAGENT_PYTHON to an interpreter that has the easyagent package, or reinstall EasyAgent.",
            spec.program
        )
    })?;
    match host::wait_for_child(port, &mut child, START_TIMEOUT) {
        Ok(()) => Ok(Some(child)),
        Err(message) => {
            host::terminate_child(&mut child);
            Err(message)
        }
    }
}

/// The runtime that ships in the installer, unpacked on first start into the
/// per-user local app folder. None when this build carries no runtime (a dev build).
fn bundled_python(app: &tauri::AppHandle) -> Result<Option<PathBuf>, String> {
    let Ok(resources) = app.path().resource_dir() else {
        return Ok(None);
    };
    let archive = resources.join("runtime").join(host::runtime_archive_name());
    if !archive.is_file() {
        return Ok(None);
    }
    let base = app
        .path()
        .app_local_data_dir()
        .map_err(|err| format!("Could not find the per-user app folder: {err}"))?;
    set_phase(app, Phase::Starting("Setting up EasyAgent. The first start takes a minute…".into()));
    host::prepare_runtime(&archive, &base, &app.package_info().version.to_string()).map(Some)
}

fn finish_smoke(app: &tauri::AppHandle, port: u16, log: &std::path::Path, started: Result<(), String>, target: &std::path::Path) {
    let mut report = serde_json::json!({"port": port, "log": log.to_string_lossy()});
    let outcome = started.and_then(|_| {
        let health = host::http_get(port, "/api/health", Duration::from_secs(5))?;
        report["health"] = serde_json::Value::String(health.body.clone());
        let (page, script, js) = host::check_ui(port)?;
        report["page_bytes"] = page.into();
        report["script"] = script.into();
        report["script_bytes"] = js.into();
        // A new user's first step: add a bot with a name and no connection.
        report["added_bot"] = host::smoke_add_bot(port, "Smoke Test Bot")?.into();
        // The window itself must reach the server page.
        let window = app.get_webview_window("main").ok_or("no main window")?;
        let start = std::time::Instant::now();
        loop {
            let url = window.url().map(|url| url.to_string()).unwrap_or_default();
            if url.starts_with(&host::app_url(port)) {
                report["window_url"] = url.into();
                break;
            }
            if start.elapsed() > Duration::from_secs(30) {
                return Err(format!("the window stayed on {url}"));
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        Ok(())
    });
    let ok = outcome.is_ok();
    report["ok"] = ok.into();
    if let Err(err) = outcome {
        report["error"] = err.into();
        report["server_log"] = std::fs::read_to_string(log).unwrap_or_default().into();
    }
    let _ = std::fs::write(target, serde_json::to_string_pretty(&report).unwrap_or_default());
    if let Some(state) = app.try_state::<OwnedServer>() {
        if let Some(mut child) = state.0.lock().ok().and_then(|mut slot| slot.take()) {
            host::terminate_child(&mut child);
        }
    }
    app.exit(if ok { 0 } else { 1 });
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
            std::thread::sleep(host::UNREAD_POLL);
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

/// The window's first page is the local server. Navigate again once that
/// process is actually listening, including when `EASYAGENT_PORT` is not 44721.
/// If a Windows webview stayed on the copy bundled into the app, leave it.
fn open_server_page(window: &tauri::WebviewWindow, port: u16) -> Result<(), String> {
    let target = host::app_url(port);
    let url = target
        .parse::<tauri::Url>()
        .map_err(|err| format!("Could not open the EasyAgent page. {err}"))?;
    window
        .navigate(url)
        .map_err(|err| format!("Could not open the EasyAgent page. {err}"))?;
    let script = format!(
        "try {{ if (location.hostname !== '127.0.0.1' && location.hostname !== 'localhost') location.replace({}); }} catch (e) {{}}",
        serde_json::to_string(&target).unwrap_or_else(|_| "\"http://127.0.0.1:44721/\"".into())
    );
    let _ = window.eval(&script);
    Ok(())
}

fn focus(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}
