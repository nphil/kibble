//! Local push: a WebSocket feed of the same JSON the GET endpoints serve, sent when it changes.
//!
//! Design: docs/33-local-push-design.md. The load-bearing decisions, restated:
//!
//! - **Its own listener and thread** ([`PUSH_BIND`], :8766). The HTTP server in `http.rs` is one
//!   thread, one connection at a time; a held connection there blocks every other client (the
//!   live incident that made Scrypted stop using `GET /events/stream`). Nothing here touches
//!   that server.
//! - **Exactly one client.** Home Assistant is the only intended consumer. A second connection
//!   replaces the first (the old socket gets a close frame with reason `replaced`). One socket
//!   is the whole budget: one 64 KiB thread stack, one write buffer, no queue.
//! - **Whole fields, same bytes as the GETs.** Every frame body is produced by the very function
//!   the matching `GET` route calls ([`Field::json`] takes the serialisers from `main.rs`), so
//!   the HA side reuses its existing parsers unchanged and "what does this field contain" has
//!   exactly one answer.
//! - **No new pollers.** Producers that already know a value changed call [`Bus::mark`]; the push
//!   thread coalesces every mark that lands between two sends into one `update` frame. A mark is
//!   a mutex-protected bitset write and a condvar notify -- cheaper than the log line next to it.
//! - **A slow client can't hurt the agent.** Writes carry a 10 s timeout; on timeout the client is
//!   dropped and HA reconnects (with its own backoff) to a fresh snapshot.
//!
//! Wire protocol (server -> client, text frames, one JSON object each):
//!
//! ```text
//! {"type":"hello","proto":1,"seq":N}
//! {"type":"snapshot","seq":N,"fields":{<every field>: <GET body>}}
//! {"type":"update","seq":N,"fields":{<changed field>: <GET body>, ...}}
//! ```
//!
//! Client -> server: a text frame `{"type":"resync"}` requests a fresh `snapshot`; RFC 6455
//! ping/pong/close are honoured. The server pings every [`PING_INTERVAL`] and drops a client
//! that has sent nothing (pong included) for [`CLIENT_TIMEOUT`].
//!
//! RFC 6455 is implemented by hand (handshake, framing, masking, control frames; no
//! extensions, no fragmentation of our own frames) because kibbled carries no dependencies
//! and the subset needed is small. SHA-1 lives in `sha1.rs` for the same reason `md5.rs` does.

use std::collections::BTreeSet;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::sha1;

pub const PUSH_BIND: &str = "0.0.0.0:8766";
pub const PROTO: u32 = 1;
/// Server-initiated ping cadence. HA's aiohttp client answers pongs automatically.
const PING_INTERVAL: Duration = Duration::from_secs(30);
/// Nothing at all from the client (data or pong) for this long -> it is gone; drop it.
const CLIENT_TIMEOUT: Duration = Duration::from_secs(90);
/// Bound on a blocked `write` to a client that has stopped reading.
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);
/// Client text frames are tiny control requests; anything bigger is not ours.
const MAX_CLIENT_FRAME: usize = 1024;
/// The push thread's stack. Frame bodies are heap `String`s; the stack only holds framing.
const STACK_SIZE: usize = 64 * 1024;
const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/// One top-level field of the HA snapshot -- exactly the set of `GET` bodies the integration's
/// coordinator fetches. The `name` is the JSON key in a frame and the field name in HA's
/// `KibbleData`; keep both in lockstep.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
pub enum Field {
    State,
    Schedule,
    Config,
    Cloud,
    Wifi,
    WifiScan,
    Cats,
    Identify,
    ReviewFace,
    PendingFaces,
    Clips,
    Feeds,
    Events,
}

impl Field {
    pub const ALL: [Field; 13] = [
        Field::State,
        Field::Schedule,
        Field::Config,
        Field::Cloud,
        Field::Wifi,
        Field::WifiScan,
        Field::Cats,
        Field::Identify,
        Field::ReviewFace,
        Field::PendingFaces,
        Field::Clips,
        Field::Feeds,
        Field::Events,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Field::State => "state",
            Field::Schedule => "schedule",
            Field::Config => "config",
            Field::Cloud => "cloud",
            Field::Wifi => "wifi",
            Field::WifiScan => "wifi_scan",
            Field::Cats => "cats",
            Field::Identify => "identify",
            Field::ReviewFace => "review_face",
            Field::PendingFaces => "pending_faces",
            Field::Clips => "clips",
            Field::Feeds => "feeds",
            Field::Events => "events",
        }
    }
}

/// Producers mark fields here; the push thread drains it. Process-wide so that modules with no
/// handle to anything (`cloud.rs`'s reconciler, `clips::save`) can still report a change.
pub struct Bus {
    dirty: Mutex<BTreeSet<Field>>,
    changed: Condvar,
    seq: AtomicU64,
}

static BUS: Bus = Bus {
    dirty: Mutex::new(BTreeSet::new()),
    changed: Condvar::new(),
    seq: AtomicU64::new(0),
};

/// Record that `field`'s GET body may now differ from what any client last saw. Cheap, never
/// blocks for long (the set is only ever held for a few instructions), safe from any thread.
pub fn mark(field: Field) {
    let mut dirty = BUS.dirty.lock().unwrap_or_else(|e| e.into_inner());
    dirty.insert(field);
    BUS.seq.fetch_add(1, Ordering::Relaxed);
    drop(dirty);
    BUS.changed.notify_all();
}

/// Marks several fields at once (one notify).
pub fn mark_all(fields: &[Field]) {
    let mut dirty = BUS.dirty.lock().unwrap_or_else(|e| e.into_inner());
    dirty.extend(fields.iter().copied());
    BUS.seq.fetch_add(1, Ordering::Relaxed);
    drop(dirty);
    BUS.changed.notify_all();
}

/// Blocks until at least one field is dirty or `timeout` elapses, then returns and clears the
/// dirty set. An empty result means timeout.
fn drain(timeout: Duration) -> BTreeSet<Field> {
    let dirty = BUS.dirty.lock().unwrap_or_else(|e| e.into_inner());
    let (mut dirty, _) = BUS
        .changed
        .wait_timeout_while(dirty, timeout, |d| d.is_empty())
        .unwrap_or_else(|e| e.into_inner());
    std::mem::take(&mut *dirty)
}

/// Serialises one field -- must be the exact function the matching `GET` route uses.
pub type Serialize = Arc<dyn Fn(Field) -> String + Send + Sync>;

/// Start the push server thread. `serialize` is built in `main.rs` from the route serialisers.
pub fn spawn(serialize: Serialize) -> io::Result<()> {
    let listener = TcpListener::bind(PUSH_BIND)?;
    thread::Builder::new()
        .name("push".into())
        .stack_size(STACK_SIZE)
        .spawn(move || serve(listener, serialize))?;
    Ok(())
}

fn serve(listener: TcpListener, serialize: Serialize) {
    let mut current: Option<TcpStream> = None;
    for stream in listener.incoming() {
        let mut stream = match stream {
            Ok(s) => s,
            Err(e) => {
                eprintln!("kibbled: push accept: {e}");
                continue;
            }
        };
        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let _ = stream.set_write_timeout(Some(WRITE_TIMEOUT));
        match handshake(&mut stream) {
            Ok(()) => {}
            Err(e) => {
                eprintln!("kibbled: push handshake: {e}");
                continue;
            }
        }
        // One client: whoever is still connected gets told why, then dropped.
        if let Some(mut old) = current.take() {
            let _ = send_close(&mut old, 1000, "replaced");
        }
        // Drain marks that accumulated with nobody listening; the snapshot covers them.
        let _ = drain(Duration::ZERO);
        if let Err(e) = session(&mut stream, &serialize) {
            eprintln!("kibbled: push session ended: {e}");
        }
        current = None;
    }
}

/// One client's lifetime: hello + snapshot, then updates until the socket dies.
fn session(stream: &mut TcpStream, serialize: &Serialize) -> io::Result<()> {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
    let _ = stream.set_nodelay(true);
    let seq = BUS.seq.load(Ordering::Relaxed);
    send_text(stream, &format!(r#"{{"type":"hello","proto":{PROTO},"seq":{seq}}}"#))?;
    send_text(stream, &frame_json("snapshot", seq, &Field::ALL, serialize))?;

    let mut last_rx = Instant::now();
    let mut last_ping = Instant::now();
    let mut inbox = Vec::new();
    loop {
        // Client side first: control frames and resync requests. Read is short-timeout so
        // this loop also serves as the ping/timeout tick.
        match read_client_frame(stream, &mut inbox) {
            Ok(Some(ClientFrame::Close)) => return Ok(()),
            Ok(Some(ClientFrame::Ping(payload))) => {
                last_rx = Instant::now();
                send_frame(stream, 0xA, &payload)?;
            }
            Ok(Some(ClientFrame::Pong)) => last_rx = Instant::now(),
            Ok(Some(ClientFrame::Text(t))) => {
                last_rx = Instant::now();
                if t.contains("\"resync\"") {
                    let seq = BUS.seq.load(Ordering::Relaxed);
                    send_text(stream, &frame_json("snapshot", seq, &Field::ALL, serialize))?;
                }
            }
            Ok(None) => {}
            Err(e) => return Err(e),
        }
        if last_rx.elapsed() > CLIENT_TIMEOUT {
            let _ = send_close(stream, 1001, "timeout");
            return Err(io::Error::new(io::ErrorKind::TimedOut, "client silent"));
        }
        if last_ping.elapsed() >= PING_INTERVAL {
            send_frame(stream, 0x9, b"kibble")?;
            last_ping = Instant::now();
        }
        let dirty = drain(Duration::from_millis(200));
        if !dirty.is_empty() {
            let fields: Vec<Field> = dirty.into_iter().collect();
            let seq = BUS.seq.load(Ordering::Relaxed);
            send_text(stream, &frame_json("update", seq, &fields, serialize))?;
        }
    }
}

fn frame_json(kind: &str, seq: u64, fields: &[Field], serialize: &Serialize) -> String {
    let mut out = String::with_capacity(256);
    out.push_str(&format!(r#"{{"type":"{kind}","seq":{seq},"fields":{{"#));
    for (i, f) in fields.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        out.push('"');
        out.push_str(f.name());
        out.push_str("\":");
        out.push_str(&serialize(*f));
    }
    out.push_str("}}");
    out
}

// --- RFC 6455 -------------------------------------------------------------------------------

/// Reads the HTTP upgrade request and answers 101. Anything else gets a 400 and an error.
fn handshake(stream: &mut TcpStream) -> io::Result<()> {
    let mut buf = vec![0u8; 4096];
    let mut filled = 0;
    let head_end = loop {
        if filled == buf.len() {
            return bad_request(stream, "request too large");
        }
        let n = stream.read(&mut buf[filled..])?;
        if n == 0 {
            return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "hung up"));
        }
        filled += n;
        if let Some(i) = buf[..filled].windows(4).position(|w| w == b"\r\n\r\n") {
            break i + 4;
        }
    };
    let head = String::from_utf8_lossy(&buf[..head_end]);
    let mut key = None;
    let mut upgrade = false;
    for line in head.lines().skip(1) {
        let Some((k, v)) = line.split_once(':') else { continue };
        let v = v.trim();
        if k.eq_ignore_ascii_case("sec-websocket-key") {
            key = Some(v.to_string());
        } else if k.eq_ignore_ascii_case("upgrade") && v.eq_ignore_ascii_case("websocket") {
            upgrade = true;
        }
    }
    let (Some(key), true) = (key, upgrade) else {
        return bad_request(stream, "not a websocket upgrade");
    };
    let accept = accept_key(&key);
    let resp = format!(
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
         Sec-WebSocket-Accept: {accept}\r\n\r\n"
    );
    stream.write_all(resp.as_bytes())
}

fn bad_request(stream: &mut TcpStream, why: &str) -> io::Result<()> {
    let _ = stream.write_all(
        format!("HTTP/1.1 400 Bad Request\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{why}", why.len())
            .as_bytes(),
    );
    Err(io::Error::new(io::ErrorKind::InvalidData, why.to_string()))
}

/// `Sec-WebSocket-Accept` for a client key (RFC 6455 §4.2.2 step 5.4).
pub fn accept_key(client_key: &str) -> String {
    let mut input = Vec::with_capacity(client_key.len() + WS_GUID.len());
    input.extend_from_slice(client_key.trim().as_bytes());
    input.extend_from_slice(WS_GUID.as_bytes());
    base64(&sha1::digest(&input))
}

fn send_text(stream: &mut TcpStream, text: &str) -> io::Result<()> {
    send_frame(stream, 0x1, text.as_bytes())
}

fn send_close(stream: &mut TcpStream, code: u16, reason: &str) -> io::Result<()> {
    let mut payload = code.to_be_bytes().to_vec();
    payload.extend_from_slice(reason.as_bytes());
    send_frame(stream, 0x8, &payload)
}

/// Writes one unmasked (server -> client) frame with FIN set.
fn send_frame(stream: &mut TcpStream, opcode: u8, payload: &[u8]) -> io::Result<()> {
    stream.write_all(&encode_frame(opcode, payload))?;
    stream.flush()
}

/// Server frames are never masked (RFC 6455 §5.1).
pub fn encode_frame(opcode: u8, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(payload.len() + 10);
    out.push(0x80 | (opcode & 0x0f));
    let len = payload.len();
    if len < 126 {
        out.push(len as u8);
    } else if len <= u16::MAX as usize {
        out.push(126);
        out.extend_from_slice(&(len as u16).to_be_bytes());
    } else {
        out.push(127);
        out.extend_from_slice(&(len as u64).to_be_bytes());
    }
    out.extend_from_slice(payload);
    out
}

pub enum ClientFrame {
    Text(String),
    Ping(Vec<u8>),
    Pong,
    Close,
}

/// Reads as many bytes as are available (bounded by the socket's read timeout) into `inbox`
/// and decodes one client frame if a whole one is present. `Ok(None)` = nothing complete yet.
fn read_client_frame(stream: &mut TcpStream, inbox: &mut Vec<u8>) -> io::Result<Option<ClientFrame>> {
    let mut chunk = [0u8; 512];
    match stream.read(&mut chunk) {
        Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "client closed")),
        Ok(n) => inbox.extend_from_slice(&chunk[..n]),
        Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => {}
        Err(e) => return Err(e),
    }
    if inbox.len() > MAX_CLIENT_FRAME + 14 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "client frame too large"));
    }
    match decode_client_frame(inbox)? {
        Some((frame, used)) => {
            inbox.drain(..used);
            Ok(Some(frame))
        }
        None => Ok(None),
    }
}

/// Decodes one masked client frame from the front of `buf`. Returns the frame and the number
/// of bytes it occupied, or `None` if `buf` does not yet hold a complete frame.
pub fn decode_client_frame(buf: &[u8]) -> io::Result<Option<(ClientFrame, usize)>> {
    if buf.len() < 2 {
        return Ok(None);
    }
    let fin = buf[0] & 0x80 != 0;
    let opcode = buf[0] & 0x0f;
    let masked = buf[1] & 0x80 != 0;
    if !masked {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "unmasked client frame"));
    }
    if !fin && opcode != 0 {
        // A fragmented client message is nothing this protocol ever sends.
        return Err(io::Error::new(io::ErrorKind::InvalidData, "fragmented client frame"));
    }
    let (len, mut at) = match buf[1] & 0x7f {
        126 => {
            if buf.len() < 4 {
                return Ok(None);
            }
            (u16::from_be_bytes([buf[2], buf[3]]) as usize, 4)
        }
        127 => return Err(io::Error::new(io::ErrorKind::InvalidData, "64-bit client frame")),
        n => (n as usize, 2),
    };
    if len > MAX_CLIENT_FRAME {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "client frame too large"));
    }
    if buf.len() < at + 4 + len {

        return Ok(None);
    }
    let mask = [buf[at], buf[at + 1], buf[at + 2], buf[at + 3]];
    at += 4;
    let payload: Vec<u8> = buf[at..at + len].iter().enumerate().map(|(i, b)| b ^ mask[i % 4]).collect();
    let used = at + len;
    let frame = match opcode {
        0x1 => ClientFrame::Text(String::from_utf8_lossy(&payload).into_owned()),
        0x8 => ClientFrame::Close,
        0x9 => ClientFrame::Ping(payload),
        0xA => ClientFrame::Pong,
        other => return Err(io::Error::new(io::ErrorKind::InvalidData, format!("opcode {other}"))),
    };
    Ok(Some((frame, used)))
}

/// Standard base64 with padding -- only ever used on a 20-byte SHA-1 digest.
fn base64(bytes: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((bytes.len() + 2) / 3 * 4);
    for chunk in bytes.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | b[2] as u32;
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accept_key_matches_the_rfc_6455_worked_example() {
        // RFC 6455 §1.3: key "dGhlIHNhbXBsZSBub25jZQ==" -> "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
        assert_eq!(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
    }

    #[test]
    fn server_frames_use_the_three_length_encodings() {
        assert_eq!(encode_frame(0x1, b"hi")[..2], [0x81, 2]);
        let mid = encode_frame(0x1, &[0u8; 300]);
        assert_eq!(mid[..4], [0x81, 126, 1, 44]);
        let big = encode_frame(0x1, &[0u8; 70_000]);
        assert_eq!(big[1], 127);
        assert_eq!(u64::from_be_bytes(big[2..10].try_into().unwrap()), 70_000);
    }

    fn masked(opcode: u8, payload: &[u8]) -> Vec<u8> {
        let mask = [0x37, 0xfa, 0x21, 0x3d];
        let mut f = vec![0x80 | opcode, 0x80 | payload.len() as u8];
        f.extend_from_slice(&mask);
        f.extend(payload.iter().enumerate().map(|(i, b)| b ^ mask[i % 4]));
        f
    }

    #[test]
    fn decodes_the_rfc_masked_hello_and_reports_bytes_used() {
        // RFC 6455 §5.7: a single-frame masked text message containing "Hello".
        let buf = [0x81, 0x85, 0x37, 0xfa, 0x21, 0x3d, 0x7f, 0x9f, 0x4d, 0x51, 0x58, 0xff];
        let (frame, used) = decode_client_frame(&buf).unwrap().unwrap();
        assert!(matches!(&frame, ClientFrame::Text(t) if t == "Hello"));
        assert_eq!(used, 11);
    }

    #[test]
    fn partial_frames_wait_and_unmasked_or_oversized_frames_are_rejected() {
        let full = masked(0x1, br#"{"type":"resync"}"#);
        assert!(decode_client_frame(&full[..5]).unwrap().is_none());
        assert!(decode_client_frame(&[0x81, 0x05, b'H']).is_err()); // no mask bit
        let mut big = vec![0x81, 0x80 | 126];
        big.extend_from_slice(&(MAX_CLIENT_FRAME as u16 + 1).to_be_bytes());
        assert!(decode_client_frame(&big).is_err());
    }

    #[test]
    fn control_frames_decode_to_their_variants() {
        assert!(matches!(&decode_client_frame(&masked(0x9, b"x")).unwrap().unwrap().0, ClientFrame::Ping(p) if p == b"x"));
        assert!(matches!(decode_client_frame(&masked(0xA, b"")).unwrap().unwrap().0, ClientFrame::Pong));
        assert!(matches!(decode_client_frame(&masked(0x8, &[0x03, 0xe8])).unwrap().unwrap().0, ClientFrame::Close));
    }

    #[test]
    fn marks_coalesce_and_drain_clears() {
        let _ = drain(Duration::ZERO); // isolate from other tests' marks
        mark(Field::Events);
        mark(Field::State);
        mark(Field::Events);
        let got = drain(Duration::ZERO);
        assert_eq!(got.into_iter().collect::<Vec<_>>(), vec![Field::State, Field::Events]);
        assert!(drain(Duration::ZERO).is_empty());
    }

    #[test]
    fn frame_json_serialises_only_the_named_fields_in_order() {
        let ser: Serialize = Arc::new(|f: Field| format!(r#"{{"is":"{}"}}"#, f.name()));
        let json = frame_json("update", 7, &[Field::Cloud, Field::Events], &ser);
        assert_eq!(json, r#"{"type":"update","seq":7,"fields":{"cloud":{"is":"cloud"},"events":{"is":"events"}}}"#);
    }

    #[test]
    fn field_names_are_unique_and_cover_all() {
        let names: BTreeSet<&str> = Field::ALL.iter().map(|f| f.name()).collect();
        assert_eq!(names.len(), Field::ALL.len());
    }

    /// End to end over a real socket: handshake, hello, snapshot, a marked update, a resync.
    #[test]
    fn session_over_a_real_socket() {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let ser: Serialize = Arc::new(|f: Field| format!(r#"{{"f":"{}"}}"#, f.name()));
        thread::spawn(move || serve(listener, ser));

        let mut c = TcpStream::connect(addr).unwrap();
        c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        c.write_all(
            b"GET / HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\
              Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n",
        )
        .unwrap();
        let mut buf = Vec::new();
        let mut tmp = [0u8; 4096];
        // Read until we have the 101 plus the first two frames.
        // The 101 response first; the server writes hello+snapshot right behind it, possibly
        // in the same segment, so parse what is already buffered before blocking again.
        loop {
            if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                let head = String::from_utf8_lossy(&buf[..i]).into_owned();
                assert!(head.contains("s3pPLMBiTxaQ9kYGzzhZRbK+xOo="), "{head}");
                buf.drain(..i + 4);
                break;
            }
            let n = c.read(&mut tmp).unwrap();
            buf.extend_from_slice(&tmp[..n]);
        }
        let texts = [next_server_text(&mut c, &mut buf), next_server_text(&mut c, &mut buf)];
        assert!(texts[0].starts_with(r#"{"type":"hello","proto":1"#), "{}", texts[0]);
        assert!(texts[1].starts_with(r#"{"type":"snapshot""#) && texts[1].contains(r#""events":{"f":"events"}"#));

        mark(Field::Cloud);
        let t = next_server_text(&mut c, &mut buf);
        assert!(t.starts_with(r#"{"type":"update""#) && t.contains(r#""cloud":{"f":"cloud"}"#), "{t}");

        c.write_all(&masked(0x1, br#"{"type":"resync"}"#)).unwrap();
        let t = next_server_text(&mut c, &mut buf);
        assert!(t.starts_with(r#"{"type":"snapshot""#), "{t}");
    }

    /// Parses one unmasked server text frame from the front of `buf` (test helper).
    fn server_text(buf: &[u8]) -> Option<(String, usize)> {
        if buf.len() < 2 || buf[0] != 0x81 {
            return None;
        }
        let (len, at) = match buf[1] {
            126 => (u16::from_be_bytes([buf[2], buf[3]]) as usize, 4),
            127 => (u64::from_be_bytes(buf[2..10].try_into().ok()?) as usize, 10),
            n => (n as usize, 2),
        };
        (buf.len() >= at + len).then(|| (String::from_utf8_lossy(&buf[at..at + len]).into_owned(), at + len))
    }

    fn next_server_text(c: &mut TcpStream, buf: &mut Vec<u8>) -> String {
        let mut tmp = [0u8; 4096];
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some((t, used)) = server_text(buf) {
                buf.drain(..used);
                return t;
            }
            // Skip a ping frame (0x89) if one lands in between.
            if buf.len() >= 2 && buf[0] == 0x89 {
                let used = 2 + (buf[1] & 0x7f) as usize;
                if buf.len() >= used {
                    buf.drain(..used);
                    continue;
                }
            }
            assert!(Instant::now() < deadline, "timed out waiting for a server frame");
            let n = c.read(&mut tmp).unwrap();
            buf.extend_from_slice(&tmp[..n]);
        }
    }
}
