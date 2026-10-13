//! Unpack a runtime archive, start the server the way the window does, and check the page.
//! `cargo run -p easyagent-supervisor --example launch_check -- <archive> <app-folder> <port>`
use std::path::PathBuf;
use std::time::Duration;

use easyagent_supervisor as host;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let archive = PathBuf::from(&args[1]);
    let app = PathBuf::from(&args[2]);
    let port: u16 = args[3].parse().unwrap();
    let python = host::prepare_runtime(&archive, &app, "check").expect("unpack");
    let (cwd, data) = host::data_plan(None, None, &app);
    let spec = host::server_launch(port, python.to_string_lossy().to_string(), true, cwd, data);
    let log = app.join("logs").join("server.log");
    let mut child = host::spawn_logged(&spec, &log).expect("spawn");
    let result = host::wait_for_child(port, &mut child, Duration::from_secs(120)).and_then(|_| host::check_ui(port));
    host::terminate_child(&mut child);
    match result {
        Ok((page, script, js)) => println!("OK page={page} script={script} js={js}"),
        Err(err) => {
            eprintln!("FAIL {err}\n{}", std::fs::read_to_string(&log).unwrap_or_default());
            std::process::exit(1);
        }
    }
}
