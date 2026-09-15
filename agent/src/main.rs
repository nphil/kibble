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
//!   RTSP   :8554/main               live H.264 video (ring's "main" channel, 1728x1080),
//!                                   zero re-encode
//!   RTSP   :8554/sub                live H.264 video (ring's "sub" channel, 1152x720@25fps),
//!                                   zero re-encode
//!   GET    :8765/streams            per-mount diagnostics: resolution/fps seen in the ring,
//!                                   active session count, each session's peer address
//!   GET    /cloud                   {"enabled","last_error","routes","connections"} -- the
//!                                   Petkit-cloud kill switch's status (see cloud.rs)
//!   POST   /cloud                   {"enabled": bool}  flip the kill switch
//!   GET    /events                  last 50 detections (see ai.rs) -- vendor JPEG side effects
//!                                   polled from /tmp, NOT a tap of ctrl's private mqueue inbox;
//!                                   score/pet_id/box are honestly null -- see ai.rs's module doc
//!   GET    /events/stream?since=N   long-poll for detections past sequence N (empty array on
//!                                   timeout, ~25s)
//!   GET    /faces/pending           pending face crops awaiting a human label (see faces.rs)
//!   GET    /faces/pending/<name>    one pending crop's raw JPEG bytes
//!   POST   /faces/label             {"name": "...", "cat": "..."}  moves a pending crop into
//!                                   permanent, cat-named storage
//!   GET    /feeds                   before/after dish snapshots per feed cycle (see
//!                                   feed_capture.rs), manual and scheduled alike
//!   GET    /feeds/<name>            one snapshot's raw H.264 keyframe bytes
//!   GET    /ble                     {"advertising","until"} -- last state kibbled itself asked
//!                                   for; see advertise.rs for why this is not a live MCU read
//!   POST   /ble/advertise           {"on": bool, "seconds": N}  toggle BLE advertising for the
//!                                   WiFi-down fallback (docs/26-ble-advertising.md); `seconds`
//!                                   optional, defaults/clamps per advertise.rs, `on` only
//!   GET    /wifi                    {"ssid","bssid","freq_mhz","band","signal_dbm","ip","state",
//!                                   "desired_ssid","last_error"} -- current Wi-Fi association
//!                                   (see wifi.rs)
//!   GET    /wifi/scan               [{"ssid","bssid","freq_mhz","band","signal_dbm","security"}]
//!                                   deduped by SSID (strongest kept), hidden SSIDs omitted
//!   POST   /wifi/connect            {"ssid": "...", "psk": "..."}  fail-safe add+select, with
//!                                   automatic rollback on failure -- psk never echoed back
//!   POST   /wifi/forget             {"ssid": "..."}  remove a Kibble-added network (never the
//!                                   vendor's own)
//!
//! What it deliberately does not do: talk to any cloud, replace any vendor process, or write to
//! the vendor's own (AES-encrypted, key unrecovered) `/opt/user.conf`, or flash outside
//! `/opt/kibble`. It sits beside the stock firmware, speaks its internal bus (and, for video, its
//! shared-memory frame ring), and keeps its own settings record in `/opt/kibble/` — see
//! `persist.rs` and `docs/21-config-encryption.md`.

mod advertise;
mod ai;
mod backup;
mod bus;
mod cloud;
mod desired;
mod faces;
mod feed_capture;
mod http;
mod md5;
mod persist;
mod ring;
mod rtsp;
mod schedule;
mod settings;
mod state;
mod wifi;

use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use advertise::BleAdv;
use bus::{msg, FeedCtrl, Peer, Sender};
use http::{json_field, Request, Response};
use schedule::Schedule;
use ring::VideoFeed;
use state::Shm;

/// `src` we stamp on bus messages. Stock `ctrl` is 1; replies to our feed land in its queue,
/// where it handles them exactly as it would a cloud-originated feed. That keeps the vendor's
/// own feed accounting (event log, cloud counters while it still runs) consistent.
const SRC_AS_CTRL: u16 = Peer::Ctrl as u16;

const DEFAULT_BIND: &str = "0.0.0.0:8765";
const RTSP_BIND: &str = "0.0.0.0:8554";

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
    let ble_adv = advertise::BleAdv::spawn()
        .unwrap_or_else(|e| die(&format!("open ble_adv queue: {e}")));
    let mut schedule = Schedule::load(PathBuf::from(schedule::CACHE_PATH))
        .unwrap_or_else(|e| die(&format!("load {}: {e}", schedule::CACHE_PATH)));
    let listener = TcpListener::bind(&bind).unwrap_or_else(|e| die(&format!("bind {bind}: {e}")));

    let main_feed = VideoFeed::new();
    let sub_feed = VideoFeed::new();
    let _poller = ring::spawn(main_feed.clone(), sub_feed.clone())
        .unwrap_or_else(|e| die(&format!("open {}: {e}", ring::RING_PATH)));
    let rtsp_listener =
        TcpListener::bind(RTSP_BIND).unwrap_or_else(|e| die(&format!("bind {RTSP_BIND}: {e}")));
    let capture = feed_capture::spawn(Arc::clone(&shm), Arc::clone(&sub_feed));
    let feeds = Arc::new(rtsp::Feeds { main: main_feed, sub: sub_feed });
    let _rtsp = rtsp::spawn(rtsp_listener, Arc::clone(&feeds));
    let ai_feed = ai::spawn();

    eprintln!(
        "kibbled: listening on {bind}, rtsp on {RTSP_BIND} (/main chan {}, /sub chan {})",
        ring::CHAN_MAIN,
        ring::CHAN_SUB
    );

    cloud::spawn_reconciler();
    persist::spawn_reconciler(Arc::clone(&shm));
    wifi::spawn_reconciler();
    let _ = http::serve(listener, |req| {
        route(req, &shm, &ble, &ble_adv, &mut schedule, &feeds, &ai_feed, &capture)
    });
}

fn die(msg: &str) -> ! {
    eprintln!("kibbled: {msg}");
    std::process::exit(1)
}

fn route(
    req: &Request,
    shm: &Shm,
    ble: &Sender,
    ble_adv: &BleAdv,
    schedule: &mut Schedule,
    feeds: &rtsp::Feeds,
    ai_feed: &ai::Feed,
    capture: &feed_capture::FeedCapture,
) -> Response {
    let (path, query) = http::split_query(&req.path);
    match (req.method.as_str(), path) {
        ("GET", "/state") => Response::Json(shm.snapshot().to_json()),
        ("GET", "/config") => Response::Json(settings::to_json(shm)),
        ("POST", "/config") => config_write(req),
        ("POST", "/feed") => feed(req, ble, capture),
        ("POST", "/feed/cancel") => send_feed(
            ble,
            FeedCtrl {
                cancel: true,
                id: String::new(),
                amount1: 0,
                amount2: 0,
            },
        ),
        ("GET", "/streams") => Response::Json(rtsp::streams_json(feeds)),
        ("GET", "/cloud") => Response::Json(cloud::status_json()),
        ("POST", "/cloud") => cloud_write(req),
        ("GET", "/schedule") => Response::Json(schedule.snapshot_json()),
        ("PUT", "/schedule") => put_schedule(req, schedule, ble),
        ("POST", "/schedule/entry") => post_schedule_entry(req, schedule, ble),
        ("DELETE", "/schedule/entry") => delete_schedule_entry(query, schedule, ble),
        ("POST", "/schedule/entry/enabled") => post_schedule_entry_enabled(req, schedule, ble),
        ("GET", "/events") => Response::Json(ai_feed.snapshot_json()),
        ("GET", "/events/stream") => events_stream(query, ai_feed),
        ("GET", "/faces/pending") => faces_pending_list(),
        ("GET", p) if p.starts_with("/faces/pending/") => {
            faces_pending_get(&p["/faces/pending/".len()..])
        }
        ("POST", "/faces/label") => faces_label_post(req),
        ("GET", "/feeds") => feeds_list(capture),
        ("GET", p) if p.starts_with("/feeds/") => feeds_get(capture, &p["/feeds/".len()..]),
        ("GET", "/ble") => Response::Json(ble_adv.status_json()),
        ("POST", "/ble/advertise") => ble_advertise_write(req, ble_adv),
        ("GET", "/wifi") => Response::Json(wifi::status_json()),
        ("GET", "/wifi/scan") => Response::Json(wifi::scan_json()),
        ("POST", "/wifi/connect") => wifi_connect(req),
        ("POST", "/wifi/forget") => wifi_forget(req),
        _ => Response::NotFound,
    }
}

/// `GET /events/stream?since=N`: `since` defaults to 0 (an HA/Scrypted client's first call gets
/// everything currently buffered, exactly like `GET /events`, then remembers the highest `seq`
/// it saw for the next call).
fn events_stream(query: &str, ai_feed: &ai::Feed) -> Response {
    let since: u64 = http::query_field(query, "since").and_then(|v| v.parse().ok()).unwrap_or(0);
    Response::Json(ai_feed.wait_since(since, ai::LONG_POLL_TIMEOUT))
}

fn faces_pending_list() -> Response {
    match faces::list_pending() {
        Ok(names) => {
            let items: Vec<String> = names.iter().map(|n| format!("\"{}\"", n.escape_debug())).collect();
            Response::Json(format!("[{}]", items.join(",")))
        }
        Err(e) => Response::Error(e.to_string()),
    }
}

fn faces_pending_get(name: &str) -> Response {
    match faces::read_pending(name) {
        Ok(bytes) => Response::Blob("image/jpeg", bytes),
        Err(faces::FaceError::InvalidName) => Response::BadRequest("invalid file name".into()),
        Err(faces::FaceError::NotFound) => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

fn faces_label_post(req: &Request) -> Response {
    let name = match json_field(&req.body, "name").filter(|s| !s.is_empty()) {
        Some(n) => n,
        None => return Response::BadRequest(r#""name" is required"#.into()),
    };
    let cat = match json_field(&req.body, "cat").filter(|s| !s.is_empty()) {
        Some(c) => c,
        None => return Response::BadRequest(r#""cat" is required"#.into()),
    };
    match faces::label(name, cat) {
        Ok(()) => Response::Json(format!(
            r#"{{"ok":true,"name":"{}","cat":"{}"}}"#,
            name.escape_debug(),
            cat.escape_debug()
        )),
        Err(e @ (faces::FaceError::InvalidName | faces::FaceError::InvalidCat(_))) => {
            Response::BadRequest(e.to_string())
        }
        Err(faces::FaceError::NotFound) => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

fn feeds_list(capture: &feed_capture::FeedCapture) -> Response {
    match capture.list_json() {
        Ok(json) => Response::Json(json),
        Err(e) => Response::Error(e.to_string()),
    }
}

fn feeds_get(capture: &feed_capture::FeedCapture, name: &str) -> Response {
    match capture.read_file(name) {
        Ok(bytes) => Response::Blob("video/h264", bytes),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Response::NotFound,
        Err(e) if e.kind() == std::io::ErrorKind::InvalidInput => {
            Response::BadRequest("invalid file name".into())
        }
        Err(e) => Response::Error(e.to_string()),
    }
}

fn ble_advertise_write(req: &Request, ble_adv: &BleAdv) -> Response {
    let on = match json_field(&req.body, "on") {
        Some(v) => v != "false",
        None => return Response::BadRequest(r#""on" is required"#.into()),
    };
    let seconds = json_field(&req.body, "seconds").and_then(|v| v.parse::<u64>().ok());
    match ble_adv.set(on, seconds) {
        Ok(()) => Response::Json(ble_adv.status_json()),
        Err(e) => Response::Error(format!("bus send failed: {e}")),
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

fn cloud_write(req: &Request) -> Response {
    let enabled = match json_field(&req.body, "enabled") {
        Some(v) => v != "false",
        None => return Response::BadRequest(r#""enabled" is required"#.into()),
    };
    let result = if enabled { cloud::enable() } else { cloud::disable() };
    match result {
        Ok(_) => Response::Json(cloud::status_json()),
        Err(e) => Response::Error(e.to_string()),
    }
}

fn wifi_connect(req: &Request) -> Response {
    let ssid = match json_field(&req.body, "ssid") {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return Response::BadRequest(r#""ssid" is required"#.into()),
    };
    let psk = json_field(&req.body, "psk").filter(|p| !p.is_empty()).map(str::to_string);
    match wifi::connect(&ssid, psk.as_deref()) {
        Ok(_) => Response::Json(wifi::status_json()),
        Err(e @ wifi::Error::PskRequired) => Response::BadRequest(e.to_string()),
        Err(e) => Response::Error(e.to_string()),
    }
}

fn wifi_forget(req: &Request) -> Response {
    let ssid = match json_field(&req.body, "ssid") {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return Response::BadRequest(r#""ssid" is required"#.into()),
    };
    match wifi::forget(&ssid) {
        Ok(()) => Response::Json(wifi::status_json()),
        Err(e @ wifi::Error::Command(_)) => Response::Error(e.to_string()),
        Err(e) => Response::BadRequest(e.to_string()),
    }
}

fn feed(req: &Request, ble: &Sender, capture: &feed_capture::FeedCapture) -> Response {
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
    // Before sending: leave a note the feed-capture watcher can claim the instant it sees the
    // feeding flag rise, so the resulting before/after pair is attributed to this exact call
    // (amounts, id, manual=true) instead of falling back to an unattributed "scheduled-*" id.
    capture.note_manual_feed(id.clone(), a1, a2);
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
