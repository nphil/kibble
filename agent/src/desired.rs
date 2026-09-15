//! Kibble's own declarative desired-state store for settings it has written.
//!
//! `/opt/user.conf`'s content is AES-encrypted with a key this repo has not recovered (see
//! `docs/21-config-encryption.md`), so kibbled cannot persist through the vendor's own config
//! file without risking silently corrupting it. Instead it keeps a flat, plaintext record on the
//! same writable `/opt` volume the vendor uses, of every value it has been asked to set, and
//! `persist.rs` re-applies all of them:
//!   - once at startup, once `config_shm`'s own `loaded` flag says the vendor has finished
//!     populating it;
//!   - continuously afterward, so a value some other process resets (the vendor's own settings
//!     path, a Petkit cloud config sync while that integration stays enabled, ...) is corrected
//!     again rather than left to silently drift.
//!
//! This is a feature, not a stand-in for the encrypted file: the desired state is human-readable
//! (`cat /opt/kibble/settings.json`), and if Kibble is ever removed the device is left exactly as
//! the vendor's own app last configured it, nothing this store invented.

use std::fs;
use std::io;

pub const PATH: &str = "/opt/kibble/settings.json";

/// Parse the flat `{"key": 123, ...}` object at `PATH`. A missing file means "no settings ever
/// written through Kibble yet" and is not an error — first boot after install looks like this.
pub fn load() -> Vec<(String, u32)> {
    load_from(PATH)
}

/// Load the current map, set `key` to `value` (replacing any existing entry for it), save.
pub fn upsert(key: &str, value: u32) -> io::Result<()> {
    let mut entries = load();
    match entries.iter_mut().find(|(k, _)| k == key) {
        Some(entry) => entry.1 = value,
        None => entries.push((key.to_string(), value)),
    }
    save_to(PATH, &entries)
}

fn load_from(path: &str) -> Vec<(String, u32)> {
    match fs::read_to_string(path) {
        Ok(text) => parse(&text),
        Err(_) => Vec::new(),
    }
}

/// Deliberately tolerant: one malformed entry is skipped, not fatal to the whole file. A
/// hand-edited or torn `settings.json` should degrade one setting, not lose every recorded one.
fn parse(text: &str) -> Vec<(String, u32)> {
    let mut out = Vec::new();
    let body = text.trim();
    let body = body.strip_prefix('{').unwrap_or(body);
    let body = body.strip_suffix('}').unwrap_or(body);
    for entry in body.split(',') {
        let entry = entry.trim();
        if entry.is_empty() {
            continue;
        }
        let Some((k, v)) = entry.split_once(':') else {
            continue;
        };
        let key = k.trim().trim_matches('"');
        if key.is_empty() {
            continue;
        }
        let Ok(value) = v.trim().parse::<u32>() else {
            continue;
        };
        out.push((key.to_string(), value));
    }
    out
}

/// Atomically replace `path` with `entries` serialised as flat JSON: write a temp file in the
/// same directory, then `rename` over the real path. `rename(2)` within one filesystem is atomic,
/// so a crash or power loss mid-write leaves either the old file or the fully-written new one,
/// never a half-written `settings.json`.
fn save_to(path: &str, entries: &[(String, u32)]) -> io::Result<()> {
    let mut body = String::from("{\n");
    for (i, (k, v)) in entries.iter().enumerate() {
        if i > 0 {
            body.push_str(",\n");
        }
        body.push_str(&format!("  \"{k}\": {v}"));
    }
    body.push_str("\n}\n");

    let tmp = format!("{path}.tmp");
    fs::write(&tmp, body)?;
    fs::rename(&tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path(name: &str) -> String {
        std::env::temp_dir()
            .join(format!("kibbled-test-{name}-{:?}.json", std::thread::current().id()))
            .to_string_lossy()
            .into_owned()
    }

    #[test]
    fn missing_file_loads_as_empty() {
        assert_eq!(load_from("/nonexistent/path/kibble-settings-test.json"), vec![]);
    }

    #[test]
    fn save_then_load_round_trips() {
        let path = temp_path("roundtrip");
        let entries = vec![("volume".to_string(), 5u32), ("light".to_string(), 1u32)];
        save_to(&path, &entries).unwrap();
        assert_eq!(load_from(&path), entries);
        let _ = fs::remove_file(&path);
    }

    #[test]
    fn parse_skips_malformed_entries_without_losing_wellformed_neighbours() {
        // Each bad entry stays comma-separated from its neighbours (the one structural guarantee
        // this flat, hand-rolled parser relies on) but is individually broken: no colon, and a
        // non-numeric value.
        let text = r#"{
  "volume": 5,
  "no_colon_here",
  "light": 1,
  "not_a_number": "oops",
  "manual_lock": 1
}"#;
        let mut got = parse(text);
        got.sort();
        assert_eq!(
            got,
            vec![
                ("light".to_string(), 1),
                ("manual_lock".to_string(), 1),
                ("volume".to_string(), 5),
            ]
        );
    }

    /// A comma dropped between two entries merges their text on this parser's simple
    /// comma-split design — documented here as a known limitation rather than silently assumed:
    /// `settings.json` is written exclusively by `save_to` (always comma-correct), so this shape
    /// only arises from manual editing, and the merged "key" is gibberish no real setting key
    /// will ever match, so it is inert (silently dropped by whoever looks it up) rather than
    /// misapplied to the wrong setting.
    #[test]
    fn a_dropped_comma_merges_two_entries_into_one_unmatchable_key_rather_than_corrupting_either_value() {
        let text = r#"{"volume": 5, "no_comma_after_this" "light": 1}"#;
        let got = parse(text);
        assert!(got.contains(&("volume".to_string(), 5)));
        // "light" never appears cleanly — its text got absorbed into the merged, garbled key.
        assert!(!got.iter().any(|(k, v)| k == "light" && *v == 1));
        assert!(got.iter().any(|(k, _)| k.contains("no_comma_after_this")));
    }

    #[test]
    fn upsert_replaces_an_existing_key_in_place_rather_than_duplicating() {
        let path = temp_path("upsert");
        save_to(&path, &[("volume".to_string(), 5)]).unwrap();
        let mut entries = load_from(&path);
        match entries.iter_mut().find(|(k, _)| k == "volume") {
            Some(e) => e.1 = 8,
            None => entries.push(("volume".to_string(), 8)),
        }
        save_to(&path, &entries).unwrap();
        assert_eq!(load_from(&path), vec![("volume".to_string(), 8)]);
        let _ = fs::remove_file(&path);
    }
}
