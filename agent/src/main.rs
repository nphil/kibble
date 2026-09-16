//! kibbled — local control for a Petkit YumShare Dual feeder, running on the feeder itself.
//!
//! What it does today:
//!   GET    /state                   live telemetry from the shared config
//!   GET    /config                  current value of every device setting this study has mapped
//!   POST   /config                  {"key": "volume", "value": 5}  change one setting, verified
//!                                   subset only
//!   POST   /feed                    {"hopper": 1|2|"both", "amount": N, "id": "..."}  dispense
//!   POST   /feed/cancel             stop a dispense in progress
//!   GET    /schedule                kibbled's cached copy of the feed schedule plus, per entry,
//!                                   its next local fire time and last resolved outcome -- see
//!                                   schedule.rs/scheduler.rs (the MCU has no schedule read-back;
//!                                   kibbled itself, not the MCU, is what actually fires a feed,
//!                                   disabled by default behind `KIBBLE_SCHEDULER_ENABLED`)
//!   PUT    /schedule                {"entries": [...]}  replace the whole table
//!   POST   /schedule/entry          {"time": "HH:MM", "amount_l": N, "amount_r": N,
//!                                   "id": "...", "enabled": bool}  add one entry
//!   DELETE /schedule/entry?id=      remove one entry
//!   POST   /schedule/entry/enabled  {"id": "...", "enabled": bool}  enable/disable one entry
//!   POST   /speak                   raw body = signed 16-bit LE mono 16kHz PCM; normalizes to
//!                                   the vendor prompts' loudness, encodes, and plays it once
//!                                   (409 if the speaker already has a writer -- see audioout.rs)
//!   GET    /clips                   `[{"name","bytes"}, ...]` every stored clip
//!   PUT    /clips/<name>            raw PCM body, same format as /speak; normalizes, encodes,
//!                                   and saves it under that name (`/opt/kibble/clips/`)
//!   GET    /clips/<name>            the stored clip's encoded bytes (`audio/aac`)
//!   DELETE /clips/<name>            remove a stored clip
//!   POST   /clips/<name>/play       plays a stored clip through the speaker (409 as above)
//!   RTSP   :8554/main               live H.264 + AAC mic video (ring's "main" channel,
//!                                   1728x1080), zero re-encode; backchannel when negotiated
//!   RTSP   :8554/sub                live H.264 + AAC mic video (ring's "sub" channel,
//!                                   1152x720@25fps), zero re-encode; backchannel when negotiated
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
//!   GET    /events/<file>           one detection's raw crop bytes (`image/jpeg`), for every
//!                                   class (`face`/`visit`/`eat`) -- `Detection.image` names it
//!   GET    /faces/pending           `[{"name","ts","vendor_pet_id","guess":{"cat","score"}|null}]`
//!                                   pending face crops awaiting a human label, newest last (see
//!                                   faces.rs); `ts`/`vendor_pet_id` are parsed from the
//!                                   filename, `guess` is the classifier's cached verdict
//!   GET    /faces/pending/<name>    one pending crop's raw JPEG bytes
//!   POST   /faces/label             {"name": "...", "cat": "..."}  moves a pending crop into
//!                                   permanent, cat-named storage
//!   POST   /faces/unlabel           {"name": "...", "cat": "..."}  moves a labelled crop back
//!                                   into the pending queue (undo, or the first half of a
//!                                   re-label -- follow with another POST /faces/label)
//!   GET    /faces/current           raw JPEG of the crop to review: oldest pending, else the
//!                                   most recently labelled crop (see faces.rs's review_target)
//!   GET    /faces/current/info      {"status":"pending"|"labelled"|"none","name","cat"} --
//!                                   metadata for the image above
//!   GET    /faces/samples/<cat>     `[{"name","ts"}]` every labelled sample of one cat, oldest
//!                                   first; unknown cat is a 404
//!   GET    /faces/samples/<cat>/<name>  one labelled sample's raw JPEG bytes
//!   GET    /cats                    [{"name","samples","last_seen","avatar"}] every enrolled
//!                                   cat (see catid.rs/faces.rs's Gallery); `avatar` is the
//!                                   sample name nearest the cat's centroid, or null
//!   POST   /cats                    {"name": "..."}  pre-register a cat with zero samples
//!   GET    /identify                {"cat","score","second_best","crop","source","ts"} --
//!                                   Kibble's own classifier's best guess for the newest
//!                                   pending crop, or ground truth from the most recently
//!                                   labelled one once the queue is empty -- see docs/27-cat-id.md
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

mod adts;
mod advertise;
mod ai;
mod audioout;
mod backchannel;
mod backup;
mod bus;
mod clips;
mod catid;
mod cloud;
mod desired;
mod embed;
mod faces;
mod feed_capture;
mod g711;
mod health;
mod http;
mod md5;
mod persist;
mod localtime;
mod push;
mod rfc3640;
mod ring;
mod rtsp;
mod schedule;
mod scheduler;
mod settings;
mod sha1;
mod state;
mod wifi;

use std::fs;
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

/// `src` we stamp on bus messages: `ctrl`'s own queue id, exactly as `ctrl!dispatch_send_msg`
/// stamps it (it copies a process-global "my queue id", docs/24-onboard-ai.md §3). Until
/// 2026-09-16 this evaluated to `1` because `Peer::Ctrl` was misnumbered (see `bus.rs`); every
/// proven feed went out with `src=1`. No `ble` handler is known to read `src` (it only appears
/// in `dispatch_mqueue_read`'s log line), so `2` is expected to be equally accepted -- the next
/// scheduled feed is the confirmation.
const SRC_AS_CTRL: u16 = Peer::Ctrl as u16;

const DEFAULT_BIND: &str = "0.0.0.0:8765";
const RTSP_BIND: &str = "0.0.0.0:8554";

fn main() {
    let health = health::record_start();
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
    eprintln!(
        "kibbled: start_count={} (this boot; {} = 1 means first start since /opt/kibble/health.json was last cleared)",
        health.start_count,
        health.start_count
    );

    let shm = Shm::open().unwrap_or_else(|e| die(&format!("open {}: {e}", state::SHM_PATH)));
    // Shared, not leaked: the request-handling closure below borrows it for the life of the
    // (never-returning) `http::serve` call, and the reconciler owns its own clone of the `Arc`.
    let shm = Arc::new(shm);
    let ble = Sender::open(Peer::Ble, SRC_AS_CTRL)
        .unwrap_or_else(|e| die(&format!("open ble queue: {e}")));
    let ble_adv = advertise::BleAdv::spawn()
        .unwrap_or_else(|e| die(&format!("open ble_adv queue: {e}")));
    let schedule = Arc::new(
        Schedule::load(PathBuf::from(schedule::CACHE_PATH))
            .unwrap_or_else(|e| die(&format!("load {}: {e}", schedule::CACHE_PATH))),
    );
    let listener = TcpListener::bind(&bind).unwrap_or_else(|e| die(&format!("bind {bind}: {e}")));

    // Scans every labelled crop on disk and computes/caches any embedding not already cached --
    // see `faces::Gallery::load`. A one-time startup cost, not on any request's critical path.
    let gallery = Arc::new(faces::Gallery::load());

    let main_feed = VideoFeed::new();
    let sub_feed = VideoFeed::new();
    let audio_feed = ring::AudioFeed::new();
    let _poller = ring::spawn(main_feed.clone(), sub_feed.clone(), audio_feed.clone())
        .unwrap_or_else(|e| die(&format!("open {}: {e}", ring::RING_PATH)));
    let speaker_owner = audioout::SpeakerOwner::new();
    let rtsp_listener =
        TcpListener::bind(RTSP_BIND).unwrap_or_else(|e| die(&format!("bind {RTSP_BIND}: {e}")));
    let capture = feed_capture::spawn(Arc::clone(&shm), Arc::clone(&sub_feed));
    let feeds = Arc::new(rtsp::Feeds {
        main: main_feed,
        sub: sub_feed,
        audio: audio_feed,
        speaker_owner: Arc::clone(&speaker_owner),
    });
    let _rtsp = rtsp::spawn(rtsp_listener, Arc::clone(&feeds));
    let ai_feed = ai::spawn(Arc::clone(&gallery), Arc::clone(&shm));

    eprintln!(
        "kibbled: listening on {bind}, rtsp on {RTSP_BIND} (/main chan {}, /sub chan {})",
        ring::CHAN_MAIN,
        ring::CHAN_SUB
    );

    cloud::spawn_reconciler();
    persist::spawn_reconciler(Arc::clone(&shm));
    wifi::spawn_reconciler();

    // Read the device's real, configured zone once at startup (STUDY-schedule-encoding.md
    // §11.1 item 2 / localtime.rs's own doc): refuse to run the scheduler at all, loudly, rather
    // than guess a DST rule, if it isn't one this project has a table for. Disabled by default
    // either way -- see `scheduler.rs`'s module doc for the two independent ways to turn it on.
    let timezone_name = shm.timezone_name();
    let tz = localtime::tz_for_iana_name(&timezone_name);
    let scheduler_flag_on = scheduler::enabled();
    let scheduler_enabled = scheduler_flag_on && tz.is_some();
    match (scheduler_flag_on, tz) {
        (true, Some(_)) => {
            let scheduler_ble = Sender::open(Peer::Ble, SRC_AS_CTRL)
                .unwrap_or_else(|e| die(&format!("open ble queue for scheduler: {e}")));
            scheduler::spawn(Arc::clone(&schedule), scheduler_ble, tz.unwrap());
            eprintln!(
                "kibbled: scheduler ENABLED (zone={timezone_name:?}) -- will dispense directly at each enabled entry's local time"
            );
        }
        (true, None) => eprintln!(
            "kibbled: scheduler flag is ON but config_shm reports timezone {timezone_name:?}, which \
             kibbled has no DST rule for -- REFUSING to schedule (a feed at the wrong local time is \
             worse than no feed at all; see GET /state's scheduler_tz_supported, and extend \
             localtime::tz_for_iana_name to add this zone)"
        ),
        (false, _) => eprintln!(
            "kibbled: scheduler disabled (default) -- set {}=1 or {:?}=1 in {} to enable",
            scheduler::ENV_ENABLED,
            scheduler::SETTINGS_KEY,
            crate::desired::PATH
        ),
    }

    // Local push (docs/33-local-push.md): every frame body comes from the exact function
    // the matching GET route uses, so HA's parsers see one shape whichever path delivered it.
    {
        let shm = Arc::clone(&shm);
        let schedule = Arc::clone(&schedule);
        let ai_feed = Arc::clone(&ai_feed);
        let capture = Arc::clone(&capture);
        let gallery = Arc::clone(&gallery);
        let serialize: push::Serialize = Arc::new(move |f| match f {
            push::Field::State => state_json(&shm, health),
            push::Field::Schedule => schedule_status_json(&schedule, scheduler_enabled, tz),
            push::Field::Config => settings::to_json(&shm),
            push::Field::Cloud => cloud::status_json(),
            push::Field::Wifi => wifi::status_json(),
            push::Field::WifiScan => wifi::scan_json(),
            push::Field::Cats => gallery.cats_json(),
            push::Field::Identify => identify_json(&gallery).unwrap_or_else(|_| "null".into()),
            push::Field::ReviewFace => faces_current_info_json().unwrap_or_else(|_| "null".into()),
            push::Field::PendingFaces => faces_pending_json().unwrap_or_else(|_| "null".into()),
            push::Field::Clips => clips_json(),
            push::Field::Feeds => feeds_json(&capture).unwrap_or_else(|_| "null".into()),
            push::Field::Events => ai_feed.snapshot_json(),
        });
        match push::spawn(serialize) {
            Ok(()) => eprintln!("kibbled: push listening on {}", push::PUSH_BIND),
            Err(e) => eprintln!("kibbled: push disabled: bind {}: {e}", push::PUSH_BIND),
        }
    }

    let _ = http::serve(listener, |req| {
        route(
            req,
            &shm,
            &ble,
            &ble_adv,
            &schedule,
            scheduler_enabled,
            tz,
            &feeds,
            &ai_feed,
            &capture,
            &speaker_owner,
            &gallery,
            health,
        )
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
    schedule: &Schedule,
    scheduler_enabled: bool,
    tz: Option<localtime::Tz>,
    feeds: &rtsp::Feeds,
    ai_feed: &ai::Feed,
    capture: &feed_capture::FeedCapture,
    speaker_owner: &Arc<audioout::SpeakerOwner>,
    gallery: &faces::Gallery,
    health: health::Health,
) -> Response {
    let (path, query) = http::split_query(&req.path);
    match (req.method.as_str(), path) {
        ("GET", "/state") => Response::Json(state_json(shm, health)),
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
        ("GET", "/schedule") => Response::Json(schedule_status_json(schedule, scheduler_enabled, tz)),
        ("PUT", "/schedule") => put_schedule(req, schedule, ble, scheduler_enabled, tz),
        ("POST", "/schedule/entry") => post_schedule_entry(req, schedule, ble, scheduler_enabled, tz),
        ("DELETE", "/schedule/entry") => delete_schedule_entry(query, schedule, ble, scheduler_enabled, tz),
        ("POST", "/schedule/entry/enabled") => {
            post_schedule_entry_enabled(req, schedule, ble, scheduler_enabled, tz)
        }
        ("GET", "/events") => Response::Json(ai_feed.snapshot_json()),
        ("GET", "/events/stream") => events_stream(query, ai_feed),
        ("GET", p) if p.starts_with("/events/") => events_file_get(&p["/events/".len()..]),
        ("GET", "/faces/pending") => json_response(faces_pending_json()),
        ("GET", p) if p.starts_with("/faces/pending/") => {
            faces_pending_get(&p["/faces/pending/".len()..])
        }
        ("POST", "/faces/label") => faces_label_post(req, gallery),
        ("POST", "/faces/unlabel") => faces_unlabel_post(req, gallery),
        ("GET", "/faces/current") => faces_current_get(),
        ("GET", "/faces/current/info") => json_response(faces_current_info_json()),
        ("GET", p) if p.starts_with("/faces/samples/") => {
            faces_samples_route(&p["/faces/samples/".len()..])
        }
        ("GET", "/cats") => cats_get(gallery),
        ("POST", "/cats") => cats_post(req, gallery),
        ("GET", "/identify") => json_response(identify_json(gallery)),
        ("GET", "/feeds") => json_response(feeds_json(capture)),
        ("GET", p) if p.starts_with("/feeds/") => feeds_get(capture, &p["/feeds/".len()..]),
        ("GET", "/ble") => Response::Json(ble_adv.status_json()),
        ("POST", "/ble/advertise") => ble_advertise_write(req, ble_adv),
        ("GET", "/wifi") => Response::Json(wifi::status_json()),
        ("GET", "/wifi/scan") => Response::Json(wifi::scan_json()),
        ("POST", "/wifi/connect") => wifi_connect(req),
        ("POST", "/wifi/forget") => wifi_forget(req),
        ("GET", "/audio") => Response::Json(audio_status_json()),
        ("POST", "/audio") => audio_write(req),
        ("POST", "/speak") => speak(req, speaker_owner),
        ("GET", "/clips") => Response::Json(clips_json()),
        (method, p) if p.starts_with("/clips/") => {
            clip_route(method, &p["/clips/".len()..], req, speaker_owner)
        }
        _ => Response::NotFound,
    }
}

/// Merges kibbled's own process-health fields (docs/23-audio-codec.md §19 -- visible restart
/// tracking, so a crash loop shows up on a dashboard instead of needing a kernel-log
/// investigation) onto the vendor-state JSON `Snapshot::to_json` already builds, rather than
/// teaching `state.rs` (which is otherwise only about the vendor's own `config_shm`) about
/// kibbled's own bookkeeping.
fn state_json(shm: &Shm, health: health::Health) -> String {
    let base = shm.snapshot().to_json();
    let exit_code = health::last_exit_code().map_or("null".to_string(), |c| c.to_string());
    format!(
        r#"{},"kibbled_start_count":{},"kibbled_last_start_unix":{},"kibbled_last_exit_code":{}}}"#,
        &base[..base.len() - 1],
        health.start_count,
        health.last_start_unix,
        exit_code,
    )
}

/// `GET /audio`: whether the off-by-default audio gate (`audioout::enabled`,
/// docs/23-audio-codec.md §19) is currently on.
fn audio_status_json() -> String {
    let last = audioout::LAST_STATS.lock().unwrap_or_else(|p| p.into_inner());
    format!(
        r#"{{"enabled":{},"last_session":{}}}"#,
        audioout::enabled(),
        last.map_or_else(|| "null".to_string(), |s| s.to_json())
    )
}

fn audio_write(req: &Request) -> Response {
    let enabled = match json_field(&req.body_str(), "enabled") {
        Some(v) => v != "false",
        None => return Response::BadRequest(r#""enabled" is required"#.into()),
    };
    match audioout::set_enabled(enabled) {
        Ok(()) => Response::Json(format!(r#"{{"enabled":{enabled}}}"#)),
        Err(e) => Response::Error(e.to_string()),
    }
}

/// `GET /events/stream?since=N`: `since` defaults to 0 (an HA/Scrypted client's first call gets
/// everything currently buffered, exactly like `GET /events`, then remembers the highest `seq`
/// it saw for the next call).
fn events_stream(query: &str, ai_feed: &ai::Feed) -> Response {
    let since: u64 = http::query_field(query, "since").and_then(|v| v.parse().ok()).unwrap_or(0);
    Response::Json(ai_feed.wait_since(since, ai::LONG_POLL_TIMEOUT))
}

/// `GET /events/<file>`: one detection crop's raw bytes -- mirrors `faces_pending_get` exactly
/// (same path-safety rules, same content type; see `ai::read_event`'s doc for why this route
/// exists: `Detection.image` names files here for every class, not just `face`).
fn events_file_get(name: &str) -> Response {
    match ai::read_event(name) {
        Ok(bytes) => Response::Blob("image/jpeg", bytes),
        Err(ai::EventFileError::InvalidName) => Response::BadRequest("invalid file name".into()),
        Err(ai::EventFileError::NotFound) => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

/// `Result<json, error>` -> HTTP. The JSON producers below are shared with the push channel
/// (`push::Serialize` in `main`), which is why they don't build a `Response` themselves.
fn json_response(r: Result<String, String>) -> Response {
    match r {
        Ok(json) => Response::Json(json),
        Err(e) => Response::Error(e),
    }
}

fn faces_pending_json() -> Result<String, String> {
    let crops = faces::list_pending_full().map_err(|e| e.to_string())?;
    let items: Vec<String> = crops.iter().map(pending_crop_json).collect();
    Ok(format!("[{}]", items.join(",")))
}

fn pending_crop_json(c: &faces::PendingCrop) -> String {
    let vendor_pet_id = c.vendor_pet_id.map_or("null".to_string(), |v| v.to_string());
    let guess = match &c.guess {
        Some(g) => format!(r#"{{"cat":"{}","score":{}}}"#, g.cat.escape_debug(), g.score),
        None => "null".to_string(),
    };
    format!(
        r#"{{"name":"{}","ts":{},"vendor_pet_id":{},"guess":{}}}"#,
        c.name.escape_debug(),
        c.ts,
        vendor_pet_id,
        guess,
    )
}

/// Routes both `GET /faces/samples/<cat>` (`rest` has no further `/`) and
/// `GET /faces/samples/<cat>/<name>` (the rest of the path after the first `/`).
fn faces_samples_route(rest: &str) -> Response {
    match rest.split_once('/') {
        Some((cat, name)) => faces_sample_get(cat, name),
        None => faces_samples_json(rest),
    }
}

fn faces_samples_json(cat: &str) -> Response {
    match faces::list_samples(cat) {
        Ok(samples) => {
            let items: Vec<String> = samples
                .iter()
                .map(|s| format!(r#"{{"name":"{}","ts":{}}}"#, s.name.escape_debug(), s.ts))
                .collect();
            Response::Json(format!("[{}]", items.join(",")))
        }
        Err(faces::FaceError::InvalidCat(_)) => Response::BadRequest("invalid cat".into()),
        Err(faces::FaceError::NotFound) => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

fn faces_sample_get(cat: &str, name: &str) -> Response {
    match faces::read_sample(cat, name) {
        Ok(bytes) => Response::Blob("image/jpeg", bytes),
        Err(faces::FaceError::InvalidName) => Response::BadRequest("invalid file name".into()),
        Err(faces::FaceError::InvalidCat(_)) => Response::BadRequest("invalid cat".into()),
        Err(faces::FaceError::NotFound) => Response::NotFound,
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

fn faces_label_post(req: &Request, gallery: &faces::Gallery) -> Response {
    let body = req.body_str();
    let name = match json_field(&body, "name").filter(|s| !s.is_empty()) {
        Some(n) => n,
        None => return Response::BadRequest(r#""name" is required"#.into()),
    };
    let cat = match json_field(&body, "cat").filter(|s| !s.is_empty()) {
        Some(c) => c,
        None => return Response::BadRequest(r#""cat" is required"#.into()),
    };
    match faces::label(name, cat) {
        Ok(()) => {
            let dest = PathBuf::from(faces::FACES_ROOT).join(cat).join(name);
            match faces::ensure_embedding(&dest) {
                Ok(feat) => gallery.on_labelled(cat, &feat),
                Err(e) => eprintln!("kibbled: faces: embed after label {}: {e}", dest.display()),
            }
            Response::Json(format!(
                r#"{{"ok":true,"name":"{}","cat":"{}"}}"#,
                name.escape_debug(),
                cat.escape_debug()
            ))
        }
        Err(e @ (faces::FaceError::InvalidName | faces::FaceError::InvalidCat(_))) => {
            Response::BadRequest(e.to_string())
        }
        Err(faces::FaceError::NotFound) => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

fn faces_unlabel_post(req: &Request, gallery: &faces::Gallery) -> Response {
    let body = req.body_str();
    let name = match json_field(&body, "name").filter(|s| !s.is_empty()) {
        Some(n) => n,
        None => return Response::BadRequest(r#""name" is required"#.into()),
    };
    let cat = match json_field(&body, "cat").filter(|s| !s.is_empty()) {
        Some(c) => c,
        None => return Response::BadRequest(r#""cat" is required"#.into()),
    };
    // Computed before the move below, from whichever directory the crop is in right now --
    // `faces::unlabel` renames it away, and a `.emb` sidecar right alongside travels with it.
    let src = PathBuf::from(faces::FACES_ROOT).join(cat).join(name);
    let feat = faces::ensure_embedding(&src).ok();
    match faces::unlabel(cat, name) {
        Ok(()) => {
            if let Some(feat) = feat {
                gallery.on_unlabelled(cat, &feat);
            }
            Response::Json(format!(
                r#"{{"ok":true,"name":"{}","cat":"{}"}}"#,
                name.escape_debug(),
                cat.escape_debug()
            ))
        }
        Err(e @ (faces::FaceError::InvalidName | faces::FaceError::InvalidCat(_))) => {
            Response::BadRequest(e.to_string())
        }
        Err(faces::FaceError::NotFound) => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

fn faces_current_get() -> Response {
    match faces::review_target() {
        Ok(Some(target)) => match fs::read(target.jpg_path()) {
            Ok(bytes) => Response::Blob("image/jpeg", bytes),
            Err(_) => Response::NotFound,
        },
        Ok(None) => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

fn faces_current_info_json() -> Result<String, String> {
    match faces::review_target().map_err(|e| e.to_string())? {
        Some(faces::FaceTarget::Pending { name }) => Ok(format!(
            r#"{{"status":"pending","name":"{}","cat":null}}"#,
            name.escape_debug()
        )),
        Some(faces::FaceTarget::Labelled { cat, name }) => Ok(format!(
            r#"{{"status":"labelled","name":"{}","cat":"{}"}}"#,
            name.escape_debug(),
            cat.escape_debug()
        )),
        None => Ok(r#"{"status":"none","name":null,"cat":null}"#.into()),
    }
}

fn cats_get(gallery: &faces::Gallery) -> Response {
    Response::Json(gallery.cats_json())
}

fn cats_post(req: &Request, gallery: &faces::Gallery) -> Response {
    let body = req.body_str();
    let name = match json_field(&body, "name").filter(|s| !s.is_empty()) {
        Some(n) => n,
        None => return Response::BadRequest(r#""name" is required"#.into()),
    };
    match gallery.add_cat(name) {
        Ok(()) => Response::Json(format!(r#"{{"ok":true,"name":"{}"}}"#, name.escape_debug())),
        Err(e @ faces::FaceError::InvalidCat(_)) => Response::BadRequest(e.to_string()),
        Err(e) => Response::Error(e.to_string()),
    }
}

/// `GET /identify`: Kibble's own classifier's opinion of the newest pending crop, or ground
/// truth from the most recently labelled one once the queue is empty (`source` distinguishes
/// the two -- see the module doc and `docs/27-cat-id.md`).
fn identify_json(gallery: &faces::Gallery) -> Result<String, String> {
    let target = faces::identify_target().map_err(|e| e.to_string())?;
    let Some(target) = target else {
        return Ok(
            r#"{"cat":null,"score":null,"second_best":null,"crop":null,"source":null,"ts":null}"#
                .into(),
        );
    };
    let path = target.jpg_path();
    let ts = fs::metadata(&path)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs());
    let ts_json = ts.map_or("null".to_string(), |t| t.to_string());
    let crop_json = format!("\"{}\"", target.name().escape_debug());
    match target {
        faces::FaceTarget::Labelled { cat, .. } => Ok(format!(
            r#"{{"cat":"{}","score":null,"second_best":null,"crop":{crop_json},"source":"labelled","ts":{ts_json}}}"#,
            cat.escape_debug(),
        )),
        faces::FaceTarget::Pending { .. } => {
            let feat = faces::ensure_embedding(&path).map_err(|e| e.to_string())?;
            let (cat_json, score_json, second_json) = match gallery.identify(&feat) {
                catid::Verdict::Known { cat, score, second_best } => (
                    format!("\"{}\"", cat.escape_debug()),
                    score.to_string(),
                    second_best.map_or("null".to_string(), |s| {
                        format!(r#"{{"cat":"{}","score":{}}}"#, s.cat.escape_debug(), s.score)
                    }),
                ),
                catid::Verdict::Unknown { .. } => {
                    ("\"unknown\"".to_string(), "null".to_string(), "null".to_string())
                }
            };
            Ok(format!(
                r#"{{"cat":{cat_json},"score":{score_json},"second_best":{second_json},"crop":{crop_json},"source":"classifier","ts":{ts_json}}}"#
            ))
        }
    }
}

fn feeds_json(capture: &feed_capture::FeedCapture) -> Result<String, String> {
    capture.list_json().map_err(|e| e.to_string())
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
    let on = match json_field(&req.body_str(), "on") {
        Some(v) => v != "false",
        None => return Response::BadRequest(r#""on" is required"#.into()),
    };
    let seconds = json_field(&req.body_str(), "seconds").and_then(|v| v.parse::<u64>().ok());
    match ble_adv.set(on, seconds) {
        Ok(()) => Response::Json(ble_adv.status_json()),
        Err(e) => Response::Error(format!("bus send failed: {e}")),
    }
}

fn config_write(req: &Request) -> Response {
    let body = req.body_str();
    let key = match json_field(&body, "key") {
        Some(k) if !k.is_empty() => k,
        _ => return Response::BadRequest(r#""key" is required"#.into()),
    };
    let value: u32 = match json_field(&body, "value").and_then(|v| v.parse().ok()) {
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
    let enabled = match json_field(&req.body_str(), "enabled") {
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
    let ssid = match json_field(&req.body_str(), "ssid") {
        Some(s) if !s.is_empty() => s.to_string(),
        _ => return Response::BadRequest(r#""ssid" is required"#.into()),
    };
    let psk = json_field(&req.body_str(), "psk").filter(|p| !p.is_empty()).map(str::to_string);
    match wifi::connect(&ssid, psk.as_deref()) {
        Ok(_) => Response::Json(wifi::status_json()),
        Err(e @ wifi::Error::PskRequired) => Response::BadRequest(e.to_string()),
        Err(e) => Response::Error(e.to_string()),
    }
}

fn wifi_forget(req: &Request) -> Response {
    let ssid = match json_field(&req.body_str(), "ssid") {
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
    let amount: u8 = match json_field(&req.body_str(), "amount").and_then(|v| v.parse().ok()) {
        Some(n) if (1..=20).contains(&n) => n,
        _ => return Response::BadRequest("amount must be 1..=20 portions".into()),
    };
    let (a1, a2) = match json_field(&req.body_str(), "hopper").unwrap_or("1") {
        "1" => (amount, 0),
        "2" => (0, amount),
        "both" => (amount, amount),
        _ => return Response::BadRequest(r#"hopper must be 1, 2 or "both""#.into()),
    };
    let id = json_field(&req.body_str(), "id")
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

fn put_schedule(
    req: &Request,
    schedule: &Schedule,
    ble: &Sender,
    scheduler_enabled: bool,
    tz: Option<localtime::Tz>,
) -> Response {
    let entries = match schedule::parse_entries(&req.body_str()) {
        Ok(v) => v,
        Err(e) => return Response::BadRequest(e),
    };
    schedule_result(schedule.replace(entries, ble, schedule::now_unix()), schedule, scheduler_enabled, tz)
}

fn post_schedule_entry(
    req: &Request,
    schedule: &Schedule,
    ble: &Sender,
    scheduler_enabled: bool,
    tz: Option<localtime::Tz>,
) -> Response {
    let entry = match schedule::parse_entry(&req.body_str()) {
        Ok(e) => e,
        Err(e) => return Response::BadRequest(e),
    };
    schedule_result(schedule.add(entry, ble, schedule::now_unix()), schedule, scheduler_enabled, tz)
}

fn delete_schedule_entry(
    query: &str,
    schedule: &Schedule,
    ble: &Sender,
    scheduler_enabled: bool,
    tz: Option<localtime::Tz>,
) -> Response {
    let id = match schedule::entry_id_from_query(query) {
        Some(id) => id,
        None => return Response::BadRequest("missing ?id=".into()),
    };
    schedule_result(schedule.remove(id, ble, schedule::now_unix()), schedule, scheduler_enabled, tz)
}

fn post_schedule_entry_enabled(
    req: &Request,
    schedule: &Schedule,
    ble: &Sender,
    scheduler_enabled: bool,
    tz: Option<localtime::Tz>,
) -> Response {
    let id = match json_field(&req.body_str(), "id").filter(|s| !s.is_empty()) {
        Some(id) => id.to_string(),
        None => return Response::BadRequest("missing \"id\"".into()),
    };
    let enabled = match json_field(&req.body_str(), "enabled") {
        Some(v) => v != "false",
        None => return Response::BadRequest("missing \"enabled\"".into()),
    };
    schedule_result(schedule.set_enabled(&id, enabled, ble, schedule::now_unix()), schedule, scheduler_enabled, tz)
}

/// `GET /schedule`'s body: the cache plus, per entry, its next local fire time and last
/// resolved outcome -- see `schedule::Schedule::snapshot_json`. `tz` is `None` exactly when
/// `main.rs` couldn't resolve the device's configured zone (see `localtime::tz_for_iana_name`),
/// in which case every entry's `next_fire_utc` reads `null` -- never a guess. `now_utc` is
/// computed fresh on every call (never cached), matching `scheduler.rs`'s own "always recompute
/// from the current wall clock" rule.
fn schedule_status_json(schedule: &Schedule, scheduler_enabled: bool, tz: Option<localtime::Tz>) -> String {
    schedule.snapshot_json(tz, schedule::now_unix() as i64, scheduler_enabled)
}

/// Shared success/error -> HTTP mapping for every schedule mutation: on success, echo the fresh
/// cache so a client sees the effect immediately with no extra `GET`; `Invalid` is a 400 (the
/// caller's fault -- bad input, unknown id, over the cap), `Internal` is a 500 (ours -- cache I/O,
/// bus send).
fn schedule_result(
    result: Result<(), schedule::Error>,
    schedule: &Schedule,
    scheduler_enabled: bool,
    tz: Option<localtime::Tz>,
) -> Response {
    match result {
        Ok(()) => Response::Json(schedule_status_json(schedule, scheduler_enabled, tz)),
        Err(schedule::Error::Invalid(m)) => Response::BadRequest(m),
        Err(schedule::Error::Internal(m)) => Response::Error(m),
    }
}

/// `POST /speak`: raw body upload -- signed 16-bit little-endian mono 16kHz PCM, matching the
/// pipeline's native format exactly (no container/header; see `http.rs`'s `MAX_BODY` doc
/// comment). Normalizes and encodes synchronously (a few hundred ms at most for any realistic
/// clip length -- `tools/aacenc/`'s own validation), then stages the result as a tmpfs file and
/// has `media` play it (`audioout::play_bytes`) on a spawned thread so the HTTP server stays
/// responsive for the clip's real-time duration; only acquiring the speaker and the encode step
/// can fail synchronously.
fn speak(req: &Request, speaker_owner: &Arc<audioout::SpeakerOwner>) -> Response {
    let pcm = match pcm_from_body(&req.body) {
        Ok(p) => p,
        Err(msg) => return Response::BadRequest(msg),
    };
    let guard = match speaker_owner.try_acquire() {
        Ok(g) => g,
        Err(reason) => return Response::Conflict(reason.to_string()),
    };
    let adts_bytes = match audioout::normalize_and_encode(&pcm) {
        Ok(b) => b,
        Err(e) => return Response::Error(e.to_string()),
    };
    let samples = pcm.len();
    spawn_playback(guard, "speak".to_string(), move |g| audioout::play_bytes(&adts_bytes, g));
    Response::Json(format!(
        r#"{{"ok":true,"samples":{samples},"estimated_ms":{}}}"#,
        samples as u64 * 1000 / 16_000
    ))
}

/// Runs `play` on a spawned thread (never the request-handling thread -- see `speak`'s doc
/// comment), moving the already-acquired [`audioout::OwnerGuard`] in so it releases the speaker
/// exactly when playback finishes. Logs what the audio driver actually emitted
/// (`frames_played`, from `/proc/ax_proc/ao`) against what was handed to `media`, so a silent
/// failure shows up in `/tmp/kibbled.log` as `0/N` rather than as nothing at all.
fn spawn_playback(
    guard: audioout::OwnerGuard,
    tag: String,
    play: impl FnOnce(&audioout::OwnerGuard) -> Result<audioout::PlaybackStats, audioout::SpeakError> + Send + 'static,
) {
    std::thread::spawn(move || match play(&guard) {
        Ok(stats) => {
            audioout::record_last(stats);
            eprintln!("kibbled: {tag} playback done: {}/{} frame(s) played", stats.frames_played, stats.frames_written)
        }
        Err(e) => eprintln!("kibbled: {tag} playback error: {e}"),
    });
}

/// Parses a raw PCM body: signed 16-bit little-endian mono samples, no container/header. Rejects
/// an empty body or one that isn't sample-aligned (an odd byte count can only be a malformed or
/// truncated upload).
fn pcm_from_body(body: &[u8]) -> Result<Vec<i16>, String> {
    if body.is_empty() {
        return Err("body is empty; expected raw 16-bit/16kHz/mono PCM".into());
    }
    if body.len() % 2 != 0 {
        return Err("body length must be a multiple of 2 (16-bit samples)".into());
    }
    Ok(body.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect())
}

fn clips_json() -> String {
    match clips::list() {
        Ok(list) => {
            let items: Vec<String> = list
                .iter()
                .map(|c| format!(r#"{{"name":"{}","bytes":{}}}"#, c.name.escape_debug(), c.bytes))
                .collect();
            format!("[{}]", items.join(","))
        }
        Err(e) => {
            eprintln!("kibbled: clips::list: {e}");
            "[]".to_string()
        }
    }
}

/// Dispatches every `/clips/<name>` and `/clips/<name>/play` request. `rest` is the URL after
/// the `/clips/` prefix -- either `<name>` or `<name>/play`.
fn clip_route(
    method: &str,
    rest: &str,
    req: &Request,
    speaker_owner: &Arc<audioout::SpeakerOwner>,
) -> Response {
    let (name, play) = match rest.strip_suffix("/play") {
        Some(name) => (name, true),
        None => (rest, false),
    };
    if !clips::valid_name(name) {
        return Response::BadRequest("invalid clip name".into());
    }
    match (method, play) {
        ("PUT", false) => clip_put(name, req),
        ("GET", false) => clip_get(name),
        ("DELETE", false) => clip_delete(name),
        ("POST", true) => clip_play(name, speaker_owner),
        _ => Response::NotFound,
    }
}

fn clip_put(name: &str, req: &Request) -> Response {
    let pcm = match pcm_from_body(&req.body) {
        Ok(p) => p,
        Err(msg) => return Response::BadRequest(msg),
    };
    let adts_bytes = match audioout::normalize_and_encode(&pcm) {
        Ok(b) => b,
        Err(e) => return Response::Error(e.to_string()),
    };
    match clips::save(name, &adts_bytes) {
        Ok(()) => Response::Json(format!(
            r#"{{"ok":true,"name":"{}","bytes":{}}}"#,
            name.escape_debug(),
            adts_bytes.len()
        )),
        Err(e) => Response::Error(e.to_string()),
    }
}

fn clip_get(name: &str) -> Response {
    match clips::load(name) {
        Ok(bytes) => Response::Blob("audio/aac", bytes),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

fn clip_delete(name: &str) -> Response {
    match clips::delete(name) {
        Ok(()) => Response::NoContent,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Response::NotFound,
        Err(e) => Response::Error(e.to_string()),
    }
}

/// `POST /clips/<name>/play`: `media` reads the stored clip straight from `clips::CLIPS_DIR` --
/// no copy. The clip is still read here first, to answer 404 synchronously and to know how many
/// frames to expect the driver to emit.
fn clip_play(name: &str, speaker_owner: &Arc<audioout::SpeakerOwner>) -> Response {
    let frames = match clips::load(name) {
        Ok(b) => audioout::count_frames(&b),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Response::NotFound,
        Err(e) => return Response::Error(e.to_string()),
    };
    let guard = match speaker_owner.try_acquire() {
        Ok(g) => g,
        Err(reason) => return Response::Conflict(reason.to_string()),
    };
    let path = clips::path_for(name);
    spawn_playback(guard, format!("clip {name}"), move |g| audioout::play_path(&path, frames, g));
    Response::Json(format!(r#"{{"ok":true,"frames":{frames}}}"#))
}
