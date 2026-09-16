//! Read-only, time-bounded comparative sampler for a live Petkit talkback session
//! (docs/23-audio-codec.md §17-§20). Same audit pattern as this project's other throwaway
//! diagnostic tools (slot7mon / rt5 / ringtool2 / locktest): plain `File::open` (no
//! `O_RDWR` anywhere in this file), `PROT_READ`-only `mmap`, zero writes to the ring,
//! zero contact with any vendor process beyond reading its `/proc` entries. Not part of
//! `kibbled`.
//!
//! Purpose: answer, from one real app press-and-hold, (a) whether the vendor's own
//! talkback goes through `speak_start`/`audio_out_thread` at all (guard flag `0x767f0`
//! and `media`'s thread count, sampled together at 20 Hz), (b) who writes the ring's
//! chan=2 records and what advances the global sequence counter while it happens, and
//! (c) how the `auido-out` registry slot's bookmark/cursor move against `SndFrm`. Logs to
//! a file this process opens and flushes itself -- not stdout, which this device's
//! supervisor redirects to /dev/null.
//!
//! Output lines:
//!   ROW    fixed cadence (default 40 ms; argv[4]): uptime, media thread count, guard flag, slot-0 registry
//!          words (mutex, global_seq, second, running_total, write_cursor), slot-7
//!          bookmark (+0x14) and cursor (+0x18), records/chan2 records seen so far
//!   SLOT7  every change of any of slot 7's 11 leading u32 words (name/idx/mask/bookmark/
//!          cursor/... -- a pid field, if one exists, shows up here)
//!   REC    every new chan=2 ring record's header as the write cursor passes it
//!   PROC_* `/proc/ax_proc/{ao,adec,aenc}` every 500 ms
//!   EVENT  thread-count or guard-flag transitions, on the 5 ms tick they were seen

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::raw::{c_int, c_void};
use std::os::unix::fs::FileExt;
use std::os::unix::io::AsRawFd;
use std::time::{Duration, Instant};

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

/// Device uptime seconds from `/proc/uptime` -- the clock every prior trace in docs/23 uses,
/// so this log lines up with them and with the kernel ring buffer.
fn uptime() -> String {
    std::fs::read_to_string("/proc/uptime")
        .ok()
        .and_then(|s| s.split_whitespace().next().map(str::to_string))
        .unwrap_or_else(|| "?".into())
}

/// `audio_out_thread`'s guard flag inside the (non-PIE) `media` executable
/// (docs/23-audio-codec.md §17.1/§17.4/§19.1), read straight out of `/proc/<pid>/mem`.
const MEDIA_GUARD_FLAG_ADDR: u64 = 0x767f0;

fn vendor_pid(name: &str) -> Option<u32> {
    std::fs::read_dir("/proc").ok()?.flatten().find_map(|entry| {
        let pid: u32 = entry.file_name().to_str()?.parse().ok()?;
        let comm = std::fs::read_to_string(entry.path().join("comm")).ok()?;
        (comm.trim_end() == name).then_some(pid)
    })
}

fn thread_count(pid: u32) -> usize {
    std::fs::read_dir(format!("/proc/{pid}/task")).map(|d| d.count()).unwrap_or(0)
}

fn guard_flag(mem: &File) -> i64 {
    let mut word = [0u8; 4];
    match mem.read_exact_at(&mut word, MEDIA_GUARD_FLAG_ADDR) {
        Ok(()) => i64::from(u32::from_le_bytes(word)),
        Err(_) => -1,
    }
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

/// Only the `AO DEV STATUS` / `ADEC CHN STATUS` data rows and the aenc line count -- the rest
/// of those files is static attribute tables that would multiply the log by ~20x for nothing.
fn proc_status(path: &str, key: &str) -> String {
    let Ok(text) = std::fs::read_to_string(path) else { return "<unreadable>".into() };
    let mut lines = text.lines();
    while let Some(line) = lines.next() {
        if line.split_whitespace().any(|c| c == key) {
            return lines.next().unwrap_or("").split_whitespace().collect::<Vec<_>>().join(" ");
        }
    }
    format!("<no {key}; {} lines>", text.lines().count())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let duration_secs: u64 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(300);
    let out_path = args.get(2).cloned().unwrap_or_else(|| "/tmp/ringmon.log".to_string());
    // Poll tick and ROW cadence, ms. Defaults give ~20 Hz rows; a lighter run (e.g. `25 250`)
    // is the control for "did the sampler's own load change the talkback".
    let tick = Duration::from_millis(args.get(3).and_then(|s| s.parse().ok()).unwrap_or(5));
    let row_every = Duration::from_millis(args.get(4).and_then(|s| s.parse().ok()).unwrap_or(40));

    let f = File::open(RING_PATH).expect("open ring read-only");
    let len = f.metadata().expect("stat ring").len() as usize;
    assert!(len >= RING_LEN, "ring file smaller than expected: {len}");
    let p = unsafe { mmap(std::ptr::null_mut(), RING_LEN, PROT_READ, MAP_SHARED, f.as_raw_fd(), 0) };
    assert!(p as isize != -1, "mmap failed");
    let ring = Ring { base: p as *const u8 };

    let media_pid = vendor_pid("media").expect("no `media` process");
    let media_mem = File::open(format!("/proc/{media_pid}/mem")).expect("open media mem read-only");

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

    logln!(
        "# ringmon armed pid={} media_pid={} uptime={} duration_s={} tick_ms={} row_ms={} out={}",
        std::process::id(), media_pid, uptime(), duration_secs, tick.as_millis(), row_every.as_millis(), out_path
    );

    let mut last_s7: Vec<u32> = (0..11).map(|i| ring.u32(SLOT7_BASE + i * 4)).collect();
    logln!(
        "SLOT7_BASELINE t={} raw={}",
        uptime(),
        last_s7.iter().map(|v| format!("{v:#010x}")).collect::<Vec<_>>().join(",")
    );

    let start = Instant::now();
    let mut last_row = Instant::now() - Duration::from_secs(1);
    let mut last_proc = Instant::now() - Duration::from_secs(1);
    let mut scan_cursor = ring.u32(OFF_WRITE_CURSOR);
    let mut record_count: u64 = 0;
    let mut chan2_count: u64 = 0;
    let mut chan_tally = [0u64; 32];
    let mut last_threads = thread_count(media_pid);
    let mut last_guard = guard_flag(&media_mem);

    while start.elapsed() < Duration::from_secs(duration_secs) {
        let threads = thread_count(media_pid);
        let guard = guard_flag(&media_mem);
        if threads != last_threads || guard != last_guard {
            logln!(
                "EVENT t={} threads={}->{} guard={}->{} seq={} cursor={:#x}",
                uptime(), last_threads, threads, last_guard, guard,
                ring.u32(OFF_GLOBAL_SEQ), ring.u32(OFF_WRITE_CURSOR)
            );
            last_threads = threads;
            last_guard = guard;
        }

        let s7: Vec<u32> = (0..11).map(|i| ring.u32(SLOT7_BASE + i * 4)).collect();
        if s7 != last_s7 {
            logln!(
                "SLOT7 t={} raw={}",
                uptime(),
                s7.iter().map(|v| format!("{v:#010x}")).collect::<Vec<_>>().join(",")
            );
            last_s7 = s7;
        }

        let cursor = ring.u32(OFF_WRITE_CURSOR);
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
                                uptime(), pos, h.seq, h.chan, h.chan_seq, h.length, h.frame_type, h.pts_us
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

        if last_row.elapsed() >= row_every {
            logln!(
                "ROW t={} threads={} guard={} mutex={} seq={} second={} running={} cursor={:#x} s7_bookmark={} s7_cursor={:#x} records={} chan2={} tally={}",
                uptime(), threads, guard,
                ring.u32(OFF_MUTEX), ring.u32(OFF_GLOBAL_SEQ), ring.u32(OFF_SECOND),
                ring.u32(OFF_RUNNING_TOTAL), cursor,
                ring.u32(SLOT7_BASE + 0x14), ring.u32(SLOT7_BASE + 0x18),
                record_count, chan2_count,
                chan_tally.iter().enumerate().filter(|(_, c)| **c > 0)
                    .map(|(ch, c)| format!("{ch}:{c}")).collect::<Vec<_>>().join(",")
            );
            last_row = Instant::now();
        }

        if last_proc.elapsed() >= Duration::from_millis(500) {
            logln!(
                "PROC t={} ao[{}] adec[{}] aenc_lines={}",
                uptime(),
                proc_status("/proc/ax_proc/ao", "SndFrm"),
                proc_status("/proc/ax_proc/adec", "SndStrm"),
                std::fs::read_to_string("/proc/ax_proc/aenc").map(|t| t.lines().count()).unwrap_or(0)
            );
            last_proc = Instant::now();
        }

        std::thread::sleep(tick);
    }

    logln!("# ringmon done t={}", uptime());
}
