//! Throwaway CLI: send one raw message on the feeder's internal bus (agent/src/bus.rs's
//! own documented wire format: `u16 msg_id | u16 src | payload[]`, `mq_send` to
//! `/msg_dispatch_<peer>`). Reimplements `bus::Sender` standalone (no dependency on the
//! `agent` lib target) so this can be built, deployed, and deleted independently of
//! `kibbled` itself -- this test does not touch the running kibbled binary at all.
//!
//! Usage: sendmsg <peer_id> <msg_id_hex_or_dec> <payload_string|-empty-> [linger_secs]
//! `src` is stamped as `1` (`Peer::Ctrl`), matching this project's existing
//! "we speak on ctrl's behalf" convention (main.rs's `SRC_AS_CTRL`). `payload_string` is
//! sent verbatim plus one trailing NUL byte we append ourselves; the literal argument
//! `-empty-` sends a true zero-length payload (exact byte-for-byte replica of
//! `bus::Sender::send(msg_id, &[])`, the proven-working `speak_start`/`speak_stop` shape).
//! Optional `linger_secs` keeps the process (and its mqd) alive after `mq_send` returns,
//! to test whether sender-process lifetime affects delivery.

use std::ffi::CString;
use std::os::raw::{c_char, c_int, c_uint};

extern "C" {
    fn mq_open(name: *const c_char, oflag: c_int, ...) -> c_int;
    fn mq_send(mqdes: c_int, msg_ptr: *const c_char, msg_len: usize, msg_prio: c_uint) -> c_int;
    fn mq_close(mqdes: c_int) -> c_int;
}
const O_WRONLY: c_int = 1;
const SRC_AS_CTRL: u16 = 1;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 {
        eprintln!("usage: sendmsg <peer_id> <msg_id> <payload_string|-empty-> [linger_secs]");
        std::process::exit(2);
    }
    let peer: u32 = args[1].parse().expect("peer_id must be a number");
    let msg_id: u16 = parse_num(&args[2]);
    let payload: Vec<u8> = if args[3] == "-empty-" {
        Vec::new()
    } else {
        let mut p = args[3].clone().into_bytes();
        p.push(0);
        p
    };

    if payload.len() > 0x21c {
        eprintln!("payload too large: {} > 540", payload.len());
        std::process::exit(2);
    }

    let qname = CString::new(format!("/msg_dispatch_{peer}")).unwrap();
    let mqd = unsafe { mq_open(qname.as_ptr(), O_WRONLY) };
    if mqd < 0 {
        eprintln!("mq_open({:?}) failed: {}", qname, std::io::Error::last_os_error());
        std::process::exit(1);
    }

    let mut buf = vec![0u8; 4 + payload.len()];
    buf[0..2].copy_from_slice(&msg_id.to_le_bytes());
    buf[2..4].copy_from_slice(&SRC_AS_CTRL.to_le_bytes());
    buf[4..].copy_from_slice(&payload);

    let rc = unsafe { mq_send(mqd, buf.as_ptr() as *const c_char, buf.len(), 0) };
    let err = if rc < 0 { Some(std::io::Error::last_os_error()) } else { None };

    if err.is_none() {
        println!(
            "sent msg_id={:#x} src={} peer={} payload_len={} payload={:?}",
            msg_id, SRC_AS_CTRL, peer, payload.len(), args[3]
        );
    }

    if let Some(linger) = args.get(4).and_then(|s| s.parse::<u64>().ok()) {
        eprintln!("lingering {linger}s before mq_close/exit...");
        std::thread::sleep(std::time::Duration::from_secs(linger));
    }

    unsafe { mq_close(mqd) };

    if let Some(e) = err {
        eprintln!("mq_send failed: {e}");
        std::process::exit(1);
    }
}

fn parse_num(s: &str) -> u16 {
    if let Some(hex) = s.strip_prefix("0x") {
        u16::from_str_radix(hex, 16).expect("bad hex msg_id")
    } else {
        s.parse().expect("bad msg_id")
    }
}
