//! Face-crop capture for future gallery-matching enrolment.
//!
//! `libalgo.so` runs its own re-identification pipeline entirely in-process inside `media`
//! (`docs/12-ai.md`), and the 512-float embedding it computes never crosses the public API
//! boundary -- only `pet_id`/`score` do, and only via a message this study confirmed lands in
//! `ctrl`'s own private inbox (see `ai.rs`'s module doc). So there is no gallery-matching *here*
//! yet, deliberately: this module's job is only to accumulate the raw material (real face crops,
//! as they are observed) so that a future pass can run its own embedding model against them, or a
//! human can hand-label them. Crops arrive from `ai.rs`'s poller, which watches the vendor's own
//! `/tmp/saveFace.jpg` for changes -- see that module's doc for why the on-device `pet_id` this
//! crop was associated with is not attached (it lives in the same unreachable bus message).
//!
//! Layout: `PENDING_DIR/<unix>-<label>.jpg` (label is `pet_id` if one is ever available, else
//! `"unknown"`), capped at [`MAX_PENDING`] files with the oldest evicted by mtime. A human names a
//! pending crop with `POST /faces/label`, which moves it to `<FACES_ROOT>/<cat>/<same filename>`
//! -- permanent storage, outside the cap, one directory per label.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub const FACES_ROOT: &str = "/opt/kibble/faces";
pub const PENDING_DIR: &str = "/opt/kibble/faces/pending";
/// `/opt` has roughly 50 MB free on this device; a face crop is a few tens of KB, so 200 caps
/// pending storage at single-digit megabytes even if nothing is ever labelled.
pub const MAX_PENDING: usize = 200;

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// A name is safe to join onto a directory we control if it has no path separators and doesn't
/// spell a traversal -- the only names this module should ever be asked to read/move/label back
/// are ones it generated itself, but `POST /faces/label`'s `name` comes from an HTTP client.
fn is_safe_name(name: &str) -> bool {
    !name.is_empty() && !name.contains('/') && !name.contains('\\') && name != "." && name != ".."
}

/// Save a newly observed face crop under `PENDING_DIR`, evicting the oldest file first if already
/// at [`MAX_PENDING`]. `pet_id` is `None` today (see the module doc); kept as a parameter so a
/// future real tap can pass one through without changing this function's shape.
pub fn save_pending(bytes: &[u8], pet_id: Option<u32>) -> io::Result<String> {
    save_pending_in(Path::new(PENDING_DIR), bytes, pet_id)
}

fn save_pending_in(dir: &Path, bytes: &[u8], pet_id: Option<u32>) -> io::Result<String> {
    fs::create_dir_all(dir)?;
    let label = pet_id.map(|id| id.to_string()).unwrap_or_else(|| "unknown".into());
    let name = format!("{}-{label}.jpg", now_unix());
    fs::write(dir.join(&name), bytes)?;
    evict_oldest_if_over_cap(dir, MAX_PENDING)?;
    Ok(name)
}

/// Delete oldest-by-mtime files until at most `cap` remain. Pure filesystem logic (no clock
/// dependency beyond mtimes the OS already sets), so it's directly unit-testable.
fn evict_oldest_if_over_cap(dir: &Path, cap: usize) -> io::Result<()> {
    let mut entries: Vec<(SystemTime, PathBuf)> = fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let meta = e.metadata().ok()?;
            if !meta.is_file() {
                return None;
            }
            Some((meta.modified().ok()?, e.path()))
        })
        .collect();
    if entries.len() <= cap {
        return Ok(());
    }
    entries.sort_by_key(|(mtime, _)| *mtime);
    for (_, path) in entries.iter().take(entries.len() - cap) {
        fs::remove_file(path)?;
    }
    Ok(())
}

/// `GET /faces/pending`: filenames only, newest last. Missing directory (nothing captured yet)
/// is an empty list, not an error.
pub fn list_pending() -> io::Result<Vec<String>> {
    list_pending_in(Path::new(PENDING_DIR))
}

fn list_pending_in(dir: &Path) -> io::Result<Vec<String>> {
    let read = match fs::read_dir(dir) {
        Ok(r) => r,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut entries: Vec<(SystemTime, String)> = read
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let mtime = e.metadata().ok()?.modified().ok()?;
            let name = e.file_name().into_string().ok()?;
            Some((mtime, name))
        })
        .collect();
    entries.sort();
    Ok(entries.into_iter().map(|(_, name)| name).collect())
}

#[derive(Debug)]
pub enum FaceError {
    InvalidName,
    InvalidCat(String),
    NotFound,
    Io(io::Error),
}

impl std::fmt::Display for FaceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FaceError::InvalidName => write!(f, "invalid file name"),
            FaceError::InvalidCat(c) => write!(f, "invalid \"cat\" {c:?}"),
            FaceError::NotFound => write!(f, "no such pending face crop"),
            FaceError::Io(e) => write!(f, "{e}"),
        }
    }
}

/// `GET /faces/pending/<name>`: the raw JPEG bytes.
pub fn read_pending(name: &str) -> Result<Vec<u8>, FaceError> {
    if !is_safe_name(name) {
        return Err(FaceError::InvalidName);
    }
    match fs::read(Path::new(PENDING_DIR).join(name)) {
        Ok(b) => Ok(b),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Err(FaceError::NotFound),
        Err(e) => Err(FaceError::Io(e)),
    }
}

/// `POST /faces/label {"name": "...", "cat": "Rashy"|"other"|"not_a_cat"}`: move a pending crop
/// out of the capped/evictable pending pool into permanent, cat-named storage. `cat` is whatever
/// label the caller supplies (HA owns the actual cat roster -- `docs/design-entities.md` §6.2:
/// "cat names are not recoverable from the device", a one-time HA-side naming step), sanitised
/// only enough to stay inside `FACES_ROOT`.
pub fn label(name: &str, cat: &str) -> Result<(), FaceError> {
    label_in(Path::new(PENDING_DIR), Path::new(FACES_ROOT), name, cat)
}

fn label_in(pending_dir: &Path, faces_root: &Path, name: &str, cat: &str) -> Result<(), FaceError> {
    if !is_safe_name(name) {
        return Err(FaceError::InvalidName);
    }
    if !is_safe_name(cat) {
        return Err(FaceError::InvalidCat(cat.to_string()));
    }
    let src = pending_dir.join(name);
    if !src.is_file() {
        return Err(FaceError::NotFound);
    }
    let dest_dir = faces_root.join(cat);
    fs::create_dir_all(&dest_dir).map_err(FaceError::Io)?;
    fs::rename(&src, dest_dir.join(name)).map_err(FaceError::Io)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};

    static COUNTER: AtomicU32 = AtomicU32::new(0);

    /// A fresh, unique scratch directory per test -- these run concurrently, and filesystem
    /// state is exactly what this module manipulates.
    fn temp_dir(tag: &str) -> PathBuf {
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("kibble-faces-test-{tag}-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn save_pending_creates_the_directory_and_names_by_unix_time_and_label() {
        let dir = temp_dir("save");
        let name = save_pending_in(&dir, b"fake jpeg bytes", Some(7)).unwrap();
        assert!(name.ends_with("-7.jpg"));
        assert_eq!(fs::read(dir.join(&name)).unwrap(), b"fake jpeg bytes");
    }

    #[test]
    fn save_pending_labels_unknown_when_no_pet_id_is_available() {
        let dir = temp_dir("unknown");
        let name = save_pending_in(&dir, b"x", None).unwrap();
        assert!(name.ends_with("-unknown.jpg"), "got {name:?}");
    }

    #[test]
    fn eviction_keeps_exactly_the_cap_and_drops_the_oldest_first() {
        let dir = temp_dir("evict");
        fs::create_dir_all(&dir).unwrap();
        // Write files with explicit, increasing mtimes so eviction order is deterministic
        // regardless of filesystem timestamp resolution.
        let mut names = Vec::new();
        for i in 0..5u64 {
            let name = format!("f{i}.jpg");
            let path = dir.join(&name);
            fs::write(&path, b"x").unwrap();
            let mtime = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000 + i);
            let f = fs::File::open(&path).unwrap();
            f.set_modified(mtime).unwrap();
            names.push(name);
        }
        evict_oldest_if_over_cap(&dir, 3).unwrap();
        let remaining = list_pending_in(&dir).unwrap();
        assert_eq!(remaining.len(), 3);
        // f0/f1 (oldest) evicted; f2,f3,f4 (newest) survive.
        assert!(!remaining.contains(&names[0]));
        assert!(!remaining.contains(&names[1]));
        assert!(remaining.contains(&names[2]));
        assert!(remaining.contains(&names[3]));
        assert!(remaining.contains(&names[4]));
    }

    #[test]
    fn eviction_is_a_no_op_under_the_cap() {
        let dir = temp_dir("nocap");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("a.jpg"), b"x").unwrap();
        evict_oldest_if_over_cap(&dir, 200).unwrap();
        assert_eq!(list_pending_in(&dir).unwrap().len(), 1);
    }

    #[test]
    fn save_pending_never_exceeds_the_cap_across_many_saves() {
        let dir = temp_dir("cap-loop");
        for i in 0..(MAX_PENDING + 25) {
            let name = save_pending_in(&dir, b"x", Some(i as u32)).unwrap();
            // force distinct, increasing mtimes so ordering is unambiguous even when several
            // saves land within the same wall-clock second
            let mtime = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(i as u64);
            fs::File::open(dir.join(&name)).unwrap().set_modified(mtime).unwrap();
        }
        assert_eq!(list_pending_in(&dir).unwrap().len(), MAX_PENDING);
    }

    #[test]
    fn list_pending_on_a_missing_directory_is_an_empty_list_not_an_error() {
        let dir = temp_dir("missing");
        assert_eq!(list_pending_in(&dir).unwrap(), Vec::<String>::new());
    }

    #[test]
    fn read_pending_rejects_path_traversal() {
        let err = read_pending("../../etc/passwd").unwrap_err();
        assert!(matches!(err, FaceError::InvalidName));
    }

    #[test]
    fn read_pending_reports_not_found_for_a_missing_file() {
        // PENDING_DIR is the real device path; on a dev box it won't exist, which must surface
        // as NotFound (or an Io wrapping the same "no such file"), never a panic.
        let err = read_pending("2026-01-01-nope.jpg");
        assert!(err.is_err());
    }

    #[test]
    fn label_moves_a_pending_crop_into_the_named_directory() {
        let pending = temp_dir("label-pending");
        let root = temp_dir("label-root");
        fs::create_dir_all(&pending).unwrap();
        fs::write(pending.join("1700000000-unknown.jpg"), b"crop").unwrap();
        label_in(&pending, &root, "1700000000-unknown.jpg", "Rashy").unwrap();
        assert!(!pending.join("1700000000-unknown.jpg").exists());
        assert_eq!(fs::read(root.join("Rashy").join("1700000000-unknown.jpg")).unwrap(), b"crop");
    }

    #[test]
    fn label_rejects_traversal_in_either_the_name_or_the_cat() {
        let pending = temp_dir("label-bad-pending");
        let root = temp_dir("label-bad-root");
        fs::create_dir_all(&pending).unwrap();
        fs::write(pending.join("a.jpg"), b"x").unwrap();
        assert!(matches!(
            label_in(&pending, &root, "../a.jpg", "Rashy"),
            Err(FaceError::InvalidName)
        ));
        assert!(matches!(
            label_in(&pending, &root, "a.jpg", "../escape"),
            Err(FaceError::InvalidCat(_))
        ));
    }

    #[test]
    fn label_reports_not_found_for_a_name_that_was_never_pending() {
        let pending = temp_dir("label-missing-pending");
        let root = temp_dir("label-missing-root");
        fs::create_dir_all(&pending).unwrap();
        assert!(matches!(label_in(&pending, &root, "ghost.jpg", "other"), Err(FaceError::NotFound)));
    }
}
