//! Throwaway CLI: report a POSIX mqueue's current attributes (mq_open + mq_getattr, read-only,
//! no send/receive). Diagnostic for whether messages sent via `sendmsg` are piling up unread
//! (mq_curmsgs > 0, proving accept-but-never-consumed) or never landing at all.
//!
//! Usage: mqstat <peer_id>

use std::ffi::CString;
use std::os::raw::{c_char, c_int, c_long};

#[repr(C)]
struct MqAttr {
    mq_flags: c_long,
    mq_maxmsg: c_long,
    mq_msgsize: c_long,
    mq_curmsgs: c_long,
    // musl's struct mq_attr pads to 8 longs total; harmless if unused.
    pad: [c_long; 4],
}

extern "C" {
    fn mq_open(name: *const c_char, oflag: c_int, ...) -> c_int;
    fn mq_getattr(mqdes: c_int, attr: *mut MqAttr) -> c_int;
    fn mq_close(mqdes: c_int) -> c_int;
}
const O_RDONLY: c_int = 0;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("usage: mqstat <peer_id>");
        std::process::exit(2);
    }
    let peer: u32 = args[1].parse().expect("peer_id must be a number");
    let qname = CString::new(format!("/msg_dispatch_{peer}")).unwrap();
    let mqd = unsafe { mq_open(qname.as_ptr(), O_RDONLY) };
    if mqd < 0 {
        eprintln!("mq_open({:?}) failed: {}", qname, std::io::Error::last_os_error());
        std::process::exit(1);
    }
    let mut attr = MqAttr { mq_flags: 0, mq_maxmsg: 0, mq_msgsize: 0, mq_curmsgs: 0, pad: [0; 4] };
    let rc = unsafe { mq_getattr(mqd, &mut attr as *mut MqAttr) };
    if rc < 0 {
        eprintln!("mq_getattr failed: {}", std::io::Error::last_os_error());
        unsafe { mq_close(mqd) };
        std::process::exit(1);
    }
    println!(
        "queue={:?} mq_flags={} mq_maxmsg={} mq_msgsize={} mq_curmsgs={}",
        qname, attr.mq_flags, attr.mq_maxmsg, attr.mq_msgsize, attr.mq_curmsgs
    );
    unsafe { mq_close(mqd) };
}
