//! Neutralizes the vendor's `reset_wifi.sh` (see `docs/35-wifi-tug-of-war.md`): `ctrl`'s own
//! `wifi_monitor_timer` calls it roughly every 180s, disassembly-confirmed to fire on its
//! *healthy* code path (association `COMPLETED`, MAC readback succeeded) gated only by two
//! unrelated `config_shm` fields that are permanently in the triggering state on this unit and a
//! plain elapsed-time check -- not a response to any actual Wi-Fi fault. The script itself
//! power-cycles the radio's GPIO and fully restarts `wpa_supplicant`/`udhcpc` for no operational
//! reason, which is exactly what re-adds the default route and re-enables the Petkit cloud
//! (`cloud.rs`'s fail-safe rollback) on a fixed cadence independent of anything else on the
//! device.
//!
//! `/app` is confirmed read-only squashfs (live `mount` output), so `reset_wifi.sh` cannot be
//! edited in place, and this project's own rule is to never touch the vendor's flash contents
//! regardless. A kernel bind mount shadows the file in the VFS without writing to the underlying
//! squashfs at all: `ctrl` still `popen()`s the same path successfully, but the script that
//! actually runs is a no-op that only records the request (kept observable -- silently
//! discarding it entirely would hide the one signal available for diagnosing a real future fault)
//! instead of touching the radio.
//!
//! Reversible from the device itself, without a reboot: `umount /app/script/reset_wifi.sh`
//! immediately restores the vendor's own script, since the bind mount is the only thing shadowing
//! it and nothing on flash was ever modified. A real device reboot also reverts it on its own --
//! a bind mount never survives one -- so [`install`] simply re-establishes it, idempotently, on
//! every Kibble startup (checked via [`already_mounted`], not `/proc/mounts` -- see its own doc
//! comment for why).

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

const TARGET: &str = "/app/script/reset_wifi.sh";
const REPLACEMENT: &str = "/opt/kibble/reset_wifi_noop.sh";

const REPLACEMENT_SCRIPT: &str = "#!/bin/sh\n\
# Neutralized by kibble (agent/src/resetguard.rs) -- see docs/35-wifi-tug-of-war.md. The real\n\
# reset_wifi.sh power-cycles the Wi-Fi radio's GPIO and fully restarts wpa_supplicant/udhcpc;\n\
# ctrl calls it roughly every 180s on its own *healthy* code path, not in response to an actual\n\
# fault. This records every time it was requested without touching the radio or the link.\n\
# tmpfs, not flash: this fires every ~180s for the life of the device. Not a *.log name either --\n\
# the vendor's syslog stack periodically truncates every /tmp/*.log (docs/34 Part 5).\n\
echo \"$(date +%s) ctrl requested reset_wifi.sh $*\" >> /tmp/reset_wifi_suppressed.dat\n\
exit 0\n";

/// Whether `a` and `b` currently resolve to the exact same underlying inode -- true for two
/// paths connected by a bind mount (or a hard link), false otherwise, including when either path
/// is missing.
fn same_file(a: &str, b: &str) -> bool {
    match (fs::metadata(a), fs::metadata(b)) {
        (Ok(ma), Ok(mb)) => ma.dev() == mb.dev() && ma.ino() == mb.ino(),
        _ => false,
    }
}

/// Whether [`TARGET`] is already bind-mounted from [`REPLACEMENT`] -- checked so a Kibble restart
/// never stacks a second bind mount on top of the first. Deliberately does **not** parse
/// `/proc/mounts` for a `<source> <target>` pair, the way an initial version of this module did:
/// confirmed live on this device, a *file* bind mount's `/proc/mounts` line names the underlying
/// block device (`/dev/ubi1_2`, the `ubifs` volume `/opt` lives on) as its source field, not the
/// bind-mount source path (`/opt/kibble/reset_wifi_noop.sh`) -- matching against the expected
/// source string there never matches, which would have silently re-`mount --bind`ed on every
/// single Kibble restart forever. Comparing `(device, inode)` numbers instead is exactly what
/// identifies "these two paths are the same file", regardless of which kernel mechanism (bind
/// mount here; a hard link would look identical) made them so, and regardless of how any given
/// kernel/filesystem chooses to render it in `/proc/mounts`.
fn already_mounted() -> bool {
    same_file(TARGET, REPLACEMENT)
}

/// Writes the no-op replacement script to `path` and marks it executable. A plain
/// `fs::write` (not the atomic temp-file-plus-rename `wifi.rs`/`persist.rs` use for their own
/// state files) is fine here: this is a static, content-addressed-by-this-binary script, not
/// state that must never appear half-written mid-update, and it is rewritten identically on
/// every restart regardless.
fn write_replacement_script(path: &str) -> std::io::Result<()> {
    fs::write(path, REPLACEMENT_SCRIPT)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o755))
}

/// Idempotently neutralizes `reset_wifi.sh` for the lifetime of the current boot. Best-effort and
/// non-fatal throughout -- a failure here should never stop Kibble from starting the rest of its
/// features, since the device is no worse off than before this module existed.
pub fn install() {
    if let Err(e) = write_replacement_script(REPLACEMENT) {
        eprintln!("kibbled: resetguard: could not write {REPLACEMENT}: {e} (not installing)");
        return;
    }
    if already_mounted() {
        eprintln!("kibbled: resetguard: {TARGET} already neutralized");
        return;
    }
    match Command::new("mount").args(["--bind", REPLACEMENT, TARGET]).output() {
        Ok(out) if out.status.success() => {
            eprintln!("kibbled: resetguard: neutralized {TARGET} (revert: umount {TARGET})");
        }
        Ok(out) => eprintln!(
            "kibbled: resetguard: `mount --bind {REPLACEMENT} {TARGET}` failed ({}): {}{}",
            out.status,
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        ),
        Err(e) => eprintln!("kibbled: resetguard: exec `mount --bind {REPLACEMENT} {TARGET}`: {e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_file_true_for_a_hard_link_to_the_same_inode() {
        let dir = std::env::temp_dir();
        let a = dir.join(format!("kibble-resetguard-same-a-{:?}", std::thread::current().id()));
        let b = dir.join(format!("kibble-resetguard-same-b-{:?}", std::thread::current().id()));
        let _ = fs::remove_file(&a);
        let _ = fs::remove_file(&b);
        fs::write(&a, b"x").unwrap();
        // A hard link is a different, independent path resolving to the identical inode -- the
        // same relationship a bind mount creates between the replacement and the vendor's path.
        fs::hard_link(&a, &b).unwrap();
        assert!(same_file(a.to_str().unwrap(), b.to_str().unwrap()));
        let _ = fs::remove_file(&a);
        let _ = fs::remove_file(&b);
    }

    #[test]
    fn same_file_false_for_two_distinct_files_with_identical_content() {
        // The whole reason this isn't a content/hash comparison: two independent files that
        // happen to read identically (e.g. before a `mount --bind` was ever run) must not be
        // mistaken for an established mount.
        let dir = std::env::temp_dir();
        let a = dir.join(format!("kibble-resetguard-distinct-a-{:?}", std::thread::current().id()));
        let b = dir.join(format!("kibble-resetguard-distinct-b-{:?}", std::thread::current().id()));
        fs::write(&a, b"identical content").unwrap();
        fs::write(&b, b"identical content").unwrap();
        assert!(!same_file(a.to_str().unwrap(), b.to_str().unwrap()));
        let _ = fs::remove_file(&a);
        let _ = fs::remove_file(&b);
    }

    #[test]
    fn same_file_false_when_either_path_is_missing() {
        assert!(!same_file("/nonexistent/kibble-resetguard-a", "/nonexistent/kibble-resetguard-b"));
        let real = std::env::temp_dir()
            .join(format!("kibble-resetguard-half-{:?}", std::thread::current().id()));
        fs::write(&real, b"x").unwrap();
        assert!(!same_file(real.to_str().unwrap(), "/nonexistent/kibble-resetguard-other"));
        let _ = fs::remove_file(&real);
    }

    #[test]
    fn replacement_script_is_a_well_formed_noop() {
        assert!(REPLACEMENT_SCRIPT.starts_with("#!/bin/sh\n"));
        assert!(REPLACEMENT_SCRIPT.trim_end().ends_with("exit 0"));
        // The entire point: the lines that actually *run* must never touch the radio, the link,
        // or the vendor's own supplicant/dhcp processes -- checked against executable lines only
        // so the explanatory comments above (which necessarily name the very things being
        // avoided) can't produce a false positive here.
        let executable: String = REPLACEMENT_SCRIPT
            .lines()
            .filter(|l| !l.trim_start().starts_with('#'))
            .collect::<Vec<_>>()
            .join("\n")
            .to_lowercase();
        for forbidden in ["gpio", "wpa_supplicant", "udhcpc", "ifconfig", "killall", "wpa_cli"] {
            assert!(!executable.contains(forbidden), "replacement script must never run anything touching {forbidden}");
        }
    }

    #[test]
    fn write_replacement_script_creates_an_executable_file_with_the_exact_content() {
        let path = std::env::temp_dir()
            .join(format!("kibble-resetguard-test-{:?}.sh", std::thread::current().id()))
            .to_string_lossy()
            .into_owned();
        write_replacement_script(&path).unwrap();
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o755, "ctrl execs this directly, it must be executable");
        assert_eq!(fs::read_to_string(&path).unwrap(), REPLACEMENT_SCRIPT);
        let _ = fs::remove_file(&path);
    }
}
