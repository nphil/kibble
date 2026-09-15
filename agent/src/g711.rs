//! G.711 mu-law/A-law decode and simple 2x upsampling, for the RTSP backchannel
//! (`backchannel.rs`): Scrypted's ONVIF intercom and go2rtc both send PCMU (payload type 0)
//! or PCMA (payload type 8) at 8 kHz; the feeder's speaker only accepts 16 kHz/16-bit PCM
//! (`docs/23-audio-codec.md` §6). This module does the decode and the 8kHz->16kHz step;
//! `backchannel.rs` does the RTP unwrapping and the ring write.
//!
//! Decode tables are the standard ITU-T G.711 formulas, byte-identical to the widely deployed
//! public-domain reference implementation (Sun Microsystems' `g711.c`, also reproduced verbatim
//! in SpanDSP's `g711.h`): both undo the *transmission* bit inversion each law applies (one's
//! complement for mu-law, even-bit XOR 0x55 for A-law) before unpacking the `seeemmmm`
//! sign/exponent/mantissa code word. No lookup tables -- each decode is a handful of shifts and
//! masks, cheap enough per-sample and avoids a 512-entry static table for a codec this agent
//! only ever runs at a low, bursty packet rate (one 8 kHz talkback session at a time).

/// Bias added into the exponent/mantissa unpacking before the segment shift, and subtracted
/// back out after -- the standard ITU-T G.711 mu-law constant (0x84 = 132). Produces output
/// scaled to (nearly) full 16-bit range, matching the reference implementation exactly.
const ULAW_BIAS: i32 = 0x84;

/// Decode one G.711 mu-law byte (RTP payload type 0, PCMU) to a signed 16-bit linear sample.
pub fn ulaw_decode(byte: u8) -> i16 {
    // The wire byte is the one's complement of the natural sign+exponent+mantissa code word.
    let u = !byte;
    let sign = u & 0x80;
    let exponent = (u & 0x70) >> 4;
    let mantissa = u & 0x0F;
    let t = (((mantissa as i32) << 3) + ULAW_BIAS) << exponent;
    (if sign != 0 { ULAW_BIAS - t } else { t - ULAW_BIAS }) as i16
}

/// Decode one G.711 A-law byte (RTP payload type 8, PCMA) to a signed 16-bit linear sample.
pub fn alaw_decode(byte: u8) -> i16 {
    // The wire byte has every even-numbered bit (0-indexed from the LSB) inverted; XOR 0x55
    // (01010101) undoes that and recovers the natural sign+exponent+mantissa code word.
    let a = byte ^ 0x55;
    let seg = (a & 0x70) >> 4;
    let mantissa = ((a & 0x0F) as i32) << 4;
    let magnitude = if seg != 0 { (mantissa + 0x108) << (seg - 1) } else { mantissa + 8 };
    (if a & 0x80 != 0 { magnitude } else { -magnitude }) as i16
}

/// Upsample 8 kHz PCM to 16 kHz by linear interpolation: for each input sample, emit the sample
/// itself followed by the midpoint to the next one (or a repeat of itself, for the last sample
/// in the slice -- there is no "next" to interpolate toward). Output is always exactly
/// `2 * input.len()` samples.
///
/// Plain linear interpolation, not a proper sinc/polyphase resampler -- deliberately: this is
/// talkback-grade voice through a small speaker, the doubling only needs to avoid introducing
/// audible aliasing artifacts worse than the codec's own 8 kHz bandwidth limit already implies,
/// and a real resampler would cost more CPU and code for a difference nobody feeding G.711
/// through a pet feeder's speaker will hear.
pub fn upsample_2x_linear(input: &[i16]) -> Vec<i16> {
    let mut out = Vec::with_capacity(input.len() * 2);
    for (i, &s) in input.iter().enumerate() {
        out.push(s);
        let next = input.get(i + 1).copied().unwrap_or(s);
        out.push(midpoint(s, next));
    }
    out
}

fn midpoint(a: i16, b: i16) -> i16 {
    ((a as i32 + b as i32) / 2) as i16
}

#[cfg(test)]
mod tests {
    use super::*;

    // Reference values hand-derived from the ITU-T G.711 formulas and cross-checked byte-exact
    // against SpanDSP's g711.h (public domain, Steve Underwood) `ulaw_to_linear`/
    // `alaw_to_linear` -- the same widely-deployed reference implementation used by Asterisk,
    // FFmpeg and most other open-source telephony codecs.
    #[test]
    fn ulaw_decode_silence_bytes_are_zero() {
        // Both the "positive zero" and "negative zero" mu-law code words decode to exactly 0;
        // 0xFF is the byte a silent mu-law encoder actually transmits.
        assert_eq!(ulaw_decode(0xFF), 0);
        assert_eq!(ulaw_decode(0x7F), 0);
    }

    #[test]
    fn ulaw_decode_matches_itu_reference_values() {
        assert_eq!(ulaw_decode(0x00), -32124); // maximum-magnitude negative code word
        assert_eq!(ulaw_decode(0x3C), -2364); // arbitrary mid-range code word
    }

    #[test]
    fn alaw_decode_matches_itu_reference_values() {
        // 0xD5 is the byte a silent A-law encoder actually transmits (per the standard); A-law's
        // formula never produces exactly zero (a documented property: the +0.5 quantization
        // step), so this is a small nonzero value, not 0.
        assert_eq!(alaw_decode(0xD5), 8);
        assert_eq!(alaw_decode(0x55), -8);
        assert_eq!(alaw_decode(0x2A), -32256); // maximum-magnitude negative code word
        assert_eq!(alaw_decode(0x13), -2880); // arbitrary mid-range code word
    }

    #[test]
    fn alaw_and_ulaw_never_panic_across_every_byte_value() {
        for b in 0..=255u8 {
            let _ = ulaw_decode(b);
            let _ = alaw_decode(b);
        }
    }

    #[test]
    fn upsample_doubles_length_and_interpolates_midpoints() {
        let input: [i16; 3] = [0, 100, 200];
        let out = upsample_2x_linear(&input);
        assert_eq!(out, vec![0, 50, 100, 150, 200, 200]);
    }

    #[test]
    fn upsample_handles_negative_and_single_sample_input() {
        assert_eq!(upsample_2x_linear(&[-100, 100]), vec![-100, 0, 100, 100]);
        assert_eq!(upsample_2x_linear(&[42]), vec![42, 42]);
        assert_eq!(upsample_2x_linear(&[]), Vec::<i16>::new());
    }
}
