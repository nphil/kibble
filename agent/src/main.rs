//! kibbled — local control for a Petkit YumShare Dual feeder, running on the feeder itself.
//!
//! What it does today:
//!   GET  /state          live telemetry from the shared config
//!   POST /feed           {"hopper": 1|2|"both", "amount": N, "id": "..."}  dispense
//!   POST /feed/cancel    stop a dispense in progress
//!
//! What it deliberately does not do: talk to any cloud, replace any vendor process, or write to
//! flash. It sits beside the stock firmware and speaks its internal bus.

mod bus;
mod http;
mod state;

use std::net::TcpListener;
use std::time::{SystemTime, UNIX_EPOCH};

use bus::{msg, FeedCtrl, Peer, Sender};
use http::{json_field, Request, Response};
use state::Shm;

/// `src` we stamp on bus messages. Stock `ctrl` is 1; replies to our feed land in its queue,
/// where it handles them exactly as it would a cloud-originated feed. That keeps the vendor's
/// own feed accounting (event log, cloud counters while it still runs) consistent.
const SRC_AS_CTRL: u16 = Peer::Ctrl as u16;

const DEFAULT_BIND: &str = "0.0.0.0:8765";

fn main() {
    let bind = std::env::args().nth(1).unwrap_or_else(|| DEFAULT_BIND.to_string());

    let shm = Shm::open().unwrap_or_else(|e| die(&format!("open {}: {e}", state::SHM_PATH)));
    let ble = Sender::open(Peer::Ble, SRC_AS_CTRL)
        .unwrap_or_else(|e| die(&format!("open ble queue: {e}")));
    let listener = TcpListener::bind(&bind).unwrap_or_else(|e| die(&format!("bind {bind}: {e}")));
    eprintln!("kibbled: listening on {bind}");

    let _ = http::serve(listener, |req| route(req, &shm, &ble));
}

fn die(msg: &str) -> ! {
    eprintln!("kibbled: {msg}");
    std::process::exit(1)
}

fn route(req: &Request, shm: &Shm, ble: &Sender) -> Response {
    match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/state") => Response::Json(shm.snapshot().to_json()),
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
        _ => Response::NotFound,
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
