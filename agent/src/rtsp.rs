//! A deliberately small RTSP/1.0 server, serving the ring's "sub" channel (1152x720@25fps H.264)
//! straight through as RTP with no re-encoding.
//!
//! Same philosophy as `http.rs`: one accept loop, one connection at a time, no framework. RTP
//! rides interleaved on the same TCP connection as the RTSP control channel (`RTP/AVP/TCP`,
//! channel 0) -- that keeps this server to a single listening socket and sidesteps UDP port
//! negotiation entirely, which is both simpler to implement from scratch and exactly what the
//! verification command (`ffprobe -rtsp_transport tcp`) and Scrypted both use anyway.
//!
//! Supported methods: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER. There is only ever
//! one stream, so SETUP/PLAY don't gate on the requested URL path -- whatever path a client
//! connects with, it gets the one video track. The GOP on this stream is ~4s (100 frames @25fps,
//! see docs/11-media.md §2), which is too long to make a newly-connected client wait for the next
//! keyframe, so PLAY sends `VideoFeed`'s cached most recent keyframe immediately, then forwards
//! whatever the ring poller publishes from there.
//!
//! While playing, a single thread interleaves two things on the same socket: waiting (with a
//! short timeout) for the next frame from `VideoFeed`, and a short non-blocking-ish read for an
//! incoming client request (GET_PARAMETER keepalive, or TEARDOWN) -- that avoids needing a second
//! thread per connection just to notice a keepalive or hangup.

use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::ring::{Frame, VideoFeed};

const MAX_REQUEST: usize = 4096;
/// RTP payload PT 96 = the one dynamic type we declare, always H.264.
const PAYLOAD_TYPE: u8 = 96;
/// Arbitrary but fixed for the process lifetime -- there's only ever one active session, so it
/// never needs to distinguish sources.
const SSRC: u32 = 0x4B42_4C44; // "KBLD"
/// Conservative per-RTP-packet payload cap; NAL units larger than this get FU-A fragmented
/// (RFC 6184 §5.8). TCP transport has no hard MTU requirement, but this is the size every RTP
/// implementation targets by convention and there is no reason to deviate.
const RTP_MTU: usize = 1400;
/// Fixed RTSP session id -- fine because only one client is ever connected at a time.
const SESSION_ID: &str = "kibble1";

/// Open the listener's accept loop on a background thread. Mirrors `http::serve`'s shape: one
/// connection handled fully before the next `accept()`.
pub fn spawn(listener: TcpListener, feed: Arc<VideoFeed>) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        for stream in listener.incoming() {
            match stream {
                Ok(stream) => {
                    if let Err(e) = handle_session(stream, &feed) {
                        eprintln!("kibbled: rtsp connection: {e}");
                    }
                }
                Err(e) => eprintln!("kibbled: rtsp accept: {e}"),
            }
        }
    })
}

struct RtspRequest {
    method: String,
    url: String,
    cseq: String,
}

fn handle_session(mut stream: TcpStream, feed: &Arc<VideoFeed>) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    loop {
        let req = match read_request(&mut stream)? {
            Some(r) => r,
            None => return Ok(()), // peer hung up before sending a full request
        };
        match req.method.as_str() {
            "OPTIONS" => respond(
                &mut stream,
                &req,
                "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER\r\n",
                "",
            )?,
            "DESCRIBE" => respond_describe(&mut stream, &req, feed)?,
            "SETUP" => respond(
                &mut stream,
                &req,
                &format!("Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: {SESSION_ID}\r\n"),
                "",
            )?,
            "PLAY" => {
                respond(&mut stream, &req, &format!("Session: {SESSION_ID}\r\nRange: npt=0.000-\r\n"), "")?;
                return stream_media(&mut stream, feed);
            }
            "TEARDOWN" => return respond(&mut stream, &req, &format!("Session: {SESSION_ID}\r\n"), ""),
            "GET_PARAMETER" => respond(&mut stream, &req, &format!("Session: {SESSION_ID}\r\n"), "")?,
            _ => write_status(&mut stream, &req.cseq, 501, "Not Implemented", "", "")?,
        }
    }
}

fn respond(stream: &mut TcpStream, req: &RtspRequest, extra: &str, body: &str) -> io::Result<()> {
    write_status(stream, &req.cseq, 200, "OK", extra, body)
}

fn respond_describe(stream: &mut TcpStream, req: &RtspRequest, feed: &VideoFeed) -> io::Result<()> {
    let Some(kf) = wait_for_keyframe(feed, Duration::from_secs(6)) else {
        return write_status(stream, &req.cseq, 503, "Service Unavailable", "", "");
    };
    let nals = split_nal_units(&kf.data);
    let (Some(sps), Some(pps)) = (find_nal_by_type(&nals, 7), find_nal_by_type(&nals, 8)) else {
        return write_status(stream, &req.cseq, 500, "Internal Server Error", "", "");
    };
    let sdp = build_sdp(&req.url, sps, pps);
    let extra = format!("Content-Base: {}\r\nContent-Type: application/sdp\r\n", req.url);
    write_status(stream, &req.cseq, 200, "OK", &extra, &sdp)
}

/// Poll `VideoFeed` for its cached keyframe until one shows up or `timeout` elapses. Only used
/// from DESCRIBE, which is infrequent, so a simple sleep loop is fine.
fn wait_for_keyframe(feed: &VideoFeed, timeout: Duration) -> Option<Frame> {
    let deadline = Instant::now() + timeout;
    loop {
        if let (_, Some(kf)) = feed.latest_keyframe() {
            return Some(kf);
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn build_sdp(base_url: &str, sps: &[u8], pps: &[u8]) -> String {
    let profile_level_id = if sps.len() >= 4 { hex_encode(&sps[1..4]) } else { "000000".to_string() };
    let control = format!("{}/trackID=0", base_url.trim_end_matches('/'));
    format!(
        "v=0\r\n\
         o=- 0 0 IN IP4 0.0.0.0\r\n\
         s=kibble sub\r\n\
         t=0 0\r\n\
         m=video 0 RTP/AVP {PAYLOAD_TYPE}\r\n\
         c=IN IP4 0.0.0.0\r\n\
         a=rtpmap:{PAYLOAD_TYPE} H264/90000\r\n\
         a=fmtp:{PAYLOAD_TYPE} packetization-mode=1;profile-level-id={profile_level_id};\
         sprop-parameter-sets={},{}\r\n\
         a=control:{control}\r\n",
        base64_encode(sps),
        base64_encode(pps),
    )
}

/// How often to check the socket for an incoming client request while playing, independent of
/// how often frames arrive. Keeping this decoupled (rather than doing one bounded read per frame
/// loop iteration) matters: a bounded read still costs its full timeout whenever the client has
/// nothing pending, which -- paid on every single frame -- was enough overhead per cycle to fall
/// behind the sub channel's ~40 ms cadence and silently drop frames (via `VideoFeed`'s
/// latest-wins hand-off), corrupting decode until the next keyframe. A live camera view can
/// easily tolerate 100+ ms of extra latency noticing GET_PARAMETER/TEARDOWN; it can't tolerate
/// dropped interframes.
const CONTROL_CHECK_INTERVAL: Duration = Duration::from_millis(100);

/// Stream RTP over the same connection until the client tears down or disconnects. Runs entirely
/// on this one thread: frame delivery is paced by `VideoFeed::wait_next`'s own timeout, and the
/// control-socket check (keepalive/teardown) rides along on a much coarser timer so it never taxes
/// the frame path (see `CONTROL_CHECK_INTERVAL`).
fn stream_media(stream: &mut TcpStream, feed: &Arc<VideoFeed>) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(2)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;

    let (gen, kf) = feed.latest_keyframe();
    let mut last_seen = gen;
    let mut seq: u16 = 0;
    if let Some(kf) = kf {
        send_access_unit(stream, &kf, &mut seq)?;
    }

    let mut ctrl_buf = Vec::new();
    let mut last_control_check = Instant::now();
    loop {
        if let Some(frame) = feed.wait_next(&mut last_seen, Duration::from_millis(40)) {
            send_access_unit(stream, &frame, &mut seq)?;
        }
        if last_control_check.elapsed() >= CONTROL_CHECK_INTERVAL {
            last_control_check = Instant::now();
            match poll_control(stream, &mut ctrl_buf)? {
                ControlEvent::Teardown => return Ok(()),
                ControlEvent::None | ControlEvent::Handled => {}
            }
        }
    }
}

enum ControlEvent {
    None,
    Handled,
    Teardown,
}

/// Non-blocking-ish check for a pending client request on `stream` (whose read timeout is
/// already set short by the caller), accumulating partial reads in `buf` across calls. Answers
/// GET_PARAMETER inline; TEARDOWN is answered too, then reported so the caller ends the session.
fn poll_control(stream: &mut TcpStream, buf: &mut Vec<u8>) -> io::Result<ControlEvent> {
    let mut chunk = [0u8; 512];
    match stream.read(&mut chunk) {
        Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "peer closed during play")),
        Ok(n) => buf.extend_from_slice(&chunk[..n]),
        Err(e) if e.kind() == io::ErrorKind::WouldBlock || e.kind() == io::ErrorKind::TimedOut => {
            return Ok(ControlEvent::None);
        }
        Err(e) => return Err(e),
    }
    let Some(head_end) = find_headers_end(buf) else {
        return Ok(ControlEvent::None); // still accumulating a full request
    };
    let req = parse_request(&buf[..head_end]);
    buf.drain(..head_end);
    let teardown = req.method.eq_ignore_ascii_case("TEARDOWN");
    write_status(stream, &req.cseq, 200, "OK", &format!("Session: {SESSION_ID}\r\n"), "")?;
    Ok(if teardown { ControlEvent::Teardown } else { ControlEvent::Handled })
}

fn read_request(stream: &mut TcpStream) -> io::Result<Option<RtspRequest>> {
    let mut buf = vec![0u8; MAX_REQUEST];
    let mut filled = 0;
    let head_end = loop {
        if filled == buf.len() {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "rtsp request too large"));
        }
        let n = stream.read(&mut buf[filled..])?;
        if n == 0 {
            return Ok(None);
        }
        filled += n;
        if let Some(i) = find_headers_end(&buf[..filled]) {
            break i;
        }
    };
    Ok(Some(parse_request(&buf[..head_end])))
}

fn parse_request(head: &[u8]) -> RtspRequest {
    let head = String::from_utf8_lossy(head);
    let mut lines = head.split("\r\n");
    let mut start = lines.next().unwrap_or_default().split_whitespace();
    let method = start.next().unwrap_or_default().to_string();
    let url = start.next().unwrap_or_default().to_string();
    let mut cseq = "0".to_string();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            if k.trim().eq_ignore_ascii_case("CSeq") {
                cseq = v.trim().to_string();
            }
        }
    }
    RtspRequest { method, url, cseq }
}

fn find_headers_end(b: &[u8]) -> Option<usize> {
    b.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4)
}

fn write_status(
    stream: &mut TcpStream,
    cseq: &str,
    code: u16,
    reason: &str,
    extra: &str,
    body: &str,
) -> io::Result<()> {
    let mut resp = format!("RTSP/1.0 {code} {reason}\r\nCSeq: {cseq}\r\n{extra}");
    if body.is_empty() {
        resp.push_str("\r\n");
    } else {
        resp.push_str(&format!("Content-Length: {}\r\n\r\n{body}", body.len()));
    }
    stream.write_all(resp.as_bytes())
}

// ---- H.264 Annex-B / RTP (RFC 6184) ----------------------------------------------------------

/// Split an Annex-B byte string into its NAL units (start-code prefix stripped from each).
/// Accepts both 3-byte (`00 00 01`) and 4-byte (`00 00 00 01`) start codes, since real encoders
/// mix them within one access unit; H.264's own emulation-prevention byte (0x03) guarantees a
/// well-formed NAL's payload never contains a byte run that looks like a start code, so this
/// linear scan can't misfire on real bitstream data.
fn split_nal_units(data: &[u8]) -> Vec<&[u8]> {
    let mut marks: Vec<(usize, usize)> = Vec::new();
    let mut i = 0;
    while i + 3 <= data.len() {
        if data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1 {
            marks.push((i, i + 3));
            i += 3;
            continue;
        }
        if i + 4 <= data.len() && data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 0 && data[i + 3] == 1 {
            marks.push((i, i + 4));
            i += 4;
            continue;
        }
        i += 1;
    }
    let mut out = Vec::with_capacity(marks.len());
    for (idx, &(_, payload_start)) in marks.iter().enumerate() {
        let end = marks.get(idx + 1).map(|&(prefix_start, _)| prefix_start).unwrap_or(data.len());
        if end > payload_start {
            out.push(&data[payload_start..end]);
        }
    }
    out
}

fn find_nal_by_type<'a>(nals: &[&'a [u8]], nal_type: u8) -> Option<&'a [u8]> {
    nals.iter().find(|n| !n.is_empty() && (n[0] & 0x1F) == nal_type).copied()
}

/// Send every NAL in `frame`'s payload as its own RTP-packetized unit(s), marker bit set on the
/// last packet of the last NAL (i.e. the end of this access unit).
fn send_access_unit(stream: &mut TcpStream, frame: &Frame, seq: &mut u16) -> io::Result<()> {
    let rtp_ts = (frame.pts_us as u64 * 90_000 / 1_000_000) as u32;
    let nals = split_nal_units(&frame.data);
    let last = nals.len().saturating_sub(1);
    for (i, nal) in nals.iter().enumerate() {
        send_nal(stream, nal, seq, rtp_ts, i == last)?;
    }
    Ok(())
}

fn send_nal(stream: &mut TcpStream, nal: &[u8], seq: &mut u16, rtp_ts: u32, marker_on_last: bool) -> io::Result<()> {
    let fragments = fragment_nal(nal);
    let last = fragments.len().saturating_sub(1);
    for (i, frag) in fragments.iter().enumerate() {
        let marker = i == last && marker_on_last;
        let pkt = build_rtp_packet(frag, *seq, rtp_ts, SSRC, marker);
        *seq = seq.wrapping_add(1);
        stream.write_all(&interleaved_frame(0, &pkt))?;
    }
    Ok(())
}

/// Split a NAL unit (header byte included) into RTP payloads: the whole NAL as-is if it fits in
/// one packet, else FU-A fragments (RFC 6184 §5.8).
fn fragment_nal(nal: &[u8]) -> Vec<Vec<u8>> {
    if nal.is_empty() {
        return Vec::new();
    }
    if nal.len() <= RTP_MTU {
        return vec![nal.to_vec()];
    }
    let nal_header = nal[0];
    let nal_type = nal_header & 0x1F;
    let fu_indicator = (nal_header & 0xE0) | 28;
    let payload = &nal[1..];
    let chunk_cap = RTP_MTU - 2;
    let mut out = Vec::new();
    let mut offset = 0;
    while offset < payload.len() {
        let end = (offset + chunk_cap).min(payload.len());
        let mut fu_header = nal_type;
        if offset == 0 {
            fu_header |= 0x80; // S
        }
        if end == payload.len() {
            fu_header |= 0x40; // E
        }
        let mut pkt = Vec::with_capacity(2 + (end - offset));
        pkt.push(fu_indicator);
        pkt.push(fu_header);
        pkt.extend_from_slice(&payload[offset..end]);
        out.push(pkt);
        offset = end;
    }
    out
}

fn build_rtp_packet(payload: &[u8], seq: u16, rtp_ts: u32, ssrc: u32, marker: bool) -> Vec<u8> {
    let mut pkt = Vec::with_capacity(12 + payload.len());
    pkt.push(0x80); // V=2, P=0, X=0, CC=0
    pkt.push((if marker { 0x80 } else { 0 }) | PAYLOAD_TYPE);
    pkt.extend_from_slice(&seq.to_be_bytes());
    pkt.extend_from_slice(&rtp_ts.to_be_bytes());
    pkt.extend_from_slice(&ssrc.to_be_bytes());
    pkt.extend_from_slice(payload);
    pkt
}

fn interleaved_frame(channel: u8, data: &[u8]) -> Vec<u8> {
    let mut framed = Vec::with_capacity(4 + data.len());
    framed.push(b'$');
    framed.push(channel);
    framed.extend_from_slice(&(data.len() as u16).to_be_bytes());
    framed.extend_from_slice(data);
    framed
}

fn base64_encode(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0];
        let b1 = *chunk.get(1).unwrap_or(&0);
        let b2 = *chunk.get(2).unwrap_or(&0);
        let n = ((b0 as u32) << 16) | ((b1 as u32) << 8) | (b2 as u32);
        out.push(ALPHABET[(n >> 18 & 0x3F) as usize] as char);
        out.push(ALPHABET[(n >> 12 & 0x3F) as usize] as char);
        out.push(if chunk.len() > 1 { ALPHABET[(n >> 6 & 0x3F) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { ALPHABET[(n & 0x3F) as usize] as char } else { '=' });
    }
    out
}

fn hex_encode(data: &[u8]) -> String {
    data.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_nal_units_handles_mixed_start_code_lengths() {
        let data = [
            &[0, 0, 0, 1][..], &[0x67, 1, 2][..], // 4-byte start code, SPS-ish
            &[0, 0, 1][..], &[0x68, 3, 4][..],     // 3-byte start code, PPS-ish
            &[0, 0, 0, 1][..], &[0x65, 5, 6, 7][..], // 4-byte start code, slice-ish
        ]
        .concat();
        let nals = split_nal_units(&data);
        assert_eq!(nals, vec![&[0x67, 1, 2][..], &[0x68, 3, 4][..], &[0x65, 5, 6, 7][..]]);
    }

    #[test]
    fn find_nal_by_type_matches_on_the_low_five_bits_of_the_first_byte() {
        let sps = [0x67u8, 9, 9];
        let pps = [0x68u8, 8, 8];
        let nals: Vec<&[u8]> = vec![&sps, &pps];
        assert_eq!(find_nal_by_type(&nals, 7), Some(&sps[..]));
        assert_eq!(find_nal_by_type(&nals, 8), Some(&pps[..]));
        assert_eq!(find_nal_by_type(&nals, 5), None);
    }

    #[test]
    fn fragment_nal_keeps_small_nals_whole() {
        let nal = vec![0x65, 1, 2, 3];
        assert_eq!(fragment_nal(&nal), vec![nal]);
    }

    #[test]
    fn fragment_nal_splits_large_nals_and_reassembles_byte_identical() {
        let mut nal = vec![0x65u8]; // nal_ref_idc=3, type=5 (IDR)
        nal.extend((0..5000u32).map(|i| (i % 256) as u8));
        let frags = fragment_nal(&nal);
        assert!(frags.len() > 1, "5000 bytes must not fit in one RTP_MTU-sized packet");
        assert_eq!(frags[0][0] & 0xE0, nal[0] & 0xE0, "nal_ref_idc preserved in FU indicator");
        assert_eq!(frags[0][0] & 0x1F, 28, "FU-A type in the indicator byte");
        assert_eq!(frags[0][1] & 0x80, 0x80, "S=1 on the first fragment");
        assert_eq!(frags[0][1] & 0x40, 0, "E=0 on the first fragment");
        assert_eq!(frags[0][1] & 0x1F, nal[0] & 0x1F, "original nal_type carried in the FU header");
        let last = frags.last().unwrap();
        assert_eq!(last[1] & 0x40, 0x40, "E=1 on the last fragment");
        assert_eq!(last[1] & 0x80, 0, "S=0 on the last fragment");
        let mut rebuilt = vec![nal[0]];
        for f in &frags {
            rebuilt.extend_from_slice(&f[2..]);
        }
        assert_eq!(rebuilt, nal, "concatenated fragment payloads must reproduce the original NAL exactly");
    }

    #[test]
    fn base64_encode_matches_rfc4648_test_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn hex_encode_is_lowercase_zero_padded() {
        assert_eq!(hex_encode(&[0x00, 0x0a, 0xff]), "000aff");
    }

    #[test]
    fn build_rtp_packet_lays_out_fixed_header_fields() {
        let pkt = build_rtp_packet(&[1, 2, 3], 0x1234, 0x89AB_CDEF, 0xDEAD_BEEF, true);
        assert_eq!(pkt[0], 0x80, "V=2,P=0,X=0,CC=0");
        assert_eq!(pkt[1], 0x80 | PAYLOAD_TYPE, "marker set, PT=96");
        assert_eq!(&pkt[2..4], &0x1234u16.to_be_bytes());
        assert_eq!(&pkt[4..8], &0x89AB_CDEFu32.to_be_bytes());
        assert_eq!(&pkt[8..12], &0xDEAD_BEEFu32.to_be_bytes());
        assert_eq!(&pkt[12..], &[1, 2, 3]);
    }

    #[test]
    fn build_rtp_packet_clears_marker_bit_when_not_set() {
        let pkt = build_rtp_packet(&[], 0, 0, 0, false);
        assert_eq!(pkt[1], PAYLOAD_TYPE);
    }

    #[test]
    fn interleaved_frame_uses_dollar_marker_and_big_endian_length() {
        let framed = interleaved_frame(0, &[9, 9, 9]);
        assert_eq!(framed[0], b'$');
        assert_eq!(framed[1], 0);
        assert_eq!(&framed[2..4], &3u16.to_be_bytes());
        assert_eq!(&framed[4..], &[9, 9, 9]);
    }

    #[test]
    fn parse_request_extracts_method_url_and_cseq() {
        let head = b"DESCRIBE rtsp://host:8554/sub RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n\r\n";
        let req = parse_request(head);
        assert_eq!(req.method, "DESCRIBE");
        assert_eq!(req.url, "rtsp://host:8554/sub");
        assert_eq!(req.cseq, "2");
    }

    #[test]
    fn headers_end_is_found_only_on_the_blank_line() {
        assert_eq!(find_headers_end(b"OPTIONS rtsp://x RTSP/1.0\r\n"), None);
        assert_eq!(find_headers_end(b"OPTIONS rtsp://x RTSP/1.0\r\n\r\n"), Some(29));
    }

    #[test]
    fn build_sdp_embeds_base64_parameter_sets_and_profile_level_id() {
        let sps = [0x67, 0x42, 0x00, 0x1F, 0xAA];
        let pps = [0x68, 0xCE, 0x3C, 0x80];
        let sdp = build_sdp("rtsp://host:8554/sub", &sps, &pps);
        assert!(sdp.contains("profile-level-id=42001f"));
        assert!(sdp.contains(&format!(
            "sprop-parameter-sets={},{}",
            base64_encode(&sps),
            base64_encode(&pps)
        )));
        assert!(sdp.contains("a=rtpmap:96 H264/90000"));
        assert!(sdp.contains("a=control:rtsp://host:8554/sub/trackID=0"));
    }
}
