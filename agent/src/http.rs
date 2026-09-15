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
    pub body: String,
}

pub enum Response {
    Json(String),
    NoContent,
    BadRequest(String),
    NotFound,
    Error(String),
}

impl Response {
    fn parts(&self) -> (u16, &'static str, String) {
        match self {
            Response::Json(b) => (200, "application/json", b.clone()),
            Response::NoContent => (204, "text/plain", String::new()),
            Response::BadRequest(m) => (400, "application/json", err_json(m)),
            Response::NotFound => (404, "application/json", err_json("not found")),
            Response::Error(m) => (500, "application/json", err_json(m)),
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

fn handle_one<F>(stream: &mut TcpStream, handler: &mut F) -> io::Result<()>
where
    F: FnMut(&Request) -> Response,
{
    let mut buf = vec![0u8; MAX_REQUEST];
    let mut filled = 0;
    let head_end = loop {
        if filled == buf.len() {
            return write_response(stream, &Response::BadRequest("request too large".into()));
        }
        let n = stream.read(&mut buf[filled..])?;
        if n == 0 {
            return Ok(()); // peer hung up before sending a full request
        }
        filled += n;
        if let Some(i) = find_headers_end(&buf[..filled]) {
            break i;
        }
    };

    let head = String::from_utf8_lossy(&buf[..head_end]).into_owned();
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
    if want > MAX_REQUEST - head_end {
        return write_response(stream, &Response::BadRequest("body too large".into()));
    }
    while filled < head_end + want {
        let n = stream.read(&mut buf[filled..])?;
        if n == 0 {
            return write_response(stream, &Response::BadRequest("truncated body".into()));
        }
        filled += n;
    }

    let req = Request {
        method,
        path,
        body: String::from_utf8_lossy(&buf[head_end..head_end + want]).into_owned(),
    };
    let resp = handler(&req);
    write_response(stream, &resp)
}

fn find_headers_end(b: &[u8]) -> Option<usize> {
    b.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4)
}

fn write_response(stream: &mut TcpStream, resp: &Response) -> io::Result<()> {
    let (code, ctype, body) = resp.parts();
    let head = format!(
        "HTTP/1.1 {code} {}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        reason(code),
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body.as_bytes())?;
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
}
