//! On-device clip storage for `/clips/*` (`main.rs`): each stored clip is pre-normalized,
//! pre-encoded ADTS AAC (concatenated access units, a valid standalone `.aac` elementary stream)
//! at `/opt/kibble/clips/<name>.aac` -- encoding once at save time rather than on every play saves
//! both the `aacenc` subprocess spawn and the RMS/encode CPU cost on each repeat playback, and an
//! encoded clip is roughly 7-8x smaller than the raw PCM it came from (AAC-LC's ~12-35kbps vs raw
//! 16kHz/16-bit PCM's 256kbps), which matters on a device with a small writable `/opt` volume.

use std::fs;
use std::io;
use std::path::PathBuf;

pub const CLIPS_DIR: &str = "/opt/kibble/clips";

const MAX_NAME_LEN: usize = 64;

/// One stored clip's metadata for `GET /clips`.
pub struct ClipInfo {
    pub name: String,
    pub bytes: u64,
}

/// Rejects anything that isn't a single plain path segment -- no `/`, no `..`, nothing that could
/// escape [`CLIPS_DIR`] once turned into a path. The only names this API ever produces itself are
/// whatever a caller passed to `PUT /clips/<name>`, so this is the one and only gate; every other
/// clip-store function trusts a name that already passed it.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME_LEN
        && name != "."
        && name != ".."
        && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
}

fn path_for(name: &str) -> PathBuf {
    PathBuf::from(CLIPS_DIR).join(format!("{name}.aac"))
}

/// Every stored clip, sorted by name. An absent [`CLIPS_DIR`] (nothing ever saved yet) is an
/// empty list, not an error.
pub fn list() -> io::Result<Vec<ClipInfo>> {
    let mut out = Vec::new();
    let entries = match fs::read_dir(CLIPS_DIR) {
        Ok(e) => e,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(out),
        Err(e) => return Err(e),
    };
    for entry in entries {
        let entry = entry?;
        let file_name = entry.file_name();
        let file_name = file_name.to_string_lossy();
        let Some(name) = file_name.strip_suffix(".aac") else { continue };
        let bytes = entry.metadata()?.len();
        out.push(ClipInfo { name: name.to_string(), bytes });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

pub fn load(name: &str) -> io::Result<Vec<u8>> {
    fs::read(path_for(name))
}

pub fn save(name: &str, adts_bytes: &[u8]) -> io::Result<()> {
    fs::create_dir_all(CLIPS_DIR)?;
    fs::write(path_for(name), adts_bytes)
}

pub fn delete(name: &str) -> io::Result<()> {
    fs::remove_file(path_for(name))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn valid_name_accepts_ordinary_identifiers() {
        for name in ["feed-start", "alert_1", "v2.announcement"] {
            assert!(valid_name(name), "{name} should be valid");
        }
    }

    #[test]
    fn valid_name_rejects_path_traversal_and_separators() {
        for name in ["..", ".", "../etc/passwd", "a/b", "a\\b", ""] {
            assert!(!valid_name(name), "{name} must be rejected");
        }
    }

    #[test]
    fn valid_name_rejects_names_over_the_length_cap() {
        let long = "a".repeat(MAX_NAME_LEN + 1);
        assert!(!valid_name(&long));
        let ok = "a".repeat(MAX_NAME_LEN);
        assert!(valid_name(&ok));
    }

    #[test]
    fn path_for_stays_inside_the_clips_dir_for_any_valid_name() {
        let p = path_for("feed-start");
        assert_eq!(p, PathBuf::from("/opt/kibble/clips/feed-start.aac"));
    }
}
