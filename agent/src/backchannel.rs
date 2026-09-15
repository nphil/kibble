//! RTSP backchannel: decodes RTP-framed G.711 (PCMU/PCMA) arriving from a client (Scrypted's
//! ONVIF intercom, or HA's go2rtc-mediated return path -- `docs/20-two-way-audio.md`), upsamples
//! it to the ring's native 16kHz format (`g711.rs`), and hands it to `audioout.rs`'s live speaker
//! session. `rtsp.rs` owns the RTSP/RTP framing and the interleaved-channel demultiplexing; this
//! module only knows about RTP payloads and PCM.

use std::sync::Arc;

use crate::audioout::{LiveSession, OwnerGuard, PlaybackStats, SpeakError};
use crate::g711;
use crate::ring::TailCursor;

/// RTP payload type values this backchannel accepts (`docs/23-audio-codec.md` §7.2): PCMU is the
/// mandatory baseline every consumer (Scrypted, go2rtc) offers; PCMA is accepted too since it
/// costs nothing extra once PCMU's decode path exists.
pub const PT_PCMU: u8 = 0;
pub const PT_PCMA: u8 = 8;

/// A live backchannel session bound to one RTSP PLAY: holds the speaker for its whole lifetime
/// (via the [`OwnerGuard`] it was started with) and turns each incoming RTP packet into PCM fed
/// straight to the encoder.
pub struct Backchannel {
    live: LiveSession,
}

impl Backchannel {
    pub fn start(tail: Arc<TailCursor>, owner: OwnerGuard) -> Result<Self, SpeakError> {
        Ok(Self { live: LiveSession::start(tail, owner)? })
    }

    /// Strips the RTP header (accounting for CSRC entries so a compliant sender using them
    /// doesn't desync every packet after), decodes the G.711 payload per `payload_type`,
    /// upsamples 8kHz -> 16kHz, and feeds the result to the encoder. Silently drops -- not an
    /// error -- anything shorter than a bare RTP header or carrying a payload type this
    /// backchannel didn't negotiate: an occasional malformed or stray packet on a live network
    /// path shouldn't tear down the whole call.
    pub fn on_rtp_packet(&mut self, packet: &[u8], payload_type: u8) -> std::io::Result<()> {
        let Some(payload) = rtp_payload(packet) else { return Ok(()) };
        let decode: fn(u8) -> i16 = match payload_type {
            PT_PCMU => g711::ulaw_decode,
            PT_PCMA => g711::alaw_decode,
            _ => return Ok(()),
        };
        if payload.is_empty() {
            return Ok(());
        }
        let samples_8k: Vec<i16> = payload.iter().map(|&b| decode(b)).collect();
        let samples_16k = g711::upsample_2x_linear(&samples_8k);
        self.live.feed(&samples_16k)
    }

    pub fn finish(self) -> Result<PlaybackStats, SpeakError> {
        self.live.finish()
    }
}

/// Returns the payload bytes after a standard RTP header, accounting for `CC` CSRC entries
/// (header + `4*CC` more bytes) if present. `None` if the packet is too short to even hold a
/// bare 12-byte header, or too short for the CSRC count it claims.
fn rtp_payload(packet: &[u8]) -> Option<&[u8]> {
    if packet.len() < 12 {
        return None;
    }
    let cc = (packet[0] & 0x0F) as usize;
    let header_len = 12 + cc * 4;
    packet.get(header_len..)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rtp_packet(cc: u8, payload: &[u8]) -> Vec<u8> {
        let mut p = vec![0x80 | cc, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
        p.extend(std::iter::repeat(0u8).take(cc as usize * 4));
        p.extend_from_slice(payload);
        p
    }

    #[test]
    fn rtp_payload_strips_the_bare_12_byte_header() {
        let packet = rtp_packet(0, &[0xFF, 0x7F, 0xD5]);
        assert_eq!(rtp_payload(&packet), Some(&[0xFFu8, 0x7F, 0xD5][..]));
    }

    #[test]
    fn rtp_payload_accounts_for_csrc_entries() {
        let packet = rtp_packet(2, &[0x55]);
        assert_eq!(rtp_payload(&packet), Some(&[0x55u8][..]));
    }

    #[test]
    fn rtp_payload_rejects_a_packet_shorter_than_the_bare_header() {
        assert_eq!(rtp_payload(&[0u8; 11]), None);
    }

    #[test]
    fn rtp_payload_rejects_a_packet_too_short_for_its_own_csrc_count() {
        // Claims 3 CSRC entries (12 more bytes) but the packet doesn't have them.
        assert_eq!(rtp_payload(&rtp_packet(0, &[])[..12]), Some(&[][..]));
        assert_eq!(rtp_payload(&[0x83u8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]), None);
    }
}
