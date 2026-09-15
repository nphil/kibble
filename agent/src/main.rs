//! kibbled — local control for a Petkit YumShare Dual feeder, running on the feeder itself.
//!
//! What it does today:
//!   GET    /state                   live telemetry from the shared config
//!   GET    /config                  current value of every device setting this study has mapped
//!   POST   /config                  {"key": "volume", "value": 5}  change one setting, verified
//!                                   subset only
//!   POST   /feed                    {"hopper": 1|2|"both", "amount": N, "id": "..."}  dispense
//!   POST   /feed/cancel             stop a dispense in progress
//!   GET    /schedule                kibbled's cached copy of the feed schedule (the MCU has no
//!                                   read-back -- see schedule.rs)
//!   PUT    /schedule                {"entries": [...]}  replace the whole table
//!   POST   /schedule/entry          {"time": "HH:MM", "amount_l": N, "amount_r": N,
//!                                   "id": "...", "enabled": bool}  add one entry
//!   DELETE /schedule/entry?id=      remove one entry
//!   POST   /schedule/entry/enabled  {"id": "...", "enabled": bool}  enable/disable one entry
//!
//! What it deliberately does not do: talk to any cloud, replace any vendor process, or write to
//! the vendor's own (AES-encrypted, key unrecovered) `/opt/user.conf`, or flash outside
//! `/opt/kibble`. It sits beside the stock firmware, speaks its internal bus, and keeps its own
//! settings record in `/opt/kibble/` — see `persist.rs` and `docs/21-config-encryption.md`.

mod backup;
mod bus;
mod desired;
mod http;
mod md5;
mod persist;
mod schedule;
mod settings;
mod state;

use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use bus::{msg, FeedCtrl, Peer, Sender};
use http::{json_field, Request, Response};
use schedule::Schedule;
use state::Shm;

/// `src` we stamp on bus messages. Stock `ctrl` is 1; replies to our feed land in its queue,
/// where it handles them exactly as it would a cloud-originated feed. That keeps the vendor's
/// own feed accounting (event log, cloud counters while it still runs) consistent.
const SRC_AS_CTRL: u16 = Peer::Ctrl as u16;

const DEFAULT_BIND: &str = "0.0.0.0:8765";

fn main() {
    let bind = std::env::args().nth(1).unwrap_or_else(|| DEFAULT_BIND.to_string());

    match backup::backup_once() {
        Ok(Some((source_md5, backup_md5))) => eprintln!(
            "kibbled: backed up /opt/user.conf to {} (md5 {source_md5}, backup md5 {backup_md5}, match={})",
            backup::BACKUP,
            source_md5 == backup_md5
        ),
        Ok(None) => eprintln!("kibbled: {} already exists, leaving it alone", backup::BACKUP),
        Err(e) => eprintln!(
            "kibbled: could not back up /opt/user.conf: {e} (continuing — settings writes never touch that file)"
        ),
    }

    let shm = Shm::open().unwrap_or_else(|e| die(&format!("open {}: {e}", state::SHM_PATH)));
    // Shared, not leaked: the request-handling closure below borrows it for the life of the
    // (never-returning) `http::serve` call, and the reconciler owns its own clone of the `Arc`.
    let shm = Arc::new(shm);
    let ble = Sender::open(Peer::Ble, SRC_AS_CTRL)
        .unwrap_or_else(|e| die(&format!("open ble queue: {e}")));
    let mut schedule = Schedule::load(PathBuf::from(schedule::CACHE_PATH))
        .unwrap_or_else(|e| die(&format!("load {}: {e}", schedule::CACHE_PATH)));
    let listener = TcpListener::bind(&bind).unwrap_or_else(|e| die(&format!("bind {bind}: {e}")));
    eprintln!("kibbled: listening on {bind}");

    persist::spawn_reconciler(Arc::clone(&shm));
    let _ = http::serve(listener, |req| route(req, &shm, &ble, &mut schedule));
}

fn die(msg: &str) -> ! {
    eprintln!("kibbled: {msg}");
    std::process::exit(1)
}

fn route(req: &Request, shm: &Shm, ble: &Sender, schedule: &mut Schedule) -> Response {
    let (path, query) = http::split_query(&req.path);
    match (req.method.as_str(), path) {
        ("GET", "/state") => Response::Json(shm.snapshot().to_json()),
        ("GET", "/config") => Response::Json(settings::to_json(shm)),
        ("POST", "/config") => config_write(req),
        ("POST", "/feed") => feed(req, ble),
        ("POST", "/feed/cancel") => send_feed(
            ble,
            FeedCtrl {
                cancel: true,
                id: String::new(),
                amount1: 0,
                amount2: 0,
            },
        ),
        ("GET", "/schedule") => Response::Json(schedule.snapshot_json()),
        ("PUT", "/schedule") => put_schedule(req, schedule, ble),
        ("POST", "/schedule/entry") => post_schedule_entry(req, schedule, ble),
        ("DELETE", "/schedule/entry") => delete_schedule_entry(query, schedule, ble),
        ("POST", "/schedule/entry/enabled") => post_schedule_entry_enabled(req, schedule, ble),
        _ => Response::NotFound,
    }
}

fn config_write(req: &Request) -> Response {
    let key = match json_field(&req.body, "key") {
        Some(k) if !k.is_empty() => k,
        _ => return Response::BadRequest(r#""key" is required"#.into()),
    };
    let value: u32 = match json_field(&req.body, "value").and_then(|v| v.parse().ok()) {
        Some(v) => v,
        None => return Response::BadRequest(r#""value" must be a non-negative integer"#.into()),
    };
    match persist::write_setting(key, value) {
        Ok(outcome) => Response::Json(format!(
            r#"{{"ok":true,"key":"{}","value":{},"notified":{}}}"#,
            outcome.setting.key,
            value,
            match outcome.notified {
                Some(true) => "true",
                Some(false) => "false",
                None => "null",
            }
        )),
        Err(persist::WriteError::UnknownKey) => Response::NotFound,
        Err(e @ persist::WriteError::Io(_)) => Response::Error(e.to_string()),
        Err(e) => Response::BadRequest(e.to_string()),
    }
}

fn feed(req: &Request, ble: &Sender) -> Response {
    let amount: u8 = match json_field(&req.body, "amount").and_then(|v| v.parse().ok()) {
        Some(n) if (1..=20).contains(&n) => n,
        _ => return Response::BadRequest("amount must be 1..=20 portions".into()),
    };
    let (a1, a2) = match json_field(&req.body, "hopper").unwrap_or("1") {
        "1" => (amount, 0),
        "2" => (0, amount),
        "both" => (amount, amount),
        _ => return Response::BadRequest(r#"hopper must be 1, 2 or "both""#.into()),
    };
    let id = json_field(&req.body, "id")
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| {
            let t = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
            format!("kibble-{t}")
        });
    send_feed(
        ble,
        FeedCtrl {
            cancel: false,
            id,
            amount1: a1,
            amount2: a2,
        },
    )
}

fn send_feed(ble: &Sender, f: FeedCtrl) -> Response {
    match ble.send(msg::BLE_FEED_CTRL, &f.encode()) {
        Ok(()) => Response::Json(format!(
            r#"{{"ok":true,"id":"{}","amount1":{},"amount2":{},"cancel":{}}}"#,
            f.id.escape_debug(),
            f.amount1,
            f.amount2,
            f.cancel
        )),
        Err(e) => Response::Error(format!("bus send failed: {e}")),
    }
}

fn put_schedule(req: &Request, schedule: &mut Schedule, ble: &Sender) -> Response {
    let entries = match schedule::parse_entries(&req.body) {
        Ok(v) => v,
        Err(e) => return Response::BadRequest(e),
    };
    schedule_result(schedule.replace(entries, ble, schedule::now_unix()), schedule)
}

fn post_schedule_entry(req: &Request, schedule: &mut Schedule, ble: &Sender) -> Response {
    let entry = match schedule::parse_entry(&req.body) {
        Ok(e) => e,
        Err(e) => return Response::BadRequest(e),
    };
    schedule_result(schedule.add(entry, ble, schedule::now_unix()), schedule)
}

fn delete_schedule_entry(query: &str, schedule: &mut Schedule, ble: &Sender) -> Response {
    let id = match schedule::entry_id_from_query(query) {
        Some(id) => id,
        None => return Response::BadRequest("missing ?id=".into()),
    };
    schedule_result(schedule.remove(id, ble, schedule::now_unix()), schedule)
}

fn post_schedule_entry_enabled(req: &Request, schedule: &mut Schedule, ble: &Sender) -> Response {
    let id = match json_field(&req.body, "id").filter(|s| !s.is_empty()) {
        Some(id) => id.to_string(),
        None => return Response::BadRequest("missing \"id\"".into()),
    };
    let enabled = match json_field(&req.body, "enabled") {
        Some(v) => v != "false",
        None => return Response::BadRequest("missing \"enabled\"".into()),
    };
    schedule_result(schedule.set_enabled(&id, enabled, ble, schedule::now_unix()), schedule)
}

/// Shared success/error -> HTTP mapping for every schedule mutation: on success, echo the fresh
/// cache so a client sees the effect immediately with no extra `GET`; `Invalid` is a 400 (the
/// caller's fault -- bad input, unknown id, over the cap), `Internal` is a 500 (ours -- cache I/O,
/// bus send).
fn schedule_result(result: Result<(), schedule::Error>, schedule: &Schedule) -> Response {
    match result {
        Ok(()) => Response::Json(schedule.snapshot_json()),
        Err(schedule::Error::Invalid(m)) => Response::BadRequest(m),
        Err(schedule::Error::Internal(m)) => Response::Error(m),
    }
}
