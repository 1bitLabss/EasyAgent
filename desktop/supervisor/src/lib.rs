//! Decide whether the desktop window should attach to EasyAgent or start it.
//!
//! The FastAPI process is the app. This crate only checks `GET /api/health`
//! and, when that is down, builds the `python -m easyagent` command.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
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
    fn a_closed_port_is_started() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert_eq!(probe_port(port), Probe::Down);
        assert_eq!(decide(Probe::Down), Ok(Launch::Spawn));
    }
}
