//! A from-scratch MD5 implementation (RFC 1321).
//!
//! `/opt/user.conf` is persisted by the vendor as a 32-character lowercase-hex MD5 digest of the
//! content, followed by the content itself (`STUDY-settings-write.md` §4: `MD5_Init`/
//! `MD5_Update`/`MD5_Final`, then a nibble-split hex-encode loop at `ctrl` 0x83370-0x83396).
//! Kept inline instead of pulling in a crate — kibbled has zero dependencies, and hashing a
//! ~2.5 KB buffer once per settings write costs nothing.

/// Per-round left-rotate amounts.
const S: [u32; 64] = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, //
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, //
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, //
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
];

/// `K[i] = floor(abs(sin(i + 1)) * 2^32)`, precomputed so this file needs no float/libm dependency.
const K: [u32; 64] = [
    0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, //
    0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501, //
    0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, //
    0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821, //
    0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, //
    0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8, //
    0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, //
    0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a, //
    0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, //
    0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70, //
    0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, //
    0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665, //
    0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, //
    0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1, //
    0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, //
    0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391,
];

/// The 16-byte MD5 digest of `input`.
pub fn digest(input: &[u8]) -> [u8; 16] {
    let mut a0: u32 = 0x67452301;
    let mut b0: u32 = 0xefcdab89;
    let mut c0: u32 = 0x98badcfe;
    let mut d0: u32 = 0x10325476;

    // Padding: one 0x80 bit-marker byte, zeros up to a 56-mod-64 length, then the original
    // bit-length as a little-endian u64.
    let bit_len = (input.len() as u64).wrapping_mul(8);
    let mut msg = Vec::with_capacity(input.len() + 72);
    msg.extend_from_slice(input);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_le_bytes());

    for chunk in msg.chunks_exact(64) {
        let mut m = [0u32; 16];
        for (i, word) in chunk.chunks_exact(4).enumerate() {
            m[i] = u32::from_le_bytes(word.try_into().unwrap());
        }

        let (mut a, mut b, mut c, mut d) = (a0, b0, c0, d0);
        for (i, (&k, &s)) in K.iter().zip(S.iter()).enumerate() {
            let (f, g) = match i {
                0..=15 => ((b & c) | (!b & d), i),
                16..=31 => ((d & b) | (!d & c), (5 * i + 1) % 16),
                32..=47 => (b ^ c ^ d, (3 * i + 5) % 16),
                _ => (c ^ (b | !d), (7 * i) % 16),
            };
            let f = f
                .wrapping_add(a)
                .wrapping_add(k)
                .wrapping_add(m[g]);
            a = d;
            d = c;
            c = b;
            b = b.wrapping_add(f.rotate_left(s));
        }

        a0 = a0.wrapping_add(a);
        b0 = b0.wrapping_add(b);
        c0 = c0.wrapping_add(c);
        d0 = d0.wrapping_add(d);
    }

    let mut out = [0u8; 16];
    out[0..4].copy_from_slice(&a0.to_le_bytes());
    out[4..8].copy_from_slice(&b0.to_le_bytes());
    out[8..12].copy_from_slice(&c0.to_le_bytes());
    out[12..16].copy_from_slice(&d0.to_le_bytes());
    out
}

/// Lowercase 32-character hex encoding of `digest(input)`, matching `ctrl`'s own header format.
pub fn hex(input: &[u8]) -> String {
    let d = digest(input);
    let mut s = String::with_capacity(32);
    for b in d {
        s.push(nibble(b >> 4));
        s.push(nibble(b & 0xf));
    }
    s
}

fn nibble(n: u8) -> char {
    (if n < 10 { b'0' + n } else { b'a' + (n - 10) }) as char
}

#[cfg(test)]
mod tests {
    use super::*;

    // RFC 1321 §A.5 test suite.
    #[test]
    fn empty_string_vector() {
        assert_eq!(hex(b""), "d41d8cd98f00b204e9800998ecf8427e");
    }

    #[test]
    fn abc_vector() {
        assert_eq!(hex(b"abc"), "900150983cd24fb0d6963f7d28e17f72");
    }

    #[test]
    fn message_digest_vector() {
        assert_eq!(hex(b"message digest"), "f96b697d7cb7938d525a2f31aaf161d0");
    }

    #[test]
    fn alphabet_vector() {
        assert_eq!(
            hex(b"abcdefghijklmnopqrstuvwxyz"),
            "c3fcd3d76192e4007dfb496cca67e13b"
        );
    }

    #[test]
    fn alphanumeric_vector() {
        assert_eq!(
            hex(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"),
            "d174ab98d277d9f5a5611c2c9f419d9f"
        );
    }

    #[test]
    fn long_digits_vector() {
        assert_eq!(
            hex(b"12345678901234567890123456789012345678901234567890123456789012345678901234567890"),
            "57edf4a22be3c955ac49da2e2107b67a"
        );
    }

    /// A ~2.5 KB buffer of repeated bytes, the size a real `/opt/user.conf` content section is —
    /// spans multiple 64-byte blocks, unlike the short RFC vectors above. Expected digest computed
    /// independently with Python's `hashlib.md5`, not derived from this implementation.
    #[test]
    fn multi_block_input_vector() {
        let input = vec![0x42u8; 2600];
        assert_eq!(hex(&input), "95cebcf06b0df9551b9f8287f7289b91");
    }

    /// A second multi-block vector built from readable text rather than a repeated byte, so a bug
    /// that only manifests for varied block content wouldn't hide behind the vector above.
    #[test]
    fn multi_block_text_vector() {
        let input = "user.conf test content ".repeat(50);
        assert_eq!(hex(input.as_bytes()), "a76d3e54b540b22b5246818cebbe3271");
    }
}
