//! RFC 3640 (`MPEG4-GENERIC` / AAC-hbr) AU-header section construction for the outgoing audio
//! RTP track (`rtsp.rs`).
//!
//! Each RTP packet carries exactly one AAC access unit -- the ring already hands us one per
//! record (`docs/23-audio-codec.md` §3) -- so per RFC 3640 §3.3.6 the AU-header section is
//! always the same fixed shape: a 2-byte `AU-headers-length` field giving the length of the
//! headers that follow *in bits* (one 16-bit AU-header = `16`), then that one AU-header itself:
//! a 13-bit `AU-size` (this access unit's byte length) followed by a 3-bit `AU-Index` (always
//! `0`, the "first and only" access unit in this packet -- RFC 3640 §3.3.6 defines `AU-Index` as
//! `0` for the first AU and `AU-Index-delta` for subsequent ones in the same packet; we only
//! ever put one AU per packet, so there is no delta field to add).

/// Largest AU size the 13-bit `AU-size` field can represent.
pub const MAX_AU_SIZE: u16 = 0x1FFF;

/// Build the 4-byte AU-header section for one AAC access unit of `size` bytes: `[0x00, 0x10]`
/// (AU-headers-length = 16 bits) followed by the 2-byte AU-header itself (13-bit size, 3-bit
/// index = 0). `size` is silently truncated to 13 bits if it somehow exceeds [`MAX_AU_SIZE`] --
/// AAC-LC/16kHz/mono access units from this ring are on the order of a few hundred bytes
/// (`docs/23-audio-codec.md` §4), nowhere near the 8191-byte ceiling this would ever matter for.
pub fn au_header_section(size: u16) -> [u8; 4] {
    debug_assert!(size <= MAX_AU_SIZE, "AU size {size} exceeds the 13-bit AU-size field");
    let au_size = size & MAX_AU_SIZE;
    let au_header: u16 = au_size << 3; // 13-bit size, then 3-bit AU-Index = 0
    let headers_length_bits: u16 = 16;
    [
        (headers_length_bits >> 8) as u8,
        (headers_length_bits & 0xFF) as u8,
        (au_header >> 8) as u8,
        (au_header & 0xFF) as u8,
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_au_header_for_a_300_byte_access_unit() {
        // AU-headers-length = 16 (0x0010); AU-header = (300 << 3) | 0 = 2400 = 0x0960.
        assert_eq!(au_header_section(300), [0x00, 0x10, 0x09, 0x60]);
    }

    #[test]
    fn builds_au_header_for_a_real_capture_sized_access_unit() {
        // 266 bytes -- the exact size of the real captured ADTS frame in adts.rs's tests.
        // AU-header = (266 << 3) | 0 = 2128 = 0x0850.
        assert_eq!(au_header_section(266), [0x00, 0x10, 0x08, 0x50]);
    }

    #[test]
    fn round_trips_size_through_the_13_bit_field() {
        let hdr = au_header_section(1);
        let au_header = u16::from_be_bytes([hdr[2], hdr[3]]);
        assert_eq!(au_header >> 3, 1);
        assert_eq!(au_header & 0x7, 0); // AU-Index always 0
    }

    #[test]
    fn headers_length_field_is_always_16_bits() {
        for size in [0u16, 1, 266, MAX_AU_SIZE] {
            let hdr = au_header_section(size);
            assert_eq!(u16::from_be_bytes([hdr[0], hdr[1]]), 16);
        }
    }
}
