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
//! every Kibble startup.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

const TARGET: &str = "/app/script/reset_wifi.sh";
const REPLACEMENT: &str = "/opt/kibble/reset_wifi_noop.sh";

const REPLACEMENT_SCRIPT: &str = "#!/bin/sh\n\
# Neutralized by kibble (agent/src/resetguard.rs) -- see docs/35-wifi-tug-of-war.md. The real\n\
# reset_wifi.sh power-cycles the Wi-Fi radio's GPIO and fully restarts wpa_supplicant/udhcpc;\n\
# ctrl calls it roughly every 180s on its own *healthy* code path, not in response to an actual\n\
# fault. This records every time it was requested without touching the radio or the link.\n\
echo \"$(date +%s) ctrl requested reset_wifi.sh $*\" >> /opt/kibble/reset_wifi_suppressed.log\n\
exit 0\n";

/// Whether `target` is already bind-mounted from `replacement`, read from a `/proc/mounts`-style
/// listing (`<source> <target> <fstype> <options> <freq> <passno>` per line). Checked so a Kibble
/// restart never stacks a second bind mount on top of the first -- each would shadow the last
/// harmlessly, but `/proc/mounts` would grow one entry longer per restart forever -- and so an
/// unrelated mount that happens to already sit on `target` (from something else entirely) is
/// never mistaken for this one.
fn already_mounted_in(mounts: &str, replacement: &str, target: &str) -> bool {
    mounts.lines().any(|line| {
        let mut fields = line.split_whitespace();
        fields.next() == Some(replacement) && fields.next() == Some(target)
    })
}

fn already_mounted() -> bool {
    match fs::read_to_string("/proc/mounts") {
        Ok(mounts) => already_mounted_in(&mounts, REPLACEMENT, TARGET),
        Err(e) => {
            eprintln!("kibbled: resetguard: could not read /proc/mounts: {e} (assuming not mounted)");
            false
        }
    }
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
    fn already_mounted_true_when_proc_mounts_has_the_exact_pair() {
        let mounts = "/dev/root / ext4 rw,relatime 0 0\n\
                       /opt/kibble/reset_wifi_noop.sh /app/script/reset_wifi.sh none rw,bind 0 0\n";
        assert!(already_mounted_in(mounts, REPLACEMENT, TARGET));
    }

    #[test]
    fn already_mounted_false_when_absent_entirely() {
        let mounts = "/dev/root / ext4 rw,relatime 0 0\n";
        assert!(!already_mounted_in(mounts, REPLACEMENT, TARGET));
    }

    #[test]
    fn already_mounted_false_for_a_different_source_on_the_same_target() {
        // Something else entirely happens to have a bind mount sitting on the same path -- must
        // not be mistaken for this module's own mount.
        let mounts = "/some/unrelated/file /app/script/reset_wifi.sh none rw,bind 0 0\n";
        assert!(!already_mounted_in(mounts, REPLACEMENT, TARGET));
    }

    #[test]
    fn already_mounted_false_for_the_replacement_mounted_somewhere_else() {
        let mounts = "/opt/kibble/reset_wifi_noop.sh /app/script/wifi_connect.sh none rw,bind 0 0\n";
        assert!(!already_mounted_in(mounts, REPLACEMENT, TARGET));
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
