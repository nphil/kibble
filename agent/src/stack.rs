//! Which userland boots next: the LibreFeed A/B selector's mode file (`/opt/librefeed/mode`,
//! read by `/opt/app_init.sh` before either stack starts). kibbled runs only on the vendor
//! stack, so `running` is always `"vendor"` here; `librefeedd` reports `"librefeed"` from the
//! same route, which is what lets one HA select entity drive the switch in both directions.
//!
//! `POST /mode {"mode":"librefeed"}` writes the file and reboots. Rule 0 of LibreFeed's plan
//! applies on the other side: a LibreFeed boot that cannot be reached, or is not confirmed
//! within 10 minutes, reverts to vendor on its own.

use std::fs;
use std::io::Write;
use std::process::Command;

const MODE_FILE: &str = "/opt/librefeed/mode";
const BOOT_FAIL_FILE: &str = "/opt/librefeed/boot_fail";
const HOOK: &str = "/opt/app_init.sh";

pub fn status_json() -> String {
    let next = fs::read_to_string(MODE_FILE).map(|s| s.trim().to_string()).unwrap_or_else(|_| "vendor".into());
    let installed = fs::metadata(HOOK).is_ok() && fs::metadata("/opt/librefeed/librefeed-watchdog").is_ok();
    format!(r#"{{"running":"vendor","next":"{next}","librefeed_installed":{installed}}}"#)
}

/// Write the mode atomically (temp + rename, like the watchdog does), then reboot 1 s later so
/// the HTTP reply gets out first.
pub fn set(mode: &str) -> Result<(), String> {
    if !matches!(mode, "vendor" | "librefeed" | "recovery") {
        return Err(format!("unknown mode {mode:?}"));
    }
    if mode != "vendor" && fs::metadata(HOOK).is_err() {
        return Err("LibreFeed is not installed (/opt/app_init.sh missing)".into());
    }
    let tmp = format!("{MODE_FILE}.tmp");
    let mut f = fs::File::create(&tmp).map_err(|e| e.to_string())?;
    f.write_all(mode.as_bytes()).and_then(|_| f.write_all(b"\n")).and_then(|_| f.sync_all()).map_err(|e| e.to_string())?;
    fs::rename(&tmp, MODE_FILE).map_err(|e| e.to_string())?;
    let _ = fs::write(BOOT_FAIL_FILE, b"0\n");
    let _ = Command::new("sync").status();
    std::thread::spawn(|| {
        std::thread::sleep(std::time::Duration::from_secs(1));
        let _ = Command::new("reboot").status();
    });
    Ok(())
}
