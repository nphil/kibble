//! Petkit-cloud kill switch: replace the default route with a kernel blackhole so every packet
//! not addressed to the LAN is dropped before it leaves the device, while Home Assistant,
//! Scrypted and the router on `192.168.0.0/16` keep working through an explicit route via the
//! same gateway.
//!
//! ## Why routes, not a packet filter
//!
//! This SoC's kernel has no netfilter at all (no iptables/nftables, no `/proc/net/ip_tables_*`,
//! no `/lib/modules`) and `tc` fails every operation -- even a read-only `qdisc show` -- with
//! "Operation not supported" (`CONFIG_NET_SCHED` not built in). Confirmed live, not assumed.
//! The kernel's FIB (`ip route`) is a *different* subsystem, unaffected by either gap, and
//! this device's busybox `ip` applet supports `add|del|replace` plus the `blackhole` route
//! type -- confirmed live: `ip route add blackhole 203.0.113.113/32` / `ip route del` round-trip
//! cleanly, and a `blackhole default` silently drops everything not matched by a more specific
//! route, with zero kernel module or packet-filter support required.
//!
//! ## Why the LAN carve-out has to exist *and verify* before the blackhole goes in
//!
//! The feeder's only directly-connected subnet is `192.168.4.0/24`; every other host in
//! `192.168.0.0/16` -- Home Assistant, Scrypted, the router itself -- is reached *through* the
//! same gateway the default route uses. A bare `blackhole default` would cut that traffic too.
//! `disable()` therefore adds `192.168.0.0/16 via <gw> dev <iface>` and re-reads `ip route show`
//! to confirm the kernel actually accepted it *before* touching the default route at all; if
//! that check fails for any reason, the default route is left completely alone. This was not a
//! theoretical worry: an earlier hand-run mechanism test on this exact device that blackholed
//! `default` *before* the carve-out route existed immediately cut the tester's own control
//! session and (for about 90s, until manually recovered) Scrypted's RTSP session, because
//! `192.168.1.0/24` has no other path off the feeder's subnet. See the retained project memory
//! from that session for the full kernel-behaviour writeup. Never again: every path in this
//! module either verifies the carve-out first or doesn't touch the default route.
//!
//! ## Why a self-check and automatic rollback, not just "hope the carve-out worked"
//!
//! Confirming the carve-out route *exists* in `ip route show` proves the kernel accepted the
//! netlink request; it does not prove the router will actually forward across it (ACLs, a typo
//! this study made in the gateway it derived, etc.). After the blackhole is applied,
//! [`disable`] opens one real TCP connection (3s timeout) to a LAN host on the other side of
//! that carve-out -- preferably whoever is actively pulling the RTSP stream right now (found by
//! reading `/proc/net/tcp` for an ESTABLISHED peer on the RTSP port, not by hardcoding an IP),
//! falling back to the gateway itself if nothing is currently streaming. A completed connection
//! *or* an immediate refusal both prove the packet reached the target and a reply came back --
//! the route works. Only a timeout means it doesn't. On failure the default route is restored
//! immediately and the attempt is recorded as `enabled: true` with a `last_error` -- the switch
//! fails SAFE (cloud stays reachable) and never fails CLOSED (device stranded), even for one
//! HTTP request.
//!
//! ## Why a reconcile loop, not a one-shot
//!
//! `/usr/share/udhcpc/default.script` -- the *stock*, unmodified busybox default script;
//! confirmed live, and confirmed in use here because `/app/script/default.script` does not
//! exist on this device so the live `udhcpc -i wlan0 -b` command line (no `-s`) falls back to
//! it -- deletes and re-adds `route add default gw <router> dev <iface>` on **every** DHCP
//! renew or initial bind (`renew|bound)` case, lines 55/66/71). The vendor's own
//! `/app/script/wifi_connect.sh`, which `ctrl` calls on every WiFi reconnect
//! (`docs/02-boot.md` line 548), does the same thing from scratch by killing and restarting
//! udhcpc. Either event re-adds a real default route while cloud is supposed to stay off.
//! Confirmed live: a real default route added (via either the legacy `route` tool or
//! `ip route replace`) while a `blackhole default` already exists does **not** replace it --
//! both entries coexist in `ip route show`, and the *real* route wins for actual forwarding
//! (a coexisting blackhole is not enough to keep traffic blocked). So [`reconcile_once`] does
//! not just re-assert the blackhole; it explicitly deletes any live real default first (see
//! [`assert_disabled`]), then re-blackholes, using the exact same verified-order-plus-self-check
//! path as an explicit `POST /cloud {"enabled":false}` -- there is only one "make cloud
//! disabled" code path in this module, [`disable_with_safety`], and both callers share it.
//!
//! Desired state (`enabled`, the last-known gateway/interface, and the last self-check failure
//! if any) is persisted to `/opt/kibble/cloud.json` so a restart -- or the boot-time first
//! reconcile pass, run once `wlan0` has a live route -- picks back up where the user left it.
//! Nothing here ever needs to know the gateway or interface as a compile-time constant: both
//! are always read back from live `ip route show` output, cached only for the moments (like
//! `enable()` from an already-blackholed state) where nothing live is left to read it from.

use std::fs;
use std::io;
use std::net::{SocketAddr, TcpStream};
use std::process::Command;
use std::sync::Mutex;
use std::time::{Duration, Instant};

const STATE_PATH: &str = "/opt/kibble/cloud.json";
/// Covers every subnet this LAN uses (the router itself, Home Assistant on `192.168.1.0/24`,
/// the feeder's own `192.168.4.0/24`, ...) via one static route through the same gateway the
/// default route already uses. Deliberately broad -- narrowing this to "just the hosts we
/// know about today" is exactly the kind of change that quietly strands a *new* LAN host added
/// next year, so it stays at the /16 the network actually spans.
const LAN_CIDR: &str = "192.168.0.0/16";
/// The port kibbled's own RTSP server listens on (mirrors `main::RTSP_BIND`; kept as a local
/// constant rather than importing from `main` to avoid coupling this module to the RTSP
/// module's internals -- both already hardcode `8554` today, same as the HA integration's
/// `const.py`). Used only to recognise *which* live `/proc/net/tcp` entry is the RTSP peer
/// worth self-checking against, not to open anything.
const RTSP_PORT: u16 = 8554;

pub const RECONCILE_INTERVAL: Duration = Duration::from_secs(15);
const WLAN_WAIT_POLL: Duration = Duration::from_secs(1);
/// How long to wait, at boot, for a live default route (i.e. for `wlan0` to have an address)
/// before reconciling anyway. Mirrors `persist::READY_MAX_WAIT`'s "bounded extra wait, then
/// proceed" shape.
const WLAN_WAIT_MAX: Duration = Duration::from_secs(60);
const SELF_CHECK_TIMEOUT: Duration = Duration::from_secs(3);

/// Serialises every route mutation between the HTTP handler thread and the reconciler thread,
/// exactly like `persist::WRITE_LOCK`. Same `panic = "abort"` reasoning applies: an ordinary
/// `unwrap()` on a poisoned lock is fine because a poisoned mutex is not a state this process
/// can observe and keep running.
static LOCK: Mutex<()> = Mutex::new(());

#[derive(Debug, Clone, PartialEq)]
pub struct State {
    pub enabled: bool,
    /// Last gateway this module derived from a live `ip route show`. Only ever read back when
    /// nothing live is available (typically: `enable()` while already blackholed, or a boot
    /// right after a `disabled` shutdown).
    pub gateway: Option<String>,
    pub dev: Option<String>,
    /// Set when the last attempt to disable cloud rolled back because either the LAN carve-out
    /// didn't verify or the post-blackhole self-check failed. Cleared on the next successful
    /// `enable()` or successful disable.
    pub last_error: Option<String>,
}

impl Default for State {
    fn default() -> Self {
        // No file yet means no `POST /cloud` has ever run: cloud is exactly as the vendor left
        // it (enabled), and there is nothing to restore, so no cached gateway either.
        State { enabled: true, gateway: None, dev: None, last_error: None }
    }
}

#[derive(Debug)]
pub enum Error {
    /// No live default route to read the gateway from, and nothing cached from a previous
    /// successful run either.
    NoGateway,
    /// The `192.168.0.0/16` carve-out route was added but did not verify as present in a
    /// follow-up `ip route show`. The default route was never touched.
    LanRouteMissing,
    /// An `ip` invocation itself failed (non-zero exit or couldn't exec at all).
    Command(String),
    /// The carve-out verified, the blackhole was applied, but the post-blackhole LAN
    /// self-check failed. The default route has already been restored by the time this is
    /// returned.
    SelfCheckFailed(String),
    Io(io::Error),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::NoGateway => write!(
                f,
                "no default route visible and no cached gateway; cannot determine the LAN next-hop"
            ),
            Error::LanRouteMissing => write!(
                f,
                "{LAN_CIDR} carve-out route did not verify as present after being added; \
                 the default route was left untouched"
            ),
            Error::Command(m) => write!(f, "{m}"),
            Error::SelfCheckFailed(m) => write!(f, "{m}"),
            Error::Io(e) => write!(f, "{e}"),
        }
    }
}

/// Runs one `ip` subcommand and returns its stdout, or a description of why it failed.
/// Abstracted behind a trait so the ordering/safety logic below (the part that actually
/// matters to get right) can be unit-tested against a fake that records the exact call
/// sequence, with no real device and no real `ip` binary involved.
trait Runner {
    fn run(&mut self, args: &[&str]) -> Result<String, String>;
}

struct RealRunner;

impl Runner for RealRunner {
    fn run(&mut self, args: &[&str]) -> Result<String, String> {
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
}

/// Ensure `disable()` is possible: replace/keep the default route disabled. Returns the fresh
/// state on success.
pub fn disable() -> Result<State, Error> {
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut runner = RealRunner;
    let (gw, dev) = current_gateway(&mut runner)?;
    disable_with_safety(&mut runner, &gw, &dev)
}

/// Restore the real default route. Idempotent: safe to call whether or not cloud is currently
/// disabled.
pub fn enable() -> Result<State, Error> {
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut runner = RealRunner;
    let (gw, dev) = current_gateway(&mut runner)?;
    assert_enabled(&mut runner, &gw, &dev).map_err(Error::Command)?;
    let state = State { enabled: true, gateway: Some(gw), dev: Some(dev), last_error: None };
    save(&state).map_err(Error::Io)?;
    Ok(state)
}

/// `GET /cloud`'s body: persisted desired state, the live route table (raw lines, for
/// diagnostics), and every non-LAN TCP socket with a real remote peer (not just
/// `ESTABLISHED` -- watching one drain through `CLOSE_WAIT` to gone is exactly what the live
/// verification in the project report needs to show).
pub fn status_json() -> String {
    let state = load();
    let mut runner = RealRunner;
    let routes = runner.run(&["route", "show"]).unwrap_or_default();
    let route_lines: Vec<&str> = routes.lines().map(str::trim).filter(|l| !l.is_empty()).collect();
    let conns = fs::read_to_string("/proc/net/tcp")
        .map(|t| parse_proc_net_tcp(&t))
        .unwrap_or_default();

    let mut body = String::from("{\n");
    body.push_str(&format!("  \"enabled\": {},\n", state.enabled));
    body.push_str(&format!("  \"last_error\": {},\n", opt_json_escaped(&state.last_error)));
    body.push_str("  \"routes\": [");
    for (i, r) in route_lines.iter().enumerate() {
        if i > 0 {
            body.push(',');
        }
        body.push_str(&format!("\"{}\"", r.escape_debug()));
    }
    body.push_str("],\n");
    body.push_str("  \"connections\": [");
    let mut first = true;
    for c in conns.iter().filter(|c| !is_lan(&c.remote_ip)) {
        if !first {
            body.push(',');
        }
        first = false;
        body.push_str(&format!(
            "{{\"remote\":\"{}:{}\",\"state\":\"{}\"}}",
            c.remote_ip, c.remote_port, c.state
        ));
    }
    body.push_str("]\n}\n");
    body
}

/// Shared by `disable()` and the reconciler's regression-correction path -- there is only one
/// "make cloud disabled" code path. Verified order: add the LAN carve-out, confirm it is
/// actually present, only then touch the default route; then self-check LAN reachability and
/// roll back immediately (fail SAFE) if either step didn't hold. Always persists the resulting
/// state, including `last_error` on a rollback.
fn disable_with_safety(runner: &mut impl Runner, gw: &str, dev: &str) -> Result<State, Error> {
    if let Err(e) = assert_disabled(runner, gw, dev) {
        if matches!(e, Error::LanRouteMissing) {
            // assert_disabled never touched the default route in this case -- nothing to roll
            // back, and cloud's live state is exactly whatever it already was.
            return Err(e);
        }
        // Something failed after the default route was already touched (e.g. the blackhole
        // replace itself). Best-effort restore rather than risk stranding the device.
        let _ = assert_enabled(runner, gw, dev);
        let state = State {
            enabled: true,
            gateway: Some(gw.to_string()),
            dev: Some(dev.to_string()),
            last_error: Some(e.to_string()),
        };
        let _ = save(&state);
        return Err(e);
    }

    if let Err(reason) = self_check(gw) {
        let _ = assert_enabled(runner, gw, dev);
        let state = State {
            enabled: true,
            gateway: Some(gw.to_string()),
            dev: Some(dev.to_string()),
            last_error: Some(reason.clone()),
        };
        let _ = save(&state);
        return Err(Error::SelfCheckFailed(reason));
    }

    let state = State {
        enabled: false,
        gateway: Some(gw.to_string()),
        dev: Some(dev.to_string()),
        last_error: None,
    };
    save(&state).map_err(Error::Io)?;
    Ok(state)
}

/// (a) Add the LAN carve-out and verify it is present. (b) Only then delete any live real
/// default and assert the blackhole. Returns [`Error::LanRouteMissing`] without ever touching
/// the default route if (a) doesn't verify.
fn assert_disabled(runner: &mut impl Runner, gw: &str, dev: &str) -> Result<(), Error> {
    runner
        .run(&["route", "replace", LAN_CIDR, "via", gw, "dev", dev])
        .map_err(Error::Command)?;
    let after = runner.run(&["route", "show"]).map_err(Error::Command)?;
    if !lan_route_present(&after, gw, dev) {
        return Err(Error::LanRouteMissing);
    }
    // Best-effort: deleting a route that isn't there errors, and that's fine -- it just means
    // cloud was already disabled (or never was). What matters is that nothing relies on
    // `replace` alone to suppress a real default that might coexist with a blackhole one --
    // confirmed live, it doesn't (see module docs).
    let _ = runner.run(&["route", "del", "default", "via", gw, "dev", dev]);
    runner
        .run(&["route", "replace", "blackhole", "default"])
        .map_err(Error::Command)?;
    Ok(())
}

/// Restore the real default route, then drop any coexisting blackhole remnant. Best-effort on
/// the cleanup half: a blackhole that was never there errors harmlessly.
fn assert_enabled(runner: &mut impl Runner, gw: &str, dev: &str) -> Result<(), String> {
    runner.run(&["route", "replace", "default", "via", gw, "dev", dev])?;
    let _ = runner.run(&["route", "del", "blackhole", "default"]);
    Ok(())
}

/// Re-applies desired cloud state once a live default route is visible (i.e. once `wlan0` has
/// an address), then keeps checking every [`RECONCILE_INTERVAL`] for the rest of the process's
/// life.
pub fn spawn_reconciler() {
    std::thread::spawn(|| {
        wait_for_live_route();
        // `routes`/`connections` in the status JSON are live kernel state that changes without
        // any write of ours; diff the exact bytes HA would receive, on the tick this thread
        // already takes, and mark only when they differ.
        let mut last_status = status_json();
        loop {
            reconcile_once();
            let status = status_json();
            if status != last_status {
                crate::push::mark(crate::push::Field::Cloud);
                last_status = status;
            }
            std::thread::sleep(RECONCILE_INTERVAL);
        }
    });
}

fn wait_for_live_route() {
    let deadline = Instant::now() + WLAN_WAIT_MAX;
    let mut runner = RealRunner;
    loop {
        if let Ok(routes) = runner.run(&["route", "show"]) {
            if parse_default_route(&routes).is_some() {
                return;
            }
        }
        if Instant::now() >= deadline {
            eprintln!(
                "kibbled: cloud: no live default route after {WLAN_WAIT_MAX:?}, reconciling anyway"
            );
            return;
        }
        std::thread::sleep(WLAN_WAIT_POLL);
    }
}

fn reconcile_once() {
    let _guard = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let mut runner = RealRunner;
    let state = load();
    let live = match runner.run(&["route", "show"]) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("kibbled: cloud reconcile: `ip route show` failed, skipping this tick: {e}");
            return;
        }
    };
    let seen = parse_default_route(&live);
    match (state.enabled, seen) {
        (false, Some((gw, dev))) => {
            eprintln!(
                "kibbled: cloud reconcile: default route reappeared (via {gw} dev {dev}) while \
                 cloud is disabled, re-blackholing"
            );
            match disable_with_safety(&mut runner, &gw, &dev) {
                Ok(_) => eprintln!("kibbled: cloud reconcile: re-blackholed default (via {gw} dev {dev})"),
                Err(e) => eprintln!(
                    "kibbled: cloud reconcile: could not safely re-blackhole, left cloud enabled: {e}"
                ),
            }
        }
        (true, None) => {
            let (Some(gw), Some(dev)) = (state.gateway.clone(), state.dev.clone()) else {
                eprintln!(
                    "kibbled: cloud reconcile: cloud should be enabled but no default route is \
                     visible and no gateway is cached, cannot self-heal"
                );
                return;
            };
            eprintln!(
                "kibbled: cloud reconcile: default route missing while cloud is enabled, \
                 restoring via {gw} dev {dev}"
            );
            match assert_enabled(&mut runner, &gw, &dev) {
                Ok(()) => {
                    let _ = save(&State {
                        enabled: true,
                        gateway: Some(gw),
                        dev: Some(dev),
                        last_error: None,
                    });
                }
                Err(e) => eprintln!("kibbled: cloud reconcile: failed to restore default route: {e}"),
            }
        }
        _ => {} // already consistent -- silent, so a healthy device produces no log spam
    }
}

fn current_gateway(runner: &mut impl Runner) -> Result<(String, String), Error> {
    let live = runner.run(&["route", "show"]).map_err(Error::Command)?;
    resolve_gateway(&live, &load())
}

/// Pure decision, split out from [`current_gateway`] for testing without any I/O: prefer a
/// gateway/interface read straight out of live `ip route show` output; fall back to whatever
/// was cached from the last time this module *did* see one live.
fn resolve_gateway(live: &str, cached: &State) -> Result<(String, String), Error> {
    if let Some(pair) = parse_default_route(live) {
        return Ok(pair);
    }
    match (&cached.gateway, &cached.dev) {
        (Some(gw), Some(dev)) => Ok((gw.clone(), dev.clone())),
        _ => Err(Error::NoGateway),
    }
}

/// Finds the first *live* (non-blackhole) IPv4 default route in `ip route show` output and
/// returns its gateway and outbound interface, e.g. `("192.168.4.1", "wlan0")` for
/// `default via 192.168.4.1 dev wlan0`. Whitespace-tolerant: this device's `ip route show`
/// pads some fields with runs of spaces (e.g. `dev wlan0 scope link  src 192.168.4.85`), so
/// this splits on any amount of whitespace rather than assuming single spaces.
///
/// A blackholed default (`blackhole default`) starts with the token `blackhole`, not
/// `default` (confirmed live), so it is never mistaken for a live route here -- which is
/// exactly what lets [`reconcile_once`] tell "still off" from "a real default came back" just
/// by calling this on every poll.
pub fn parse_default_route(routes: &str) -> Option<(String, String)> {
    for line in routes.lines() {
        let mut tok = line.split_whitespace();
        if tok.next() != Some("default") {
            continue;
        }
        let (mut gw, mut dev) = (None, None);
        while let Some(word) = tok.next() {
            match word {
                "via" => gw = tok.next(),
                "dev" => dev = tok.next(),
                _ => {}
            }
        }
        if let (Some(gw), Some(dev)) = (gw, dev) {
            return Some((gw.to_string(), dev.to_string()));
        }
    }
    None
}

/// Whether `routes` contains a `<LAN_CIDR> via <gw> dev <dev>` line -- the verification step
/// between adding the carve-out and touching the default route.
fn lan_route_present(routes: &str, gw: &str, dev: &str) -> bool {
    routes.lines().any(|line| {
        let mut tok = line.split_whitespace();
        if tok.next() != Some(LAN_CIDR) {
            return false;
        }
        let (mut seen_gw, mut seen_dev) = (None, None);
        while let Some(word) = tok.next() {
            match word {
                "via" => seen_gw = tok.next(),
                "dev" => seen_dev = tok.next(),
                _ => {}
            }
        }
        seen_gw == Some(gw) && seen_dev == Some(dev)
    })
}

fn is_lan(ip: &str) -> bool {
    ip.starts_with("192.168.") || ip.starts_with("127.")
}

/// Picks the LAN self-check target: the remote IP of whoever is currently pulling the RTSP
/// stream (a real cross-subnet consumer -- e.g. Scrypted on `192.168.1.0/24` -- which is
/// exactly the path the carve-out exists for), found by reading `/proc/net/tcp` for an
/// `ESTABLISHED` peer on kibbled's own RTSP port rather than depending on `rtsp.rs`'s session
/// bookkeeping. Falls back to the gateway on port 80 if nobody is currently streaming: a
/// weaker check (it only proves the immediate next hop, not a cross-subnet path) but always
/// available.
fn self_check_target(gw: &str) -> (String, u16) {
    match rtsp_peer_ip() {
        Some(ip) => (ip, RTSP_PORT),
        None => (gw.to_string(), 80),
    }
}

fn rtsp_peer_ip() -> Option<String> {
    let text = fs::read_to_string("/proc/net/tcp").ok()?;
    parse_proc_net_tcp(&text)
        .into_iter()
        .find(|c| c.local_port == RTSP_PORT && c.state == "ESTABLISHED")
        .map(|c| c.remote_ip)
}

/// Proves the LAN carve-out actually works before committing to it: one real TCP connect (3s
/// timeout) to the target [`self_check_target`] picks. A completed connection *or* an
/// immediate refusal both mean a packet reached the target and a reply came back -- the route
/// works, whatever is or isn't listening on the exact port. Only a timeout means it doesn't.
fn self_check(gw: &str) -> Result<(), String> {
    let (ip, port) = self_check_target(gw);
    let addr: SocketAddr = match format!("{ip}:{port}").parse() {
        Ok(a) => a,
        Err(e) => return Err(format!("self-check target {ip}:{port} is not a valid address: {e}")),
    };
    let outcome = TcpStream::connect_timeout(&addr, SELF_CHECK_TIMEOUT).map(|_| ());
    classify_self_check(outcome)
        .map_err(|e| format!("LAN unreachable after blackholing ({ip}:{port}): {e}, rolled back"))
}

/// Split out from [`self_check`] so the pass/fail interpretation is testable without any real
/// socket: a completed connection or an immediate refusal both prove the route works.
fn classify_self_check(result: io::Result<()>) -> Result<(), String> {
    match result {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::ConnectionRefused => Ok(()),
        Err(e) => Err(e.to_string()),
    }
}

pub struct Connection {
    pub local_port: u16,
    pub remote_ip: String,
    pub remote_port: u16,
    pub state: &'static str,
}

fn tcp_state_name(code: u8) -> &'static str {
    match code {
        0x01 => "ESTABLISHED",
        0x02 => "SYN_SENT",
        0x03 => "SYN_RECV",
        0x04 => "FIN_WAIT1",
        0x05 => "FIN_WAIT2",
        0x06 => "TIME_WAIT",
        0x07 => "CLOSE",
        0x08 => "CLOSE_WAIT",
        0x09 => "LAST_ACK",
        0x0A => "LISTEN",
        0x0B => "CLOSING",
        0x0C => "NEW_SYN_RECV",
        _ => "UNKNOWN",
    }
}

/// Decodes one `/proc/net/tcp` hex `address:port` field (e.g. `5504A8C0:216A`) into
/// dotted-decimal + a plain decimal port. The kernel prints the address as `%08X` of the raw
/// `__be32` on a little-endian CPU, which byte-reverses the dotted-decimal octets relative to
/// the hex text -- confirmed against this device's own live address, `192.168.4.85`, which
/// prints as `5504A8C0` (octet4=0x55=85 first, octet1=0xC0=192 last). The port is a plain
/// big-endian `%04X`, no reversal (`216A` = 8554, kibbled's own RTSP port).
fn decode_addr(field: &str) -> Option<(String, u16)> {
    let (hex_ip, hex_port) = field.split_once(':')?;
    if hex_ip.len() != 8 {
        return None;
    }
    let byte = |i: usize| u8::from_str_radix(&hex_ip[i * 2..i * 2 + 2], 16).ok();
    let ip = format!("{}.{}.{}.{}", byte(3)?, byte(2)?, byte(1)?, byte(0)?);
    let port = u16::from_str_radix(hex_port, 16).ok()?;
    Some((ip, port))
}

/// Parses `/proc/net/tcp` (IPv4) into every socket that has a real remote peer -- i.e. not a
/// `LISTEN` socket, which reports `rem_address` as `00000000:0000`. Malformed lines (just the
/// header, in practice) are skipped rather than failing the whole read.
pub fn parse_proc_net_tcp(text: &str) -> Vec<Connection> {
    let mut out = Vec::new();
    for line in text.lines().skip(1) {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.len() < 4 {
            continue;
        }
        let Some((_, local_port)) = decode_addr(fields[1]) else { continue };
        let Some((remote_ip, remote_port)) = decode_addr(fields[2]) else { continue };
        if remote_ip == "0.0.0.0" && remote_port == 0 {
            continue; // LISTEN socket, no peer
        }
        let Ok(state_code) = u8::from_str_radix(fields[3], 16) else { continue };
        out.push(Connection {
            local_port,
            remote_ip,
            remote_port,
            state: tcp_state_name(state_code),
        });
    }
    out
}

fn load() -> State {
    load_from(STATE_PATH)
}

fn load_from(path: &str) -> State {
    match fs::read_to_string(path) {
        Ok(text) => parse_state(&text),
        Err(_) => State::default(),
    }
}

fn parse_state(text: &str) -> State {
    let enabled = crate::http::json_field(text, "enabled")
        .map(|v| v == "true")
        .unwrap_or(true);
    let gateway = crate::http::json_field(text, "gateway")
        .filter(|v| *v != "null")
        .map(str::to_string);
    let dev = crate::http::json_field(text, "dev")
        .filter(|v| *v != "null")
        .map(str::to_string);
    let last_error = crate::http::json_field(text, "last_error")
        .filter(|v| *v != "null")
        .map(str::to_string);
    State { enabled, gateway, dev, last_error }
}

fn save(state: &State) -> io::Result<()> {
    let r = save_to(STATE_PATH, state);
    crate::push::mark(crate::push::Field::Cloud);
    r
}

fn save_to(path: &str, state: &State) -> io::Result<()> {
    let _ = fs::create_dir_all(std::path::Path::new(path).parent().unwrap_or(std::path::Path::new(".")));
    let tmp = format!("{path}.tmp");
    fs::write(&tmp, to_json(state))?;
    fs::rename(&tmp, path)
}

fn to_json(state: &State) -> String {
    format!(
        "{{\n  \"enabled\": {},\n  \"gateway\": {},\n  \"dev\": {},\n  \"last_error\": {}\n}}\n",
        state.enabled,
        opt_json(&state.gateway),
        opt_json(&state.dev),
        opt_json_escaped(&state.last_error),
    )
}

fn opt_json(v: &Option<String>) -> String {
    match v {
        Some(s) => format!("\"{s}\""),
        None => "null".to_string(),
    }
}

/// Like [`opt_json`] but for values that can contain arbitrary text (error messages): escapes
/// quotes/backslashes the same way `http::err_json` does. This hand-rolled reader/writer pair
/// (no JSON crate, matching every other persisted file in this agent) has the same documented
/// limitation `desired.rs` does: an unescaped `,`/`}}` inside the *value* would confuse only
/// that field's own re-extraction on the next load, never the other fields, since each is
/// looked up independently by key.
fn opt_json_escaped(v: &Option<String>) -> String {
    match v {
        Some(s) => format!("\"{}\"", s.escape_debug()),
        None => "null".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    // ---- real captures from the live device (2026-09-15), used verbatim where practical ----

    const BASELINE_ROUTES: &str = "default via 192.168.4.1 dev wlan0 \n\
        192.168.4.0/24 dev wlan0 scope link  src 192.168.4.85 \n";

    const DISABLED_ROUTES: &str = "blackhole default \n\
        192.168.0.0/16 via 192.168.4.1 dev wlan0 \n\
        192.168.4.0/24 dev wlan0 scope link  src 192.168.4.85 \n";

    /// The exact 3-line table observed live when a real default route reappeared (DHCP-style)
    /// while a blackhole default was already present -- both entries coexisting, confirmed by
    /// this project's own live testing to *not* be a case where the blackhole line alone keeps
    /// traffic blocked.
    const COEXISTING_ROUTES: &str = "default via 192.168.4.1 dev wlan0 \n\
        blackhole default \n\
        192.168.4.0/24 dev wlan0 scope link  src 192.168.4.85 \n";

    /// One real `/proc/net/tcp` line per case, captured live from the device:
    /// - LISTEN on kibbled's own RTSP port (8554 = 0x216A)
    /// - an ESTABLISHED RTSP session from the Scrypted/Unraid host (192.168.1.69)
    /// - an ESTABLISHED MQTT connection from `ctrl` out to Petkit's Alibaba-Cloud broker
    ///   (47.251.247.167:33882 -- matches `docs/03-app.md`'s independently-recovered live
    ///   connection string exactly)
    /// - a TIME_WAIT OSS/HTTPS connection from `cloud` to 47.88.20.79:443 (also matches
    ///   `docs/03-app.md`)
    const PROC_NET_TCP: &str = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n\
   1: 00000000:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 35086 1 0393fe37 100 0 0 10 0\n\
   6: 5504A8C0:C7BE 4F14582F:01BB 06 00000000:00000000 03:000015E5 00000000     0        0 0 3 9a1c3937\n\
  17: 5504A8C0:216A 4501A8C0:E334 01 00002A75:00000000 01:0000000A 00000000     0        0 41575 2 39a78dbd 29 4 1 39 34\n\
  19: 5504A8C0:91B0 A7F7FB2F:845A 01 00000000:00000000 00:00000000 00000000     0        0 4518 1 40902911 32 4 30 10 -1\n";

    #[test]
    fn decodes_the_devices_own_live_address() {
        assert_eq!(decode_addr("5504A8C0:216A"), Some(("192.168.4.85".to_string(), 8554)));
    }

    #[test]
    fn decodes_the_live_alibaba_cloud_mqtt_broker_address() {
        // docs/03-app.md: "the previously-observed live connection to 47.251.247.167:33882
        // (Alibaba Cloud US)".
        assert_eq!(
            decode_addr("A7F7FB2F:845A"),
            Some(("47.251.247.167".to_string(), 33882))
        );
    }

    #[test]
    fn parses_real_proc_net_tcp_capture_into_peers_only() {
        let conns = parse_proc_net_tcp(PROC_NET_TCP);
        // The LISTEN line (rem_address 00000000:0000) must not appear -- it has no peer.
        assert_eq!(conns.len(), 3);

        let rtsp = conns.iter().find(|c| c.local_port == 8554).expect("rtsp peer");
        assert_eq!(rtsp.remote_ip, "192.168.1.69");
        assert_eq!(rtsp.state, "ESTABLISHED");

        let mqtt = conns.iter().find(|c| c.remote_port == 33882).expect("mqtt peer");
        assert_eq!(mqtt.remote_ip, "47.251.247.167");
        assert_eq!(mqtt.state, "ESTABLISHED");

        let oss = conns.iter().find(|c| c.remote_port == 443).expect("oss peer");
        assert_eq!(oss.remote_ip, "47.88.20.79");
        assert_eq!(oss.state, "TIME_WAIT");
    }

    #[test]
    fn is_lan_matches_this_networks_ranges_only() {
        assert!(is_lan("192.168.1.69"));
        assert!(is_lan("192.168.4.85"));
        assert!(is_lan("127.0.0.1"));
        assert!(!is_lan("47.251.247.167"));
        assert!(!is_lan("8.8.8.8"));
    }

    #[test]
    fn parses_gateway_and_interface_from_the_baseline_table() {
        assert_eq!(
            parse_default_route(BASELINE_ROUTES),
            Some(("192.168.4.1".to_string(), "wlan0".to_string()))
        );
    }

    #[test]
    fn a_blackholed_default_is_not_mistaken_for_a_live_one() {
        assert_eq!(parse_default_route(DISABLED_ROUTES), None);
    }

    #[test]
    fn a_coexisting_real_default_is_still_found_even_with_a_blackhole_present() {
        // This is the regression-detection case: reconcile must still find the live gw/dev to
        // delete, not just notice a blackhole line and consider itself done.
        assert_eq!(
            parse_default_route(COEXISTING_ROUTES),
            Some(("192.168.4.1".to_string(), "wlan0".to_string()))
        );
    }

    #[test]
    fn lan_carveout_is_detected_when_present() {
        assert!(lan_route_present(DISABLED_ROUTES, "192.168.4.1", "wlan0"));
    }

    #[test]
    fn lan_carveout_is_not_detected_when_absent_or_mismatched() {
        assert!(!lan_route_present(BASELINE_ROUTES, "192.168.4.1", "wlan0"));
        // A route to a different gateway must not satisfy the check.
        assert!(!lan_route_present(DISABLED_ROUTES, "10.0.0.1", "wlan0"));
    }

    #[test]
    fn resolve_gateway_prefers_live_over_cached() {
        let cached = State {
            enabled: false,
            gateway: Some("10.0.0.1".to_string()),
            dev: Some("eth9".to_string()),
            last_error: None,
        };
        assert_eq!(
            resolve_gateway(BASELINE_ROUTES, &cached).unwrap(),
            ("192.168.4.1".to_string(), "wlan0".to_string())
        );
    }

    #[test]
    fn resolve_gateway_falls_back_to_cached_when_nothing_live() {
        let cached = State {
            enabled: false,
            gateway: Some("192.168.4.1".to_string()),
            dev: Some("wlan0".to_string()),
            last_error: None,
        };
        assert_eq!(
            resolve_gateway(DISABLED_ROUTES, &cached).unwrap(),
            ("192.168.4.1".to_string(), "wlan0".to_string())
        );
    }

    #[test]
    fn resolve_gateway_errors_with_nothing_live_or_cached() {
        assert!(matches!(
            resolve_gateway(DISABLED_ROUTES, &State::default_for_test()),
            Err(Error::NoGateway)
        ));
    }

    impl State {
        fn default_for_test() -> Self {
            State { enabled: true, gateway: None, dev: None, last_error: None }
        }
    }

    #[test]
    fn connection_refused_counts_as_a_reachable_lan() {
        let err = io::Error::from(io::ErrorKind::ConnectionRefused);
        assert!(classify_self_check(Err(err)).is_ok());
    }

    #[test]
    fn timeout_counts_as_an_unreachable_lan() {
        let err = io::Error::from(io::ErrorKind::TimedOut);
        assert!(classify_self_check(Err(err)).is_err());
    }

    #[test]
    fn a_completed_connection_counts_as_reachable() {
        assert!(classify_self_check(Ok(())).is_ok());
    }

    // ---- fake command runner: proves the ordering invariant Main required, with no `ip`
    // binary and no real device involved ----

    struct FakeRunner {
        calls: Vec<Vec<String>>,
        responses: VecDeque<Result<String, String>>,
    }

    impl FakeRunner {
        fn new(responses: Vec<Result<&str, &str>>) -> Self {
            FakeRunner {
                calls: Vec::new(),
                responses: responses
                    .into_iter()
                    .map(|r| r.map(str::to_string).map_err(str::to_string))
                    .collect(),
            }
        }
    }

    impl Runner for FakeRunner {
        fn run(&mut self, args: &[&str]) -> Result<String, String> {
            self.calls.push(args.iter().map(|s| s.to_string()).collect());
            self.responses.pop_front().unwrap_or(Ok(String::new()))
        }
    }

    #[test]
    fn assert_disabled_runs_in_verified_order_on_the_happy_path() {
        let mut runner = FakeRunner::new(vec![
            Ok(""),             // route replace <LAN_CIDR> via ... dev ...
            Ok(DISABLED_ROUTES.split_once('\n').unwrap().1), // route show, missing the first line on purpose to prove it's not just re-parroting BASELINE
            Ok(""),             // route del default via ... dev ... (best-effort)
            Ok(""),             // route replace blackhole default
        ]);
        // lan_route_present needs the LAN_CIDR line; give it a realistic post-add table.
        runner.responses[1] = Ok(DISABLED_ROUTES.to_string());

        assert!(assert_disabled(&mut runner, "192.168.4.1", "wlan0").is_ok());
        assert_eq!(
            runner.calls,
            vec![
                vec!["route", "replace", LAN_CIDR, "via", "192.168.4.1", "dev", "wlan0"],
                vec!["route", "show"],
                vec!["route", "del", "default", "via", "192.168.4.1", "dev", "wlan0"],
                vec!["route", "replace", "blackhole", "default"],
            ]
        );
    }

    #[test]
    fn assert_disabled_never_touches_the_default_route_if_the_carveout_does_not_verify() {
        let mut runner = FakeRunner::new(vec![
            Ok(""),               // route replace <LAN_CIDR> ... "succeeds" (exit 0)...
            Ok(BASELINE_ROUTES),  // ...but the verification read shows it's NOT actually there
        ]);

        let result = assert_disabled(&mut runner, "192.168.4.1", "wlan0");
        assert!(matches!(result, Err(Error::LanRouteMissing)));
        // Exactly two calls: the LAN-route add and the verifying `route show`. No `route del
        // default` and no `route replace blackhole default` -- the default route was never
        // touched, per Main's requirement.
        assert_eq!(
            runner.calls,
            vec![
                vec!["route", "replace", LAN_CIDR, "via", "192.168.4.1", "dev", "wlan0"],
                vec!["route", "show"],
            ]
        );
    }

    #[test]
    fn assert_enabled_restores_default_then_cleans_up_any_blackhole_remnant() {
        let mut runner = FakeRunner::new(vec![Ok(""), Ok("")]);
        assert!(assert_enabled(&mut runner, "192.168.4.1", "wlan0").is_ok());
        assert_eq!(
            runner.calls,
            vec![
                vec!["route", "replace", "default", "via", "192.168.4.1", "dev", "wlan0"],
                vec!["route", "del", "blackhole", "default"],
            ]
        );
    }

    #[test]
    fn state_json_round_trips_including_an_error_message_with_a_quote() {
        let state = State {
            enabled: true,
            gateway: Some("192.168.4.1".to_string()),
            dev: Some("wlan0".to_string()),
            last_error: Some(r#"LAN unreachable after blackholing ("192.168.1.69:8554")"#.to_string()),
        };
        let text = to_json(&state);
        let parsed = parse_state(&text);
        assert_eq!(parsed.enabled, state.enabled);
        assert_eq!(parsed.gateway, state.gateway);
        assert_eq!(parsed.dev, state.dev);
        // The escaped quotes must not have broken the surrounding JSON string.
        assert!(parsed.last_error.unwrap().contains("192.168.1.69:8554"));
    }

    #[test]
    fn missing_state_file_defaults_to_enabled_with_nothing_cached() {
        let s = load_from("/nonexistent/path/kibble-cloud-test.json");
        assert_eq!(s, State::default());
    }

    #[test]
    fn save_then_load_round_trips() {
        let path = std::env::temp_dir()
            .join(format!("kibble-cloud-test-{:?}.json", std::thread::current().id()))
            .to_string_lossy()
            .into_owned();
        let state = State {
            enabled: false,
            gateway: Some("192.168.4.1".to_string()),
            dev: Some("wlan0".to_string()),
            last_error: None,
        };
        save_to(&path, &state).unwrap();
        assert_eq!(load_from(&path), state);
        let _ = fs::remove_file(&path);
    }
}
