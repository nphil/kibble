//! ADTS (Audio Data Transport Stream) header parsing for the ring's `chan=1` mic-audio records.
//!
//! `docs/23-audio-codec.md` pins the mic audio as MPEG-4 AAC-LC, 16 kHz, mono, one complete
//! 1024-sample access unit per ring record, wrapped in a standard 7-byte ADTS header
//! (`protection_absent=1`, no CRC) -- confirmed by disassembling `media`'s FDK-AAC
//! `aacEncoder_SetParam` calls and by a clean `ffmpeg`/`ffprobe` decode of real captures.
//!
//! `rtsp.rs` uses this to strip the ADTS header before building an RFC 3640 RTP payload (the AAC
//! bytes on the wire are the raw access unit, not ADTS-framed -- `config` in the SDP `fmtp`
//! carries the codec parameters instead) and to sanity-check that the ring's own `length` field
//! agrees with what the AAC bitstream itself declares before trusting the record.

/// MPEG-4 `AudioSpecificConfig` for this exact stream (AAC-LC / 16000 Hz / mono), derived two
/// independent ways in `docs/23-audio-codec.md` §4.1 (from-first-principles bit-packing, and
/// extracted byte-for-byte from an `ffmpeg`-remuxed `esds` box) -- the `config=1408` fmtp value
/// every RTSP session advertises for the outgoing audio track.
pub const AUDIO_SPECIFIC_CONFIG: [u8; 2] = [0x14, 0x08];

/// MPEG-4 Audio Object Type 2 = AAC-LC (the ADTS `profile` field stores `AOT - 1`).
const AOT_AAC_LC: u8 = 2;

/// A parsed, structurally validated ADTS fixed+variable header.
#[derive(Debug, PartialEq, Eq)]
pub struct AdtsHeader {
    /// MPEG-4 Audio Object Type (already `profile + 1`; `2` = AAC-LC).
    pub audio_object_type: u8,
    pub sample_rate: u32,
    pub channels: u8,
    /// Whether a 2-byte CRC follows the header (`protection_absent == 0`). Every mic-audio
    /// record observed on this device has `protection_absent = 1` (no CRC, 7-byte header), but
    /// both forms are parsed correctly rather than assuming it.
    pub protection_absent: bool,
    /// Total header length in bytes: 7 without a CRC, 9 with one.
    pub header_len: usize,
    /// `aac_frame_length` from the header: header bytes *plus* the AAC payload that follows,
    /// i.e. the whole ADTS frame -- what a reader should expect the containing ring record's
    /// `length` field to equal.
    pub frame_length: u16,
}

const SAMPLE_RATES: [u32; 13] = [
    96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350,
];

/// Parse and structurally validate an ADTS header at the start of `buf`. Returns `None` on a bad
/// syncword, a reserved sampling-frequency index, a frame length that doesn't even fit the
/// header it claims, or a buffer shorter than the header itself.
pub fn parse(buf: &[u8]) -> Option<AdtsHeader> {
    if buf.len() < 7 {
        return None;
    }
    // 12-bit syncword: all of byte 0, and the top 4 bits of byte 1.
    if buf[0] != 0xFF || (buf[1] & 0xF0) != 0xF0 {
        return None;
    }
    let protection_absent = buf[1] & 0x01 != 0;
    let header_len = if protection_absent { 7 } else { 9 };
    if buf.len() < header_len {
        return None;
    }
    let profile = (buf[2] >> 6) & 0x03;
    let audio_object_type = profile + 1;
    let sample_rate_index = (buf[2] >> 2) & 0x0F;
    let sample_rate = *SAMPLE_RATES.get(sample_rate_index as usize)?;
    let channels = ((buf[2] & 0x01) << 2) | ((buf[3] >> 6) & 0x03);
    let frame_length = ((buf[3] as u16 & 0x03) << 11) | ((buf[4] as u16) << 3) | ((buf[5] as u16) >> 5);
    if (frame_length as usize) < header_len {
        return None;
    }
    Some(AdtsHeader { audio_object_type, sample_rate, channels, protection_absent, header_len, frame_length })
}

/// Whether a parsed header matches this device's mic-audio stream exactly (AAC-LC, 16 kHz,
/// mono) -- the check `rtsp.rs` runs before trusting a ring record's payload as an RTP-ready AAC
/// access unit.
pub fn is_kibble_mic_stream(h: &AdtsHeader) -> bool {
    h.audio_object_type == AOT_AAC_LC && h.sample_rate == 16000 && h.channels == 1
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A real 7-byte ADTS header, byte-for-byte the first packet published in
    /// `docs/23-audio-codec.md` §3 (`"0 266B fff16040215ffc..."`, one of 95/95 captured
    /// mic-audio (`chan=1`) records whose ADTS `frame_length` field was independently checked
    /// against the real ring record length for every packet): syncword `FFF`, profile `01`
    /// (LC), sampling-freq-index `1000` (16000 Hz), channel config `0001` (mono),
    /// `protection_absent=1`. That packet is 266 bytes total.
    const REAL_HEADER: [u8; 7] = [0xff, 0xf1, 0x60, 0x40, 0x21, 0x5f, 0xfc];

    #[test]
    fn parses_real_captured_header() {
        let h = parse(&REAL_HEADER).expect("real capture must parse");
        assert_eq!(h.audio_object_type, AOT_AAC_LC);
        assert_eq!(h.sample_rate, 16000);
        assert_eq!(h.channels, 1);
        assert!(h.protection_absent);
        assert_eq!(h.header_len, 7);
        assert_eq!(h.frame_length, 266);
        assert!(is_kibble_mic_stream(&h));
    }

    #[test]
    fn parses_a_second_real_captured_header_with_a_different_frame_length() {
        // The very next packet in the same docs/23-audio-codec.md §3 capture
        // (`"1 287B fff1604023fffc..."`), same stream parameters, different frame_length.
        let raw = [0xff, 0xf1, 0x60, 0x40, 0x23, 0xff, 0xfc];
        let h = parse(&raw).expect("second real capture must parse");
        assert_eq!(h.frame_length, 287);
        assert!(is_kibble_mic_stream(&h));
    }

    #[test]
    fn rejects_bad_syncword() {
        let mut bad = REAL_HEADER;
        bad[1] = 0x00;
        assert!(parse(&bad).is_none());
    }

    #[test]
    fn rejects_short_buffer() {
        assert!(parse(&REAL_HEADER[..6]).is_none());
    }

    #[test]
    fn rejects_frame_length_shorter_than_its_own_header() {
        // Syncword and protection_absent bit intact, but the 13-bit frame_length field decodes
        // to less than the 7-byte header itself -- structurally impossible for a real frame.
        let bad = [0xff, 0xf1, 0x60, 0x00, 0x00, 0x20, 0xe8];
        assert!(parse(&bad).is_none());
    }

    #[test]
    fn parses_nine_byte_header_when_protection_present() {
        // Same as REAL_HEADER but with the protection_absent bit cleared (CRC present) and two
        // extra CRC bytes appended; frame_length must account for the longer header.
        let mut raw = REAL_HEADER;
        raw[1] &= !0x01;
        let mut buf = raw.to_vec();
        buf.extend_from_slice(&[0x00, 0x00]);
        let h = parse(&buf).expect("9-byte header must parse");
        assert!(!h.protection_absent);
        assert_eq!(h.header_len, 9);
    }
}
