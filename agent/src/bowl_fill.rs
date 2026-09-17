//! Requesting a fresh hopper-fill (leftover food) reading from the T31 MCU.
//!
//! `config_shm`'s `state.rs::off::BOWL_FILL_1/2` mirror the MCU's own "Food Surplus Ctrl" sensor
//! report (`docs/08-mcu.md` UART CMD 0x19, `docs/09-ble.md`'s confirmed host->MCU send-site
//! list). The vendor invalidates both fields to `0xffffffff` at the start of every feed
//! (`docs/14-feed-test.md`) and, on stock firmware, only refreshes them when the Petkit cloud
//! round-trips a device-state request through `ctrl`. With the cloud unreachable by design,
//! nothing ever asks again -- the reading is stuck at `0xffffffff` forever unless something else
//! prods the MCU.
//!
//! ## The bus message (2026-09-16, live-verified against the running `ble` binary)
//!
//! `ble`'s inbox message `0x601b` ([`crate::bus::msg::SUBCHIP_REQ_DATA`], `subchip_req_data` in
//! `docs/16-schedule.md`'s 30-entry table) is a generic "forward this byte to the T31 MCU as a
//! bare UART CMD" passthrough. Its handler was resolved through the GOT (base `0x50000` --
//! byte-for-byte the same base `docs/26-ble-advertising.md` found independently): it reads
//! `payload[0]` and tail-calls a wrapper that calls `build_and_send_uart_frame(cmd=payload[0],
//! flag_bit6=1, subaddr=0, payload=NULL, len=0)`. Two things were cross-checked live before
//! trusting this reading: the same GOT-resolution method recovers `0x6004`'s handler at `ble`
//! vaddr `0x16ecc` and `0x600d`'s at `0x16d79`, both matching the docs' independently-
//! disassembly-derived addresses exactly; and `build_and_send_uart_frame` itself (vaddr `0x16970`)
//! was read directly and confirmed to special-case `cmd == 0x19` (an extra hex-dump-to-log step
//! before enqueueing) -- the vendor singles this exact command out for extra care, and the
//! function writes `frame[4] = cmd` with no translation, so `0x19` sent here is `0x19` on the
//! wire. The same trace also resolved what first looked like a contradiction: the feed handler's
//! own call into this function uses `cmd = 5`, not the `0x0A` UART code the MCU echoes back in
//! its *own* acknowledgment frame -- request and ack are different directions with independent
//! numbering, not the same value, so this mechanism shares nothing with the dispense path.
//!
//! `subchip_req_data` cannot carry the T31's normal 5-byte CMD-0x19 payload -- every payload byte
//! past `[0]` is dropped by the handler itself, and the wrapper it tail-calls hardcodes a NULL/
//! zero-length payload regardless of what was sent -- so this asks with a bare, empty-payload
//! frame rather than whatever shape the vendor's own dedicated 5-byte call site (elsewhere in
//! `ble`, not traced) uses. Live-tested 2026-09-16 on the deployed feeder: sending exactly this
//! moved `bowl_fill` from `null` to a real reading with no other change, confirming the MCU
//! answers the bare form.
//!
//! ## Why never during a feed, why rate-limited
//!
//! [`BowlFillRefresh::request_if_due`] checks `off::FEEDING` because the MCU is already busy
//! running the motor and reporting motor telemetry over the same UART link during a cycle, and
//! `9916`/`9920` are already `0xffffffff` at that point anyway (the vendor invalidates them at
//! feed start) -- there is nothing to ask for until the cycle ends. The one-per-minute floor
//! exists because this is an unsolicited MCU command outside the vendor's own request cadence;
//! nothing about it is expensive, but no consumer plausibly needs a fresher number than that.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::bus::{msg, Sender};
use crate::state::{off, Shm};

/// UART CMD byte for "Food Surplus Ctrl" (`docs/08-mcu.md` CMD 0x19). See the module doc for the
/// full evidence chain from this byte to the wire. Kept as a bare `const`, never accepted as an
/// argument anywhere: the same bus message (`msg::SUBCHIP_REQ_DATA`) can forward *any* CMD byte,
/// including `5` (the feed command in this same numbering -- see the module doc), so nothing in
/// this crate may ever construct that payload from anything other than this literal.
const FOOD_SURPLUS_CMD: u8 = 0x19;

const MIN_INTERVAL_SECS: u64 = 60;

/// Pure request-gating decision (never during a feed, never more than once a minute), split out
/// from the actual send so it's unit-testable without a real mqueue -- see `bus.rs`/`state.rs`'s
/// own tests for why nothing touching a real `Sender` or `Shm::open` is unit tested here either.
struct RateLimiter {
    /// Unix seconds of the last successful claim; `0` means "never", which is deliberately always
    /// claimable (a fresh process should be able to ask once immediately).
    last_sent: AtomicU64,
}

impl RateLimiter {
    fn claim(&self, now: u64) -> bool {
        let last = self.last_sent.load(Ordering::Relaxed);
        if now.saturating_sub(last) < MIN_INTERVAL_SECS {
            return false;
        }
        // CAS, not a plain store: two near-simultaneous callers (the post-feed watcher thread and
        // a startup check racing it) must not both win.
        self.last_sent.compare_exchange(last, now, Ordering::Relaxed, Ordering::Relaxed).is_ok()
    }
}

pub struct BowlFillRefresh {
    ble: Sender,
    limiter: RateLimiter,
}

impl BowlFillRefresh {
    pub fn new(ble: Sender) -> Self {
        Self { ble, limiter: RateLimiter { last_sent: AtomicU64::new(0) } }
    }

    /// Sends the request if a feed isn't in flight and the last send was over a minute ago.
    /// Returns whether it actually sent -- both call sites (post-feed, startup) just want "ask if
    /// it's a good time" and don't need to branch on the outcome themselves.
    pub fn request_if_due(&self, shm: &Shm) -> bool {
        if shm.u8(off::FEEDING) != 0 {
            return false;
        }
        if !self.limiter.claim(now_unix()) {
            return false;
        }
        match self.ble.send(msg::SUBCHIP_REQ_DATA, &[FOOD_SURPLUS_CMD]) {
            Ok(()) => true,
            Err(e) => {
                eprintln!("kibbled: bowl_fill: request failed: {e}");
                false
            }
        }
    }
}

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_claim_always_succeeds() {
        let l = RateLimiter { last_sent: AtomicU64::new(0) };
        assert!(l.claim(1_000));
    }

    #[test]
    fn claim_within_a_minute_is_refused() {
        let l = RateLimiter { last_sent: AtomicU64::new(1_000) };
        assert!(!l.claim(1_030));
        assert!(!l.claim(1_059));
    }

    #[test]
    fn claim_at_and_past_the_minute_succeeds() {
        let l = RateLimiter { last_sent: AtomicU64::new(1_000) };
        assert!(l.claim(1_060));
        // The successful claim above moved last_sent to 1_060; immediately after, even a much
        // later timestamp within *that* new window is refused.
        assert!(!l.claim(1_090));
    }

    #[test]
    fn a_refused_claim_does_not_move_the_deadline() {
        let l = RateLimiter { last_sent: AtomicU64::new(1_000) };
        assert!(!l.claim(1_010));
        // Still gated from the original 1_000, not reset by the refused attempt.
        assert!(l.claim(1_060));
    }
}
