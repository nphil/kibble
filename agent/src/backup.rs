//! One-time backup of `/opt/user.conf`, taken before Kibble's settings feature can possibly
//! write anything.
//!
//! Kibble's settings feature never writes `/opt/user.conf` itself — its content is AES-encrypted
//! with a key this repo never recovered, so kibbled writes `config_shm` directly and keeps its
//! own plaintext record instead (see `persist.rs`'s module doc and `docs/21-config-encryption.md`).
//! But taking this backup once, unconditionally, before the reconciler's first pass can run, is
//! cheap insurance against every *other* way that file could end up different later (a vendor
//! OTA, a future Kibble change that does start touching it, direct `pktool`/app use) — a
//! known-good copy of the pre-Kibble file plus its own recorded MD5 is exactly what "back up
//! before the first write" is for.

use std::fs;
use std::io;
use std::path::Path;

const SOURCE: &str = "/opt/user.conf";
pub const BACKUP: &str = "/opt/kibble/user.conf.bak";

/// Back up the real `/opt/user.conf` — see [`backup_at`].
pub fn backup_once() -> io::Result<Option<(String, String)>> {
    backup_at(SOURCE, BACKUP)
}

/// Copy `source` to `backup` if, and only if, `backup` does not already exist — idempotent across
/// restarts, so a later call never overwrites an already-taken backup with a since-changed
/// source. Returns the source and backup content's MD5 (equal by construction on a successful
/// copy; re-hashed independently after the copy rather than assumed, so a truncated write is
/// caught here rather than discovered only when someone needs the backup). `Ok(None)` means a
/// backup already existed and nothing was touched.
fn backup_at(source: &str, backup: &str) -> io::Result<Option<(String, String)>> {
    if Path::new(backup).exists() {
        return Ok(None);
    }
    let content = fs::read(source)?;
    let source_md5 = crate::md5::hex(&content);
    fs::write(backup, &content)?;
    let backup_md5 = crate::md5::hex(&fs::read(backup)?);
    Ok(Some((source_md5, backup_md5)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path(name: &str) -> String {
        std::env::temp_dir()
            .join(format!(
                "kibbled-test-backup-{name}-{:?}.bin",
                std::thread::current().id()
            ))
            .to_string_lossy()
            .into_owned()
    }

    #[test]
    fn backup_path_lives_under_the_kibble_directory_not_alongside_the_original() {
        assert!(BACKUP.starts_with("/opt/kibble/"));
        assert_ne!(BACKUP, SOURCE);
    }

    #[test]
    fn first_call_copies_the_source_and_returns_matching_hashes() {
        let src = temp_path("src1");
        let dst = temp_path("dst1");
        fs::write(&src, b"pre-kibble user.conf content").unwrap();

        let (source_md5, backup_md5) = backup_at(&src, &dst).unwrap().expect("first call copies");
        assert_eq!(source_md5, backup_md5);
        assert_eq!(fs::read(&dst).unwrap(), fs::read(&src).unwrap());

        let _ = fs::remove_file(&src);
        let _ = fs::remove_file(&dst);
    }

    #[test]
    fn a_second_call_does_not_overwrite_an_existing_backup() {
        let src = temp_path("src2");
        let dst = temp_path("dst2");
        fs::write(&src, b"original content").unwrap();
        backup_at(&src, &dst).unwrap();

        // Source changes after the first backup (e.g. a later, unrelated save) -- the backup
        // must still reflect the *original* content, not this new one.
        fs::write(&src, b"content changed after the backup was taken").unwrap();
        let second = backup_at(&src, &dst).unwrap();

        assert!(second.is_none(), "must not re-copy once a backup exists");
        assert_eq!(fs::read(&dst).unwrap(), b"original content");

        let _ = fs::remove_file(&src);
        let _ = fs::remove_file(&dst);
    }

    #[test]
    fn missing_source_is_an_error_not_a_silent_skip() {
        let src = temp_path("does-not-exist");
        let dst = temp_path("dst3");
        assert!(backup_at(&src, &dst).is_err());
        assert!(!Path::new(&dst).exists());
    }
}
