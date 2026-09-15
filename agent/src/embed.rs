//! Face-crop -> 512-float embedding, via the proven second-process NPU path.
//!
//! `docs/18-npu-confirmed.md` proves a second, independent process can call `AX_ENGINE_*`
//! (`AX_SYS_Init` -> `AX_ENGINE_Init` -> `CreateHandle` -> `GetIOInfo` -> `RunSync` -> teardown)
//! against the vendor's own frozen face-recognition model while `media` keeps its own eight
//! engine handles open: no crash, no leak, `media`/`ble`/`ctrl`/`watchdog`/`agora`/`cloud` all
//! continuously running throughout. Live-confirmed shapes: input `[1,224,224,3]` UINT8 NHWC,
//! output `feat[1,512]` FLOAT32 (the embedding) + `prob[1]` FLOAT32.
//!
//! `kibbled` itself is a statically-linked musl binary; the device's `libax_engine.so`/
//! `libax_sys.so` are glibc shared objects meant to be dynamically linked (exactly how
//! `docs/18-npu-confirmed.md`'s probe and `tools/kibble-npu.c`/`tools/kibble-embed.c` build) --
//! two incompatible worlds that cannot be linked into one binary. So this module does not call
//! the NPU itself: it shells out to `tools/kibble-embed.c`, a separate, small, dynamically-linked
//! ARM/glibc executable deployed at [`HELPER_PATH`] (build instructions: `tools/README.md`) that
//! *is* that second process, one invocation per crop. That keeps `kibbled`'s own Cargo.toml at
//! zero dependencies -- this module only ever touches `std::process::Command` -- while still
//! reusing the exact call sequence and ABI the NPU investigation validated live.
//!
//! The helper prints nothing but two things on success: exactly [`RAW_OUTPUT_LEN`] bytes on
//! stdout ([`EMBED_DIM`] little-endian `f32`s, then one more for the model's own `prob` scalar),
//! exit code 0. Anything else -- nonzero exit, wrong-length output -- is a real failure, surfaced
//! as [`EmbedError`] rather than guessed at.

use std::io;
use std::path::Path;
use std::process::Command;

pub const EMBED_DIM: usize = 512;
/// `EMBED_DIM` little-endian `f32`s (the `feat` embedding) followed by one more (the `prob`
/// scalar) -- the exact byte shape `tools/kibble-embed.c` writes to stdout.
pub const RAW_OUTPUT_LEN: usize = (EMBED_DIM + 1) * 4;

/// Real device path of the vendor's face-recognition model -- read-only input, never modified.
/// Confirmed present and this exact shape live: `docs/18-npu-confirmed.md` §1.
pub const MODEL_PATH: &str = "/alg/petkit_face_rec_mtl_s2_v5_sim.axmodel";
/// Where the cross-compiled second-process helper (`tools/kibble-embed.c`) is deployed --
/// alongside `kibbled` itself, under the one directory this project ever writes to.
pub const HELPER_PATH: &str = "/opt/kibble/kibble-embed";

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Embedding {
    pub feat: [f32; EMBED_DIM],
    /// The model's own second output -- "almost certainly a face-quality/liveness gate"
    /// (`docs/12-ai.md` §3.1), not part of the identity vector. Carried through for callers that
    /// want it (diagnostics); the classifier (`catid.rs`) never reads it.
    pub prob: f32,
}

#[derive(Debug)]
pub enum EmbedError {
    /// Couldn't even start the helper process (missing binary, permissions, ...).
    Spawn(io::Error),
    /// The helper ran but exited nonzero; `stderr` is whatever it printed, truncated is fine --
    /// this is for a log line, not machine parsing.
    ExitStatus { code: Option<i32>, stderr: String },
    /// The helper exited 0 but stdout wasn't exactly [`RAW_OUTPUT_LEN`] bytes -- a version
    /// mismatch between `kibbled` and the deployed helper, or a truncated pipe. Never silently
    /// truncated/padded; always a hard error.
    BadOutputLen(usize),
}

impl std::fmt::Display for EmbedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            EmbedError::Spawn(e) => write!(f, "spawn {HELPER_PATH}: {e}"),
            EmbedError::ExitStatus { code, stderr } => {
                write!(f, "{HELPER_PATH} exited {code:?}: {}", stderr.trim())
            }
            EmbedError::BadOutputLen(n) => {
                write!(f, "{HELPER_PATH} wrote {n} bytes, expected {RAW_OUTPUT_LEN}")
            }
        }
    }
}

/// Decodes the helper's raw stdout into an [`Embedding`]. Pure and independent of the process
/// spawn, so it's directly unit-testable without a real helper binary or NPU.
fn parse_output(bytes: &[u8]) -> Result<Embedding, EmbedError> {
    if bytes.len() != RAW_OUTPUT_LEN {
        return Err(EmbedError::BadOutputLen(bytes.len()));
    }
    let mut feat = [0.0f32; EMBED_DIM];
    for (i, chunk) in bytes[..EMBED_DIM * 4].chunks_exact(4).enumerate() {
        feat[i] = f32::from_le_bytes(chunk.try_into().expect("chunks_exact(4)"));
    }
    let prob = f32::from_le_bytes(bytes[EMBED_DIM * 4..].try_into().expect("last 4 bytes"));
    Ok(Embedding { feat, prob })
}

/// Extracts the embedding for one face crop, using the real deployed helper and model path.
pub fn extract(jpeg_path: &Path) -> Result<Embedding, EmbedError> {
    extract_with(Path::new(HELPER_PATH), Path::new(MODEL_PATH), jpeg_path)
}

fn extract_with(helper: &Path, model: &Path, jpeg_path: &Path) -> Result<Embedding, EmbedError> {
    // `LD_LIBRARY_PATH=/soc/lib` matches the exact invocation `docs/18-npu-confirmed.md` proved
    // works (the helper's own `-Wl,-rpath,/soc/lib` link-time flag should already cover this;
    // setting it here too is the same belt-and-suspenders that session's own probe run used).
    let output = Command::new(helper)
        .arg(model)
        .arg(jpeg_path)
        .env("LD_LIBRARY_PATH", "/soc/lib")
        .output()
        .map_err(EmbedError::Spawn)?;
    if !output.status.success() {
        return Err(EmbedError::ExitStatus {
            code: output.status.code(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        });
    }
    parse_output(&output.stdout)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::{AtomicU32, Ordering};

    static COUNTER: AtomicU32 = AtomicU32::new(0);

    /// Writes a tiny `/bin/sh` script standing in for `kibble-embed` and returns its path --
    /// these tests exercise `kibbled`'s own subprocess/parsing plumbing on the host (any Linux
    /// box, no ARM/NPU/musl involved), not the real helper binary or a live model.
    fn script(tag: &str, body: &str) -> std::path::PathBuf {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("kibble-embed-test-{tag}-{}-{n}.sh", std::process::id()));
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn raw_bytes(feat: [f32; EMBED_DIM], prob: f32) -> Vec<u8> {
        let mut out = Vec::with_capacity(RAW_OUTPUT_LEN);
        for f in feat {
            out.extend_from_slice(&f.to_le_bytes());
        }
        out.extend_from_slice(&prob.to_le_bytes());
        out
    }

    #[test]
    fn parse_output_decodes_exact_length_input_bit_for_bit() {
        let mut feat = [0.0f32; EMBED_DIM];
        feat[0] = 1.5;
        feat[511] = -3.25;
        let bytes = raw_bytes(feat, 0.987);
        let decoded = parse_output(&bytes).unwrap();
        assert_eq!(decoded.feat[0], 1.5);
        assert_eq!(decoded.feat[511], -3.25);
        assert_eq!(decoded.prob, 0.987);
    }

    #[test]
    fn parse_output_rejects_wrong_length() {
        match parse_output(&[0u8; 10]) {
            Err(EmbedError::BadOutputLen(10)) => {}
            other => panic!("expected BadOutputLen(10), got {other:?}"),
        }
    }

    #[test]
    fn extract_with_a_missing_helper_binary_is_a_spawn_error() {
        let helper = Path::new("/nonexistent/kibble-embed-does-not-exist");
        match extract_with(helper, Path::new("model"), Path::new("crop.jpg")) {
            Err(EmbedError::Spawn(_)) => {}
            other => panic!("expected Spawn error, got {other:?}"),
        }
    }

    #[test]
    fn extract_with_a_nonzero_exit_reports_exit_status_and_stderr() {
        let helper = script("exit-nonzero", "echo 'boom' >&2\nexit 3\n");
        let result = extract_with(&helper, Path::new("model"), Path::new("crop.jpg"));
        fs::remove_file(&helper).ok();
        match result {
            Err(EmbedError::ExitStatus { code: Some(3), stderr }) => {
                assert!(stderr.contains("boom"));
            }
            other => panic!("expected ExitStatus{{code:3}}, got {other:?}"),
        }
    }

    #[test]
    fn extract_with_wrong_length_stdout_is_a_bad_output_error() {
        let helper = script("bad-len", "printf 'not enough bytes'\n");
        let result = extract_with(&helper, Path::new("model"), Path::new("crop.jpg"));
        fs::remove_file(&helper).ok();
        match result {
            Err(EmbedError::BadOutputLen(n)) => assert_eq!(n, "not enough bytes".len()),
            other => panic!("expected BadOutputLen, got {other:?}"),
        }
    }

    #[test]
    fn extract_with_a_well_formed_helper_decodes_the_exact_floats() {
        let mut feat = [0.0f32; EMBED_DIM];
        feat[3] = 42.0;
        let bytes = raw_bytes(feat, 0.5);
        // od turns the raw bytes into `\xHH`-escaped octal-dump form that `printf` can emit
        // byte-for-byte from a POSIX shell with no helper language (python/perl) required.
        let escaped: String = bytes.iter().map(|b| format!("\\{b:03o}")).collect();
        let helper = script("ok", &format!("printf '{escaped}'\n"));
        let result = extract_with(&helper, Path::new("model"), Path::new("crop.jpg")).unwrap();
        fs::remove_file(&helper).ok();
        assert_eq!(result.feat[3], 42.0);
        assert_eq!(result.prob, 0.5);
    }

    #[test]
    fn extract_with_passes_model_and_crop_paths_as_the_two_arguments() {
        // `$1`/`$2` echoed back through stdout, then padded/truncated by the assertion logic
        // below to prove argv wiring without needing well-formed embedding bytes for this case.
        let helper = script("argv", "printf '%s|%s' \"$1\" \"$2\" 1>&2\nexit 9\n");
        let result = extract_with(&helper, Path::new("/alg/model.axmodel"), Path::new("/opt/kibble/faces/pending/1.jpg"));
        fs::remove_file(&helper).ok();
        match result {
            Err(EmbedError::ExitStatus { stderr, .. }) => {
                assert_eq!(stderr, "/alg/model.axmodel|/opt/kibble/faces/pending/1.jpg");
            }
            other => panic!("expected ExitStatus carrying argv echo, got {other:?}"),
        }
    }
}
