//! A deliberately small HTTP/1.1 server.
//!
//! The device has ~29 MB of free RAM and a loadavg already above 7, so the agent brings no async
//! runtime and no framework: one accept loop, one connection at a time, fixed-size buffers. The
//! feeder is a single-client appliance — Home Assistant polls it — so head-of-line blocking on a
//! second request is irrelevant, and the saved ~2 MB of RSS is not.

use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;

const MAX_REQUEST: usize = 8 * 1024;

pub struct Request {
    pub method: String,
    pub path: String,
    /// Raw request body bytes. Every existing route reads JSON text out of this via
    /// [`Request::body_str`]; `POST /speak` and `PUT /clips/<name>` (see `main.rs`) read it
    /// directly -- a raw PCM body run through lossy UTF-8 conversion would corrupt sample
    /// bytes that happen to collide with invalid UTF-8 sequences, so the body is never
    /// implicitly stringified before a handler sees it.
    pub body: Vec<u8>,
}

impl Request {
    /// Lossy UTF-8 view of the body, for the JSON-ish text routes. Bad bytes become U+FFFD --
    /// exactly as before this type existed, when the body was stored as a `String` up front.
    pub fn body_str(&self) -> std::borrow::Cow<'_, str> {
        String::from_utf8_lossy(&self.body)
    }
}

pub enum Response {
    Json(String),
    NoContent,
    BadRequest(String),
    NotFound,
    /// 409: the speaker already has a writer (a live backchannel session, or another `/speak`/
    /// clip playback in progress) -- `audioout.rs`'s owner arbitration, surfaced verbatim so a
    /// caller knows to retry rather than assume the request was malformed.
    Conflict(String),
    Error(String),
    /// Raw bytes with an explicit content type -- JPEG face crops, H.264 keyframe snapshots, or
    /// a stored clip's ADTS AAC (`GET /clips/<name>`). Not representable as `Json`'s `String`
    /// since the body is arbitrary, non-UTF-8 binary.
    Blob(&'static str, Vec<u8>),
}

impl Response {
    fn into_parts(self) -> (u16, &'static str, Vec<u8>) {
        match self {
            Response::Json(b) => (200, "application/json", b.into_bytes()),
            Response::NoContent => (204, "text/plain", Vec::new()),
            Response::BadRequest(m) => (400, "application/json", err_json(&m).into_bytes()),
            Response::NotFound => (404, "application/json", err_json("not found").into_bytes()),
            Response::Conflict(m) => (409, "application/json", err_json(&m).into_bytes()),
            Response::Error(m) => (500, "application/json", err_json(&m).into_bytes()),
            Response::Blob(ctype, bytes) => (200, ctype, bytes),
        }
    }
}

fn err_json(msg: &str) -> String {
    format!(r#"{{"error":"{}"}}"#, msg.escape_debug())
}

fn reason(code: u16) -> &'static str {
    match code {
        200 => "OK",
        204 => "No Content",
        400 => "Bad Request",
        404 => "Not Found",
        409 => "Conflict",
        _ => "Internal Server Error",
    }
}

/// Serve forever, handing each request to `handler`.
pub fn serve<F>(listener: TcpListener, mut handler: F) -> io::Result<()>
where
    F: FnMut(&Request) -> Response,
{
    for stream in listener.incoming() {
        let mut stream = match stream {
            Ok(s) => s,
            Err(e) => {
                eprintln!("kibbled: accept: {e}");
                continue;
            }
        };
        // A stalled peer must never wedge the agent: every connection is on a short clock.
        let t = Some(Duration::from_secs(10));
        let _ = stream.set_read_timeout(t);
        let _ = stream.set_write_timeout(t);
        if let Err(e) = handle_one(&mut stream, &mut handler) {
            eprintln!("kibbled: connection: {e}");
        }
    }
    Ok(())
}

/// Ceiling on a request body, independent of [`MAX_REQUEST`] (which only bounds how far
/// `handle_one` will search for the end of the *headers*). `POST /speak` and `PUT /clips/<name>`
/// (`main.rs`) carry raw 16 kHz/16-bit mono PCM -- a few seconds of audio is a few hundred KB, so
/// 8 MiB comfortably covers any real request while still bounding a broken or hostile
/// `Content-Length` on a device with ~29 MB of free RAM.
const MAX_BODY: usize = 8 * 1024 * 1024;

fn handle_one<F>(stream: &mut TcpStream, handler: &mut F) -> io::Result<()>
where
    F: FnMut(&Request) -> Response,
{
    let mut head_buf = vec![0u8; MAX_REQUEST];
    let mut filled = 0;
    let head_end = loop {
        if filled == head_buf.len() {
            return write_response(stream, Response::BadRequest("request too large".into()));
        }
        let n = stream.read(&mut head_buf[filled..])?;
        if n == 0 {
            return Ok(()); // peer hung up before sending a full request
        }
        filled += n;
        if let Some(i) = find_headers_end(&head_buf[..filled]) {
            break i;
        }
    };

    let head = String::from_utf8_lossy(&head_buf[..head_end]).into_owned();
    let mut lines = head.split("\r\n");
    let mut start = lines.next().unwrap_or_default().split_whitespace();
    let method = start.next().unwrap_or_default().to_string();
    let path = start.next().unwrap_or_default().to_string();

    let want: usize = lines
        .find_map(|l| {
            let (k, v) = l.split_once(':')?;
            k.eq_ignore_ascii_case("content-length")
                .then(|| v.trim().parse().ok())?
        })
        .unwrap_or(0);
    if want > MAX_BODY {
        return write_response(stream, Response::BadRequest("body too large".into()));
    }

    // Any body bytes the same `read` that found the header already picked up (pipelined on the
    // wire) are sitting past `head_end` in `head_buf`; copy just that prefix out, then read the
    // rest straight into a body buffer sized for exactly `want` bytes rather than reusing (or
    // growing) the small header buffer -- a multi-MB PCM upload shouldn't cost a multi-MB
    // allocation on every trivial `GET /state` too.
    let mut body = Vec::with_capacity(want);
    let already = filled.saturating_sub(head_end).min(want);
    body.extend_from_slice(&head_buf[head_end..head_end + already]);
    drop(head_buf);
    while body.len() < want {
        let mut chunk = [0u8; 64 * 1024];
        let n = stream.read(&mut chunk)?;
        if n == 0 {
            return write_response(stream, Response::BadRequest("truncated body".into()));
        }
        let take = n.min(want - body.len());
        body.extend_from_slice(&chunk[..take]);
    }

    let req = Request { method, path, body };
    let resp = handler(&req);
    write_response(stream, resp)
}

fn find_headers_end(b: &[u8]) -> Option<usize> {
    b.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4)
}

fn write_response(stream: &mut TcpStream, resp: Response) -> io::Result<()> {
    let (code, ctype, body) = resp.into_parts();
    let head = format!(
        "HTTP/1.1 {code} {}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        reason(code),
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(&body)?;
    stream.flush()
}

/// Minimal reader for the flat `{"key": value}` bodies this API accepts. Avoiding a JSON crate
/// keeps the binary small; the shapes we accept are fixed and tiny.
pub fn json_field<'a>(body: &'a str, key: &str) -> Option<&'a str> {
    let pat = format!("\"{key}\"");
    let rest = &body[body.find(&pat)? + pat.len()..];
    let rest = rest.trim_start().strip_prefix(':')?.trim_start();
    let end = rest
        .find(|c: char| c == ',' || c == '}')
        .unwrap_or(rest.len());
    Some(rest[..end].trim().trim_matches('"'))
}

/// Splits `"path?query"` into `(path, query)`. No query string -> `(path, "")`. `/schedule` routes
/// are matched on the path half only; `DELETE /schedule/entry?id=` reads `id` from the query half.
pub fn split_query(path: &str) -> (&str, &str) {
    match path.split_once('?') {
        Some((p, q)) => (p, q),
        None => (path, ""),
    }
}

/// Reads one `key=value` pair out of a query string (`&`-separated, first match wins). No
/// percent-decoding: every value this API accepts is a plain id string kibbled itself generates.
pub fn query_field<'a>(query: &'a str, key: &str) -> Option<&'a str> {
    query.split('&').find_map(|kv| {
        let (k, v) = kv.split_once('=')?;
        (k == key).then_some(v)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_flat_fields_regardless_of_spacing_and_quoting() {
        let b = r#"{"hopper": 2,"amount":10, "id" : "morning" }"#;
        assert_eq!(json_field(b, "hopper"), Some("2"));
        assert_eq!(json_field(b, "amount"), Some("10"));
        assert_eq!(json_field(b, "id"), Some("morning"));
        assert_eq!(json_field(b, "missing"), None);
    }

    #[test]
    fn headers_end_is_found_only_on_the_blank_line() {
        assert_eq!(find_headers_end(b"GET / HTTP/1.1\r\n"), None);
        assert_eq!(find_headers_end(b"GET / HTTP/1.1\r\n\r\nbody"), Some(18));
    }

    #[test]
    fn splits_path_and_query() {
        assert_eq!(split_query("/schedule/entry?id=abc"), ("/schedule/entry", "id=abc"));
        assert_eq!(split_query("/schedule/entry"), ("/schedule/entry", ""));
    }

    #[test]
    fn reads_query_fields() {
        assert_eq!(query_field("id=abc123", "id"), Some("abc123"));
        assert_eq!(query_field("foo=bar&id=xyz", "id"), Some("xyz"));
        assert_eq!(query_field("", "id"), None);
    }
}
