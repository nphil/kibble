//! A deliberately small RTSP/1.0 server, serving both channels of the vendor frame ring straight
//! through as RTP with no re-encoding: `/main` is the ring's 1728x1080 channel, `/sub` is the
//! 1152x720@25fps channel (see `ring.rs`). Which mount a client gets is decided purely by a path
//! segment in its request URL (`Feeds::select` below); anything that doesn't name "main" defaults
//! to `/sub`, this server's original and most-documented mount.
//!
//! Same philosophy as `http.rs`: no framework. RTP rides interleaved on the same TCP connection as
//! the RTSP control channel (`RTP/AVP/TCP`, channel 0) -- that keeps this server to a single
//! listening socket and sidesteps UDP port negotiation entirely, which is both simpler to
//! implement from scratch and exactly what the verification command (`ffprobe -rtsp_transport
//! tcp`) and Scrypted both use anyway.
//!
//! ## Concurrency
//!
//! By design, Scrypted is the single real consumer of either stream, so [`MAX_SESSIONS_PER_STREAM`]
//! is a small, fixed ceiling (2) rather than a tunable: room for Scrypted's own prebuffer plus one
//! spare slot for a human debugging with `ffprobe` on the same stream, applied independently per
//! mount. A session past that cap gets an immediate, clean `453 Not Enough Bandwidth` at PLAY --
//! never a hang. Taking the spare slot is logged to stderr with the client's address (see
//! `handle_session`), and `GET /streams` (`streams_json`, wired in `main.rs`) exposes exactly who
//! is attached to each mount right now, so anything other than Scrypted showing up is visible.
//!
//! Each accepted connection gets its own thread (see `spawn`). That matters beyond just the
//! session cap: this server used to hand every connection to one accept-loop thread that ran the
//! whole session to completion before calling `accept()` again, so a second client's connection
//! sat fully formed in the kernel's accept backlog and its DESCRIBE was simply never read -- a
//! hang, not a refusal, and the bug that motivated this rewrite. A connection that never sends a
//! request is bounded by the same 10s read timeout every other request already gets, on its own
//! thread, so it can't delay anyone else either.
//!
//! ## Frame fan-out
//!
//! `ring.rs`'s poller polls the shared-memory ring exactly once regardless of how many clients are
//! attached to either mount, and publishes each new access unit into every attached client's own
//! small bounded queue (`ring::VideoFeed`/`ring::Subscription`, 2-4 access units). A momentarily
//! slow client only ever drops frames from its own queue -- see `Subscriber::push` in `ring.rs`
//! for the exact policy -- and never blocks the poller or any other client. Every new session is
//! seeded with the cached last keyframe (SPS+PPS+IDR) at subscribe time, so PLAY doesn't need to
//! wait out the ~4s GOP for the next one.
//!
//! Supported methods: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER. DESCRIBE/PLAY gate
//! on which mount the URL names; SETUP/PLAY *do* gate on trackID now that there are three
//! possible tracks -- video (trackID=0, interleaved 0-1), the outgoing mic (trackID=1,
//! interleaved 2-3, always offered), and the backchannel (trackID=2, interleaved 4-5, offered
//! only when DESCRIBE carried `Require: www.onvif.org/ver20/backchannel`). A session accumulates
//! whichever tracks get SETUP before PLAY starts streaming all of them at once.
//!
//! While playing, a single thread interleaves two things on the same socket: waiting (with a
//! short timeout) for the next frame from this session's `Subscription`, and a short
//! non-blocking-ish read for an incoming client request (GET_PARAMETER keepalive, or TEARDOWN) --
//! that avoids needing a second thread per connection just to notice a keepalive or hangup.

use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::adts;
use crate::backchannel::Backchannel;
use crate::rfc3640;
use crate::ring::{self, AudioFeed, AudioSubscription, Frame, Subscription, VideoFeed};
use crate::audioout;

const MAX_REQUEST: usize = 4096;
/// RTP payload PT 96 = the one dynamic type we declare, always H.264.
const PAYLOAD_TYPE: u8 = 96;
/// Arbitrary but fixed for the process lifetime -- RTP receivers only need it to disambiguate
/// sources sharing one transport, and every session here has its own TCP connection.
const SSRC: u32 = 0x4B42_4C44; // "KBLD"
/// Conservative per-RTP-packet payload cap; NAL units larger than this get FU-A fragmented
/// (RFC 6184 §5.8). TCP transport has no hard MTU requirement, but this is the size every RTP
/// implementation targets by convention and there is no reason to deviate.
const RTP_MTU: usize = 1400;
/// Concurrent PLAY sessions each mount (`/main`, `/sub`) will serve -- see the module doc's
/// "Concurrency" section. A hard ceiling, not configurable: raising it is a design decision, not
/// an operational one.
const MAX_SESSIONS_PER_STREAM: usize = 2;
/// Per-client bounded queue depth (`ring::Subscriber`). Small enough to bound memory and
/// staleness (at 25fps this is well under 200ms of buffering even full), large enough that
/// ordinary scheduling jitter -- not a genuine client stall -- doesn't trigger the drop policy on
/// every single frame.
const CLIENT_QUEUE_CAP: usize = 3;
/// RTP payload PT 97 = the outgoing mic track (`MPEG4-GENERIC`, RFC 3640), always offered.
const AUDIO_PAYLOAD_TYPE: u8 = 97;
/// Distinct from video's `SSRC`: this is a logically separate RTP stream even though it shares
/// the same TCP transport.
const AUDIO_SSRC: u32 = 0x4B42_4C41; // "KBLA"
/// Interleaved-channel numbers (RTP; the next odd number is that track's unused RTCP channel).
const VIDEO_RTP_CHANNEL: u8 = 0;
const AUDIO_RTP_CHANNEL: u8 = 2;
const BACKCHANNEL_RTP_CHANNEL: u8 = 4;
/// `trackID=N` values `build_sdp`'s `a=control:` lines advertise and `SETUP`/`track_id_from_url`
/// parse back.
const AUDIO_TRACK: u8 = 1;
const BACKCHANNEL_TRACK: u8 = 2;
/// How often to poll for a new outgoing-audio access unit. AAC frames arrive every ~64ms
/// (docs/23-audio-codec.md); riding the same loop as the video poll and the control-socket check,
/// so it needs to be short enough not to stall either of those.
const AUDIO_POLL_TIMEOUT: Duration = Duration::from_millis(20);

/// The two independently-served streams. `main.rs` owns one `Arc<Feeds>`, shared between the RTSP
/// accept loop (this module) and the HTTP `GET /streams` diagnostic handler.
pub struct Feeds {
    pub main: Arc<VideoFeed>,
    pub sub: Arc<VideoFeed>,
    /// The one microphone, shared by both mounts (see `ring::AudioFeed`'s own doc comment).
    pub audio: Arc<AudioFeed>,
    /// The one speaker lock every talkback/announce path shares -- see `audioout.rs`.
    pub speaker_owner: Arc<audioout::SpeakerOwner>,
}

impl Feeds {
    /// Picks the stream a request's URL names, returning both the feed and its canonical name
    /// (for logging and `GET /streams`, not necessarily byte-identical to whatever path segment
    /// the client actually sent). Matches on a whole path segment so `/mainstream` doesn't
    /// false-positive as `/main`; anything that doesn't say "main" -- including a client that
    /// skips DESCRIBE and just names the bare host -- defaults to `/sub`.
    fn select(&self, url: &str) -> (&'static str, &Arc<VideoFeed>) {
        if url_names_segment(url, "main") {
            ("main", &self.main)
        } else {
            ("sub", &self.sub)
        }
    }
}

fn url_names_segment(url: &str, name: &str) -> bool {
    url.split('/').any(|seg| seg.eq_ignore_ascii_case(name))
}

/// `GET /streams`: per mount, what the ring is actually producing and exactly who is attached.
/// Because Scrypted is meant to be the only real consumer (see module doc), any peer address
/// beyond one expected session is itself the diagnostic signal -- this just exposes the raw facts
/// and leaves the judgment to whoever's reading it (today: a human; the HA sensor surfaces
/// `blocked`/`connected`-style state built from this same data in a future pass).
pub fn streams_json(feeds: &Feeds) -> String {
    format!(r#"{{"main":{},"sub":{}}}"#, mount_json(&feeds.main), mount_json(&feeds.sub))
}

fn mount_json(feed: &VideoFeed) -> String {
    let snap = feed.snapshot();
    let sessions: Vec<String> = snap.sessions.iter().map(|p| format!("\"{}\"", p.escape_debug())).collect();
    format!(
        r#"{{"width":{},"height":{},"fps":{:.1},"session_count":{},"sessions":[{}]}}"#,
        snap.width,
        snap.height,
        snap.fps,
        snap.sessions.len(),
        sessions.join(",")
    )
}

/// Monotonic counter for a unique-enough RTSP session id per connection -- now that more than one
/// session can be active at once, the old single fixed id would let two clients' `Session` headers
/// collide.
static NEXT_SESSION: AtomicU64 = AtomicU64::new(1);

fn new_session_id() -> String {
    format!("kibble{:x}", NEXT_SESSION.fetch_add(1, Ordering::Relaxed))
}

/// Open the listener's accept loop on a background thread. Each accepted connection gets its own
/// thread -- see the module doc's "Concurrency" section for why that's essential, not just nice
/// to have.
pub fn spawn(listener: TcpListener, feeds: Arc<Feeds>) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        for stream in listener.incoming() {
            match stream {
                Ok(stream) => {
                    let feeds = Arc::clone(&feeds);
                    thread::spawn(move || {
                        if let Err(e) = handle_session(stream, &feeds) {
                            eprintln!("kibbled: rtsp connection: {e}");
                        }
                    });
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
    require: Option<String>,
    transport: Option<String>,
}

/// Which tracks this session has `SETUP` so far -- accumulated across possibly-several `SETUP`
/// calls before `PLAY`, per ordinary RTSP sequencing (one `SETUP` per track the client wants,
/// then one `PLAY` that starts streaming everything that was set up). Video has no flag: it's
/// implied by the mount the URL already names, exactly as before this session ever considered a
/// second track.
#[derive(Default)]
struct SessionSetup {
    audio: bool,
    backchannel: bool,
}

fn track_id_from_url(url: &str) -> u8 {
    url.rsplit_once("trackID=").and_then(|(_, id)| id.parse().ok()).unwrap_or(0)
}

fn wants_interleaved_tcp(req: &RtspRequest) -> bool {
    req.transport.as_deref().is_some_and(|t| t.contains("TCP") || t.contains("interleaved"))
}

fn handle_session(mut stream: TcpStream, feeds: &Feeds) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(10)))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    let peer = stream.peer_addr().map(|a| a.to_string()).unwrap_or_else(|_| "unknown".to_string());
    let session_id = new_session_id();
    let mut setup = SessionSetup::default();
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
            "DESCRIBE" => respond_describe(&mut stream, &req, feeds)?,
            "SETUP" => {
                let track = track_id_from_url(&req.url);
                if track == BACKCHANNEL_TRACK && !wants_interleaved_tcp(&req) {
                    // Scrypted's own documented UDP-then-TCP fallback: refuse so it retries TCP.
                    write_status(&mut stream, &req.cseq, 461, "Unsupported Transport", "", "")?;
                    continue;
                }
                let interleaved = match track {
                    AUDIO_TRACK => {
                        setup.audio = true;
                        "2-3"
                    }
                    BACKCHANNEL_TRACK => {
                        setup.backchannel = true;
                        "4-5"
                    }
                    _ => "0-1",
                };
                respond(
                    &mut stream,
                    &req,
                    &format!(
                        "Transport: RTP/AVP/TCP;unicast;interleaved={interleaved}\r\nSession: {session_id}\r\n"
                    ),
                    "",
                )?
            }
            "PLAY" => {
                let (path, feed) = feeds.select(&req.url);
                return match feed.subscribe(peer.clone(), MAX_SESSIONS_PER_STREAM, CLIENT_QUEUE_CAP) {
                    Some((session, active)) => {
                        if active == MAX_SESSIONS_PER_STREAM {
                            eprintln!(
                                "kibbled: rtsp /{path} spare slot taken by {peer} ({active}/{MAX_SESSIONS_PER_STREAM} sessions active)"
                            );
                        }
                        let audio_sub = setup.audio.then(|| feeds.audio.subscribe(CLIENT_QUEUE_CAP));
                        let backchannel = start_backchannel_if_setup(&setup, feeds, &peer);
                        respond(&mut stream, &req, &format!("Session: {session_id}\r\nRange: npt=0.000-\r\n"), "")?;
                        stream_media(&mut stream, &session, audio_sub.as_ref(), backchannel, &session_id)
                    }
                    None => write_status(&mut stream, &req.cseq, 453, "Not Enough Bandwidth", "", ""),
                };
            }
            "TEARDOWN" => return respond(&mut stream, &req, &format!("Session: {session_id}\r\n"), ""),
            "GET_PARAMETER" => respond(&mut stream, &req, &format!("Session: {session_id}\r\n"), "")?,
            _ => write_status(&mut stream, &req.cseq, 501, "Not Implemented", "", "")?,
        }
    }
}

/// Acquires the speaker and starts a live backchannel session if this session `SETUP` trackID=2
/// -- soft-fails (logs and returns `None`, letting `PLAY` proceed with video/audio only) rather
/// than refusing the whole `PLAY` if the speaker is busy: a viewer should still see and hear the
/// camera even when talkback happens to be unavailable right then.
fn start_backchannel_if_setup(setup: &SessionSetup, feeds: &Feeds, peer: &str) -> Option<Backchannel> {
    if !setup.backchannel {
        return None;
    }
    match feeds.speaker_owner.try_acquire() {
        Ok(guard) => match Backchannel::start(guard) {
            Ok(bc) => Some(bc),
            Err(e) => {
                eprintln!("kibbled: rtsp backchannel start failed for {peer}: {e}");
                None
            }
        },
        Err(reason) => {
            eprintln!("kibbled: rtsp backchannel refused for {peer}: {reason}");
            None
        }
    }
}

fn respond(stream: &mut TcpStream, req: &RtspRequest, extra: &str, body: &str) -> io::Result<()> {
    write_status(stream, &req.cseq, 200, "OK", extra, body)
}

fn respond_describe(stream: &mut TcpStream, req: &RtspRequest, feeds: &Feeds) -> io::Result<()> {
    let (_, feed) = feeds.select(&req.url);
    let Some(kf) = wait_for_keyframe(feed, Duration::from_secs(6)) else {
        return write_status(stream, &req.cseq, 503, "Service Unavailable", "", "");
    };
    let nals = split_nal_units(&kf.data);
    let (Some(sps), Some(pps)) = (find_nal_by_type(&nals, 7), find_nal_by_type(&nals, 8)) else {
        return write_status(stream, &req.cseq, 500, "Internal Server Error", "", "");
    };
    let backchannel = req
        .require
        .as_deref()
        .is_some_and(|r| r.contains("www.onvif.org/ver20/backchannel"));
    let sdp = build_sdp(&req.url, sps, pps, backchannel);
    let extra = format!("Content-Base: {}\r\nContent-Type: application/sdp\r\n", req.url);
    write_status(stream, &req.cseq, 200, "OK", &extra, &sdp)
}

/// Poll `VideoFeed` for its cached keyframe until one shows up or `timeout` elapses. Only used
/// from DESCRIBE, which is infrequent, so a simple sleep loop is fine; it doesn't consume a
/// session slot, so it's not subject to `MAX_SESSIONS_PER_STREAM`.
fn wait_for_keyframe(feed: &VideoFeed, timeout: Duration) -> Option<Frame> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(kf) = feed.latest_keyframe() {
            return Some(kf);
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn build_sdp(base_url: &str, sps: &[u8], pps: &[u8], backchannel: bool) -> String {
    let profile_level_id = if sps.len() >= 4 { hex_encode(&sps[1..4]) } else { "000000".to_string() };
    let base = base_url.trim_end_matches('/');
    let mut sdp = format!(
        "v=0\r\n\
         o=- 0 0 IN IP4 0.0.0.0\r\n\
         s=kibble sub\r\n\
         t=0 0\r\n\
         m=video 0 RTP/AVP {PAYLOAD_TYPE}\r\n\
         c=IN IP4 0.0.0.0\r\n\
         a=rtpmap:{PAYLOAD_TYPE} H264/90000\r\n\
         a=fmtp:{PAYLOAD_TYPE} packetization-mode=1;profile-level-id={profile_level_id};\
         sprop-parameter-sets={},{}\r\n\
         a=control:{base}/trackID=0\r\n\
         m=audio 0 RTP/AVP {AUDIO_PAYLOAD_TYPE}\r\n\
         a=rtpmap:{AUDIO_PAYLOAD_TYPE} MPEG4-GENERIC/16000/1\r\n\
         a=fmtp:{AUDIO_PAYLOAD_TYPE} streamtype=5; profile-level-id=1; mode=AAC-hbr; \
         sizelength=13; indexlength=3; indexdeltalength=3; config={}\r\n\
         a=control:{base}/trackID=1\r\n",
        base64_encode(sps),
        base64_encode(pps),
        hex_encode(&adts::AUDIO_SPECIFIC_CONFIG),
    );
    if backchannel {
        sdp.push_str(&format!(
            "m=audio 0 RTP/AVP 98 0 8\r\n\
             a=rtpmap:98 L16/16000\r\n\
             a=rtpmap:0 PCMU/8000\r\n\
             a=rtpmap:8 PCMA/8000\r\n\
             a=sendonly\r\n\
             a=control:{base}/trackID=2\r\n"
        ));
    }
    sdp
}

/// How often to check the socket for an incoming client request while playing, independent of
/// how often frames arrive. Keeping this decoupled (rather than doing one bounded read per frame
/// loop iteration) matters: a bounded read still costs its full timeout whenever the client has
/// nothing pending, which -- paid on every single frame -- was enough overhead per cycle to fall
/// behind the sub channel's ~40 ms cadence and silently drop frames. A live camera view can easily
/// tolerate 100+ ms of extra latency noticing GET_PARAMETER/TEARDOWN; it can't tolerate dropped
/// interframes.
const CONTROL_CHECK_INTERVAL: Duration = Duration::from_millis(100);

/// How long a blocked write to a playing session's socket is tolerated before giving up on that
/// session. Was 5s; raised after a live capture during a real Wi-Fi/USB-adapter hiccup (this
/// feeder's USB Wi-Fi dongle re-enumerates roughly every ~190s, confirmed in `dmesg`) measured
/// *simultaneous* multi-second write stalls across every path off the device at once -- both
/// direct RTSP mounts, the Scrypted rebroadcast, and even a 127.0.0.1 loopback HTTP request --
/// up to 13.3s for a single event, longer for a chained one. `ring.rs` buffers 12-20+s of
/// history, so a write that unblocks inside that window can still resume from something close to
/// current; the old 5s timeout instead killed the TCP connection *during* the hiccup, which is
/// what forced Scrypted's Rebroadcast to fully reconnect (and HomeKit, watching through it, to
/// need a manual restart) for a stall the ring had already ridden out. 30s clears every stall
/// actually measured with real margin while still bounding how long a genuinely dead peer -- not
/// just a hiccupping one -- can hold one of only [`MAX_SESSIONS_PER_STREAM`] slots.
const PLAY_WRITE_TIMEOUT: Duration = Duration::from_secs(30);

/// Stream RTP over the same connection until the client tears down or disconnects. Runs entirely
/// on this one thread: frame delivery is paced by this session's `Subscription::recv` timeout, and
/// the control-socket check (keepalive/teardown) rides along on a much coarser timer so it never
/// taxes the frame path (see `CONTROL_CHECK_INTERVAL`). `session` was already seeded with the
/// current keyframe (if any) at subscribe time, so the first `recv` below delivers it -- no
/// separate "send the cached keyframe first" step needed.
fn stream_media(
    stream: &mut TcpStream,
    video: &Subscription,
    audio: Option<&AudioSubscription>,
    mut backchannel: Option<Backchannel>,
    session_id: &str,
) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(2)))?;
    stream.set_write_timeout(Some(PLAY_WRITE_TIMEOUT))?;

    let mut video_seq: u16 = 0;
    let mut audio_seq: u16 = 0;
    let mut ctrl_buf = Vec::new();
    let mut last_control_check = Instant::now();
    let result = (|| -> io::Result<()> {
        loop {
            if let Some(frame) = video.recv(Duration::from_millis(40)) {
                send_access_unit(stream, &frame, &mut video_seq)?;
            }
            if let Some(sub) = audio {
                if let Some(frame) = sub.recv(AUDIO_POLL_TIMEOUT) {
                    send_audio_frame(stream, &frame, &mut audio_seq)?;
                }
            }
            if last_control_check.elapsed() >= CONTROL_CHECK_INTERVAL {
                last_control_check = Instant::now();
                match poll_control(stream, &mut ctrl_buf, session_id, backchannel.as_mut())? {
                    ControlEvent::Teardown => return Ok(()),
                    ControlEvent::None | ControlEvent::Handled => {}
                }
            }
        }
    })();
    if let Some(bc) = backchannel {
        match bc.finish() {
            Ok(stats) => {
                audioout::record_last(stats);
                eprintln!(
                    "kibbled: rtsp backchannel session {session_id} done: {}/{} frame(s) played, {} silence, lag max {}{}",
                    stats.frames_played,
                    stats.frames_written,
                    stats.silence_frames,
                    stats.max_lag_frames,
                    if stats.aborted_call_active { " (aborted: vendor call became active)" } else { "" }
                )
            }
            Err(e) => eprintln!("kibbled: rtsp backchannel session {session_id} error: {e}"),
        }
    }
    result
}

enum ControlEvent {
    None,
    Handled,
    Teardown,
}

/// Non-blocking-ish check for pending data on `stream` (whose read timeout is already set short
/// by the caller), accumulating partial reads in `buf` across calls. Demultiplexes RFC 2326
/// §10.12 interleaved binary frames (`$` + channel + 2-byte length + payload -- the backchannel's
/// incoming RTP, when negotiated) from ordinary RTSP text requests arriving on the same socket: a
/// binary frame on [`BACKCHANNEL_RTP_CHANNEL`] is handed to `backchannel` and never answered with
/// an RTSP status line (anything else, e.g. an RTCP channel, is accepted and discarded -- this
/// server doesn't implement RTCP feedback); a text request gets the usual
/// GET_PARAMETER/TEARDOWN handling.
fn poll_control(
    stream: &mut TcpStream,
    buf: &mut Vec<u8>,
    session_id: &str,
    mut backchannel: Option<&mut Backchannel>,
) -> io::Result<ControlEvent> {
    let mut chunk = [0u8; 4096];
    match stream.read(&mut chunk) {
        Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "peer closed during play")),
        Ok(n) => buf.extend_from_slice(&chunk[..n]),
        Err(e) if e.kind() == io::ErrorKind::WouldBlock || e.kind() == io::ErrorKind::TimedOut => {
            return Ok(ControlEvent::None);
        }
        Err(e) => return Err(e),
    }
    let mut event = ControlEvent::None;
    loop {
        match buf.first() {
            Some(b'$') => {
                if buf.len() < 4 {
                    break; // frame header itself still arriving
                }
                let channel = buf[1];
                let len = u16::from_be_bytes([buf[2], buf[3]]) as usize;
                if buf.len() < 4 + len {
                    break; // payload still arriving
                }
                let framed: Vec<u8> = buf.drain(..4 + len).collect();
                if channel == BACKCHANNEL_RTP_CHANNEL {
                    if let (Some(bc), Some(&pt_byte)) = (backchannel.as_deref_mut(), framed.get(5)) {
                        bc.on_rtp_packet(&framed[4..], pt_byte & 0x7F)?;
                    }
                }
            }
            Some(_) => {
                let Some(head_end) = find_headers_end(buf) else {
                    break; // still accumulating a full RTSP request
                };
                let req = parse_request(&buf[..head_end]);
                buf.drain(..head_end);
                let teardown = req.method.eq_ignore_ascii_case("TEARDOWN");
                write_status(stream, &req.cseq, 200, "OK", &format!("Session: {session_id}\r\n"), "")?;
                event = if teardown { ControlEvent::Teardown } else { ControlEvent::Handled };
                if teardown {
                    break;
                }
            }
            None => break,
        }
    }
    Ok(event)
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
    let mut require = None;
    let mut transport = None;
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            let k = k.trim();
            let v = v.trim().to_string();
            if k.eq_ignore_ascii_case("CSeq") {
                cseq = v;
            } else if k.eq_ignore_ascii_case("Require") {
                require = Some(v);
            } else if k.eq_ignore_ascii_case("Transport") {
                transport = Some(v);
            }
        }
    }
    RtspRequest { method, url, cseq, require, transport }
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
        let pkt = build_rtp_packet(PAYLOAD_TYPE, frag, *seq, rtp_ts, SSRC, marker);
        *seq = seq.wrapping_add(1);
        stream.write_all(&interleaved_frame(VIDEO_RTP_CHANNEL, &pkt))?;
    }
    Ok(())
}

/// Sends one AAC access unit from the ring's mic-audio channel as a single RFC 3640 RTP packet
/// (`docs/23-audio-codec.md` §7.1: one complete 1024-sample AU always fits in one packet at this
/// bitrate, so unlike video there is no fragmentation case to handle). `frame.data` is the ring's
/// own payload -- ADTS-framed, `ring::AudioFrame`'s own doc comment -- so the 7-byte ADTS header
/// is stripped first: RFC 3640 carries the raw access unit plus its own AU-header section
/// (`rfc3640.rs`), not ADTS framing.
fn send_audio_frame(stream: &mut TcpStream, frame: &ring::AudioFrame, seq: &mut u16) -> io::Result<()> {
    let Some(header) = adts::parse(&frame.data) else { return Ok(()) }; // malformed; drop, don't kill the stream
    let raw_aac = &frame.data[header.header_len..];
    let au_size = raw_aac.len().min(rfc3640::MAX_AU_SIZE as usize) as u16;
    let au_header = rfc3640::au_header_section(au_size);
    let mut payload = Vec::with_capacity(au_header.len() + raw_aac.len());
    payload.extend_from_slice(&au_header);
    payload.extend_from_slice(raw_aac);
    let rtp_ts = (frame.pts_us as u64 * 16_000 / 1_000_000) as u32; // RFC 3640: clock rate = sample rate
    let pkt = build_rtp_packet(AUDIO_PAYLOAD_TYPE, &payload, *seq, rtp_ts, AUDIO_SSRC, true);
    *seq = seq.wrapping_add(1);
    stream.write_all(&interleaved_frame(AUDIO_RTP_CHANNEL, &pkt))
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

fn build_rtp_packet(payload_type: u8, payload: &[u8], seq: u16, rtp_ts: u32, ssrc: u32, marker: bool) -> Vec<u8> {
    let mut pkt = Vec::with_capacity(12 + payload.len());
    pkt.push(0x80); // V=2, P=0, X=0, CC=0
    pkt.push((if marker { 0x80 } else { 0 }) | payload_type);
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
        let pkt = build_rtp_packet(PAYLOAD_TYPE, &[1, 2, 3], 0x1234, 0x89AB_CDEF, 0xDEAD_BEEF, true);
        assert_eq!(pkt[0], 0x80, "V=2,P=0,X=0,CC=0");
        assert_eq!(pkt[1], 0x80 | PAYLOAD_TYPE, "marker set, PT=96");
        assert_eq!(&pkt[2..4], &0x1234u16.to_be_bytes());
        assert_eq!(&pkt[4..8], &0x89AB_CDEFu32.to_be_bytes());
        assert_eq!(&pkt[8..12], &0xDEAD_BEEFu32.to_be_bytes());
        assert_eq!(&pkt[12..], &[1, 2, 3]);
    }

    #[test]
    fn build_rtp_packet_clears_marker_bit_when_not_set() {
        let pkt = build_rtp_packet(PAYLOAD_TYPE, &[], 0, 0, 0, false);
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
        let sdp = build_sdp("rtsp://host:8554/sub", &sps, &pps, false);
        assert!(sdp.contains("profile-level-id=42001f"));
        assert!(sdp.contains(&format!(
            "sprop-parameter-sets={},{}",
            base64_encode(&sps),
            base64_encode(&pps)
        )));
        assert!(sdp.contains("a=rtpmap:96 H264/90000"));
        assert!(sdp.contains("a=control:rtsp://host:8554/sub/trackID=0"));
        assert!(sdp.contains("a=rtpmap:97 MPEG4-GENERIC/16000/1"), "audio track always offered");
        assert!(sdp.contains("a=control:rtsp://host:8554/sub/trackID=1"));
        assert!(sdp.contains("config=1408"), "AudioSpecificConfig from adts::AUDIO_SPECIFIC_CONFIG");
        assert!(!sdp.contains("trackID=2"), "backchannel not offered unless requested");
    }

    #[test]
    fn build_sdp_offers_backchannel_only_when_requested() {
        let sdp = build_sdp("rtsp://host:8554/sub", &[0x67, 0, 0, 0], &[0x68], true);
        assert!(sdp.contains("a=control:rtsp://host:8554/sub/trackID=2"));
        assert!(sdp.contains("m=audio 0 RTP/AVP 98 0 8"), "L16/16000 offered first, G.711 as baseline");
        assert!(sdp.contains("a=rtpmap:98 L16/16000"));
        assert!(sdp.contains("a=rtpmap:0 PCMU/8000"));
        assert!(sdp.contains("a=sendonly"));
    }

    #[test]
    fn session_ids_are_unique_and_tagged() {
        let a = new_session_id();
        let b = new_session_id();
        assert_ne!(a, b);
        assert!(a.starts_with("kibble"));
        assert!(b.starts_with("kibble"));
    }

    #[test]
    fn url_names_segment_matches_a_whole_path_segment_case_insensitively() {
        assert!(url_names_segment("rtsp://host:8554/main", "main"));
        assert!(url_names_segment("rtsp://host:8554/Main/trackID=0", "main"));
        assert!(!url_names_segment("rtsp://host:8554/sub", "main"));
        assert!(!url_names_segment("rtsp://host:8554/mainstream", "main"), "must match a whole segment, not a substring");
    }

    fn test_feeds() -> Feeds {
        Feeds {
            main: VideoFeed::new(),
            sub: VideoFeed::new(),
            audio: AudioFeed::new(),
            speaker_owner: audioout::SpeakerOwner::new(),
        }
    }

    #[test]
    fn feeds_select_defaults_to_sub_and_recognizes_main() {
        let feeds = test_feeds();
        assert_eq!(feeds.select("rtsp://host:8554/main").0, "main");
        assert_eq!(feeds.select("rtsp://host:8554/sub").0, "sub");
        assert_eq!(feeds.select("rtsp://host:8554/").0, "sub", "unnamed mount defaults to sub");
        assert_eq!(feeds.select("rtsp://host:8554/main/trackID=0").0, "main");
    }

    #[test]
    fn streams_json_reports_each_mount_independently() {
        let feeds = test_feeds();
        let (_s, _n) = feeds.sub.subscribe("1.2.3.4:9".into(), 2, 3).unwrap();
        let body = streams_json(&feeds);
        assert!(body.starts_with(r#"{"main":{"#));
        assert!(body.contains(r#""session_count":0"#), "main mount has no sessions");
        assert!(body.contains(r#""session_count":1"#), "sub mount has the one subscribed session");
        assert!(body.contains("\"1.2.3.4:9\""));
    }
}
