//! Writing a setting's value into the live shared config, and keeping it applied.
//!
//! `/opt/user.conf` is `<32-byte lowercase-hex MD5 of content><content>` — confirmed live against
//! the device, the header really is the MD5 of what follows — but `content` is genuinely
//! AES-encrypted (measured 7.9/8.0 bits/byte of Shannon entropy on a live pull, zero byte-level
//! correspondence to `config_shm` at any offset). The key was never recovered by any study in
//! this repo (`docs/21-config-encryption.md`), so kibbled does not attempt to hand-construct new
//! encrypted content — a wrong guess would still pass the outer MD5-of-content check while
//! silently corrupting every other persisted setting and credential in the file.
//!
//! Instead: a write here (a) lands in `config_shm` immediately, under the same
//! `flock(/tmp/config.lock, LOCK_EX)` discipline the vendor's own `config_save()` uses, which
//! every vendor process reads live (`config_shm` is one shared mapping, not a per-process copy),
//! and (b) is recorded in Kibble's own `desired.rs` store so it can be re-applied — once at
//! startup, and continuously afterward by [`spawn_reconciler`] — without ever touching the
//! encrypted file.

use std::fs::OpenOptions;
use std::io;
use std::os::raw::c_int;
use std::os::unix::fs::FileExt;
use std::os::unix::io::AsRawFd;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crate::bus::{Peer, Sender};
use crate::desired;
use crate::settings::{find, Setting, Width, SETTINGS};
use crate::state::{Shm, SHM_PATH};

const LOCK_PATH: &str = "/tmp/config.lock";
const LOCK_EX: c_int = 2;
const LOCK_UN: c_int = 8;

extern "C" {
    fn flock(fd: c_int, operation: c_int) -> c_int;
}

/// Serialises every config_shm/`settings.json` write between the HTTP handler thread and the
/// reconciler thread. `kibbled` builds with `panic = "abort"` (see `Cargo.toml`), so a poisoned
/// mutex is not a state this process can observe and keep running — an ordinary `unwrap()` is
/// enough.
static WRITE_LOCK: Mutex<()> = Mutex::new(());

/// How often the reconciler re-checks every recorded setting against its live value.
const RECONCILE_INTERVAL: Duration = Duration::from_secs(30);
/// How often to poll `config_shm`'s `loaded` flag while waiting for the vendor to populate it.
const READY_POLL_INTERVAL: Duration = Duration::from_secs(1);
/// Give up waiting for `loaded` and reconcile anyway after this long (the boot hook already
/// sleeps 20s before even starting kibbled — this is a bounded *extra* wait on top of that).
const READY_MAX_WAIT: Duration = Duration::from_secs(30);

/// Holds `/tmp/config.lock` under `LOCK_EX` for its lifetime, exactly like the vendor's own
/// `config_save()` (same path, same `LOCK_EX`/`LOCK_UN` pair) — released on drop.
struct ConfigLock {
    file: std::fs::File,
}

impl ConfigLock {
    fn acquire() -> io::Result<Self> {
        let file = OpenOptions::new().create(true).write(true).open(LOCK_PATH)?;
        if unsafe { flock(file.as_raw_fd(), LOCK_EX) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self { file })
    }
}

impl Drop for ConfigLock {
    fn drop(&mut self) {
        unsafe { flock(self.file.as_raw_fd(), LOCK_UN) };
    }
}

#[derive(Debug)]
pub enum WriteError {
    UnknownKey,
    NotWritable,
    OutOfRange,
    Io(io::Error),
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WriteError::UnknownKey => write!(f, "unknown setting key"),
            WriteError::NotWritable => write!(f, "setting is read-only (write not yet verified)"),
            WriteError::OutOfRange => write!(f, "value out of range for this setting"),
            WriteError::Io(e) => write!(f, "{e}"),
        }
    }
}

pub struct WriteOutcome {
    pub setting: &'static Setting,
    /// Whether the live follow-up bus message (if this setting has one) was sent successfully.
    /// `None` when the setting has no live notify at all (most of them — see `settings.rs`).
    pub notified: Option<bool>,
}

/// Validate, then write `value` for the setting named `key`: into the live `config_shm` mapping
/// under `/tmp/config.lock`, with its live notify sent (if any) before the lock releases, then
/// recorded in `settings.json` so it survives a restart. Losing the `settings.json` write is
/// logged but does not fail the request — the live value the caller asked for is already in
/// effect either way.
pub fn write_setting(key: &str, value: u32) -> Result<WriteOutcome, WriteError> {
    let setting = find(key).ok_or(WriteError::UnknownKey)?;
    if !setting.writable {
        return Err(WriteError::NotWritable);
    }
    if !setting.kind.accepts(value) {
        return Err(WriteError::OutOfRange);
    }

    let _guard = WRITE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let notified = apply_value(setting, value).map_err(WriteError::Io)?;

    if let Err(e) = desired::upsert(key, value) {
        eprintln!(
            "kibbled: wrote {key}={value} live but failed to record it in {}: {e} \
             (value is in effect now but will not survive a restart until set again)",
            desired::PATH
        );
    }

    Ok(WriteOutcome { setting, notified })
}

/// The actual `config_shm` write plus live notify, under the vendor's own lock. Shared by
/// `write_setting` (one key, from an HTTP request) and the reconciler (every recorded key, on a
/// timer) so both go through identical, single-purpose logic.
fn apply_value(setting: &'static Setting, value: u32) -> io::Result<Option<bool>> {
    let _lock = ConfigLock::acquire()?;

    let shm = OpenOptions::new().write(true).open(SHM_PATH)?;
    let le = value.to_le_bytes();
    let field: &[u8] = match setting.width {
        Width::U8 => &le[..1],
        Width::U32 => &le[..4],
    };
    shm.write_at(field, setting.offset as u64)?;

    let notified = setting.notify.map(|n| {
        // Best-effort: the config_shm write above already landed and is what every consumer
        // actually reads (STUDY-settings-write.md §2.1: even the vendor's *own* notify is dead
        // code for every field but this one) — a failed send here must not undo it.
        Sender::open(n.peer, Peer::Ctrl as u16)
            .and_then(|s| s.send(n.msg_id, &le[..n.payload_len]))
            .is_ok()
    });

    // Both callers (`POST /config` and the boot/periodic reconciler) land here; the value is
    // in config_shm at this point, which is what `GET /config` reads.
    crate::push::mark(crate::push::Field::Config);
    Ok(notified)
}

/// Re-apply every recorded desired-state value once `config_shm` looks populated, then keep
/// checking every [`RECONCILE_INTERVAL`] for the rest of the process's life, correcting any
/// value that has drifted from what was last requested through `POST /config`. Runs on its own
/// thread, holding its own `Arc` clone of the mapping `main.rs` also hands to the HTTP handlers.
pub fn spawn_reconciler(shm: Arc<Shm>) {
    std::thread::spawn(move || {
        wait_until_ready(&shm);
        loop {
            reconcile_once(&shm);
            std::thread::sleep(RECONCILE_INTERVAL);
        }
    });
}

fn wait_until_ready(shm: &Shm) {
    let deadline = Instant::now() + READY_MAX_WAIT;
    while shm.u32(0) != 1 {
        if Instant::now() >= deadline {
            eprintln!(
                "kibbled: config_shm not marked loaded after {READY_MAX_WAIT:?}, reconciling anyway"
            );
            return;
        }
        std::thread::sleep(READY_POLL_INTERVAL);
    }
}

fn reconcile_once(shm: &Shm) {
    let _guard = WRITE_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    for (key, desired_value) in desired::load() {
        let Some(setting) = SETTINGS.iter().find(|s| s.key == key) else {
            eprintln!("kibbled: settings.json has unknown key {key:?}, skipping");
            continue;
        };
        if !setting.writable || !setting.kind.accepts(desired_value) {
            eprintln!(
                "kibbled: settings.json has a stale or invalid entry for {key:?} ({desired_value}), skipping"
            );
            continue;
        }
        let live = setting.read(shm);
        if live == desired_value {
            continue;
        }
        eprintln!("kibbled: {key} drifted (live={live}, desired={desired_value}), reapplying");
        match apply_value(setting, desired_value) {
            Ok(notified) => {
                eprintln!("kibbled: reapplied {key}={desired_value} (notified={notified:?})")
            }
            Err(e) => eprintln!("kibbled: failed to reapply {key}: {e}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::settings::Kind;

    #[test]
    fn unknown_key_is_rejected_before_touching_anything() {
        assert!(matches!(
            write_setting("not_a_real_setting", 1),
            Err(WriteError::UnknownKey)
        ));
    }

    #[test]
    fn read_only_setting_is_rejected_before_touching_anything() {
        // `camera` is documented but not in the verified-writable set.
        assert!(!crate::settings::find("camera").unwrap().writable);
        assert!(matches!(
            write_setting("camera", 1),
            Err(WriteError::NotWritable)
        ));
    }

    #[test]
    fn out_of_range_value_is_rejected_for_a_bool_kind() {
        assert!(!Kind::Bool.accepts(7));
    }
}
