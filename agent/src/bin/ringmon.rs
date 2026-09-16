//! Read-only, time-bounded comparative sampler for a live Petkit talkback session
//! (docs/23-audio-codec.md §17). Same audit pattern as this project's other throwaway
//! diagnostic tools (slot7mon / rt5 / ringtool2 / locktest): plain `File::open` (no
//! `O_RDWR` anywhere in this file), `PROT_READ`-only `mmap`, zero writes to the ring,
//! zero contact with any vendor process. Deleted after use; not part of `kibbled`.
//!
//! Purpose: capture the *exact same* coordination fields a real, externally-triggered
//! app talkback touches, at high rate, so it can be diffed byte-for-byte against our
//! own `publish()` (agent/src/audioout.rs) to find what `agora`'s writer does that we
//! do not. Logs to a file this process opens and flushes itself -- not stdout, which
//! this device's supervisor has been observed to redirect to /dev/null.
//!
//! Fields sampled (docs/23-audio-codec.md §17.2/§17.10.2, byte offsets relative to the
//! mmap base, i.e. `ring_base`):
//!   registry "slot 0":  mutex@0x00, global_seq@0x18, second_counter@0x1c,
//!                        running_total@0x20, write_cursor@0x28
//!   registry "slot 7" ("auido-out", ring_base + 7*44 = 0x134): all 11 leading u32
//!                        words (covers the documented name/idx/mask/bookmark/cursor
//!                        fields with margin for anything not yet named)
//! Plus every new ring record's header (56 bytes: seq, length, chan_seq, pts_us,
//! frame_type, chan) as the write cursor advances past it, and periodic
//! `/proc/ax_proc/{ao,adec,aenc}` snapshots.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::raw::{c_int, c_void};
use std::os::unix::io::AsRawFd;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const RING_PATH: &str = "/dev/shm/media_buffer_frame_buf";
const RING_LEN: usize = 8_389_608;
/// Vendor's own byte-exact data-region origin (docs §17.8/§17.12.1: `0x3e8` = 1000,
/// reconciled against `RING_LEN` = `0x800000 + 0x3e8` exactly). Deliberately not
/// `ring::DATA_START` (1024) -- that constant is a scan-start seed for a resyncing
/// walker, not the vendor's own cursor-relative address origin.
const DATA_ORIGIN: usize = 0x3e8;
const WRAP_MOD: u32 = 0x0080_0000;
const HDR: usize = 56;

const OFF_MUTEX: usize = 0x00;
const OFF_GLOBAL_SEQ: usize = 0x18;
const OFF_SECOND: usize = 0x1c;
const OFF_RUNNING_TOTAL: usize = 0x20;
const OFF_WRITE_CURSOR: usize = 0x28;
const SLOT7_BASE: usize = 7 * 44; // 0x134

extern "C" {
    fn mmap(addr: *mut c_void, len: usize, prot: c_int, flags: c_int, fd: c_int, offset: i64) -> *mut c_void;
}
const PROT_READ: c_int = 1;
const MAP_SHARED: c_int = 1;

fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis()
}

/// Read-only view of the ring mapping. Every accessor is a plain volatile load --
/// no accessor in this struct, and no other code in this file, ever writes through
/// `base`.
struct Ring {
    base: *const u8,
}
unsafe impl Send for Ring {}
unsafe impl Sync for Ring {}
impl Ring {
    fn u32(&self, off: usize) -> u32 {
        unsafe { std::ptr::read_volatile(self.base.add(off) as *const u32) }
    }
    fn u8(&self, off: usize) -> u8 {
        unsafe { std::ptr::read_volatile(self.base.add(off)) }
    }
}

struct Header {
    seq: u32,
    length: u32,
    chan_seq: u32,
    pts_us: u32,
    frame_type: u8,
    chan: u8,
}

/// Mirrors `ring::parse_header`'s validation exactly (sane length, known channel,
/// whole record in-bounds) so a torn/garbled read at a wrap seam is rejected the same
/// way the project's real reader already tolerates it, rather than logged as data.
fn parse_header(ring: &Ring, off: usize) -> Option<Header> {
    if off + HDR > RING_LEN {
        return None;
    }
    let seq = ring.u32(off);
    let length = ring.u32(off + 4);
    let chan_seq = ring.u32(off + 8);
    let pts_us = ring.u32(off + 16);
    let frame_type = ring.u8(off + 32);
    let chan = ring.u8(off + 34);
    if !(1..=2_000_000).contains(&length) {
        return None;
    }
    if !matches!(chan, 1 | 2 | 4 | 8 | 16) {
        return None;
    }
    if off + HDR + length as usize > RING_LEN {
        return None;
    }
    Some(Header { seq, length, chan_seq, pts_us, frame_type, chan })
}

fn read_proc(path: &str) -> String {
    std::fs::read_to_string(path).unwrap_or_else(|e| format!("<read error: {e}>")).replace('\n', " | ")
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let duration_secs: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(300);
    let out_path = args.get(2).cloned().unwrap_or_else(|| "/tmp/ringmon.log".to_string());

    let f = File::open(RING_PATH).expect("open ring read-only");
    let len = f.metadata().expect("stat ring").len() as usize;
    assert!(len >= RING_LEN, "ring file smaller than expected: {len}");
    let p = unsafe { mmap(std::ptr::null_mut(), RING_LEN, PROT_READ, MAP_SHARED, f.as_raw_fd(), 0) };
    assert!(p as isize != -1, "mmap failed");
    let ring = Ring { base: p as *const u8 };

    let mut out = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&out_path)
        .expect("open log for append");

    macro_rules! logln {
        ($($arg:tt)*) => {{
            let _ = writeln!(out, $($arg)*);
            let _ = out.flush();
        }};
    }

    logln!("# ringmon armed pid={} at_ms={} duration_s={} out={}", std::process::id(), now_ms(), duration_secs, out_path);

    let mut last_mutex = ring.u32(OFF_MUTEX);
    let mut last_seq = ring.u32(OFF_GLOBAL_SEQ);
    let mut last_second = ring.u32(OFF_SECOND);
    let mut last_running = ring.u32(OFF_RUNNING_TOTAL);
    let mut last_cursor = ring.u32(OFF_WRITE_CURSOR);
    logln!(
        "BASELINE t={} mutex={} seq={} second={} running={} cursor={:#x}",
        now_ms(), last_mutex, last_seq, last_second, last_running, last_cursor
    );

    let mut last_s7: Vec<u32> = (0..11).map(|i| ring.u32(SLOT7_BASE + i * 4)).collect();
    logln!(
        "SLOT7_BASELINE t={} raw={}",
        now_ms(),
        last_s7.iter().map(|v| format!("{v:#010x}")).collect::<Vec<_>>().join(",")
    );

    let start = Instant::now();
    let mut last_heartbeat = Instant::now();
    let mut last_proc = Instant::now();
    let mut scan_cursor = last_cursor;
    let mut record_count: u64 = 0;
    let mut chan2_count: u64 = 0;
    let mut chan_tally = [0u64; 32];

    while start.elapsed() < Duration::from_secs(duration_secs) {
        let mutex = ring.u32(OFF_MUTEX);
        let seq = ring.u32(OFF_GLOBAL_SEQ);
        let second = ring.u32(OFF_SECOND);
        let running = ring.u32(OFF_RUNNING_TOTAL);
        let cursor = ring.u32(OFF_WRITE_CURSOR);

        if mutex != last_mutex || seq != last_seq || second != last_second || running != last_running || cursor != last_cursor {
            logln!(
                "REG t={} mutex={} seq={} second={} running={} cursor={:#x} d_seq={} d_cursor={}",
                now_ms(), mutex, seq, second, running, cursor,
                seq as i64 - last_seq as i64,
                cursor as i64 - last_cursor as i64,
            );
            last_mutex = mutex;
            last_seq = seq;
            last_second = second;
            last_running = running;
            last_cursor = cursor;
        }

        let s7: Vec<u32> = (0..11).map(|i| ring.u32(SLOT7_BASE + i * 4)).collect();
        if s7 != last_s7 {
            logln!(
                "SLOT7 t={} raw={}",
                now_ms(),
                s7.iter().map(|v| format!("{v:#010x}")).collect::<Vec<_>>().join(",")
            );
            last_s7 = s7;
        }

        if cursor != scan_cursor {
            let mut pos = scan_cursor;
            let mut steps = 0;
            while pos != cursor && steps < 4096 {
                let data_off = DATA_ORIGIN + pos as usize;
                match parse_header(&ring, data_off) {
                    Some(h) => {
                        record_count += 1;
                        if (h.chan as usize) < 32 {
                            chan_tally[h.chan as usize] += 1;
                        }
                        if h.chan == 2 {
                            chan2_count += 1;
                            logln!(
                                "REC t={} pos={:#x} seq={} chan={} chan_seq={} len={} frame_type={} pts_us={}",
                                now_ms(), pos, h.seq, h.chan, h.chan_seq, h.length, h.frame_type, h.pts_us
                            );
                        }
                        let adv = HDR as u32 + h.length;
                        pos = pos.wrapping_add(adv) % WRAP_MOD;
                    }
                    None => break,
                }
                steps += 1;
            }
            scan_cursor = cursor;
        }

        if last_heartbeat.elapsed() >= Duration::from_secs(1) {
            logln!(
                "HEARTBEAT t={} elapsed_s={} records_seen={} chan2_seen={} tally={}",
                now_ms(), start.elapsed().as_secs(), record_count, chan2_count,
                chan_tally.iter().enumerate().filter(|(_, c)| **c > 0)
                    .map(|(ch, c)| format!("{ch}:{c}")).collect::<Vec<_>>().join(",")
            );
            last_heartbeat = Instant::now();
        }

        if last_proc.elapsed() >= Duration::from_millis(500) {
            logln!("PROC_AO t={} {}", now_ms(), read_proc("/proc/ax_proc/ao"));
            logln!("PROC_ADEC t={} {}", now_ms(), read_proc("/proc/ax_proc/adec"));
            logln!("PROC_AENC t={} {}", now_ms(), read_proc("/proc/ax_proc/aenc"));
            last_proc = Instant::now();
        }

        std::thread::sleep(Duration::from_millis(5));
    }

    logln!("# ringmon done at_ms={}", now_ms());
}
