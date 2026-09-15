//! Wi-Fi network selection: scan, show, and switch the feeder's wireless network through the
//! already-running `wpa_supplicant` control interface (`wpa_cli -i wlan0`), without ever
//! touching the vendor's own stored credentials.
//!
//! ## Why `wpa_cli`, never the vendor's `set_wifi`
//!
//! `pktool set_wifi` (docs/15-settings-write.md) provisions Wi-Fi credentials directly into the
//! vendor's own encrypted store -- exactly the flash write this project avoids everywhere else
//! (`persist.rs`'s module docs; `docs/21-config-encryption.md`). `wpa_supplicant` itself is
//! already running with `ctrl_interface=/var/run/wpa_supplicant` and `update_config=1`
//! (confirmed live), so `wpa_cli` can add, select and remove networks against the live process
//! with no flash write and no vendor process touched at all. `/tmp/wpa_supplicant.conf` is
//! tmpfs and gets fully regenerated from the vendor's own stored credentials by its
//! `wifi_connect.sh` on every boot (docs/02-boot.md), so it is never this module's persistence
//! layer -- `/opt/kibble/wifi.json` is (see "Boot re-apply and reconcile" below), exactly the
//! same division of responsibility `desired.rs`/`persist.rs` use for device settings and
//! `cloud.rs` uses for routing state.
//!
//! ## The fail-safe connect sequence
//!
//! `add_network` (never reusing the vendor's id 0, and never reusing another SSID's id) ->
//! `set_network` ssid/psk/key_mgmt -> `enable_network` -> `select_network` (which disables
//! every *other* configured network, confirmed live and by this module's own `list_networks`
//! wrapper) -> poll `status` for `wpa_state=COMPLETED` *on that id* -> force a fresh DHCP lease
//! (kill + respawn `udhcpc`, matching the vendor's own `wifi_connect.sh` -- see `cloud.rs`'s
//! module docs for the live citation that a stale lease does not just self-heal) -> poll
//! `ip -4 addr show wlan0` for a lease, all inside one [`CONNECT_TIMEOUT`] budget shared by both
//! waits, not 30s apiece. Any failure from `select_network` onward re-`select_network`s whatever
//! id was active before this attempt and reports why in `last_error` -- the device is never left
//! associating to nowhere. A wrong password fails the *first* wait (association never reaches
//! `COMPLETED`); a right password but no reachable DHCP server fails the *second*.
//!
//! An existing Kibble-owned network (any id but the vendor's `0`) matching the requested SSID is
//! reused -- its `psk` is updated in place -- rather than adding a duplicate every time the same
//! network is retried (e.g. while a user is debugging a typo'd password). `psk` is optional on
//! reuse: reconnecting to an SSID Kibble has already saved a password for does not require
//! resending it.
//!
//! ## Never the PSK
//!
//! `psk` is never included in any HTTP response, never formatted into a `last_error` string, and
//! never passed to the generic, verbose-on-failure [`Runner::wpa`] the way every other argument
//! is -- [`Runner::set_psk`] is a dedicated call specifically so a failure there cannot echo the
//! attempted password back through an error message the way `RealRunner::wpa`'s normal
//! echo-the-failed-command-line behaviour would. It also never appears in a persisted
//! `last_error` or an `eprintln!` -- only `ssid`s and state names are ever logged.
//! `/opt/kibble/wifi.json` does hold it in plaintext (`chmod 600`, checked by a test below) --
//! that is the one place it is deliberately persisted, exactly as directed.
//!
//! ## Boot re-apply and reconcile
//!
//! The vendor's own `wifi_connect.sh` reruns at boot and reselects *its own* stored network
//! (`docs/02-boot.md`), so a Kibble-desired network never wins a boot race on its own. Once
//! `wpa_supplicant`'s control socket appears, [`spawn_reconciler`] re-runs the connect sequence
//! once if `/opt/kibble/wifi.json` names a different SSID than the live one, then rechecks every
//! [`RECONCILE_INTERVAL`] for the rest of the process's life (the vendor's own reconnect logic
//! can reselect its own network at any time, not just at boot). A target that keeps failing backs
//! off after [`MAX_CONSECUTIVE_FAILURES`] consecutive attempts rather than flapping the link
//! every tick forever -- but a *new* desired target (a fresh `connect()`, even to a network that
//! previously failed) always gets a fresh run of attempts, since backing off is about not
//! hammering the *same* bad target, not about refusing to ever try again.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::http::json_field;

pub const WIFI_IFACE: &str = "wlan0";
/// Confirmed live (docs/02-boot.md, `03-app.md`): the standard `wpa_cli` binary, not a busybox
/// applet. Kibbled's own inherited `PATH` (confirmed by reading `/proc/<kibbled-pid>/environ`
/// live: `...:/soc/bin:/soc/scripts:...`) already contains `/soc/bin`, so a bare `wpa_cli` would
/// resolve too -- the absolute path is used anyway to match how the project's own live-testing
/// notes cite this binary, and to never depend on that `PATH` entry surviving a future boot
/// script change.
const WPA_CLI: &str = "/soc/bin/wpa_cli";
const STATE_PATH: &str = "/opt/kibble/wifi.json";
const CTRL_SOCK: &str = "/var/run/wpa_supplicant/wlan0";
/// `wpa_supplicant.conf`'s network id 0 is always the vendor's own -- regenerated from its
/// encrypted store by `wifi_connect.sh` on every boot (docs/02-boot.md), confirmed live
/// (`wpa_cli list_networks` -> `0	IoT	any	[CURRENT]` on a stock, unmodified device). Kibble
/// adds every network of its own at id >= 1 and never rewrites, disables-permanently, or
/// removes id 0.
const VENDOR_NETWORK_ID: u32 = 0;

pub const RECONCILE_INTERVAL: Duration = Duration::from_secs(60);
const SOCKET_WAIT_POLL: Duration = Duration::from_secs(1);
/// Mirrors `cloud.rs`'s `WLAN_WAIT_MAX`: a bounded extra wait for `wpa_supplicant` to be up at
/// all, then reconcile anyway.
const SOCKET_WAIT_MAX: Duration = Duration::from_secs(60);
/// Fail-safe budget shared by association *and* the DHCP lease that follows it -- not 30s each,
/// 30s total, per the required fail-safe sequence.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const POLL_INTERVAL: Duration = Duration::from_millis(500);
/// `wpa_cli scan`'s reply is asynchronous (`OK` means "started", not "done"); this agent talks
/// to `wpa_cli` exactly like every other external tool here -- one request, one reply, no
/// persistent connection or event subscription -- so a fixed settle time after triggering is
/// the simplest correct-enough wait, comfortably inside both HA's default 10s client timeout
/// (`custom_components/kibble/api.py`) and its 10s poll cadence.
const SCAN_SETTLE: Duration = Duration::from_secs(2);
/// After this many consecutive reconcile failures *against the same desired target*, stop
/// actively re-selecting every tick -- `last_error` (surfaced via `GET /wifi`) already explains
/// why, and hammering a bad password every 60s would just flap the link for no benefit.
const MAX_CONSECUTIVE_FAILURES: u32 = 3;

/// Serialises every `wpa_cli`/`ip`/dhcp mutation between the HTTP handler thread and the
/// reconciler thread, exactly like `cloud.rs`'s `LOCK` and `persist.rs`'s `WRITE_LOCK`. Same
/// `panic = "abort"` reasoning applies: an ordinary `unwrap()` on a poisoned lock is fine.
static LOCK: Mutex<()> = Mutex::new(());

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Status {
    pub ssid: Option<String>,
    pub bssid: Option<String>,
    pub freq_mhz: Option<u32>,
    pub id: Option<u32>,
    pub wpa_state: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ScanResult {
    pub bssid: String,
    pub freq_mhz: u32,
    pub signal_dbm: i32,
    pub flags: String,
    /// `None` for a hidden (zero-length) SSID -- confirmed live, `wpa_cli`'s own `printf_encode`
    /// renders that as an empty column, not a placeholder.
    pub ssid: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
struct NetworkEntry {
    id: u32,
    ssid: String,
}

#[derive(Debug, Clone, PartialEq, Default)]
struct Desired {
    ssid: Option<String>,
    psk: Option<String>,
    last_error: Option<String>,
}

#[derive(Debug)]
pub enum Error {
    /// No `psk` given and no existing Kibble-owned network entry for that SSID to reuse.
    PskRequired,
    /// A `wpa_cli`/`ip`/dhcp-restart invocation itself failed (never contains a psk -- see
    /// module docs).
    Command(String),
    /// Association never reached `wpa_state=COMPLETED` on the target network within
    /// [`CONNECT_TIMEOUT`].
    AssociationFailed { rolled_back_to: Option<u32>, rollback_error: Option<String> },
    /// Associated, but no DHCP lease appeared within the (remaining) [`CONNECT_TIMEOUT`] budget.
    NoDhcpLease { rolled_back_to: Option<u32>, rollback_error: Option<String> },
    /// `forget` was asked to remove the vendor's own network (id 0).
    ForbiddenVendorNetwork,
    /// `forget` was asked to remove an SSID with no matching Kibble-owned network.
    UnknownNetwork,
    /// `forget` refused to remove the network currently providing connectivity -- removing it
    /// would strand the device with nothing left selected.
    CannotForgetActive,
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::PskRequired => write!(
                f,
                "no psk given and no existing Kibble-managed network saved for that SSID"
            ),
            Error::Command(m) => write!(f, "{m}"),
            Error::AssociationFailed { rolled_back_to, rollback_error } => {
                write!(f, "association did not reach COMPLETED within {CONNECT_TIMEOUT:?}")?;
                describe_rollback(f, rolled_back_to, rollback_error)
            }
            Error::NoDhcpLease { rolled_back_to, rollback_error } => {
                write!(f, "associated but no DHCP lease within {CONNECT_TIMEOUT:?}")?;
                describe_rollback(f, rolled_back_to, rollback_error)
            }
            Error::ForbiddenVendorNetwork => {
                write!(f, "cannot forget the vendor's own network (id {VENDOR_NETWORK_ID})")
            }
            Error::UnknownNetwork => write!(f, "no Kibble-managed network saved for that SSID"),
            Error::CannotForgetActive => {
                write!(f, "cannot forget the network currently providing connectivity")
            }
        }
    }
}

fn describe_rollback(
    f: &mut std::fmt::Formatter<'_>,
    id: &Option<u32>,
    err: &Option<String>,
) -> std::fmt::Result {
    match (id, err) {
        (Some(id), None) => write!(f, ", rolled back to network {id}"),
        (Some(id), Some(e)) => write!(f, ", rollback to network {id} also failed: {e}"),
        (None, None) => write!(f, ", rolled back to the vendor's network"),
        (None, Some(e)) => write!(f, ", rollback also failed: {e}"),
    }
}

/// Runs `wpa_cli`/`ip`/dhcp commands. Abstracted behind a trait so the fail-safe ordering logic
/// below -- the part that actually matters to get right -- can be unit-tested against a fake
/// that records the exact call sequence, with no real device and no real binaries involved.
/// Mirrors `cloud.rs`'s `Runner` trait exactly.
trait Runner {
    fn wpa(&mut self, args: &[&str]) -> Result<String, String>;
    /// Dedicated so a failure here is never formatted with its arguments the way
    /// [`Runner::wpa`]'s real implementation formats every other (non-secret) failed command --
    /// see `RealRunner::set_psk`.
    fn set_psk(&mut self, id: u32, psk: &str) -> Result<(), String>;
    fn ip(&mut self, args: &[&str]) -> Result<String, String>;
    fn restart_dhcp(&mut self) -> Result<(), String>;
}

struct RealRunner;

impl Runner for RealRunner {
    fn wpa(&mut self, args: &[&str]) -> Result<String, String> {
        let out = Command::new(WPA_CLI)
            .arg("-i")
            .arg(WIFI_IFACE)
            .args(args)
            .output()
            .map_err(|e| format!("exec `wpa_cli -i {WIFI_IFACE} {}`: {e}", args.join(" ")))?;
        // `wpa_cli`'s own exit code is only meaningful for a hard connection failure (no such
        // control socket, etc) -- confirmed live -- a semantically failed command (bad id, wrong
        // state) still exits 0 with `FAIL` on stdout, which callers that need to know check via
        // `expect_ok`, not this exit status.
        if !out.status.success() {
            return Err(format!(
                "`wpa_cli -i {WIFI_IFACE} {}` failed ({}): {}{}",
                args.join(" "),
                out.status,
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            ));
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    fn set_psk(&mut self, id: u32, psk: &str) -> Result<(), String> {
        let quoted = format!("\"{psk}\"");
        let id_s = id.to_string();
        let out = Command::new(WPA_CLI)
            .args(["-i", WIFI_IFACE, "set_network", &id_s, "psk", &quoted])
            .output()
            // `e` here is an `io::Error` about the exec syscall itself (e.g. ENOENT) -- it can
            // never contain the child's argv or output, so this is safe to format.
            .map_err(|e| format!("exec `wpa_cli set_network {id} psk`: {e}"))?;
        let stdout = String::from_utf8_lossy(&out.stdout);
        if !out.status.success() || stdout.trim() != "OK" {
            // Deliberately no stdout/stderr/argv echoed here, unlike every other command this
            // module runs: this one's argv contains a secret.
            return Err(format!("wpa_cli set_network {id} psk did not return OK"));
        }
        Ok(())
    }

    fn ip(&mut self, args: &[&str]) -> Result<String, String> {
        let out = Command::new("ip")
            .args(args)
            .output()
            .map_err(|e| format!("exec `ip {}`: {e}", args.join(" ")))?;
        if !out.status.success() {
            return Err(format!(
                "`ip {}` failed ({}): {}{}",
                args.join(" "),
                out.status,
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr)
            ));
        }
        Ok(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    fn restart_dhcp(&mut self) -> Result<(), String> {
        // Best-effort: erroring because nothing was running yet is fine. Mirrors the vendor's
        // own `wifi_connect.sh`, which kills and restarts `udhcpc` from scratch on every
        // reconnect rather than trusting it to notice the link changed on its own (see
        // `cloud.rs`'s module docs for the live-confirmed citation of that vendor behaviour).
        let _ = Command::new("killall").arg("udhcpc").output();
        Command::new("udhcpc")
            .args(["-i", WIFI_IFACE, "-b"])
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("exec `udhcpc -i {WIFI_IFACE} -b`: {e}"))
    }
}

/// `set_network`/`enable_network`/`select_network`/`remove_network` all reply with a bare `OK`
/// or `FAIL` on stdout -- `wpa_cli`'s process exit code stays 0 either way (see
/// `RealRunner::wpa`), so the acknowledgement has to be read out of the text.
fn expect_ok(runner: &mut impl Runner, args: &[&str]) -> Result<(), String> {
    let out = runner.wpa(args)?;
    if out.trim() == "OK" {
        Ok(())
    } else {
        Err(format!("wpa_cli {} did not return OK: {}", args.join(" "), out.trim()))
    }
}

// ---------------------------------------------------------------------------------------------
// Pure parsers -- tested against real captured output, see the tests module.
// ---------------------------------------------------------------------------------------------

/// Parses `wpa_cli status`'s flat `key=value` lines. Tolerant of a disconnected state, where
/// most keys (bssid/freq/ssid/id) are simply absent from the output entirely.
pub fn parse_status(text: &str) -> Status {
    let mut kv: HashMap<&str, &str> = HashMap::new();
    for line in text.lines() {
        if let Some((k, v)) = line.split_once('=') {
            kv.insert(k, v);
        }
    }
    Status {
        ssid: kv.get("ssid").map(|s| s.to_string()),
        bssid: kv.get("bssid").map(|s| s.to_string()),
        freq_mhz: kv.get("freq").and_then(|s| s.parse().ok()),
        id: kv.get("id").and_then(|s| s.parse().ok()),
        wpa_state: kv
            .get("wpa_state")
            .map(|s| s.to_string())
            .unwrap_or_else(|| "UNKNOWN".to_string()),
    }
}

/// Parses `wpa_cli scan_results`'s header + tab-separated rows. The `ssid` column can itself
/// contain spaces (a normal, legal SSID character -- confirmed live, see the tests) or be empty
/// (a hidden network -- also confirmed live), so this splits on literal tabs, never on generic
/// whitespace runs the way `cloud.rs`'s route-table parsing does.
pub fn parse_scan_results(text: &str) -> Vec<ScanResult> {
    text.lines()
        .skip(1) // header: "bssid / frequency / signal level / flags / ssid"
        .filter_map(|line| {
            let mut cols = line.splitn(5, '\t');
            let bssid = cols.next()?.to_string();
            let freq_mhz = cols.next()?.parse().ok()?;
            let signal_dbm = cols.next()?.parse().ok()?;
            let flags = cols.next()?.to_string();
            let ssid = cols.next().unwrap_or("");
            Some(ScanResult {
                bssid,
                freq_mhz,
                signal_dbm,
                flags,
                ssid: (!ssid.is_empty()).then(|| ssid.to_string()),
            })
        })
        .collect()
}

/// Parses `wpa_cli list_networks`'s header + tab-separated rows. The `bssid` column (almost
/// always the literal string `any`) and the trailing `flags` column aren't needed by this
/// module and are dropped.
fn parse_list_networks(text: &str) -> Vec<NetworkEntry> {
    text.lines()
        .skip(1) // header: "network id / ssid / bssid / flags"
        .filter_map(|line| {
            let mut cols = line.splitn(4, '\t');
            let id = cols.next()?.trim().parse().ok()?;
            let ssid = cols.next()?.to_string();
            Some(NetworkEntry { id, ssid })
        })
        .collect()
}

/// Parses `wpa_cli signal_poll`'s `RSSI=<dBm>` line. `None` if absent (e.g. `FAIL` when not
/// currently associated, or a driver that doesn't support the query).
fn parse_signal_poll(text: &str) -> Option<i32> {
    text.lines().find_map(|l| l.strip_prefix("RSSI=")?.trim().parse().ok())
}

/// Parses the current IPv4 address out of `ip -4 addr show <iface>`.
fn parse_ip_addr_show(text: &str) -> Option<String> {
    text.lines()
        .find_map(|l| l.trim().strip_prefix("inet "))
        .and_then(|rest| rest.split('/').next())
        .map(str::to_string)
}

pub fn band_for_freq(freq_mhz: u32) -> &'static str {
    if freq_mhz < 3000 {
        "2.4"
    } else {
        "5"
    }
}

/// Reduces a `scan_results` flags column (e.g. `[WPA2-PSK-CCMP][ESS]`) to a short security
/// label. Reports the first `WPA`/`WEP` group found (ignoring `ESS`/`WPS`/`P2P`); a network
/// advertising no such group at all (just `[ESS]`) is open.
pub fn security_from_flags(flags: &str) -> String {
    let groups = flags.split(|c| c == '[' || c == ']').filter(|s| !s.is_empty());
    for g in groups {
        if g.starts_with("WPA") || g.starts_with("WEP") {
            let mut parts = g.splitn(3, '-');
            return match (parts.next(), parts.next()) {
                (Some(proto), Some(auth)) => format!("{proto}-{auth}"),
                (Some(proto), None) => proto.to_string(),
                (None, _) => "open".to_string(),
            };
        }
    }
    "open".to_string()
}

/// Keeps the strongest (least negative dBm) entry per SSID, hidden entries omitted entirely,
/// sorted strongest-first. Real scans regularly show the same SSID from multiple BSSIDs -- a
/// multi-radio AP broadcasting on both bands, or simply an unrelated neighbour who happens to
/// have picked the same common name -- and `GET /wifi/scan` is for picking a network to join,
/// not enumerating radios (see the tests: two real, distinct `IoT` BSSIDs 31 dB apart, and three
/// real `BEAST_ROUTER` BSSIDs, were observed on one real scan of this exact device).
pub fn dedupe_strongest(results: Vec<ScanResult>) -> Vec<ScanResult> {
    let mut best: HashMap<String, ScanResult> = HashMap::new();
    for r in results {
        let Some(ssid) = r.ssid.clone() else { continue };
        match best.get(&ssid) {
            Some(existing) if existing.signal_dbm >= r.signal_dbm => {}
            _ => {
                best.insert(ssid, r);
            }
        }
    }
    let mut out: Vec<ScanResult> = best.into_values().collect();
    out.sort_by(|a, b| b.signal_dbm.cmp(&a.signal_dbm));
    out
}

// ---------------------------------------------------------------------------------------------
// Runner-generic orchestration -- the fail-safe sequence, tested against a fake runner.
// ---------------------------------------------------------------------------------------------

fn get_status(runner: &mut impl Runner) -> Result<Status, String> {
    runner.wpa(&["status"]).map(|t| parse_status(&t))
}

fn get_networks(runner: &mut impl Runner) -> Result<Vec<NetworkEntry>, String> {
    runner.wpa(&["list_networks"]).map(|t| parse_list_networks(&t))
}

fn get_ip(runner: &mut impl Runner) -> Option<String> {
    runner.ip(&["-4", "addr", "show", WIFI_IFACE]).ok().and_then(|t| parse_ip_addr_show(&t))
}

fn get_scan_results(runner: &mut impl Runner) -> Vec<ScanResult> {
    let _ = runner.wpa(&["scan"]); // async trigger; "FAIL-BUSY" if one's already running is fine
    std::thread::sleep(SCAN_SETTLE);
    runner.wpa(&["scan_results"]).map(|t| parse_scan_results(&t)).unwrap_or_default()
}

/// `GET /wifi`'s `signal_dbm`: a live `signal_poll` reading if the driver supports one while
/// associated, else whatever the last scan cache says about the current bssid -- never triggers
/// a fresh scan itself (that's `GET /wifi/scan`'s job).
fn current_signal_dbm(runner: &mut impl Runner, current_bssid: Option<&str>) -> Option<i32> {
    if let Ok(text) = runner.wpa(&["signal_poll"]) {
        if let Some(rssi) = parse_signal_poll(&text) {
            return Some(rssi);
        }
    }
    let bssid = current_bssid?;
    let text = runner.wpa(&["scan_results"]).ok()?;
    parse_scan_results(&text)
        .into_iter()
        .find(|r| r.bssid.eq_ignore_ascii_case(bssid))
        .map(|r| r.signal_dbm)
}

/// Ensures a network for `ssid` exists, reusing an existing Kibble-owned entry (any id but the
/// vendor's) if one matches. Returns its id.
fn ensure_network(
    runner: &mut impl Runner,
    ssid: &str,
    psk: Option<&str>,
) -> Result<u32, Error> {
    let networks = get_networks(runner).map_err(Error::Command)?;
    if let Some(existing) = networks.iter().find(|n| n.ssid == ssid && n.id != VENDOR_NETWORK_ID) {
        if let Some(psk) = psk {
            runner.set_psk(existing.id, psk).map_err(Error::Command)?;
            expect_ok(runner, &["set_network", &existing.id.to_string(), "key_mgmt", "WPA-PSK"])
                .map_err(Error::Command)?;
        }
        return Ok(existing.id);
    }
    let Some(psk) = psk else { return Err(Error::PskRequired) };
    let id_text = runner.wpa(&["add_network"]).map_err(Error::Command)?;
    let id: u32 = id_text
        .trim()
        .parse()
        .map_err(|_| Error::Command(format!("add_network returned a non-numeric id: {id_text:?}")))?;
    let id_s = id.to_string();
    let quoted_ssid = format!("\"{ssid}\"");
    expect_ok(runner, &["set_network", &id_s, "ssid", &quoted_ssid]).map_err(Error::Command)?;
    runner.set_psk(id, psk).map_err(Error::Command)?;
    expect_ok(runner, &["set_network", &id_s, "key_mgmt", "WPA-PSK"]).map_err(Error::Command)?;
    Ok(id)
}

/// `select_network` is the operation that actually matters (it changes what's live); a failed
/// `save_config` only means the on-disk tmpfs mirror didn't get updated -- the real persistence
/// is this module's own `/opt/kibble/wifi.json` -- so it's logged and swallowed rather than
/// treated as this step failing. Treating it as fatal here would abandon the fail-safe
/// verification loop below on a network that actually IS now live, which is worse than a stale
/// `/tmp` file.
fn select_and_save(runner: &mut impl Runner, id: u32) -> Result<(), String> {
    expect_ok(runner, &["select_network", &id.to_string()])?;
    if let Err(e) = expect_ok(runner, &["save_config"]) {
        eprintln!("kibbled: wifi: save_config after selecting network {id} failed (non-fatal): {e}");
    }
    Ok(())
}

/// Re-`select_network`s whatever was active before a failed attempt. `None` means a `status`
/// read failed before the attempt even started (should not happen live -- id 0 is always
/// present -- but is possible on a transient control-socket hiccup); falls back to the vendor's
/// own id 0 rather than leaving the device on the failed network.
fn rollback(runner: &mut impl Runner, previous_id: Option<u32>) -> Option<String> {
    let target = previous_id.unwrap_or(VENDOR_NETWORK_ID);
    select_and_save(runner, target).err()
}

/// Runs the full fail-safe sequence described in the module docs. `timeout`/`poll_interval` are
/// parameters (rather than always the real [`CONNECT_TIMEOUT`]/[`POLL_INTERVAL`]) purely so
/// tests can exercise the real retry-then-rollback logic in milliseconds instead of seconds.
fn connect_with_timeout(
    runner: &mut impl Runner,
    ssid: &str,
    psk: Option<&str>,
    timeout: Duration,
    poll_interval: Duration,
) -> Result<Status, Error> {
    let previous_id = get_status(runner).ok().and_then(|s| s.id);
    let id = ensure_network(runner, ssid, psk)?;
    expect_ok(runner, &["enable_network", &id.to_string()]).map_err(Error::Command)?;
    select_and_save(runner, id).map_err(Error::Command)?;

    let deadline = Instant::now() + timeout;
    let mut associated = false;
    loop {
        if matches!(get_status(runner), Ok(s) if s.wpa_state == "COMPLETED" && s.id == Some(id)) {
            associated = true;
            break;
        }
        if Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(poll_interval);
    }
    if !associated {
        let rollback_error = rollback(runner, previous_id);
        return Err(Error::AssociationFailed { rolled_back_to: previous_id, rollback_error });
    }

    if let Err(e) = runner.restart_dhcp() {
        eprintln!("kibbled: wifi: dhcp restart failed, still checking for a lease: {e}");
    }
    let mut leased = false;
    loop {
        if get_ip(runner).is_some() {
            leased = true;
            break;
        }
        if Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(poll_interval);
    }
    if !leased {
        let rollback_error = rollback(runner, previous_id);
        return Err(Error::NoDhcpLease { rolled_back_to: previous_id, rollback_error });
    }

    get_status(runner).map_err(Error::Command)
}

fn forget_network(runner: &mut impl Runner, ssid: &str) -> Result<(), Error> {
    let networks = get_networks(runner).map_err(Error::Command)?;
    let target = match networks.iter().find(|n| n.ssid == ssid) {
        Some(n) if n.id == VENDOR_NETWORK_ID => return Err(Error::ForbiddenVendorNetwork),
        Some(n) => n.id,
        None => return Err(Error::UnknownNetwork),
    };
    let current = get_status(runner).ok();
    if matches!(&current, Some(s) if s.wpa_state == "COMPLETED" && s.id == Some(target)) {
        return Err(Error::CannotForgetActive);
    }
    expect_ok(runner, &["remove_network", &target.to_string()]).map_err(Error::Command)?;
    if let Err(e) = expect_ok(runner, &["save_config"]) {
        eprintln!("kibbled: wifi: save_config after forgetting network {target} failed (non-fatal): {e}");
    }
    if load().ssid.as_deref() == Some(ssid) {
        let _ = save(&Desired::default()); // stop reconcile from chasing a network that's gone
    }
    Ok(())
}

// ---------------------------------------------------------------------------------------------
// JSON views
// ---------------------------------------------------------------------------------------------

fn opt_json(v: &Option<String>) -> String {
    match v {
        Some(s) => format!("\"{}\"", s.escape_debug()),
        None => "null".to_string(),
    }
}

fn opt_num_json<T: std::fmt::Display>(v: Option<T>) -> String {
    match v {
        Some(n) => n.to_string(),
        None => "null".to_string(),
    }
}

fn build_status_json(runner: &mut impl Runner) -> String {
    let status = get_status(runner).unwrap_or_else(|_| Status {
        wpa_state: "UNKNOWN".to_string(),
        ..Default::default()
    });
    let ip = get_ip(runner);
    let signal = current_signal_dbm(runner, status.bssid.as_deref());
    let desired = load();
    let band = status.freq_mhz.map(band_for_freq).map(|b| format!("\"{b}\"")).unwrap_or_else(|| "null".to_string());
    format!(
        "{{\n  \"ssid\": {},\n  \"bssid\": {},\n  \"freq_mhz\": {},\n  \"band\": {},\n  \
         \"signal_dbm\": {},\n  \"ip\": {},\n  \"state\": \"{}\",\n  \"desired_ssid\": {},\n  \
         \"last_error\": {}\n}}\n",
        opt_json(&status.ssid),
        opt_json(&status.bssid),
        opt_num_json(status.freq_mhz),
        band,
        opt_num_json(signal),
        opt_json(&ip),
        status.wpa_state.escape_debug(),
        opt_json(&desired.ssid),
        opt_json(&desired.last_error),
    )
}

fn scan_entry_json(r: &ScanResult) -> String {
    format!(
        r#"{{"ssid":"{}","bssid":"{}","freq_mhz":{},"band":"{}","signal_dbm":{},"security":"{}"}}"#,
        r.ssid.as_deref().unwrap_or_default().escape_debug(),
        r.bssid.escape_debug(),
        r.freq_mhz,
        band_for_freq(r.freq_mhz),
        r.signal_dbm,
        security_from_flags(&r.flags).escape_debug(),
    )
}

fn build_scan_json(runner: &mut impl Runner) -> String {
    let deduped = dedupe_strongest(get_scan_results(runner));
    let mut body = String::from("[");
    for (i, r) in deduped.iter().enumerate() {
        if i > 0 {
            body.push(',');
        }
        body.push_str(&scan_entry_json(r));
    }
    body.push(']');
    body
}

// ---------------------------------------------------------------------------------------------
// Persistence -- `/opt/kibble/wifi.json`. Hand-rolled reader/writer, matching `cloud.rs`'s and
// `desired.rs`'s own persisted-state files (no JSON crate anywhere in this agent).
// ---------------------------------------------------------------------------------------------

fn load() -> Desired {
    load_from(STATE_PATH)
}

fn load_from(path: &str) -> Desired {
    match fs::read_to_string(path) {
        Ok(text) => parse_desired(&text),
        Err(_) => Desired::default(),
    }
}

fn parse_desired(text: &str) -> Desired {
    Desired {
        ssid: json_field(text, "ssid").filter(|v| *v != "null").map(str::to_string),
        psk: json_field(text, "psk").filter(|v| *v != "null").map(str::to_string),
        last_error: json_field(text, "last_error").filter(|v| *v != "null").map(str::to_string),
    }
}

fn save(desired: &Desired) -> io::Result<()> {
    save_to(STATE_PATH, desired)
}

fn save_to(path: &str, desired: &Desired) -> io::Result<()> {
    let _ = fs::create_dir_all(Path::new(path).parent().unwrap_or(Path::new(".")));
    let tmp = format!("{path}.tmp");
    fs::write(&tmp, to_json(desired))?;
    fs::rename(&tmp, path)?;
    // This file holds a Wi-Fi password in plaintext (see module docs): restrict it explicitly
    // rather than relying on `/opt/kibble`'s directory mode, which is world-readable.
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

fn to_json(desired: &Desired) -> String {
    format!(
        "{{\n  \"ssid\": {},\n  \"psk\": {},\n  \"last_error\": {}\n}}\n",
        opt_json(&desired.ssid),
        opt_json(&desired.psk),
        opt_json(&desired.last_error),
    )
}

// ---------------------------------------------------------------------------------------------
// Boot re-apply + reconcile
// ---------------------------------------------------------------------------------------------

/// Re-applies the desired network once `wpa_supplicant`'s control socket is up, then keeps
/// checking every [`RECONCILE_INTERVAL`] for the rest of the process's life. Runs on its own
/// thread, matching `cloud::spawn_reconciler`/`persist::spawn_reconciler`.
pub fn spawn_reconciler() {
    std::thread::spawn(|| {
        wait_for_ctrl_socket();
        boot_reapply(CONNECT_TIMEOUT, POLL_INTERVAL);
        let mut consecutive_failures = 0u32;
        let mut last_target: Option<(String, Option<String>)> = None;
        loop {
            std::thread::sleep(RECONCILE_INTERVAL);
            let mut runner = RealRunner;
            reconcile_once(
                &mut runner,
                &mut consecutive_failures,
                &mut last_target,
                CONNECT_TIMEOUT,
                POLL_INTERVAL,
            );
        }
    });
}

fn wait_for_ctrl_socket() {
    let deadline = Instant::now() + SOCKET_WAIT_MAX;
    loop {
        if Path::new(CTRL_SOCK).exists() {
            return;
        }
        if Instant::now() >= deadline {
            eprintln!(
                "kibbled: wifi: no control socket at {CTRL_SOCK} after {SOCKET_WAIT_MAX:?}, \
                 proceeding anyway"
            );
            return;
        }
        std::thread::sleep(SOCKET_WAIT_POLL);
    }
}

/// `timeout`/`poll_interval` are parameters for the same reason `connect_with_timeout`'s are:
/// so tests can exercise this in milliseconds instead of seconds.
fn boot_reapply(timeout: Duration, poll_interval: Duration) {
    let desired = load();
    let Some(ssid) = desired.ssid.clone() else { return };
    let mut runner = RealRunner;
    let current = get_status(&mut runner).ok();
    if matches!(&current, Some(s) if s.wpa_state == "COMPLETED" && s.ssid.as_deref() == Some(ssid.as_str()))
    {
        eprintln!("kibbled: wifi: already on desired network {ssid} at boot");
        return;
    }
    eprintln!("kibbled: wifi: boot re-apply: selecting desired network {ssid}");
    let outcome = connect_with_timeout(&mut runner, &ssid, desired.psk.as_deref(), timeout, poll_interval);
    match &outcome {
        Ok(s) => eprintln!("kibbled: wifi: boot re-apply succeeded, state={}", s.wpa_state),
        Err(e) => eprintln!("kibbled: wifi: boot re-apply failed: {e}"),
    }
    let last_error = outcome.err().map(|e| e.to_string());
    let _ = save(&Desired { ssid: Some(ssid), psk: desired.psk, last_error });
}

/// Pure decision for one reconcile tick: attempt only while actually drifted and still under
/// the failure cap for the *current* target (see [`reconcile_once`], which resets the cap
/// whenever the desired target itself changes).
fn should_attempt(drifted: bool, consecutive_failures: u32) -> bool {
    drifted && consecutive_failures < MAX_CONSECUTIVE_FAILURES
}

/// The failure count to use *this* tick: a target that differs from whatever the previous tick
/// last examined always earns a fresh run of attempts (0), regardless of how exhausted the old
/// target's count was -- backing off is about not hammering the *same* bad target forever, not
/// about refusing to ever retry once any target has failed [`MAX_CONSECUTIVE_FAILURES`] times.
fn effective_failures(
    last_target: &Option<(String, Option<String>)>,
    target: &(String, Option<String>),
    consecutive_failures: u32,
) -> u32 {
    if last_target.as_ref() == Some(target) {
        consecutive_failures
    } else {
        0
    }
}

fn reconcile_once(
    runner: &mut impl Runner,
    consecutive_failures: &mut u32,
    last_target: &mut Option<(String, Option<String>)>,
    timeout: Duration,
    poll_interval: Duration,
) {
    let desired = load();
    let Some(ssid) = desired.ssid.clone() else {
        *last_target = None;
        *consecutive_failures = 0;
        return;
    };
    let target = (ssid.clone(), desired.psk.clone());
    *consecutive_failures = effective_failures(last_target, &target, *consecutive_failures);
    *last_target = Some(target);

    let current = get_status(runner).ok();
    let drifted = !matches!(&current, Some(s) if s.wpa_state == "COMPLETED" && s.ssid.as_deref() == Some(ssid.as_str()));
    if !drifted {
        *consecutive_failures = 0;
        return; // healthy tick -- silent, matching cloud.rs's reconcile philosophy
    }
    if !should_attempt(drifted, *consecutive_failures) {
        return; // backed off on this exact target -- last_error already explains why
    }

    eprintln!("kibbled: wifi reconcile: current SSID drifted from desired {ssid}, re-selecting");
    match connect_with_timeout(runner, &ssid, desired.psk.as_deref(), timeout, poll_interval) {
        Ok(_) => {
            *consecutive_failures = 0;
            let _ = save(&Desired { ssid: Some(ssid.clone()), psk: desired.psk, last_error: None });
            eprintln!("kibbled: wifi reconcile: re-selected {ssid}");
        }
        Err(e) => {
            *consecutive_failures += 1;
            let msg = e.to_string();
            let note = if *consecutive_failures >= MAX_CONSECUTIVE_FAILURES {
                " (giving up until the desired network changes)"
            } else {
                ""
            };
            eprintln!(
                "kibbled: wifi reconcile: attempt {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES} \
                 failed: {msg}{note}"
            );
            let _ = save(&Desired { ssid: Some(ssid), psk: desired.psk, last_error: Some(msg) });
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Public surface used by main.rs
// ---------------------------------------------------------------------------------------------

pub fn status_json() -> String {
    let mut runner = RealRunner;
    build_status_json(&mut runner)
}

pub fn scan_json() -> String {
    let mut runner = RealRunner;
    build_scan_json(&mut runner)
}

/// Runs the fail-safe connect sequence and persists the outcome (ssid/psk always, so boot
/// re-apply and reconcile can keep pursuing it even after a failure; `last_error` reflects this
/// attempt specifically).
pub fn connect(ssid: &str, psk: Option<&str>) -> Result<Status, Error> {
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut runner = RealRunner;
    let result = connect_with_timeout(&mut runner, ssid, psk, CONNECT_TIMEOUT, POLL_INTERVAL);
    let last_error = result.as_ref().err().map(|e| e.to_string());
    let _ = save(&Desired { ssid: Some(ssid.to_string()), psk: psk.map(str::to_string), last_error });
    result
}

pub fn forget(ssid: &str) -> Result<(), Error> {
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut runner = RealRunner;
    forget_network(&mut runner, ssid)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    // ---- real captures from the live device (2026-09-15) -----------------------------------

    const REAL_STATUS_COMPLETED: &str = "bssid=a2:05:d6:57:1e:ce\n\
        freq=2437\n\
        ssid=IoT\n\
        id=0\n\
        mode=station\n\
        pairwise_cipher=CCMP\n\
        group_cipher=CCMP\n\
        key_mgmt=WPA2-PSK\n\
        wpa_state=COMPLETED\n\
        ip_address=192.168.4.85\n\
        address=94:ba:06:05:33:36\n";

    const REAL_LIST_NETWORKS_ONE_ENTRY: &str = "network id / ssid / bssid / flags\n\
        0\tIoT\tany\t[CURRENT]\n";

    const REAL_SIGNAL_POLL: &str = "RSSI=-56\nLINKSPEED=72\nNOISE=9999\nFREQUENCY=2437\nWIDTH=20 MHz\n";

    const REAL_IP_ADDR_SHOW: &str = "5: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc mq qlen 1000\n    \
        inet 192.168.4.85/24 brd 192.168.4.255 scope global wlan0\n       \
        valid_lft forever preferred_lft forever\n";

    /// One full real `wpa_cli -i wlan0 scan_results` capture (both bands, home network plus
    /// neighbours), including two genuinely hidden/empty SSID rows, an SSID with embedded
    /// spaces (`Pretty Girls Only`), a fully open network (`xfinitywifi`, flags just `[ESS]`),
    /// and multiple SSID collisions used by the dedupe tests below (`IoT` at two very different
    /// signal levels, `BEAST_ROUTER` at three).
    const REAL_SCAN_RESULTS: &str = "bssid / frequency / signal level / flags / ssid\n\
        a2:05:d6:57:1e:ce\t2437\t-56\t[WPA2-PSK-CCMP][ESS]\tIoT\n\
        a2:05:d6:57:1e:cf\t5180\t-39\t[WPA2-PSK-CCMP][ESS]\t\n\
        9c:05:d6:57:1e:cf\t5180\t-42\t[WPA2-PSK-CCMP][ESS]\tBEAST_ROUTER\n\
        9c:05:d6:57:1e:ce\t2437\t-56\t[WPA2-PSK-CCMP][ESS]\tBEAST_ROUTER\n\
        a6:05:d6:57:1e:ce\t2437\t-57\t[WPA2-PSK-CCMP][ESS]\t\n\
        f8:3e:b0:3a:1d:f8\t2422\t-76\t[WPA2-PSK-CCMP][ESS]\tTMOBILE-1DF6\n\
        f8:3e:b0:3f:21:e1\t5200\t-82\t[WPA2-PSK-CCMP][ESS]\tTMOBILE-21DE\n\
        f8:3e:b0:3f:21:e0\t2417\t-79\t[WPA2-PSK-CCMP][ESS]\tTMOBILE-21DE\n\
        b6:fb:e4:9b:46:b9\t5765\t-87\t[WPA2-PSK-CCMP][ESS]\tBEAST_ROUTER\n\
        ec:c3:02:e1:a9:34\t2457\t-84\t[WPA2-PSK-CCMP][ESS]\tBrickhouse\n\
        5a:9c:27:b8:58:ad\t5745\t-89\t[WPA-EAP-CCMP+TKIP][WPA2-EAP-CCMP+TKIP][ESS]\t\n\
        ba:ab:62:2e:81:e3\t2417\t-86\t[WPA2-PSK-CCMP][ESS][P2P]\t\n\
        18:9c:27:b8:58:ac\t2462\t-87\t[WPA2-PSK-CCMP][ESS]\tPretty Girls Only\n\
        b4:fb:e4:cb:46:b8\t2462\t-87\t[WPA2-PSK-CCMP][ESS]\t\n\
        b6:fb:e4:ab:46:b8\t2462\t-87\t[WPA2-PSK-CCMP][ESS]\tIoT\n\
        18:9c:27:b8:58:ad\t5745\t-92\t[WPA2-PSK-CCMP][ESS]\tPretty Girls Only\n\
        b4:fb:e4:cb:46:b9\t5765\t-94\t[WPA2-PSK-CCMP][ESS]\t\n\
        5a:9c:27:b8:58:ac\t2462\t-92\t[WPA-EAP-CCMP+TKIP][WPA2-EAP-CCMP+TKIP][ESS]\t\n\
        e0:46:ee:96:01:66\t2437\t-93\t[WPA2-PSK-CCMP][ESS]\tNETGEAR42\n\
        42:2f:86:f1:29:38\t2442\t-97\t[WPA2-PSK-CCMP][ESS]\t\n\
        2a:9c:27:b8:58:ad\t5745\t-89\t[ESS]\txfinitywifi\n";

    #[test]
    fn parses_real_status_while_associated() {
        let s = parse_status(REAL_STATUS_COMPLETED);
        assert_eq!(s.ssid.as_deref(), Some("IoT"));
        assert_eq!(s.bssid.as_deref(), Some("a2:05:d6:57:1e:ce"));
        assert_eq!(s.freq_mhz, Some(2437));
        assert_eq!(s.id, Some(0));
        assert_eq!(s.wpa_state, "COMPLETED");
    }

    #[test]
    fn parse_status_on_a_disconnected_device_leaves_most_fields_absent() {
        let s = parse_status("wpa_state=DISCONNECTED\naddress=94:ba:06:05:33:36\n");
        assert_eq!(s.ssid, None);
        assert_eq!(s.bssid, None);
        assert_eq!(s.freq_mhz, None);
        assert_eq!(s.id, None);
        assert_eq!(s.wpa_state, "DISCONNECTED");
    }

    #[test]
    fn parses_real_list_networks_with_one_entry() {
        let networks = parse_list_networks(REAL_LIST_NETWORKS_ONE_ENTRY);
        assert_eq!(networks, vec![NetworkEntry { id: 0, ssid: "IoT".to_string() }]);
    }

    #[test]
    fn parses_real_signal_poll() {
        assert_eq!(parse_signal_poll(REAL_SIGNAL_POLL), Some(-56));
    }

    #[test]
    fn signal_poll_fail_text_parses_to_none() {
        assert_eq!(parse_signal_poll("FAIL\n"), None);
    }

    #[test]
    fn parses_real_ip_addr_show() {
        assert_eq!(parse_ip_addr_show(REAL_IP_ADDR_SHOW), Some("192.168.4.85".to_string()));
    }

    #[test]
    fn empty_ip_addr_show_has_no_address() {
        assert_eq!(parse_ip_addr_show("5: wlan0: <BROADCAST,MULTICAST,UP> mtu 1450\n"), None);
    }

    #[test]
    fn parses_real_scan_results_row_count_and_current_entry() {
        let results = parse_scan_results(REAL_SCAN_RESULTS);
        assert_eq!(results.len(), 21);
        let current = results.iter().find(|r| r.bssid == "a2:05:d6:57:1e:ce").unwrap();
        assert_eq!(current.ssid.as_deref(), Some("IoT"));
        assert_eq!(current.freq_mhz, 2437);
        assert_eq!(current.signal_dbm, -56);
    }

    #[test]
    fn hidden_ssid_rows_parse_to_ssid_none_not_a_placeholder() {
        let results = parse_scan_results(REAL_SCAN_RESULTS);
        let hidden = results.iter().find(|r| r.bssid == "a2:05:d6:57:1e:cf").unwrap();
        assert_eq!(hidden.ssid, None);
        assert_eq!(hidden.signal_dbm, -39);
    }

    #[test]
    fn ssid_with_embedded_spaces_is_preserved_verbatim() {
        let results = parse_scan_results(REAL_SCAN_RESULTS);
        let spaced = results.iter().find(|r| r.bssid == "18:9c:27:b8:58:ac").unwrap();
        assert_eq!(spaced.ssid.as_deref(), Some("Pretty Girls Only"));
    }

    #[test]
    fn band_for_freq_covers_both_bands() {
        assert_eq!(band_for_freq(2412), "2.4");
        assert_eq!(band_for_freq(2462), "2.4");
        assert_eq!(band_for_freq(5180), "5");
        assert_eq!(band_for_freq(5765), "5");
    }

    #[test]
    fn security_from_flags_reports_wpa2_psk() {
        assert_eq!(security_from_flags("[WPA2-PSK-CCMP][ESS]"), "WPA2-PSK");
    }

    #[test]
    fn security_from_flags_reports_wpa_eap_from_the_real_mixed_capture() {
        assert_eq!(
            security_from_flags("[WPA-EAP-CCMP+TKIP][WPA2-EAP-CCMP+TKIP][ESS]"),
            "WPA-EAP"
        );
    }

    #[test]
    fn security_from_flags_ignores_ess_and_p2p_flags() {
        assert_eq!(security_from_flags("[WPA2-PSK-CCMP][ESS][P2P]"), "WPA2-PSK");
    }

    #[test]
    fn security_from_flags_reports_open_for_the_real_xfinitywifi_capture() {
        assert_eq!(security_from_flags("[ESS]"), "open");
    }

    #[test]
    fn dedupe_keeps_the_strongest_of_the_two_real_ssid_collisions() {
        let deduped = dedupe_strongest(parse_scan_results(REAL_SCAN_RESULTS));
        let iot: Vec<&ScanResult> = deduped.iter().filter(|r| r.ssid.as_deref() == Some("IoT")).collect();
        assert_eq!(iot.len(), 1, "two real IoT bssids in the capture must collapse to one");
        assert_eq!(iot[0].signal_dbm, -56, "must keep the stronger of the two, not -87");

        let beast: Vec<&ScanResult> =
            deduped.iter().filter(|r| r.ssid.as_deref() == Some("BEAST_ROUTER")).collect();
        assert_eq!(beast.len(), 1, "three real BEAST_ROUTER bssids must collapse to one");
        assert_eq!(beast[0].signal_dbm, -42, "must keep the strongest of the three");
        assert_eq!(beast[0].freq_mhz, 5180, "the strongest BEAST_ROUTER bssid is the 5GHz one");
    }

    #[test]
    fn dedupe_omits_hidden_ssids_entirely() {
        let deduped = dedupe_strongest(parse_scan_results(REAL_SCAN_RESULTS));
        assert!(deduped.iter().all(|r| r.ssid.is_some()));
        // 21 real rows, 8 of them hidden/empty-ssid, leaves 13 named rows; of those, IoT (x2),
        // BEAST_ROUTER (x3), TMOBILE-21DE (x2) and Pretty Girls Only (x2) collapse to one each,
        // removing 1+2+1+1 = 5 extra rows, so 13 - 5 = 8 unique named networks survive.
        assert_eq!(deduped.len(), 8);
    }

    #[test]
    fn dedupe_result_is_sorted_strongest_first() {
        let deduped = dedupe_strongest(parse_scan_results(REAL_SCAN_RESULTS));
        let signals: Vec<i32> = deduped.iter().map(|r| r.signal_dbm).collect();
        let mut sorted = signals.clone();
        sorted.sort_by(|a, b| b.cmp(a));
        assert_eq!(signals, sorted);
        assert_eq!(deduped.first().unwrap().ssid.as_deref(), Some("BEAST_ROUTER"));
    }

    // ---- fake command runner: proves the fail-safe ordering, no real device involved --------

    struct FakeRunner {
        wpa_calls: Vec<Vec<String>>,
        wpa_responses: VecDeque<Result<String, String>>,
        psk_calls: Vec<(u32, String)>,
        ip_calls: Vec<Vec<String>>,
        ip_responses: VecDeque<Result<String, String>>,
        dhcp_restarts: u32,
    }

    impl FakeRunner {
        fn new(wpa: Vec<Result<&str, &str>>, ip: Vec<Result<&str, &str>>) -> Self {
            FakeRunner {
                wpa_calls: Vec::new(),
                wpa_responses: wpa.into_iter().map(|r| r.map(str::to_string).map_err(str::to_string)).collect(),
                psk_calls: Vec::new(),
                ip_calls: Vec::new(),
                ip_responses: ip.into_iter().map(|r| r.map(str::to_string).map_err(str::to_string)).collect(),
                dhcp_restarts: 0,
            }
        }
    }

    impl Runner for FakeRunner {
        fn wpa(&mut self, args: &[&str]) -> Result<String, String> {
            self.wpa_calls.push(args.iter().map(|s| s.to_string()).collect());
            self.wpa_responses.pop_front().unwrap_or(Ok("OK".to_string()))
        }
        fn set_psk(&mut self, id: u32, psk: &str) -> Result<(), String> {
            self.psk_calls.push((id, psk.to_string()));
            Ok(())
        }
        fn ip(&mut self, args: &[&str]) -> Result<String, String> {
            self.ip_calls.push(args.iter().map(|s| s.to_string()).collect());
            self.ip_responses.pop_front().unwrap_or(Ok(String::new()))
        }
        fn restart_dhcp(&mut self) -> Result<(), String> {
            self.dhcp_restarts += 1;
            Ok(())
        }
    }

    fn flat(calls: &[Vec<String>]) -> Vec<Vec<&str>> {
        calls.iter().map(|c| c.iter().map(String::as_str).collect()).collect()
    }

    #[test]
    fn connect_happy_path_adds_a_new_network_and_never_touches_id_zero() {
        let mut runner = FakeRunner::new(
            vec![
                Ok("wpa_state=COMPLETED\nid=0\nssid=IoT\n"), // previous_id capture
                Ok(REAL_LIST_NETWORKS_ONE_ENTRY),            // ensure_network: only id 0 exists
                Ok("5\n"),                                   // add_network
                Ok("OK"),                                    // set_network 5 ssid
                Ok("OK"),                                    // set_network 5 key_mgmt
                Ok("OK"),                                    // enable_network 5
                Ok("OK"),                                    // select_network 5
                Ok("OK"),                                    // save_config
                Ok("wpa_state=COMPLETED\nid=5\nssid=BEAST_ROUTER\n"), // poll: associated
                Ok("wpa_state=COMPLETED\nid=5\nssid=BEAST_ROUTER\n"), // final status returned to the caller
            ],
            vec![Ok(REAL_IP_ADDR_SHOW)],
        );

        let result = connect_with_timeout(
            &mut runner,
            "BEAST_ROUTER",
            Some("test-password"),
            Duration::from_millis(200),
            Duration::from_millis(5),
        );
        assert!(matches!(result, Ok(s) if s.id == Some(5) && s.wpa_state == "COMPLETED"));
        assert_eq!(runner.psk_calls, vec![(5, "test-password".to_string())]);
        assert_eq!(runner.dhcp_restarts, 1);
        assert_eq!(
            flat(&runner.wpa_calls),
            vec![
                vec!["status"],
                vec!["list_networks"],
                vec!["add_network"],
                vec!["set_network", "5", "ssid", "\"BEAST_ROUTER\""],
                vec!["set_network", "5", "key_mgmt", "WPA-PSK"],
                vec!["enable_network", "5"],
                vec!["select_network", "5"],
                vec!["save_config"],
                vec!["status"],
                vec!["status"],
            ]
        );
        // The vendor's id 0 network is never targeted by any `set_network`/`enable_network`.
        assert!(runner.wpa_calls.iter().all(|c| !(c.first().map(String::as_str) == Some("set_network") && c.get(1).map(String::as_str) == Some("0"))));
    }

    #[test]
    fn connect_reuses_an_existing_kibble_network_instead_of_adding_a_duplicate() {
        let existing_list = "network id / ssid / bssid / flags\n0\tIoT\tany\t[CURRENT]\n3\tBEAST_ROUTER\tany\t[DISABLED]\n";
        let mut runner = FakeRunner::new(
            vec![
                Ok("wpa_state=COMPLETED\nid=0\nssid=IoT\n"), // previous_id
                Ok(existing_list),                           // ensure_network finds id 3
                Ok("OK"),                                    // set_network 3 key_mgmt (reuse path)
                Ok("OK"),                                    // enable_network 3
                Ok("OK"),                                    // select_network 3
                Ok("OK"),                                    // save_config
                Ok("wpa_state=COMPLETED\nid=3\nssid=BEAST_ROUTER\n"), // poll: associated
                Ok("wpa_state=COMPLETED\nid=3\nssid=BEAST_ROUTER\n"), // final status returned to the caller
            ],
            vec![Ok(REAL_IP_ADDR_SHOW)],
        );
        let result = connect_with_timeout(
            &mut runner, "BEAST_ROUTER", Some("corrected-password"),
            Duration::from_millis(200), Duration::from_millis(5),
        );
        assert!(matches!(result, Ok(s) if s.id == Some(3) && s.wpa_state == "COMPLETED"));
        assert_eq!(runner.psk_calls, vec![(3, "corrected-password".to_string())]);
        assert!(!flat(&runner.wpa_calls).iter().any(|c| c.first() == Some(&"add_network")));
    }

    #[test]
    fn connect_without_psk_reuses_an_existing_network_with_no_psk_update() {
        let existing_list = "network id / ssid / bssid / flags\n0\tIoT\tany\t[CURRENT]\n2\tGuest\tany\t[DISABLED]\n";
        let mut runner = FakeRunner::new(
            vec![
                Ok("wpa_state=COMPLETED\nid=0\nssid=IoT\n"),
                Ok(existing_list),
                Ok("OK"), // enable_network 2 (no key_mgmt/psk calls -- psk was None)
                Ok("OK"), // select_network 2
                Ok("OK"), // save_config
                Ok("wpa_state=COMPLETED\nid=2\nssid=Guest\n"), // poll: associated
                Ok("wpa_state=COMPLETED\nid=2\nssid=Guest\n"), // final status returned to the caller
            ],
            vec![Ok(REAL_IP_ADDR_SHOW)],
        );
        let result = connect_with_timeout(
            &mut runner, "Guest", None, Duration::from_millis(200), Duration::from_millis(5),
        );
        assert!(matches!(result, Ok(s) if s.id == Some(2) && s.wpa_state == "COMPLETED"));
        assert!(runner.psk_calls.is_empty());
    }

    #[test]
    fn connect_without_psk_and_no_existing_network_is_rejected_before_touching_anything() {
        let mut runner = FakeRunner::new(
            vec![
                Ok("wpa_state=COMPLETED\nid=0\nssid=IoT\n"),
                Ok(REAL_LIST_NETWORKS_ONE_ENTRY), // only id 0, no "BEAST_ROUTER"
            ],
            vec![],
        );
        let result = connect_with_timeout(
            &mut runner, "BEAST_ROUTER", None, Duration::from_millis(200), Duration::from_millis(5),
        );
        assert!(matches!(result, Err(Error::PskRequired)));
        assert!(!flat(&runner.wpa_calls).iter().any(|c| c.first() == Some(&"select_network")));
    }

    #[test]
    fn association_timeout_rolls_back_to_the_previous_network() {
        let mut runner = FakeRunner::new(
            vec![
                Ok("wpa_state=COMPLETED\nid=0\nssid=IoT\n"), // previous_id = 0
                Ok(REAL_LIST_NETWORKS_ONE_ENTRY),
                Ok("7\n"),
                Ok("OK"), // set_network ssid
                Ok("OK"), // set_network key_mgmt
                Ok("OK"), // enable_network
                Ok("OK"), // select_network 7
                Ok("OK"), // save_config
                // Every poll afterward: never reaches COMPLETED on id 7 -- a wrong password
                // symptom (stuck in the handshake, never associates).
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                Ok("wpa_state=4WAY_HANDSHAKE\nid=7\n"),
                // rollback:
                Ok("OK"), // select_network 0
                Ok("OK"), // save_config
            ],
            vec![],
        );
        let result = connect_with_timeout(
            &mut runner, "BEAST_ROUTER", Some("wrong-password"),
            Duration::from_millis(30), Duration::from_millis(3),
        );
        assert!(matches!(result, Err(Error::AssociationFailed { rolled_back_to: Some(0), rollback_error: None })));
        assert_eq!(runner.dhcp_restarts, 0, "must never restart dhcp without first associating");
        assert!(runner.ip_calls.is_empty(), "must never even check for a lease");
        let calls = flat(&runner.wpa_calls);
        assert_eq!(calls.last(), Some(&vec!["save_config"]));
        assert_eq!(calls[calls.len() - 2], vec!["select_network", "0"]);
    }

    #[test]
    fn dhcp_timeout_rolls_back_to_the_previous_network() {
        let mut runner = FakeRunner::new(
            vec![
                Ok("wpa_state=COMPLETED\nid=0\nssid=IoT\n"),
                Ok(REAL_LIST_NETWORKS_ONE_ENTRY),
                Ok("9\n"),
                Ok("OK"),
                Ok("OK"),
                Ok("OK"),
                Ok("OK"), // select_network 9
                Ok("OK"), // save_config
                Ok("wpa_state=COMPLETED\nid=9\nssid=BEAST_ROUTER\n"), // associates fine
                Ok("OK"), // rollback: select_network 0
                Ok("OK"), // rollback: save_config
            ],
            vec![Ok(""), Ok(""), Ok(""), Ok(""), Ok(""), Ok(""), Ok(""), Ok(""), Ok(""), Ok("")], // no `inet` line, ever
        );
        let result = connect_with_timeout(
            &mut runner, "BEAST_ROUTER", Some("right-password-bad-dhcp"),
            Duration::from_millis(30), Duration::from_millis(3),
        );
        assert!(matches!(result, Err(Error::NoDhcpLease { rolled_back_to: Some(0), rollback_error: None })));
        assert_eq!(runner.dhcp_restarts, 1);
        let calls = flat(&runner.wpa_calls);
        assert_eq!(calls.last(), Some(&vec!["save_config"]));
        assert_eq!(calls[calls.len() - 2], vec!["select_network", "0"]);
    }

    #[test]
    fn rollback_falls_back_to_vendor_id_zero_when_no_previous_status_was_readable() {
        let mut runner = FakeRunner::new(
            vec![
                Err("control socket hiccup"), // previous_id capture fails -> None
                Ok(REAL_LIST_NETWORKS_ONE_ENTRY),
                Ok("2\n"),
                Ok("OK"),
                Ok("OK"),
                Ok("OK"),
                Ok("OK"), // select_network 2
                Ok("OK"), // save_config
                Ok("wpa_state=DISCONNECTED\n"), // never completes
                Ok("OK"), // rollback: select_network 0 (fallback, no previous id known)
                Ok("OK"),
            ],
            vec![],
        );
        let result = connect_with_timeout(
            &mut runner, "BEAST_ROUTER", Some("x"),
            Duration::from_millis(10), Duration::from_millis(2),
        );
        assert!(matches!(result, Err(Error::AssociationFailed { rolled_back_to: None, .. })));
        assert_eq!(flat(&runner.wpa_calls).last(), Some(&vec!["save_config"]));
        let calls = flat(&runner.wpa_calls);
        assert!(calls.contains(&vec!["select_network", "0"]));
    }

    // ---- forget ------------------------------------------------------------------------------

    #[test]
    fn forget_refuses_the_vendors_own_network() {
        let mut runner = FakeRunner::new(vec![Ok(REAL_LIST_NETWORKS_ONE_ENTRY)], vec![]);
        let result = forget_network(&mut runner, "IoT");
        assert!(matches!(result, Err(Error::ForbiddenVendorNetwork)));
    }

    #[test]
    fn forget_refuses_an_unknown_ssid() {
        let mut runner = FakeRunner::new(vec![Ok(REAL_LIST_NETWORKS_ONE_ENTRY)], vec![]);
        let result = forget_network(&mut runner, "NeverAddedThis");
        assert!(matches!(result, Err(Error::UnknownNetwork)));
    }

    #[test]
    fn forget_refuses_the_currently_active_network() {
        let list = "network id / ssid / bssid / flags\n0\tIoT\tany\t[DISABLED]\n4\tBEAST_ROUTER\tany\t[CURRENT]\n";
        let mut runner = FakeRunner::new(
            vec![Ok(list), Ok("wpa_state=COMPLETED\nid=4\nssid=BEAST_ROUTER\n")],
            vec![],
        );
        let result = forget_network(&mut runner, "BEAST_ROUTER");
        assert!(matches!(result, Err(Error::CannotForgetActive)));
    }

    #[test]
    fn forget_removes_an_inactive_kibble_owned_network() {
        let list = "network id / ssid / bssid / flags\n0\tIoT\tany\t[CURRENT]\n4\tBEAST_ROUTER\tany\t[DISABLED]\n";
        let mut runner = FakeRunner::new(
            vec![
                Ok(list),
                Ok("wpa_state=COMPLETED\nid=0\nssid=IoT\n"),
                Ok("OK"), // remove_network 4
                Ok("OK"), // save_config
            ],
            vec![],
        );
        let result = forget_network(&mut runner, "BEAST_ROUTER");
        assert!(result.is_ok());
        assert!(flat(&runner.wpa_calls).contains(&vec!["remove_network", "4"]));
    }

    // ---- reconcile backoff (pure decision) ---------------------------------------------------

    #[test]
    fn should_not_attempt_when_not_drifted() {
        assert!(!should_attempt(false, 0));
    }

    #[test]
    fn should_attempt_while_drifted_and_under_the_cap() {
        assert!(should_attempt(true, 0));
        assert!(should_attempt(true, MAX_CONSECUTIVE_FAILURES - 1));
    }

    #[test]
    fn should_not_attempt_once_the_cap_is_reached() {
        assert!(!should_attempt(true, MAX_CONSECUTIVE_FAILURES));
        assert!(!should_attempt(true, MAX_CONSECUTIVE_FAILURES + 1));
    }

    #[test]
    fn effective_failures_keeps_the_count_when_the_target_is_unchanged() {
        let last = Some(("BEAST_ROUTER".to_string(), Some("bad".to_string())));
        let same = ("BEAST_ROUTER".to_string(), Some("bad".to_string()));
        assert_eq!(effective_failures(&last, &same, MAX_CONSECUTIVE_FAILURES), MAX_CONSECUTIVE_FAILURES);
    }

    #[test]
    fn effective_failures_resets_when_the_ssid_or_psk_changes() {
        let last = Some(("BEAST_ROUTER".to_string(), Some("bad".to_string())));
        let different_ssid = ("IoT".to_string(), Some("bad".to_string()));
        let different_psk = ("BEAST_ROUTER".to_string(), Some("corrected".to_string()));
        assert_eq!(effective_failures(&last, &different_ssid, MAX_CONSECUTIVE_FAILURES), 0);
        assert_eq!(effective_failures(&last, &different_psk, MAX_CONSECUTIVE_FAILURES), 0);
    }

    #[test]
    fn effective_failures_starts_at_zero_with_no_prior_target() {
        let target = ("BEAST_ROUTER".to_string(), Some("bad".to_string()));
        assert_eq!(effective_failures(&None, &target, 0), 0);
    }

    /// Composes `effective_failures` + `should_attempt` across a realistic sequence of ticks --
    /// the exact composition `reconcile_once` performs -- proving the full behaviour promised
    /// by the module docs: a bad target gets exactly `MAX_CONSECUTIVE_FAILURES` attempts, then
    /// backs off, then a *changed* target (e.g. a corrected password) immediately gets a fresh
    /// attempt despite the old target having been exhausted.
    #[test]
    fn backoff_then_reset_sequence_matches_the_module_docs() {
        let mut consecutive_failures = 0u32;
        let mut last_target: Option<(String, Option<String>)> = None;
        let bad_target = ("BEAST_ROUTER".to_string(), Some("bad".to_string()));
        let drifted = true;

        for attempt in 0..MAX_CONSECUTIVE_FAILURES {
            consecutive_failures = effective_failures(&last_target, &bad_target, consecutive_failures);
            last_target = Some(bad_target.clone());
            assert!(should_attempt(drifted, consecutive_failures), "attempt {attempt} must still be allowed");
            consecutive_failures += 1; // simulates this attempt failing
        }
        // Same target, cap now reached: no further attempt.
        consecutive_failures = effective_failures(&last_target, &bad_target, consecutive_failures);
        last_target = Some(bad_target.clone());
        assert!(!should_attempt(drifted, consecutive_failures), "must have backed off by now");

        // A changed target (corrected password) immediately gets a fresh attempt.
        let fixed_target = ("BEAST_ROUTER".to_string(), Some("corrected".to_string()));
        consecutive_failures = effective_failures(&last_target, &fixed_target, consecutive_failures);
        assert!(should_attempt(drifted, consecutive_failures), "a changed target must not stay backed off");
    }

    // ---- persistence --------------------------------------------------------------------------

    #[test]
    fn missing_wifi_file_has_no_desired_network() {
        let desired = load_from("/nonexistent/path/kibble-wifi-test.json");
        assert_eq!(desired, Desired::default());
    }

    #[test]
    fn save_then_load_round_trips_including_the_psk() {
        let path = std::env::temp_dir()
            .join(format!("kibble-wifi-test-{:?}-a.json", std::thread::current().id()))
            .to_string_lossy()
            .into_owned();
        let desired = Desired {
            ssid: Some("BEAST_ROUTER".to_string()),
            psk: Some("dummy-test-secret-not-a-real-password".to_string()),
            last_error: None,
        };
        save_to(&path, &desired).unwrap();
        assert_eq!(load_from(&path), desired);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn saved_wifi_file_is_not_world_or_group_readable() {
        let path = std::env::temp_dir()
            .join(format!("kibble-wifi-test-{:?}-b.json", std::thread::current().id()))
            .to_string_lossy()
            .into_owned();
        let desired = Desired {
            ssid: Some("IoT".to_string()),
            psk: Some("dummy-test-secret".to_string()),
            last_error: None,
        };
        save_to(&path, &desired).unwrap();
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "wifi.json holds a plaintext psk and must be root-only");
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn last_error_with_a_quote_round_trips_without_breaking_the_surrounding_json() {
        let path = std::env::temp_dir()
            .join(format!("kibble-wifi-test-{:?}-c.json", std::thread::current().id()))
            .to_string_lossy()
            .into_owned();
        let desired = Desired {
            ssid: Some("BEAST_ROUTER".to_string()),
            psk: Some("x".to_string()),
            last_error: Some(r#"wpa_cli set_network 5 psk did not return OK ("weird")"#.to_string()),
        };
        save_to(&path, &desired).unwrap();
        let loaded = load_from(&path);
        assert!(loaded.last_error.unwrap().contains("weird"));
        let _ = fs::remove_file(&path);
    }
}
