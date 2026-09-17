//! BLE advertising control for the WiFi-down fallback path.
//!
//! The lever: `msg_id 0x6001` (`ble`'s `dispatch_handler_ble_set_adv`) to `dst=8`, a 4-byte
//! payload whose first byte is `1`=on/`0`=off. `ble` relays it to the T31 MCU as UART CMD `0x09`,
//! subaddr `2`. Fully traced -- three independent vendor senders (`pktool`'s `bleadv 0|1`, and
//! two call sites inside `ctrl`'s own pairing flow) converge on the exact same wire bytes -- in
//! `docs/26-ble-advertising.md`. That document's finding this module exists to act on:
//!
//! > A bare/direct send (bypassing `ctrl`'s `bind_event_start`) sets none of the config flags
//! > `ctrl`'s own 300s pairing-window timeout (`check_close_ble_broadcast`) watches. It is
//! > invisible to that timeout and will advertise until explicitly told off -- no MCU-side
//! > timeout was found either. A caller that forgets to turn it back off leaves the radio
//! > advertising indefinitely.
//!
//! So every `on` here gets its own bounded deadline, enforced independently of whatever the
//! HTTP caller does next ([`DEFAULT_SECONDS`]/[`MAX_SECONDS`], modelled on `ctrl`'s own 300s
//! window rather than an invented number). Three lines of defence, in the order a real failure
//! would hit them:
//!
//! 1. **The reconciler** ([`BleAdv::spawn`]'s background thread) turns advertising back off once
//!    its own deadline passes, whether or not anyone asks. This is the one that matters: it is
//!    the only defence against a caller that simply never sends the `off` request.
//! 2. **The shutdown handler** ([`install_shutdown_handler`]) sends one best-effort `off` on
//!    `SIGTERM`/`SIGINT` before the process exits -- the ordinary "service is being stopped"
//!    case, where a clean exit is possible.
//! 3. **Startup correction**: [`BleAdv::spawn`] sends one best-effort `off` before doing
//!    anything else, in case a *previous* instance died (crash, `SIGKILL`, power loss) before
//!    its own reconciler or shutdown handler could run and left the radio advertising. This
//!    never sends `on` -- advertising always starts this process believing it is `off`, matching
//!    every other "do not resume the last state across a restart" choice in this agent.
//!
//! None of this can prove the MCU actually stopped advertising (docs/26 §3.2: the MCU-mirrored
//! state flag's exact `config_shm` offset is unconfirmed) -- it proves kibbled always *asks* it
//! to, promptly and from every exit path this agent controls.

use std::io;
use std::os::raw::c_int;
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::bus::{self, msg, Peer, Sender};

/// Matches `ctrl`'s own pairing-window length (`bind_ctrl.c`, disassembly-confirmed `0x12c` =
/// 300s -- docs/26-ble-advertising.md §3.1) rather than inventing an unrelated number.
pub const DEFAULT_SECONDS: u64 = 300;
/// A caller that wants a longer window may ask for one (`"seconds"` in the request body), up to
/// this. Nothing here should let a single request advertise for hours if the caller forgets to
/// turn it back off; the reconciler is the backstop, not a substitute for a sane ceiling.
pub const MAX_SECONDS: u64 = 1800;

const POLL_INTERVAL: Duration = Duration::from_secs(1);

const SIGINT: c_int = 2;
const SIGTERM: c_int = 15;
type SigHandler = extern "C" fn(c_int);

extern "C" {
    fn signal(signum: c_int, handler: SigHandler) -> SigHandler;
    fn _exit(status: c_int) -> !;
}

/// `src` we stamp on our own sends; matches `main`'s `SRC_AS_CTRL` (ctrl's own queue id).
const SRC: u16 = Peer::Ctrl as u16;

/// Set once at [`BleAdv::spawn`], read only from [`handle_shutdown_signal`]. A plain atomic
/// rather than anything through the `BleAdv`/`Sender` it came from: a signal handler must not
/// take a lock the interrupted thread might already hold, and must not allocate (ruling out
/// `Sender::send`, whose queue name would need a fresh `CString`) -- see [`bus::send_raw`].
static SHUTDOWN_MQD: AtomicI32 = AtomicI32::new(-1);

/// `payload[0]` is the only byte `dispatch_handler_ble_set_adv` reads; the rest is padding to
/// the 4-byte shape every sender uses (docs/26-ble-advertising.md §1-§2).
fn payload(on: bool) -> [u8; 4] {
    [on as u8, 0, 0, 0]
}

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Pure: resolves a caller's optional `seconds` into an absolute deadline. Split out from
/// [`BleAdv::set`] for testing without a `Sender` -- same shape as `cloud::resolve_gateway`.
fn resolve_deadline(now: u64, seconds: Option<u64>) -> u64 {
    now + seconds.unwrap_or(DEFAULT_SECONDS).clamp(1, MAX_SECONDS)
}

/// Pure: `GET /ble`'s JSON body. `until` is only meaningful (and only reported) while `on`.
fn format_status(on: bool, until: u64) -> String {
    let until = if on { until.to_string() } else { "null".to_string() };
    format!(r#"{{"advertising":{on},"until":{until}}}"#)
}

struct Inner {
    on: bool,
    /// Unix seconds; meaningful only while `on`. The reconciler turns advertising off once
    /// `now_unix() >= until`.
    until: u64,
}

/// Shared handle: one `Sender` to `ble`'s queue, one mutex-protected view of "what we last
/// asked for and until when". `GET /ble` reads this -- it is deliberately *not* a live read of
/// any MCU-reported state, both because docs/26 §3.2 couldn't pin the mirror flag's offset and
/// because "what did we last ask for" is what a caller deciding whether to ask again needs.
pub struct BleAdv {
    sender: Sender,
    state: Mutex<Inner>,
}

impl BleAdv {
    /// Opens our own handle to `ble`'s queue (cheap -- `bus::Sender`'s own doc note: "the vendor
    /// does the same"), installs the `SIGTERM`/`SIGINT` handler, sends one startup correction
    /// (module doc, point 3), and starts the reconciler thread. Never sends `on`.
    pub fn spawn() -> io::Result<Arc<BleAdv>> {
        let sender = Sender::open(Peer::Ble, SRC)?;
        SHUTDOWN_MQD.store(sender.raw(), Ordering::SeqCst);
        install_shutdown_handler();

        let adv = Arc::new(BleAdv { sender, state: Mutex::new(Inner { on: false, until: 0 }) });
        // Best-effort: if this fails there is nothing more this process can do about it, and
        // the reconciler tick below will simply find nothing to correct.
        let _ = adv.sender.send(msg::BLE_SET_ADV, &payload(false));

        let bg = Arc::clone(&adv);
        std::thread::spawn(move || loop {
            bg.reconcile_once();
            std::thread::sleep(POLL_INTERVAL);
        });
        Ok(adv)
    }

    /// `POST /ble/advertise`. `seconds` (only meaningful for `on: true`) clamps to
    /// `[1, MAX_SECONDS]` and defaults to [`DEFAULT_SECONDS`].
    pub fn set(&self, on: bool, seconds: Option<u64>) -> io::Result<()> {
        self.sender.send(msg::BLE_SET_ADV, &payload(on))?;
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if on {
            state.on = true;
            state.until = resolve_deadline(now_unix(), seconds);
        } else {
            state.on = false;
            state.until = 0;
        }
        Ok(())
    }

    /// `GET /ble`'s body: `{"advertising": bool, "until": <unix seconds> | null}`.
    pub fn status_json(&self) -> String {
        let state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        format_status(state.on, state.until)
    }

    /// Runs on every [`POLL_INTERVAL`] tick from the background thread [`BleAdv::spawn`]
    /// starts. Only ever acts on a deadline *we* armed -- this never turns advertising on, and
    /// never turns it off based on anything but our own recorded `until`, so it cannot fight a
    /// pairing session someone starts by other means (e.g. the physical button).
    fn reconcile_once(&self) {
        // Send after releasing the lock, matching `set`'s own order: `bus::Sender::send` opens
        // its mqd without O_NONBLOCK (`bus.rs`), so it can block if `ble`'s queue is ever full --
        // holding `state` across that call would stall `set` (called synchronously from the HTTP
        // handler for `POST /ble/advertise`) waiting on the same lock, which would stall the
        // whole single-threaded HTTP server behind it.
        let turned_off = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            let due = state.on && now_unix() >= state.until;
            if due {
                state.on = false;
                state.until = 0;
            }
            due
        };
        if turned_off {
            let _ = self.sender.send(msg::BLE_SET_ADV, &payload(false));
        }
    }
}

fn install_shutdown_handler() {
    unsafe {
        signal(SIGTERM, handle_shutdown_signal);
        signal(SIGINT, handle_shutdown_signal);
    }
}

/// Async-signal-safe by construction: reads a plain atomic, sends one fixed-size buffer with no
/// allocation via [`bus::send_raw`], then terminates with `_exit` (not `std::process::exit`,
/// which is not guaranteed safe here -- it can run allocator-touching cleanup this handler
/// might have interrupted). Unconditional -- it does not check whether we think advertising is
/// currently on, because that check would mean taking `BleAdv::state`'s lock from inside a
/// signal handler, which is exactly the kind of thing that can deadlock if the interrupted code
/// already holds it. Sending `off` when already `off` is harmless.
extern "C" fn handle_shutdown_signal(_sig: c_int) {
    let mqd = SHUTDOWN_MQD.load(Ordering::SeqCst);
    if mqd >= 0 {
        bus::send_raw(mqd, msg::BLE_SET_ADV, SRC, &payload(false));
    }
    unsafe { _exit(0) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn payload_byte_zero_is_the_only_thing_that_differs() {
        assert_eq!(payload(true), [1, 0, 0, 0]);
        assert_eq!(payload(false), [0, 0, 0, 0]);
    }

    #[test]
    fn resolve_deadline_defaults_to_300_seconds() {
        assert_eq!(resolve_deadline(1_000, None), 1_000 + DEFAULT_SECONDS);
    }

    #[test]
    fn resolve_deadline_clamps_below_one_second_up_to_one() {
        assert_eq!(resolve_deadline(1_000, Some(0)), 1_001);
    }

    #[test]
    fn resolve_deadline_clamps_above_the_max_down_to_the_max() {
        assert_eq!(resolve_deadline(1_000, Some(999_999)), 1_000 + MAX_SECONDS);
    }

    #[test]
    fn resolve_deadline_honors_a_request_inside_the_allowed_range() {
        assert_eq!(resolve_deadline(1_000, Some(60)), 1_060);
    }

    #[test]
    fn format_status_omits_the_deadline_while_off() {
        assert_eq!(format_status(false, 12_345), r#"{"advertising":false,"until":null}"#);
    }

    #[test]
    fn format_status_reports_the_deadline_while_on() {
        assert_eq!(format_status(true, 12_345), r#"{"advertising":true,"until":12345}"#);
    }
}
