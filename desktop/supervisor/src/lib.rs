//! Decide whether the desktop window should attach to EasyAgent or start it.
//!
//! The FastAPI process is the app. This crate checks `GET /api/health` and,
//! when that is down, builds the `python -m easyagent` command. An installed
//! window carries its own Python runtime; this crate unpacks it into the
//! per-user app folder on first start.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

pub const DEFAULT_PORT: u16 = 44721;

/// Tray unread. The icon still updates while the window is hidden, but not every two seconds.
pub const UNREAD_POLL: Duration = Duration::from_secs(15);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServerCommand {
    pub program: String,
    pub args: Vec<String>,
    pub env: Vec<(String, String)>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Probe {
    /// `/api/health` answered `{"ok": true}`.
    EasyAgent,
    /// Something accepted the connection and it was not that health check.
    Other,
    /// Nothing is listening, or the connection failed.
    Down,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Launch {
    Attach,
    Spawn,
}

#[derive(Debug)]
pub struct RawResponse {
    pub status: u16,
    pub body: String,
}

pub fn port_from_env(value: Option<&str>) -> u16 {
    value
        .and_then(|text| text.trim().parse().ok())
        .filter(|port| *port > 0)
        .unwrap_or(DEFAULT_PORT)
}

pub fn app_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/")
}

pub fn health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/api/health")
}

pub fn unread_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/api/unread")
}

/// Same words as the Python tray tooltip.
pub fn title_for(count: i64) -> String {
    if count <= 0 {
        "EasyAgent".to_string()
    } else {
        format!("({count}) EasyAgent")
    }
}

/// The mark painted on the icon. Over 99 is `99+`, matching the Python tray.
pub fn icon_label(count: i64) -> String {
    if count <= 0 {
        "ea".to_string()
    } else if count > 99 {
        "99+".to_string()
    } else {
        count.to_string()
    }
}

pub fn health_is_easyagent(body: &str) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(body) else {
        return false;
    };
    value.get("ok").and_then(|item| item.as_bool()) == Some(true)
}

pub fn unread_total(body: &str) -> Option<i64> {
    let value: serde_json::Value = serde_json::from_str(body).ok()?;
    value.get("total").and_then(|item| item.as_i64())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnreadChat {
    pub bot_id: String,
    pub chat_id: String,
    pub unread: i64,
}

pub fn unread_chats(body: &str) -> Vec<UnreadChat> {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(body) else {
        return Vec::new();
    };
    let Some(rows) = value.get("chats").and_then(|item| item.as_array()) else {
        return Vec::new();
    };
    rows.iter()
        .filter_map(|row| {
            Some(UnreadChat {
                bot_id: row.get("bot_id")?.as_str()?.to_string(),
                chat_id: row.get("chat_id")?.as_str()?.to_string(),
                unread: row.get("unread")?.as_i64()?,
            })
        })
        .collect()
}

/// Choices on the latest assistant line. More than one means the bot asked a question.
pub fn choice_count(body: &str) -> usize {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(body) else {
        return 0;
    };
    let Some(last) = value.get("messages").and_then(|item| item.as_array()).and_then(|rows| rows.last()) else {
        return 0;
    };
    if last.get("role").and_then(|item| item.as_str()) != Some("assistant") {
        return 0;
    }
    last.get("choices").and_then(|item| item.as_array()).map(|rows| rows.len()).unwrap_or(0)
}

pub fn notice_line(choices: usize) -> &'static str {
    if choices > 1 {
        "A bot has a question."
    } else {
        "A bot finished a reply."
    }
}

/// Updates stay off unless this is exactly "1". Even then the window has no signing key.
pub fn updates_requested(value: Option<&str>) -> bool {
    value == Some("1")
}

pub fn decide(probe: Probe) -> Result<Launch, &'static str> {
    match probe {
        Probe::EasyAgent => Ok(Launch::Attach),
        Probe::Down => Ok(Launch::Spawn),
        Probe::Other => Err("That port is open, but it is not EasyAgent."),
    }
}

pub fn python_program() -> String {
    std::env::var("EASYAGENT_PYTHON")
        .ok()
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| {
            if cfg!(windows) {
                "python".to_string()
            } else {
                "python3".to_string()
            }
        })
}

/// The child the desktop app starts. The tray stays in the window, so Python's icon is off.
pub fn server_command(port: u16) -> ServerCommand {
    ServerCommand {
        program: python_program(),
        args: vec!["-m".into(), "easyagent".into()],
        env: vec![
            ("EASYAGENT_TRAY".into(), "0".into()),
            ("EASYAGENT_PORT".into(), port.to_string()),
        ],
    }
}

pub fn server_cwd() -> PathBuf {
    if let Ok(dir) = std::env::var("EASYAGENT_CWD") {
        let path = PathBuf::from(dir.trim());
        if path.is_dir() {
            return path;
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

/// The runtime archive this build of the window looks for in its resources.
pub fn runtime_triple() -> &'static str {
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "aarch64-apple-darwin"
    } else if cfg!(target_os = "macos") {
        "x86_64-apple-darwin"
    } else if cfg!(windows) {
        "x86_64-pc-windows-msvc"
    } else if cfg!(target_arch = "aarch64") {
        "aarch64-unknown-linux-gnu"
    } else {
        "x86_64-unknown-linux-gnu"
    }
}

pub fn runtime_archive_name() -> String {
    format!("python-{}.tar.gz", runtime_triple())
}

/// `python.exe` at the top on Windows, `bin/python3` elsewhere.
pub fn runtime_python(root: &Path) -> PathBuf {
    if cfg!(windows) {
        root.join("python.exe")
    } else {
        root.join("bin").join("python3")
    }
}

const COMPLETE: &str = ".easyagent-complete";

/// Unpack `archive` (a tar.gz whose top folder is `python/`) under `base/runtime`.
/// The folder name carries the app version and the archive size, so an upgrade
/// unpacks a fresh copy. Unpacking goes to a temporary folder that is renamed
/// into place, and a marker is written last, so a half-unpacked runtime is never used.
/// Older runtimes in that same folder, which only this window creates, are removed.
pub fn prepare_runtime(archive: &Path, base: &Path, version: &str) -> Result<PathBuf, String> {
    let size = std::fs::metadata(archive)
        .map_err(|err| format!("The bundled Python runtime is missing ({}): {err}", archive.display()))?
        .len();
    let parent = base.join("runtime");
    let name = format!("{}-{}-{}", runtime_triple(), version, size);
    let dest = parent.join(&name);
    let python = runtime_python(&dest.join("python"));
    if dest.join(COMPLETE).is_file() && python.is_file() {
        return Ok(python);
    }
    std::fs::create_dir_all(&parent).map_err(|err| format!("Could not create {}: {err}", parent.display()))?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let partial = parent.join(format!(".partial-{}-{}", std::process::id(), stamp));
    let result = (|| -> Result<(), String> {
        std::fs::create_dir_all(&partial).map_err(|err| err.to_string())?;
        let file = std::fs::File::open(archive).map_err(|err| err.to_string())?;
        let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(std::io::BufReader::new(file)));
        tar.set_preserve_permissions(true);
        tar.set_overwrite(true);
        tar.unpack(&partial).map_err(|err| err.to_string())?;
        if !runtime_python(&partial.join("python")).is_file() {
            return Err("the archive has no Python interpreter".into());
        }
        std::fs::write(partial.join(COMPLETE), name.as_bytes()).map_err(|err| err.to_string())?;
        if dest.exists() {
            std::fs::remove_dir_all(&dest).map_err(|err| err.to_string())?;
        }
        std::fs::rename(&partial, &dest).map_err(|err| err.to_string())
    })();
    if let Err(err) = result {
        let _ = std::fs::remove_dir_all(&partial);
        return Err(format!("Could not unpack the bundled Python runtime into {}: {err}", dest.display()));
    }
    if let Ok(entries) = std::fs::read_dir(&parent) {
        for entry in entries.flatten() {
            let entry_name = entry.file_name().to_string_lossy().to_string();
            if entry_name != name && entry_name.starts_with(runtime_triple()) {
                let _ = std::fs::remove_dir_all(entry.path());
            }
        }
    }
    Ok(python)
}

/// Where the server runs and which data folder it uses.
///
/// `EASYAGENT_DATA` set: left alone. `EASYAGENT_CWD` set to a folder: the server
/// runs there and keeps its usual `./data`. Neither: the server runs in the
/// window's per-user app-data folder and `EASYAGENT_DATA` points at `data` inside it.
/// No other folder's data is read or moved.
pub fn data_plan(env_data: Option<&str>, env_cwd: Option<&str>, app_data: &Path) -> (PathBuf, Option<PathBuf>) {
    let cwd = env_cwd
        .map(|text| PathBuf::from(text.trim()))
        .filter(|path| !path.as_os_str().is_empty() && path.is_dir());
    let has_data = env_data.map(|text| !text.trim().is_empty()).unwrap_or(false);
    match (has_data, cwd) {
        (true, Some(dir)) => (dir, None),
        (true, None) => (app_data.to_path_buf(), None),
        (false, Some(dir)) => (dir, None),
        (false, None) => (app_data.to_path_buf(), Some(app_data.join("data"))),
    }
}

/// Which interpreter starts the server: `EASYAGENT_PYTHON`, then the bundled
/// runtime, then `python` on PATH.
pub fn choose_python(env_python: Option<&str>, bundled: Option<&Path>) -> (String, bool) {
    if let Some(text) = env_python.map(str::trim).filter(|text| !text.is_empty()) {
        return (text.to_string(), false);
    }
    if let Some(path) = bundled {
        return (path.to_string_lossy().to_string(), true);
    }
    (python_program(), false)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServerLaunch {
    pub program: String,
    pub args: Vec<String>,
    pub env: Vec<(String, String)>,
    pub remove_env: Vec<String>,
    pub cwd: PathBuf,
}

pub fn server_launch(port: u16, program: String, bundled: bool, cwd: PathBuf, data: Option<PathBuf>) -> ServerLaunch {
    let base = server_command(port);
    let mut env = base.env;
    let mut remove_env = Vec::new();
    if let Some(dir) = data {
        env.push(("EASYAGENT_DATA".into(), dir.to_string_lossy().to_string()));
    }
    if let Some(dir) = std::env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf)) {
        // The server refuses this folder as a bot workspace.
        env.push(("EASYAGENT_INSTALL_DIR".into(), dir.to_string_lossy().to_string()));
    }
    if bundled {
        // The bundled interpreter only sees its own packages, and its folder is
        // refused as a bot workspace.
        env.push(("EASYAGENT_RUNTIME".into(), "bundled".into()));
        env.push(("PYTHONNOUSERSITE".into(), "1".into()));
        remove_env.push("PYTHONPATH".into());
        remove_env.push("PYTHONHOME".into());
    }
    ServerLaunch { program, args: base.args, env, remove_env, cwd }
}

/// Start the server with stdout and stderr in `log`. The previous log is kept as `.1`.
pub fn spawn_logged(spec: &ServerLaunch, log: &Path) -> std::io::Result<std::process::Child> {
    if let Some(dir) = log.parent() {
        std::fs::create_dir_all(dir)?;
    }
    if log.exists() {
        let _ = std::fs::rename(log, log.with_extension("log.1"));
    }
    let out = std::fs::File::create(log)?;
    let err = out.try_clone()?;
    std::fs::create_dir_all(&spec.cwd)?;
    let mut cmd = Command::new(&spec.program);
    cmd.args(&spec.args)
        .current_dir(&spec.cwd)
        .envs(spec.env.iter().cloned())
        .stdin(Stdio::null())
        .stdout(Stdio::from(out))
        .stderr(Stdio::from(err));
    for key in &spec.remove_env {
        cmd.env_remove(key);
    }
    detach_console(&mut cmd);
    process_group(&mut cmd);
    cmd.spawn()
}

/// A window app has no console. CREATE_NO_WINDOW gives the server a console with
/// no window, which the commands it runs share, so none of them flashes one.
fn detach_console(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let _ = cmd;
}

fn process_group(cmd: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
        #[cfg(target_os = "linux")]
        unsafe {
            cmd.pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM as libc::c_ulong);
                if libc::getppid() == 1 {
                    libc::raise(libc::SIGTERM);
                }
                Ok(())
            });
        }
    }
    let _ = cmd;
}

/// Wait for `/api/health`. Stops early when the child exits.
pub fn wait_for_child(port: u16, child: &mut std::process::Child, timeout: Duration) -> Result<(), String> {
    let start = std::time::Instant::now();
    loop {
        if probe_port(port) == Probe::EasyAgent {
            return Ok(());
        }
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!("EasyAgent stopped while starting ({status})."));
        }
        if start.elapsed() >= timeout {
            return Err(format!("EasyAgent did not answer {} in {} seconds.", health_url(port), timeout.as_secs()));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// The first script on the server's page.
pub fn first_script(html: &str) -> Option<String> {
    let mut rest = html;
    while let Some(index) = rest.find("<script") {
        let tag_end = rest[index..].find('>').map(|end| index + end)?;
        let tag = &rest[index..tag_end];
        if let Some(pos) = tag.find("src=\"") {
            let value = &tag[pos + 5..];
            if let Some(end) = value.find('"') {
                return Some(value[..end].to_string());
            }
        }
        rest = &rest[tag_end..];
    }
    None
}

/// The page and its script both load. Used by the install smoke test.
pub fn check_ui(port: u16) -> Result<(usize, String, usize), String> {
    let page = http_get_text(port, "/", Duration::from_secs(10))?;
    if page.status != 200 {
        return Err(format!("GET / returned {}", page.status));
    }
    let script = first_script(&page.body).ok_or("the page has no script")?;
    let js = http_get_text(port, &script, Duration::from_secs(20))?;
    if js.status != 200 || js.body.len() < 1000 {
        return Err(format!("GET {script} returned {} with {} bytes", js.status, js.body.len()));
    }
    Ok((page.body.len(), script, js.body.len()))
}

fn http_get_text(port: u16, path: &str, timeout: Duration) -> Result<RawResponse, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, timeout).map_err(|err| err.to_string())?;
    stream.set_read_timeout(Some(timeout)).ok();
    let request = format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: text/html,*/*\r\n\r\n");
    stream.write_all(request.as_bytes()).map_err(|err| err.to_string())?;
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).ok();
    parse_http(&bytes)
}

pub fn browser_command(url: &str) -> (String, Vec<String>) {
    if cfg!(target_os = "macos") {
        ("open".into(), vec![url.into()])
    } else if cfg!(windows) {
        ("cmd".into(), vec!["/C".into(), "start".into(), "".into(), url.into()])
    } else {
        ("xdg-open".into(), vec![url.into()])
    }
}

pub fn http_get(port: u16, path: &str, timeout: Duration) -> Result<RawResponse, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, timeout).map_err(|err| err.to_string())?;
    stream.set_read_timeout(Some(timeout)).ok();
    stream.set_write_timeout(Some(timeout)).ok();
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).map_err(|err| err.to_string())?;
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).ok();
    parse_http(&bytes)
}

pub fn http_post_json(port: u16, path: &str, json: &str, timeout: Duration) -> Result<RawResponse, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, timeout).map_err(|err| err.to_string())?;
    stream.set_read_timeout(Some(timeout)).ok();
    stream.set_write_timeout(Some(timeout)).ok();
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\nConnection: close\r\nContent-Type: application/json\r\nAccept: application/json\r\nContent-Length: {}\r\n\r\n{json}",
        json.len()
    );
    stream.write_all(request.as_bytes()).map_err(|err| err.to_string())?;
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).ok();
    parse_http(&bytes)
}

/// Add a bot with a name and no connection, the first thing a new user does.
/// Used by the install smoke test against a throwaway data dir.
pub fn smoke_add_bot(port: u16, name: &str) -> Result<String, String> {
    let body = serde_json::json!({"name": name}).to_string();
    let added = http_post_json(port, "/api/bots", &body, Duration::from_secs(10))?;
    if added.status != 200 {
        return Err(format!("POST /api/bots returned {}: {}", added.status, added.body));
    }
    let bot: serde_json::Value = serde_json::from_str(&added.body).map_err(|err| format!("POST /api/bots gave {err}: {}", added.body))?;
    let id = bot["id"].as_str().unwrap_or_default().to_string();
    if id.is_empty() || bot["name"].as_str() != Some(name) {
        return Err(format!("POST /api/bots gave {}", added.body));
    }
    let listed = http_get(port, "/api/bots", Duration::from_secs(10))?;
    if listed.status != 200 || !listed.body.contains(&id) {
        return Err(format!("GET /api/bots did not list the new bot: {} {}", listed.status, listed.body));
    }
    Ok(id)
}

fn parse_http(bytes: &[u8]) -> Result<RawResponse, String> {
    let text = String::from_utf8_lossy(bytes);
    let (head, raw_body) = text.split_once("\r\n\r\n").unwrap_or((text.as_ref(), ""));
    let status = head
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse().ok())
        .unwrap_or(0);
    let chunked = head.to_ascii_lowercase().contains("transfer-encoding: chunked");
    let body = if chunked { decode_chunks(raw_body) } else { raw_body.to_string() };
    Ok(RawResponse { status, body })
}

fn decode_chunks(raw: &str) -> String {
    let mut rest = raw;
    let mut out = String::new();
    loop {
        let Some((size, after)) = rest.split_once("\r\n") else {
            break;
        };
        let Ok(len) = usize::from_str_radix(size.trim().split(';').next().unwrap_or("0"), 16) else {
            break;
        };
        if len == 0 || after.len() < len {
            break;
        }
        out.push_str(&after[..len]);
        rest = after.get(len + 2..).unwrap_or("");
    }
    if out.is_empty() { raw.to_string() } else { out }
}

pub fn probe_port(port: u16) -> Probe {
    match http_get(port, "/api/health", Duration::from_millis(700)) {
        Ok(response) if response.status == 200 && health_is_easyagent(&response.body) => Probe::EasyAgent,
        Ok(_) => Probe::Other,
        Err(_) => Probe::Down,
    }
}

pub fn wait_until_up(port: u16, timeout: Duration) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if probe_port(port) == Probe::EasyAgent {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    probe_port(port) == Probe::EasyAgent
}

pub fn spawn_server(port: u16) -> std::io::Result<std::process::Child> {
    let spec = server_command(port);
    let mut cmd = Command::new(&spec.program);
    cmd.args(&spec.args)
        .current_dir(server_cwd())
        .envs(spec.env.iter().cloned())
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // A new process group lets Quit stop the server on macOS and Linux.
        cmd.process_group(0);
        // prctl is Linux-only. macOS has no PDEATHSIG; Quit still signals the group.
        #[cfg(target_os = "linux")]
        unsafe {
            cmd.pre_exec(|| {
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM as libc::c_ulong);
                if libc::getppid() == 1 {
                    libc::raise(libc::SIGTERM);
                }
                Ok(())
            });
        }
    }
    cmd.spawn()
}

pub fn terminate_child(child: &mut std::process::Child) {
    let pid = child.id();
    #[cfg(unix)]
    {
        let _ = Command::new("kill").args(["-TERM", &format!("-{pid}")]).status();
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .status();
    }
    let start = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) if start.elapsed() < Duration::from_secs(3) => {
                std::thread::sleep(Duration::from_millis(50));
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return;
            }
        }
    }
}

/// A 64px screen-face. Zero is the head. A count blits the number in the corner.
pub fn draw_icon(count: i64) -> (Vec<u8>, u32, u32) {
    const SIZE: usize = 64;
    let mut pixels = vec![0u8; SIZE * SIZE * 4];
    paint_head(&mut pixels);
    if count > 0 {
        let label = icon_label(count);
        let scale = 2;
        let width = label.chars().count() * (5 * scale + scale);
        let origin_x = SIZE.saturating_sub(width + 4);
        let origin_y = SIZE.saturating_sub(7 * scale + 4);
        fill(
            &mut pixels,
            origin_x.saturating_sub(2),
            origin_y.saturating_sub(2),
            width + 4,
            7 * scale + 4,
            28,
            27,
            25,
            255,
        );
        for (index, ch) in label.chars().enumerate() {
            blit(
                &mut pixels,
                origin_x + index * (5 * scale + scale),
                origin_y,
                ch,
                scale,
            );
        }
    }
    (pixels, SIZE as u32, SIZE as u32)
}

fn fill(pixels: &mut [u8], x: usize, y: usize, w: usize, h: usize, r: u8, g: u8, b: u8, a: u8) {
    for yy in y..y.saturating_add(h) {
        for xx in x..x.saturating_add(w) {
            put(pixels, xx, yy, r, g, b, a);
        }
    }
}

fn paint_head(pixels: &mut [u8]) {
    // The small face from easyagent/mascot.py, scaled into the 64px icon.
    const ROWS: &[&[u8]] = &[
        b"....########....",
        b"....#..AA..#....",
        b"....#...A..#....",
        b"....#...A..#....",
        b"....#..AAA.#....",
        b"....########....",
        b"......##........",
        b"..############..",
        b".#............#.",
        b".#.FFFFFFFFFF.#.",
        b".#.#........#.#.",
        b".#.#.EE..EE.#.#.",
        b".#.#.EE..EE.#.#.",
        b".#.#..M..M..#.#.",
        b".#.#...MM...#.#.",
        b".#.FFFFFFFFFF.#.",
        b".#............#.",
        b"..############..",
    ];
    const W: usize = 16;
    const H: usize = 18;
    const SCALE: usize = 3;
    let origin_x = (64 - W * SCALE) / 2;
    let origin_y = (64 - H * SCALE) / 2;
    let mut outside = [false; W * H];
    let mut stack = Vec::with_capacity(W * 2);
    for x in 0..W {
        stack.push((x, 0usize));
        stack.push((x, H - 1));
    }
    for y in 0..H {
        stack.push((0usize, y));
        stack.push((W - 1, y));
    }
    while let Some((x, y)) = stack.pop() {
        if x >= W || y >= H {
            continue;
        }
        let index = y * W + x;
        let cell = ROWS[y][x];
        if outside[index] || cell == b'#' || cell == b'A' || cell == b'E' || cell == b'M' || cell == b'F' {
            continue;
        }
        outside[index] = true;
        if x > 0 {
            stack.push((x - 1, y));
        }
        if x + 1 < W {
            stack.push((x + 1, y));
        }
        if y > 0 {
            stack.push((x, y - 1));
        }
        if y + 1 < H {
            stack.push((x, y + 1));
        }
    }
    for y in 0..H {
        for x in 0..W {
            let cell = ROWS[y][x];
            let ink = cell == b'#' || cell == b'A' || cell == b'E' || cell == b'M' || cell == b'F';
            let (r, g, b, a) = if ink {
                (28, 27, 25, 255)
            } else if !outside[y * W + x] {
                (246, 244, 239, 255)
            } else {
                continue;
            };
            for dy in 0..SCALE {
                for dx in 0..SCALE {
                    put(
                        pixels,
                        origin_x + x * SCALE + dx,
                        origin_y + y * SCALE + dy,
                        r,
                        g,
                        b,
                        a,
                    );
                }
            }
        }
    }
}

fn put(pixels: &mut [u8], x: usize, y: usize, r: u8, g: u8, b: u8, a: u8) {
    if x >= 64 || y >= 64 {
        return;
    }
    let index = (y * 64 + x) * 4;
    pixels[index] = r;
    pixels[index + 1] = g;
    pixels[index + 2] = b;
    pixels[index + 3] = a;
}

fn blit(pixels: &mut [u8], origin_x: usize, origin_y: usize, ch: char, scale: usize) {
    let rows = glyph(ch);
    for (row, bits) in rows.iter().enumerate() {
        for col in 0..5 {
            if bits & (1 << (4 - col)) == 0 {
                continue;
            }
            for dy in 0..scale {
                for dx in 0..scale {
                    put(
                        pixels,
                        origin_x + col * scale + dx,
                        origin_y + row * scale + dy,
                        246,
                        244,
                        239,
                        255,
                    );
                }
            }
        }
    }
}

fn glyph(ch: char) -> [u8; 7] {
    match ch {
        '0' => [0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110],
        '1' => [0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
        '2' => [0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111],
        '3' => [0b01110, 0b10001, 0b00001, 0b00110, 0b00001, 0b10001, 0b01110],
        '4' => [0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010],
        '5' => [0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110],
        '6' => [0b01110, 0b10000, 0b11110, 0b10001, 0b10001, 0b10001, 0b01110],
        '7' => [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000],
        '8' => [0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110],
        '9' => [0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00001, 0b01110],
        'e' => [0b00000, 0b00000, 0b01110, 0b10101, 0b10111, 0b10000, 0b01110],
        'a' => [0b00000, 0b00000, 0b01110, 0b00001, 0b01111, 0b10001, 0b01111],
        '+' => [0b00000, 0b00100, 0b00100, 0b11111, 0b00100, 0b00100, 0b00000],
        _ => [0, 0, 0, 0, 0, 0, 0],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn unread_poll_waits_at_least_fifteen_seconds() {
        assert!(UNREAD_POLL.as_secs() >= 15);
    }

    #[test]
    fn titles_match_the_python_tray() {
        assert_eq!(title_for(0), "EasyAgent");
        assert_eq!(title_for(-1), "EasyAgent");
        assert_eq!(title_for(3), "(3) EasyAgent");
        assert_eq!(title_for(120), "(120) EasyAgent");
        assert_eq!(icon_label(0), "ea");
        assert_eq!(icon_label(12), "12");
        assert_eq!(icon_label(100), "99+");
    }

    #[test]
    fn health_and_unread_json() {
        assert!(health_is_easyagent(r#"{"ok": true, "data_dir": "/tmp/ea"}"#));
        assert!(!health_is_easyagent(r#"{"ok": false}"#));
        assert!(!health_is_easyagent("not json"));
        assert_eq!(unread_total(r#"{"total": 4, "chats": [], "rooms": []}"#), Some(4));
        assert_eq!(unread_total("{}"), None);
        let chats = unread_chats(r#"{"total": 1, "chats": [{"bot_id": "b", "chat_id": "c", "unread": 2}]}"#);
        assert_eq!(chats.len(), 1);
        assert_eq!(chats[0].unread, 2);
        assert_eq!(choice_count(r#"{"messages": [{"role": "assistant", "choices": ["Yes", "No"]}]}"#), 2);
        assert_eq!(choice_count(r#"{"messages": [{"role": "assistant", "content": "Done."}]}"#), 0);
        assert_eq!(notice_line(2), "A bot has a question.");
        assert_eq!(notice_line(0), "A bot finished a reply.");
        assert!(!updates_requested(None));
        assert!(!updates_requested(Some("0")));
        assert!(updates_requested(Some("1")));
    }

    #[test]
    fn an_open_easyagent_is_attached_and_a_closed_port_is_started() {
        assert_eq!(decide(Probe::EasyAgent), Ok(Launch::Attach));
        assert_eq!(decide(Probe::Down), Ok(Launch::Spawn));
        assert!(decide(Probe::Other).is_err());
    }

    #[test]
    fn the_spawned_server_is_python_with_the_tray_left_to_the_window() {
        let command = server_command(44721);
        assert_eq!(command.args, vec!["-m", "easyagent"]);
        assert!(command.env.contains(&("EASYAGENT_TRAY".into(), "0".into())));
        assert!(command.env.contains(&("EASYAGENT_PORT".into(), "44721".into())));
        assert!(!command.env.iter().any(|(key, _)| key == "EASYAGENT_HOST"));
        assert_eq!(port_from_env(Some("45001")), 45001);
        assert_eq!(port_from_env(Some("nope")), DEFAULT_PORT);
        assert_eq!(health_url(44721), "http://127.0.0.1:44721/api/health");
    }

    #[test]
    fn chunked_health_still_counts_as_easyagent() {
        let body = r#"{"ok": true, "data_dir": "/tmp"}"#;
        let raw = format!(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n{size:x}\r\n{body}\r\n0\r\n\r\n",
            size = body.len()
        );
        let parsed = parse_http(raw.as_bytes()).unwrap();
        assert_eq!(parsed.status, 200);
        assert_eq!(parsed.body, body);
        assert!(health_is_easyagent(&parsed.body));
    }

    #[test]
    fn icons_for_zero_and_a_count_differ() {
        let (quiet, w, h) = draw_icon(0);
        let (busy, _, _) = draw_icon(4);
        assert_eq!((w, h), (64, 64));
        assert_eq!(quiet.len(), 64 * 64 * 4);
        assert_ne!(quiet, busy);
        assert_eq!(quiet[3], 0);
    }

    #[test]
    fn a_local_health_response_is_easyagent() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 512];
            let _ = stream.read(&mut buf);
            let body = br#"{"ok": true, "data_dir": "/tmp"}"#;
            let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len());
            stream.write_all(head.as_bytes()).unwrap();
            stream.write_all(body).unwrap();
        });
        let probe = probe_port(port);
        assert_eq!(probe, Probe::EasyAgent);
        assert_eq!(decide(probe), Ok(Launch::Attach));
    }

    #[test]
    fn a_different_program_on_the_port_is_left_alone() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 512];
            let _ = stream.read(&mut buf);
            let body = b"not easyagent";
            let head = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(body);
        });
        assert_eq!(probe_port(port), Probe::Other);
        assert!(decide(Probe::Other).is_err());
    }

    #[test]
    fn the_smoke_test_adds_a_bot_with_no_connection() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen = std::sync::Arc::new(std::sync::Mutex::new(String::new()));
        let record = seen.clone();
        thread::spawn(move || {
            for reply in [r#"{"id": "b1", "name": "Smoke Test Bot"}"#, r#"[{"id": "b1", "name": "Smoke Test Bot"}]"#] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut buf = [0u8; 2048];
                let n = stream.read(&mut buf).unwrap_or(0);
                record.lock().unwrap().push_str(&String::from_utf8_lossy(&buf[..n]));
                let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", reply.len());
                let _ = stream.write_all(head.as_bytes());
                let _ = stream.write_all(reply.as_bytes());
            }
        });
        assert_eq!(smoke_add_bot(port, "Smoke Test Bot"), Ok("b1".to_string()));
        let sent = seen.lock().unwrap().clone();
        assert!(sent.starts_with("POST /api/bots HTTP/1.1"));
        assert!(sent.contains(r#"{"name":"Smoke Test Bot"}"#));
        assert!(!sent.contains("endpoint"));
    }

    #[test]
    fn the_smoke_test_fails_when_adding_a_bot_fails() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0u8; 2048];
            let _ = stream.read(&mut buf);
            let body = r#"{"detail": "Name must be 1-80 characters."}"#;
            let head = format!("HTTP/1.1 400 Bad Request\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len());
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(body.as_bytes());
        });
        let err = smoke_add_bot(port, "Smoke Test Bot").unwrap_err();
        assert!(err.contains("400"), "{err}");
    }

    #[test]
    fn a_closed_port_is_started() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert_eq!(probe_port(port), Probe::Down);
        assert_eq!(decide(Probe::Down), Ok(Launch::Spawn));
    }

    fn tiny_runtime(dir: &Path) -> PathBuf {
        let src = dir.join("src");
        let python = runtime_python(&src.join("python"));
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::write(&python, b"#!/bin/sh\n").unwrap();
        let archive = dir.join("python.tar.gz");
        let file = std::fs::File::create(&archive).unwrap();
        let mut builder = tar::Builder::new(flate2::write::GzEncoder::new(file, flate2::Compression::fast()));
        builder.append_dir_all("python", src.join("python")).unwrap();
        builder.into_inner().unwrap().finish().unwrap();
        archive
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("ea-sup-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn the_runtime_is_unpacked_once_and_old_ones_are_removed() {
        let dir = scratch("unpack");
        let archive = tiny_runtime(&dir);
        let base = dir.join("app");
        let stale = base.join("runtime").join(format!("{}-0.0.1-5", runtime_triple()));
        std::fs::create_dir_all(&stale).unwrap();
        let unrelated = base.join("runtime").join("keep-me");
        std::fs::create_dir_all(&unrelated).unwrap();
        let python = prepare_runtime(&archive, &base, "9.9.9").unwrap();
        assert!(python.is_file());
        assert!(python.starts_with(base.join("runtime")));
        assert!(!stale.exists());
        assert!(unrelated.exists());
        let again = prepare_runtime(&archive, &base, "9.9.9").unwrap();
        assert_eq!(python, again);
        let partials = std::fs::read_dir(base.join("runtime")).unwrap().flatten()
            .filter(|entry| entry.file_name().to_string_lossy().starts_with(".partial")).count();
        assert_eq!(partials, 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_or_broken_runtime_is_an_error_not_a_blank_window() {
        let dir = scratch("broken");
        assert!(prepare_runtime(&dir.join("nope.tar.gz"), &dir, "1").is_err());
        let bad = dir.join("bad.tar.gz");
        std::fs::write(&bad, b"not a tarball").unwrap();
        let err = prepare_runtime(&bad, &dir.join("app"), "1").unwrap_err();
        assert!(err.contains("Could not unpack"));
        let leftovers = std::fs::read_dir(dir.join("app").join("runtime")).unwrap().count();
        assert_eq!(leftovers, 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_data_folder_defaults_to_the_app_folder_and_respects_overrides() {
        let app = std::env::temp_dir();
        let (cwd, data) = data_plan(None, None, &app);
        assert_eq!(cwd, app);
        assert_eq!(data, Some(app.join("data")));
        let (_, data) = data_plan(Some("/somewhere"), None, &app);
        assert_eq!(data, None);
        let here = std::env::current_dir().unwrap();
        let (cwd, data) = data_plan(None, Some(here.to_str().unwrap()), &app);
        assert_eq!(cwd, here);
        assert_eq!(data, None);
        let (cwd, data) = data_plan(None, Some("/no/such/folder/ea"), &app);
        assert_eq!(cwd, app);
        assert_eq!(data, Some(app.join("data")));
    }

    #[test]
    fn the_python_choice_prefers_the_override_then_the_bundle() {
        let bundled = PathBuf::from("/opt/ea/python/bin/python3");
        assert_eq!(choose_python(Some(" /usr/bin/python3.12 "), Some(&bundled)), ("/usr/bin/python3.12".to_string(), false));
        assert_eq!(choose_python(None, Some(&bundled)).1, true);
        assert_eq!(choose_python(Some(""), None).1, false);
        let spec = server_launch(44721, "py".into(), true, PathBuf::from("/tmp"), Some(PathBuf::from("/tmp/data")));
        assert_eq!(spec.args, vec!["-m", "easyagent"]);
        assert!(spec.env.contains(&("EASYAGENT_TRAY".into(), "0".into())));
        assert!(spec.env.contains(&("EASYAGENT_DATA".into(), "/tmp/data".into())));
        assert!(spec.env.contains(&("PYTHONNOUSERSITE".into(), "1".into())));
        assert!(spec.remove_env.contains(&"PYTHONPATH".to_string()));
        assert!(spec.env.contains(&("EASYAGENT_RUNTIME".into(), "bundled".into())));
        assert!(spec.env.iter().any(|(key, _)| key == "EASYAGENT_INSTALL_DIR"));
        let plain = server_launch(44721, "python3".into(), false, PathBuf::from("/tmp"), None);
        assert!(!plain.env.iter().any(|(key, _)| key == "EASYAGENT_RUNTIME" || key == "EASYAGENT_DATA"));
        assert!(!spec.env.iter().any(|(key, _)| key == "EASYAGENT_HOST"));
    }

    #[test]
    fn the_first_script_is_found() {
        let html = r#"<html><head><script>var a=1;</script><script type="module" crossorigin src="/ui/assets/index-x.js"></script></head></html>"#;
        assert_eq!(first_script(html), Some("/ui/assets/index-x.js".to_string()));
        assert_eq!(first_script("<p>none</p>"), None);
    }

    #[cfg(unix)]
    #[test]
    fn a_server_that_exits_is_reported_quickly() {
        let dir = scratch("exit");
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let spec = ServerLaunch { program: "sh".into(), args: vec!["-c".into(), "echo boom; exit 3".into()], env: vec![], remove_env: vec![], cwd: dir.clone() };
        let log = dir.join("logs").join("server.log");
        let mut child = spawn_logged(&spec, &log).unwrap();
        let start = std::time::Instant::now();
        let err = wait_for_child(port, &mut child, Duration::from_secs(20)).unwrap_err();
        assert!(err.contains("stopped"));
        assert!(start.elapsed() < Duration::from_secs(10));
        assert!(std::fs::read_to_string(&log).unwrap().contains("boom"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
