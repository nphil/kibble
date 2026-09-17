//! mqtrace — a minimal ptrace(2) syscall tracer, built for one purpose: observe what the
//! vendor's own processes send on the internal mqueue bus (`docs/05-bus.md`) and write to the
//! T31 dispenser MCU's UART (`docs/08-mcu.md`), during a real, cloud-triggered event, when
//! static disassembly has been exhausted (`docs/34-bowl-fill-surplus.md`). There is no `strace`
//! on this device (busybox only) — this is the smallest tool that fills that gap.
//!
//! ## Scope, deliberately narrow
//!
//! Attaches to one PID and every one of its threads, then runs a `PTRACE_SYSCALL` loop with
//! `PTRACE_O_TRACESYSGOOD` so entry/exit stops are unambiguous. On syscall EXIT only, for
//! exactly four ARM EABI syscall numbers (`read`=3, `write`=4, `mq_timedsend`=276,
//! `mq_timedreceive`=277 — the numbers `mq_send`/`mq_receive` actually compile down to on both
//! musl and glibc), it decodes and logs; every other syscall, and every other stop, is passed
//! straight through via another `PTRACE_SYSCALL` with no inspection. `read`/`write` are logged
//! only when the fd resolves (via `/proc/<pid>/fd/<n>`, checked fresh on every event — no
//! caching, so an fd reused for something else is never misattributed) to `--uart-path`
//! (default `/dev/ttyS3`) — unless `--all-io` is given, in which case every fd's read/write is
//! logged (tagged `IO_READ`/`IO_WRITE`, with the resolved path, instead of `UART_READ`/
//! `UART_WRITE`), for tracing a process (`media`, `cloud`) whose relevant I/O isn't the UART --
//! `docs/34-bowl-fill-surplus.md` Part 7. `mq_timedsend`/`mq_timedreceive` are always logged,
//! with the queue name resolved the same way and the envelope decoded per `docs/05-bus.md`'s
//! corrected format (`u16 msg_id | u16 src` + payload, `agent/src/bus.rs`).
//!
//! Deliberately does NOT set `PTRACE_O_TRACEFORK`/`PTRACE_O_TRACEVFORK`: `ctrl` periodically
//! `system()`s a shell (e.g. `reset_wifi.sh`) while traced, and a forked child must run
//! completely untraced rather than risk ending up stopped and orphaned. `PTRACE_O_TRACECLONE`
//! IS set, so a real pthread spawned by the traced process after attach is picked up too (a
//! thread shares the traced process's fds/memory, so this only affects thread coverage, not
//! forked-process safety).
//!
//! ## Safety
//!
//! - `--timeout <secs>` (default 240) is enforced by a single `SIGALRM`; that, `SIGINT`, and
//!   `SIGTERM` all set one flag checked by the main loop, never do work inside the handler.
//! - Once that flag is set, EVERY tracked thread is sent `PTRACE_INTERRUPT` (safe to call on a
//!   running, sleeping, or already-stopped SEIZEd tracee) and the very next stop reported for
//!   each one is answered with `PTRACE_DETACH`, never another `PTRACE_SYSCALL` — the loop does
//!   not exit until every tracked tid has been explicitly detached or has exited on its own, so
//!   a tracee is never left stopped for this tool to be killed or crash out from under.
//! - A genuine signal delivered to a tracee (anything that stops it with a signal other than
//!   the two `SIGTRAP` shapes ptrace itself uses) is always forwarded on resume/detach, never
//!   swallowed.
//! - No `PTRACE_O_EXITKILL`: if this tracer itself dies uncleanly, the kernel's default is to
//!   leave tracees running (merely untraced), never to kill them.
//! - Reads tracee memory via `/proc/<pid>/mem` (`pread`, offset = the tracee's own virtual
//!   address zero-extended into musl's 64-bit `off_t` — no 32-bit sign-extension hazard).
//!   Never writes tracee memory or registers.

use std::collections::HashMap;
use std::ffi::CString;
use std::fmt::Write as _;
use std::fs::{self, OpenOptions};
use std::io::{self, Write as _};
use std::os::raw::{c_int, c_long, c_void};
use std::os::unix::ffi::OsStrExt;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

type Pid = i32;

// ---- ptrace request numbers (arch-independent; see <linux/ptrace.h>) ----
const PTRACE_SYSCALL: c_long = 24;
const PTRACE_DETACH: c_long = 17;
const PTRACE_GETREGS: c_long = 12; // ARM-specific value, matches arch/arm/include/uapi/asm/ptrace.h
const PTRACE_GETEVENTMSG: c_long = 0x4201;
const PTRACE_SEIZE: c_long = 0x4206;
const PTRACE_INTERRUPT: c_long = 0x4207;

const PTRACE_O_TRACESYSGOOD: c_long = 0x1;
const PTRACE_O_TRACECLONE: c_long = 0x8;

const PTRACE_EVENT_CLONE: i32 = 3;

const SIGTRAP: c_int = 5;

// ---- ARM EABI syscall numbers (musl and glibc agree; mq_send/mq_receive are library wrappers
// around mq_timedsend/mq_timedreceive with a NULL timeout on both libcs, so tracing the two
// _timed_ variants covers every mq_send/mq_receive call in this codebase). ----
const SYS_READ: u32 = 3;
const SYS_WRITE: u32 = 4;
const SYS_MQ_TIMEDSEND: u32 = 276;
const SYS_MQ_TIMEDRECEIVE: u32 = 277;

const MAX_MSG: usize = 600; // bus envelope (4) + max payload (540, agent/src/bus.rs::MAX_PAYLOAD) + slack
const MAX_IO: usize = 2048; // UART frames cap at ~517 bytes (docs/08-mcu.md §3.2); generous headroom

/// ARM `struct pt_regs` (`arch/arm/include/uapi/asm/ptrace.h`): 18 32-bit words. Index 7 is the
/// syscall number (r7, untouched by the kernel across a syscall); index 17 is `ORIG_r0`, the
/// kernel's preserved copy of the original r0 argument (r0 itself is overwritten with the
/// return value by the time we see the exit stop).
#[repr(C)]
#[derive(Clone, Copy, Default)]
struct PtRegs {
    uregs: [u32; 18],
}

impl PtRegs {
    fn syscall_nr(&self) -> u32 {
        self.uregs[7]
    }
    fn orig_r0(&self) -> u32 {
        self.uregs[17]
    }
    fn r0(&self) -> i32 {
        self.uregs[0] as i32
    }
    fn r1(&self) -> u32 {
        self.uregs[1]
    }
    fn r2(&self) -> u32 {
        self.uregs[2]
    }
}

extern "C" {
    // Declared with its real, always-4-argument call shape (every call site below passes
    // exactly 4 args) rather than libc's variadic prototype -- identical ABI on ARM either way.
    fn ptrace(request: c_long, pid: Pid, addr: *mut c_void, data: *mut c_void) -> c_long;
}

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_sig: c_int) {
    STOP.store(true, Ordering::SeqCst);
}

fn install_signal_handlers() {
    unsafe {
        let mut sa: libc::sigaction = std::mem::zeroed();
        sa.sa_sigaction = on_signal as *const () as usize;
        sa.sa_flags = 0; // no SA_RESTART: a blocked waitpid must return EINTR promptly
        libc::sigemptyset(&mut sa.sa_mask);
        libc::sigaction(libc::SIGINT, &sa, std::ptr::null_mut());
        libc::sigaction(libc::SIGTERM, &sa, std::ptr::null_mut());
        libc::sigaction(libc::SIGALRM, &sa, std::ptr::null_mut());
    }
}

fn mono_now() -> f64 {
    let mut ts = libc::timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 / 1e9
}

fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(s, "{b:02x}");
    }
    s
}

fn list_tids(pid: Pid) -> Vec<Pid> {
    let dir = format!("/proc/{pid}/task");
    match fs::read_dir(&dir) {
        Ok(rd) => rd
            .filter_map(|e| e.ok())
            .filter_map(|e| e.file_name().to_str().and_then(|s| s.parse::<Pid>().ok()))
            .collect(),
        Err(_) => Vec::new(),
    }
}

/// One open `/proc/<pid>/mem` fd, shared by every thread of the traced process (they share one
/// address space). `pread` at the tracee's own virtual address, zero-extended -- musl's `off_t`
/// is always 64-bit, so there is no 32-bit sign-extension hazard reading a high userspace
/// address (e.g. a stack pointer around `0xbe...`) the way there would be on a 32-bit `off_t`.
struct MemReader {
    fd: c_int,
}

impl MemReader {
    fn open(pid: Pid) -> io::Result<Self> {
        let path = CString::new(format!("/proc/{pid}/mem")).unwrap();
        let fd = unsafe { libc::open(path.as_ptr(), libc::O_RDONLY) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self { fd })
    }

    fn read_at(&self, addr: u32, len: usize) -> io::Result<Vec<u8>> {
        let mut buf = vec![0u8; len];
        let n = unsafe {
            libc::pread(self.fd, buf.as_mut_ptr() as *mut c_void, len, addr as u64 as i64)
        };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        buf.truncate(n as usize);
        Ok(buf)
    }
}

impl Drop for MemReader {
    fn drop(&mut self) {
        unsafe { libc::close(self.fd) };
    }
}

fn fd_path(pid: Pid, fd: i32) -> Option<Vec<u8>> {
    fs::read_link(format!("/proc/{pid}/fd/{fd}"))
        .ok()
        .map(|p| p.as_os_str().as_bytes().to_vec())
}

struct Tracer {
    log: fs::File,
    mem: MemReader,
    pid: Pid,
    uart_path: Vec<u8>,
    /// `--all-io`: log every fd's read/write, not just `uart_path`'s.
    all_io: bool,
    /// tid -> "currently inside a syscall" (i.e. the next stop for this tid is an EXIT stop).
    /// Absence means "seen no stop for this tid yet".
    tracked: HashMap<Pid, bool>,
    shutting_down: bool,
}

impl Tracer {
    fn log_line(&mut self, s: &str) {
        let line = format!("{:.6} {s}\n", mono_now());
        let _ = self.log.write_all(line.as_bytes());
        let _ = self.log.flush();
    }

    fn begin_shutdown(&mut self) {
        if self.shutting_down {
            return;
        }
        self.shutting_down = true;
        self.log_line("SHUTDOWN begin");
        for tid in self.tracked.keys() {
            unsafe { ptrace(PTRACE_INTERRUPT, *tid, std::ptr::null_mut(), std::ptr::null_mut()) };
        }
    }

    fn handle_syscall_exit(&mut self, tid: Pid) {
        let mut regs = PtRegs::default();
        let rc = unsafe {
            ptrace(PTRACE_GETREGS, tid, std::ptr::null_mut(), &mut regs as *mut PtRegs as *mut c_void)
        };
        if rc != 0 {
            self.log_line(&format!("ERR tid={tid} GETREGS failed: {}", io::Error::last_os_error()));
            return;
        }
        let ret = regs.r0();
        match regs.syscall_nr() {
            SYS_MQ_TIMEDSEND if ret == 0 => {
                let mqd = regs.orig_r0() as i32;
                let len = (regs.r2() as usize).min(MAX_MSG);
                self.log_mq(tid, "MQSEND", mqd, regs.r1(), len);
            }
            SYS_MQ_TIMEDRECEIVE if ret >= 0 => {
                let mqd = regs.orig_r0() as i32;
                let len = (ret as usize).min(MAX_MSG);
                self.log_mq(tid, "MQRECV", mqd, regs.r1(), len);
            }
            SYS_WRITE if ret > 0 => {
                let fd = regs.orig_r0() as i32;
                let len = (ret as usize).min(MAX_IO);
                self.log_io(tid, "WRITE", fd, regs.r1(), len);
            }
            SYS_READ if ret > 0 => {
                let fd = regs.orig_r0() as i32;
                let len = (ret as usize).min(MAX_IO);
                self.log_io(tid, "READ", fd, regs.r1(), len);
            }
            _ => {}
        }
    }

    fn log_mq(&mut self, tid: Pid, tag: &str, mqd: i32, ptr: u32, len: usize) {
        let qname = fd_path(self.pid, mqd)
            .map(|b| String::from_utf8_lossy(&b).into_owned())
            .unwrap_or_else(|| "?".into());
        let bytes = match self.mem.read_at(ptr, len) {
            Ok(b) => b,
            Err(e) => {
                self.log_line(&format!("ERR tid={tid} {tag} mem read failed: {e}"));
                return;
            }
        };
        let (msg_id, src, payload) = if bytes.len() >= 4 {
            (u16::from_le_bytes([bytes[0], bytes[1]]), u16::from_le_bytes([bytes[2], bytes[3]]), &bytes[4..])
        } else {
            (0u16, 0u16, &bytes[..])
        };
        self.log_line(&format!(
            "{tag} tid={tid} mqd={mqd} q={qname} len={len} msg_id=0x{msg_id:04x} src={src} payload={}",
            hex(payload)
        ));
    }

    /// Logs a `read`/`write` syscall's payload. Always for `uart_path` (tag `UART_<verb>`,
    /// matching every prior capture's format byte for byte); for every other fd only when
    /// `--all-io` was given (tag `IO_<verb>`) -- `media`/`cloud` have no UART fd at all, so
    /// tracing either one's non-mqueue I/O (a file, a socket, `cloud`'s TLS write) needs this,
    /// see `docs/34-bowl-fill-surplus.md` Part 7.
    fn log_io(&mut self, tid: Pid, verb: &str, fd: i32, ptr: u32, len: usize) {
        let path = match fd_path(self.pid, fd) {
            Some(p) => p,
            None => return,
        };
        let is_uart = path == self.uart_path;
        if !is_uart && !self.all_io {
            return;
        }
        let tag = if is_uart { format!("UART_{verb}") } else { format!("IO_{verb}") };
        let bytes = match self.mem.read_at(ptr, len) {
            Ok(b) => b,
            Err(e) => {
                self.log_line(&format!("ERR tid={tid} {tag} mem read failed: {e}"));
                return;
            }
        };
        self.log_line(&format!(
            "{tag} tid={tid} fd={fd} path={} len={len} bytes={}",
            String::from_utf8_lossy(&path),
            hex(&bytes)
        ));
    }

    /// Runs until every tracked tid has exited or been detached (see the module doc's Safety
    /// section for why this loop, not a bounded one, is what "never leave a tracee stopped"
    /// requires).
    fn event_loop(&mut self) {
        loop {
            if self.tracked.is_empty() {
                break;
            }
            if STOP.load(Ordering::SeqCst) {
                self.begin_shutdown();
            }
            let mut status: c_int = 0;
            let r = unsafe { libc::waitpid(-1, &mut status, libc::__WALL) };
            if r < 0 {
                let err = io::Error::last_os_error();
                if err.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                if err.raw_os_error() == Some(libc::ECHILD) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
                continue;
            }
            let tid = r;
            if libc::WIFEXITED(status) || libc::WIFSIGNALED(status) {
                self.tracked.remove(&tid);
                self.log_line(&format!("EXIT tid={tid}"));
                continue;
            }
            if !libc::WIFSTOPPED(status) {
                continue;
            }
            let stopsig = libc::WSTOPSIG(status);
            let is_trap_stop = stopsig == SIGTRAP || stopsig == (SIGTRAP | 0x80);

            if self.shutting_down {
                let redeliver = if is_trap_stop { 0 } else { stopsig };
                unsafe {
                    ptrace(PTRACE_DETACH, tid, std::ptr::null_mut(), redeliver as isize as *mut c_void)
                };
                self.tracked.remove(&tid);
                self.log_line(&format!("DETACH tid={tid}"));
                continue;
            }

            let first_time = !self.tracked.contains_key(&tid);
            if first_time {
                self.tracked.insert(tid, false);
                self.log_line(&format!("THREAD new_tid={tid}"));
                unsafe { ptrace(PTRACE_SYSCALL, tid, std::ptr::null_mut(), std::ptr::null_mut()) };
                continue;
            }

            if stopsig == (SIGTRAP | 0x80) {
                let was_in_syscall = *self.tracked.get(&tid).unwrap();
                self.tracked.insert(tid, !was_in_syscall);
                if was_in_syscall {
                    self.handle_syscall_exit(tid);
                }
                unsafe { ptrace(PTRACE_SYSCALL, tid, std::ptr::null_mut(), std::ptr::null_mut()) };
                continue;
            }

            if stopsig == SIGTRAP {
                let event = status >> 16;
                if event == PTRACE_EVENT_CLONE {
                    let mut newpid: u32 = 0;
                    unsafe {
                        ptrace(
                            PTRACE_GETEVENTMSG,
                            tid,
                            std::ptr::null_mut(),
                            &mut newpid as *mut u32 as *mut c_void,
                        )
                    };
                    if newpid != 0 {
                        self.log_line(&format!("THREAD parent_tid={tid} new_tid={newpid}"));
                    }
                }
                unsafe { ptrace(PTRACE_SYSCALL, tid, std::ptr::null_mut(), std::ptr::null_mut()) };
                continue;
            }

            // A genuine signal stopped this tracee (not one of ptrace's own SIGTRAP shapes).
            // Forward it on resume -- never swallow a real signal the target relies on.
            self.log_line(&format!("SIGNAL tid={tid} sig={stopsig}"));
            unsafe { ptrace(PTRACE_SYSCALL, tid, std::ptr::null_mut(), stopsig as isize as *mut c_void) };
        }
    }
}

fn open_log(path: &str) -> fs::File {
    OpenOptions::new().create(true).append(true).open(path).unwrap_or_else(|e| {
        eprintln!("mqtrace: open log {path}: {e}");
        std::process::exit(1);
    })
}

fn run(pid: Pid, uart_path: &str, log_path: &str, all_io: bool) {
    let log = open_log(log_path);
    let mem = MemReader::open(pid).unwrap_or_else(|e| {
        eprintln!("mqtrace: open /proc/{pid}/mem: {e} (is the pid correct, and are you root?)");
        std::process::exit(1);
    });
    let mut t = Tracer {
        log,
        mem,
        pid,
        uart_path: uart_path.as_bytes().to_vec(),
        all_io,
        tracked: HashMap::new(),
        shutting_down: false,
    };

    let mut seized: Vec<Pid> = Vec::new();
    let opts = (PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACECLONE) as isize as *mut c_void;
    // Two passes: catch every thread present at the first listing, then re-list once to catch
    // any thread that appeared while we were still attaching to the first batch.
    for _pass in 0..2 {
        for tid in list_tids(pid) {
            if seized.contains(&tid) {
                continue;
            }
            let rc = unsafe { ptrace(PTRACE_SEIZE, tid, std::ptr::null_mut(), opts) };
            if rc == 0 {
                seized.push(tid);
            } else if _pass == 0 {
                t.log_line(&format!("ERR seize tid={tid} failed: {}", io::Error::last_os_error()));
            }
        }
    }
    if seized.is_empty() {
        eprintln!("mqtrace: failed to seize any thread of pid {pid} (see log for details)");
        std::process::exit(1);
    }
    t.log_line(&format!(
        "ATTACH pid={pid} tids={:?} uart_path={uart_path} all_io={all_io} log={log_path}",
        seized
    ));
    eprintln!("mqtrace: attached to pid {pid}, {} thread(s): {:?}", seized.len(), seized);
    for tid in &seized {
        t.tracked.insert(*tid, false);
        unsafe { ptrace(PTRACE_INTERRUPT, *tid, std::ptr::null_mut(), std::ptr::null_mut()) };
    }

    t.event_loop();
    t.log_line(&format!("DONE pid={pid}"));
    eprintln!("mqtrace: detached from every thread of pid {pid}, exiting");
}

fn usage() -> ! {
    eprintln!(
        "usage: mqtrace --pid <pid> [--timeout <secs>=240] [--uart-path <path>=/dev/ttyS3] [--log <path>=/tmp/mqtrace.log] [--all-io]"
    );
    std::process::exit(2);
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mut pid: Option<Pid> = None;
    let mut timeout_secs: u32 = 240;
    let mut uart_path = "/dev/ttyS3".to_string();
    let mut log_path = "/tmp/mqtrace.log".to_string();
    let mut all_io = false;
    let mut i = 1;
    while i < args.len() {
        let val = |i: usize| args.get(i).unwrap_or_else(|| usage());
        match args[i].as_str() {
            "--pid" => {
                pid = Some(val(i + 1).parse().unwrap_or_else(|_| usage()));
                i += 1;
            }
            "--timeout" => {
                timeout_secs = val(i + 1).parse().unwrap_or_else(|_| usage());
                i += 1;
            }
            "--uart-path" => {
                uart_path = val(i + 1).clone();
                i += 1;
            }
            "--log" => {
                log_path = val(i + 1).clone();
                i += 1;
            }
            "--all-io" => {
                all_io = true;
            }
            _ => usage(),
        }
        i += 1;
    }
    let pid = pid.unwrap_or_else(|| usage());

    install_signal_handlers();
    unsafe { libc::alarm(timeout_secs) };

    run(pid, &uart_path, &log_path, all_io);
}
