//! Face-crop capture, human labelling, and the resulting cat-identification gallery.
//!
//! `libalgo.so` runs its own re-identification pipeline entirely in-process inside `media`
//! (`docs/12-ai.md`), and the 512-float embedding it computes never crosses the public API
//! boundary -- only `pet_id`/`score` do, and only via a message this project's own studies
//! confirmed lands in `ctrl`'s own private inbox (see `ai.rs`'s module doc). Kibble's answer,
//! confirmed practical by `docs/18-npu-confirmed.md`, is to run its own copy of that same frozen
//! face model as a second, independent process (`embed.rs`) and train a tiny classifier
//! (`catid.rs`) on its output instead of depending on that unreachable message -- see
//! `docs/27-cat-id.md` for the full design and honestly-labelled accuracy findings.
//!
//! Crops arrive from `ai.rs`'s poller, which watches the vendor's own `/tmp/pet_face_pic.jpg` for
//! changes -- see that module's doc for why the on-device `pet_id` this crop was associated with
//! is not attached (it lives in the same unreachable bus message).
//!
//! Layout: `PENDING_DIR/<unix>-<label>.jpg` (label is `pet_id` if one is ever available, else
//! `"unknown"`) plus, once computed, two same-stem sidecars: `.emb` (its embedding, eagerly at
//! capture time by `ai.rs`, or lazily by [`ensure_embedding`] the first time anything needs one)
//! and `.guess` (the classifier's verdict at capture time, via [`save_pending_guess`] -- absent
//! when the classifier didn't confidently match an enrolled cat). A crop written before its
//! vendor `track` event lands keeps the `-unknown` label until [`associate_track`] renames it
//! (and its sidecars) to the real `pet_id`, within [`TRACK_ASSOCIATION_WINDOW_SECS`] of the
//! track's own `start_time` -- see `ai.rs`'s module doc for why the crop can arrive several
//! minutes early. Pending is capped at [`MAX_PENDING`] `.jpg` crops (sidecars ride along, never
//! counted themselves) with the oldest evicted by mtime. A human names a pending crop with
//! `POST /faces/label` ([`label`]), which moves the crop and its sidecars to
//! `<FACES_ROOT>/<cat>/<same filename>` -- permanent storage, outside the cap, one directory per
//! label -- and feeds the embedding into that cat's running centroid ([`Gallery::on_labelled`]).
//! [`unlabel`] is the exact inverse for the `.jpg`/`.emb` pair, for a mistaken label or the first
//! half of a re-label; the `.guess` is dropped rather than restored, since it was the
//! classifier's opinion *before* a human ever looked, stale the moment a human did.
//! [`SKIP_BUCKET`]/[`NOT_A_CAT_BUCKET`] are two reserved `cat` values (this module's own doc
//! already anticipated both, before any of this existed): a crop labelled into either still
//! leaves the review queue and keeps its embedding on record, but is never counted as "a cat" --
//! excluded from `GET /cats` and never fed to the classifier. Each enrolled cat's `GET /cats`
//! entry also reports an `"avatar"`: [`list_samples`]'s crop whose cached embedding is nearest
//! the cat's own centroid, the sample that looks most like the trained identity rather than
//! merely the newest one.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::catid::{self, Classifier};
use crate::embed::{self, EmbedError, Embedding};

pub const FACES_ROOT: &str = "/opt/kibble/faces";
pub const PENDING_DIR: &str = "/opt/kibble/faces/pending";
/// `/opt` has roughly 50 MB free on this device; a face crop is a few tens of KB, so 200 caps
/// pending storage at single-digit megabytes even if nothing is ever labelled.
pub const MAX_PENDING: usize = 200;

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn now_unix_millis() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0)
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
    let name = save_pending_in(Path::new(PENDING_DIR), bytes, pet_id)?;
    mark_faces_changed();
    Ok(name)
}

/// The face store's four HA-visible views (`/cats`, `/identify`, `/faces/current/info`,
/// `/faces/pending`) all derive from the same directories, so any mutation of those
/// directories dirties all four at once.
fn mark_faces_changed() {
    use crate::push::Field;
    crate::push::mark_all(&[Field::Cats, Field::Identify, Field::ReviewFace, Field::PendingFaces]);
}

fn save_pending_in(dir: &Path, bytes: &[u8], pet_id: Option<u32>) -> io::Result<String> {
    fs::create_dir_all(dir)?;
    let label = pet_id.map(|id| id.to_string()).unwrap_or_else(|| "unknown".into());
    let name = format!("{}-{label}.jpg", now_unix());
    fs::write(dir.join(&name), bytes)?;
    evict_oldest_if_over_cap(dir, MAX_PENDING)?;
    Ok(name)
}

/// Delete oldest-by-capture-time crops until at most `cap` real crops (`.jpg` files) remain.
/// Sidecars (`.emb`, `.guess`) never count against the cap themselves -- only a crop's own `.jpg`
/// mtime decides eviction order -- and ride along with whichever crop they belong to, so eviction
/// never leaves an orphaned sidecar pointing at a `.jpg` that's already gone. Pure filesystem
/// logic (no clock dependency beyond mtimes the OS already sets), so it's directly unit-testable.
fn evict_oldest_if_over_cap(dir: &Path, cap: usize) -> io::Result<()> {
    let mut entries: Vec<(SystemTime, PathBuf)> = fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let meta = e.metadata().ok()?;
            if !meta.is_file() {
                return None;
            }
            let path = e.path();
            if path.extension().and_then(|e| e.to_str()) != Some("jpg") {
                return None;
            }
            Some((meta.modified().ok()?, path))
        })
        .collect();
    if entries.len() <= cap {
        return Ok(());
    }
    entries.sort_by_key(|(mtime, _)| *mtime);
    for (_, path) in entries.iter().take(entries.len() - cap) {
        fs::remove_file(path)?;
        let _ = fs::remove_file(embedding_path_for(path));
        let _ = fs::remove_file(guess_path_for(path));
    }
    Ok(())
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
            let name = e.file_name().into_string().ok()?;
            if !name.ends_with(".jpg") {
                return None;
            }
            let mtime = e.metadata().ok()?.modified().ok()?;
            Some((mtime, name))
        })
        .collect();
    entries.sort();
    Ok(entries.into_iter().map(|(_, name)| name).collect())
}

/// Parses `ts` and `vendor_pet_id` out of a crop's own filename -- `{ts}-{petid|unknown}.jpg`,
/// the convention this module's doc lays out (shared by pending crops and labelled samples
/// alike, since [`label`] preserves the filename). `None` for anything that doesn't match --
/// should never happen for a name this module generated itself, but callers read names straight
/// off disk.
pub fn parse_pending_name(name: &str) -> Option<(u64, Option<u32>)> {
    let stem = name.strip_suffix(".jpg")?;
    let (ts_str, label) = stem.split_once('-')?;
    let ts = ts_str.parse::<u64>().ok()?;
    let vendor_pet_id = if label == "unknown" { None } else { label.parse::<u32>().ok() };
    Some((ts, vendor_pet_id))
}

#[derive(Debug)]
pub enum FaceError {
    InvalidName,
    InvalidCat(String),
    NotFound,
    /// `POST /faces/upload`'s body failed [`validate_upload`] -- see [`UploadError`].
    InvalidUpload(UploadError),
    Io(io::Error),
}

impl std::fmt::Display for FaceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FaceError::InvalidName => write!(f, "invalid file name"),
            FaceError::InvalidCat(c) => write!(f, "invalid \"cat\" {c:?}"),
            FaceError::NotFound => write!(f, "no such pending face crop"),
            FaceError::InvalidUpload(e) => write!(f, "{e}"),
            FaceError::Io(e) => write!(f, "{e}"),
        }
    }
}

/// Ceiling on an uploaded face-crop JPEG's raw size (`POST /faces/upload`) -- generous for a
/// 224x224 JPEG (a few tens of KB at the card's `canvas.toBlob` quality 0.9), while bounding a
/// hostile or mistaken upload well below anything that could meaningfully dent this device's
/// ~50 MB of free storage.
pub const MAX_UPLOAD_BYTES: usize = 512 * 1024;

/// Why [`validate_upload`] rejected a `POST /faces/upload` body, before ever touching disk.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UploadError {
    /// Missing the JPEG SOI-plus-marker magic (`FF D8 FF`) -- not a JPEG at all, or truncated.
    NotJpeg,
    /// Over [`MAX_UPLOAD_BYTES`]; carries the actual (rejected) size.
    TooLarge(usize),
}

impl std::fmt::Display for UploadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UploadError::NotJpeg => write!(f, "not a JPEG (missing FF D8 FF magic)"),
            UploadError::TooLarge(n) => write!(f, "{n} bytes exceeds the {MAX_UPLOAD_BYTES}-byte limit"),
        }
    }
}

/// `POST /faces/upload`'s body validation, before anything touches disk. The browser has already
/// cropped to exactly 224x224 (this module has no decoder to verify that itself -- only
/// `kibble-embed` ever decodes a JPEG on this device, see `embed.rs`'s module doc), so the only
/// two things worth checking server-side are "is this even a JPEG" and "is it a sane size" --
/// both cheap, both catch a wrong `Content-Type`, wrong form field, or hostile upload before it
/// ever reaches `kibble-embed`. Pure byte-slice logic, so it's directly unit-testable without a
/// real HTTP request.
pub fn validate_upload(bytes: &[u8]) -> Result<(), UploadError> {
    if bytes.len() > MAX_UPLOAD_BYTES {
        return Err(UploadError::TooLarge(bytes.len()));
    }
    if !bytes.starts_with(&[0xFF, 0xD8, 0xFF]) {
        return Err(UploadError::NotJpeg);
    }
    Ok(())
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
    label_in(Path::new(PENDING_DIR), Path::new(FACES_ROOT), name, cat)?;
    mark_faces_changed();
    Ok(())
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
    let src_emb = embedding_path_for(&src);
    let src_guess = guess_path_for(&src);
    let dest_dir = faces_root.join(cat);
    fs::create_dir_all(&dest_dir).map_err(FaceError::Io)?;
    let dest = dest_dir.join(name);
    fs::rename(&src, &dest).map_err(FaceError::Io)?;
    if src_emb.is_file() {
        let _ = fs::rename(&src_emb, embedding_path_for(&dest));
    }
    if src_guess.is_file() {
        let _ = fs::rename(&src_guess, guess_path_for(&dest));
    }
    Ok(())
}

/// `cat` values `POST /faces/label` treats specially: moved and embedded exactly like a real
/// cat (so the crop leaves the review queue and its embedding stays on record for later audit),
/// but never fed into the classifier and never listed by `GET /cats` -- picking one of these in
/// the HA label select means "I looked, and it's not something to train the identifier on"
/// rather than "this is a new cat". Names match the labelling convention this module's own doc
/// comment already anticipated before any of this existed.
pub const SKIP_BUCKET: &str = "other";
pub const NOT_A_CAT_BUCKET: &str = "not_a_cat";
const RESERVED_BUCKETS: &[&str] = &[SKIP_BUCKET, NOT_A_CAT_BUCKET];

pub fn is_reserved_bucket(cat: &str) -> bool {
    RESERVED_BUCKETS.contains(&cat)
}

/// The literal directory name [`PENDING_DIR`] resolves to ("pending") -- matches
/// [`list_samples_in`]/[`read_sample_in`]'s existing per-call guard against ever treating the
/// pending staging directory itself as if it were a real, enrollable cat
/// (`enrolled_cats_exclude_pending_and_reserved_buckets`'s test comment: this exact phantom-cat
/// class of bug already shipped once). [`Gallery::delete_cat`]/[`Gallery::delete_sample`]/
/// [`Gallery::upload_sample`] reuse it since a stray `cat=pending` would otherwise be able to
/// destroy or pollute the whole unreviewed-crop queue, not just mislabel one phantom cat.
fn pending_dir_name() -> &'static str {
    Path::new(PENDING_DIR).file_name().and_then(|s| s.to_str()).unwrap_or("pending")
}

/// Sidecar path for a crop's cached embedding: same directory and stem, `.emb` extension.
fn embedding_path_for(jpg_path: &Path) -> PathBuf {
    jpg_path.with_extension("emb")
}

/// Writes a raw embedding sidecar: [`embed::EMBED_DIM`] little-endian `f32`s, no header -- the
/// format is fixed by convention (this module both writes and reads it), matching how
/// `bus.rs`/`schedule.rs` encode their own fixed-shape records without a schema.
fn save_embedding(jpg_path: &Path, feat: &[f32; embed::EMBED_DIM]) -> io::Result<()> {
    let mut bytes = Vec::with_capacity(embed::EMBED_DIM * 4);
    for f in feat {
        bytes.extend_from_slice(&f.to_le_bytes());
    }
    fs::write(embedding_path_for(jpg_path), bytes)
}

/// Reads a `.emb` sidecar written by [`save_embedding`]. `None` for anything that doesn't parse
/// cleanly (missing, wrong length, ...) -- the caller always has a fallback (recompute), so this
/// deliberately collapses every failure mode to "not cached" rather than a typed error nobody
/// would branch on differently.
fn load_embedding(path: &Path) -> Option<[f32; embed::EMBED_DIM]> {
    let bytes = fs::read(path).ok()?;
    if bytes.len() != embed::EMBED_DIM * 4 {
        return None;
    }
    let mut feat = [0.0f32; embed::EMBED_DIM];
    for (i, chunk) in bytes.chunks_exact(4).enumerate() {
        feat[i] = f32::from_le_bytes(chunk.try_into().expect("chunks_exact(4)"));
    }
    Some(feat)
}

/// Returns the embedding for `jpg_path`, using the cached `.emb` sidecar if it parses cleanly,
/// else computing it fresh via [`embed::extract`] and caching the result -- "compute once and
/// cache" for both the eager (capture-time, `ai.rs`) and lazy (backfill: any crop that predates
/// this feature, or whose eager computation failed) paths, from one function.
pub fn ensure_embedding(jpg_path: &Path) -> Result<[f32; embed::EMBED_DIM], EmbedError> {
    if let Some(feat) = load_embedding(&embedding_path_for(jpg_path)) {
        return Ok(feat);
    }
    let Embedding { feat, .. } = embed::extract(jpg_path)?;
    let _ = save_embedding(jpg_path, &feat); // best-effort cache; a failed write just means the
                                              // next call recomputes -- never blocks the caller
    Ok(feat)
}

/// Sidecar path for a crop's cached classifier guess: same directory and stem, `.guess`
/// extension -- same convention as [`embedding_path_for`]'s `.emb`.
fn guess_path_for(jpg_path: &Path) -> PathBuf {
    jpg_path.with_extension("guess")
}

/// The classifier's verdict on a pending crop, cached beside it at capture time by
/// [`save_pending_guess`] -- see the module doc.
#[derive(Debug, Clone, PartialEq)]
pub struct Guess {
    pub cat: String,
    pub score: f32,
}

/// Writes a `.guess` sidecar as flat JSON (`{"cat":"...","score":...}`) -- this module both
/// writes and reads it, and reuses `http::json_field` to parse it back rather than pulling in a
/// JSON crate, matching this project's established convention (see `http.rs`'s own doc comment).
fn save_guess_file(jpg_path: &Path, guess: &Guess) -> io::Result<()> {
    let json = format!(r#"{{"cat":"{}","score":{}}}"#, guess.cat.escape_debug(), guess.score);
    fs::write(guess_path_for(jpg_path), json)
}

/// Reads a `.guess` sidecar written by [`save_guess_file`]. `None` for anything that doesn't
/// parse cleanly, including "no such file" -- a crop the classifier didn't confidently match
/// never gets one, and that is indistinguishable from (and treated identically to) "not cached".
fn load_guess(jpg_path: &Path) -> Option<Guess> {
    let text = fs::read_to_string(guess_path_for(jpg_path)).ok()?;
    let cat = crate::http::json_field(&text, "cat")?.to_string();
    let score: f32 = crate::http::json_field(&text, "score")?.parse().ok()?;
    Some(Guess { cat, score })
}

/// `ai.rs::poll_loop` calls this right after [`Gallery::identify`] returns a confident match for
/// a freshly captured pending crop -- one small on-change write per newly identified face, never
/// on a timer and never per poll tick (an unmatched/unknown crop simply never gets a `.guess`
/// file, which [`load_guess`] already treats identically to "no verdict yet").
pub fn save_pending_guess(name: &str, guess: &Guess) -> io::Result<()> {
    save_guess_file(&Path::new(PENDING_DIR).join(name), guess)
}

/// One pending crop as `GET /faces/pending` reports it: everything encoded in the filename plus
/// whatever the classifier thought at capture time.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingCrop {
    pub name: String,
    pub ts: u64,
    pub vendor_pet_id: Option<u32>,
    pub guess: Option<Guess>,
}

/// `GET /faces/pending`'s full shape -- see [`PendingCrop`] -- sorted by `ts` ascending (newest
/// last), matching the endpoint's contract.
pub fn list_pending_full() -> io::Result<Vec<PendingCrop>> {
    list_pending_full_in(Path::new(PENDING_DIR))
}

fn list_pending_full_in(dir: &Path) -> io::Result<Vec<PendingCrop>> {
    let mut crops: Vec<PendingCrop> = list_pending_in(dir)?
        .into_iter()
        .filter_map(|name| {
            let (ts, vendor_pet_id) = parse_pending_name(&name)?;
            let guess = load_guess(&dir.join(&name));
            Some(PendingCrop { name, ts, vendor_pet_id, guess })
        })
        .collect();
    crops.sort_by_key(|c| c.ts);
    Ok(crops)
}

/// `POST /faces/unlabel {"name": "...", "cat": "..."}`: the exact inverse of [`label`] for the
/// `.jpg`/`.emb` pair -- moves a previously-labelled crop (and its `.emb` sidecar, if any) back
/// into the pending review queue. Any `.guess` sidecar is dropped rather than restored -- see
/// [`unlabel_in`]'s own comment for why. A full re-label is this followed by [`label`] into the
/// correct cat.
pub fn unlabel(cat: &str, name: &str) -> Result<(), FaceError> {
    unlabel_in(Path::new(FACES_ROOT), Path::new(PENDING_DIR), cat, name)?;
    mark_faces_changed();
    Ok(())
}

fn unlabel_in(faces_root: &Path, pending_dir: &Path, cat: &str, name: &str) -> Result<(), FaceError> {
    if !is_safe_name(name) {
        return Err(FaceError::InvalidName);
    }
    if !is_safe_name(cat) {
        return Err(FaceError::InvalidCat(cat.to_string()));
    }
    let src = faces_root.join(cat).join(name);
    if !src.is_file() {
        return Err(FaceError::NotFound);
    }
    let src_emb = embedding_path_for(&src);
    // The guess is a snapshot of what the classifier thought *before* a human ever looked at this
    // crop; once labelled, that snapshot is stale, so unlabelling drops it rather than restoring
    // it to the pending queue -- see the module doc.
    let src_guess = guess_path_for(&src);
    fs::create_dir_all(pending_dir).map_err(FaceError::Io)?;
    let dest = pending_dir.join(name);
    fs::rename(&src, &dest).map_err(FaceError::Io)?;
    if src_emb.is_file() {
        let _ = fs::rename(&src_emb, embedding_path_for(&dest));
    }
    let _ = fs::remove_file(&src_guess);
    Ok(())
}

/// Window within which a pending crop written before its `track` event lands -- filename still
/// says `-unknown` -- is retroactively associated with the vendor's `pet_id`. `ai.rs`'s module
/// doc records crops arriving up to ~3 minutes *before* their track's own `start_time`; 300s
/// covers that with margin in either direction (a `track` tick landing fractionally early against
/// wall-clock skew costs nothing to also accept).
pub const TRACK_ASSOCIATION_WINDOW_SECS: u64 = 300;

/// Renames every pending `{ts}-unknown.jpg` crop (and its sidecars) whose `ts` is within
/// [`TRACK_ASSOCIATION_WINDOW_SECS`] of `start_time` to `{ts}-{pet_id}.jpg` -- the vendor's own
/// identification landing after the crop was already written (`ai.rs`'s module doc, "Update
/// 2026-09-16"). Returns the renamed crops' new names. Pure filesystem logic against `dir`, so
/// it's directly unit-testable; [`associate_track`] wires it to the real pending directory.
pub fn associate_track_in(dir: &Path, pet_id: u32, start_time: u64) -> io::Result<Vec<String>> {
    let mut renamed = Vec::new();
    let read = match fs::read_dir(dir) {
        Ok(r) => r,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(renamed),
        Err(e) => return Err(e),
    };
    for entry in read.filter_map(|e| e.ok()) {
        let Ok(name) = entry.file_name().into_string() else { continue };
        let Some(ts_str) = name.strip_suffix("-unknown.jpg") else { continue };
        let Ok(ts) = ts_str.parse::<u64>() else { continue };
        if ts.abs_diff(start_time) > TRACK_ASSOCIATION_WINDOW_SECS {
            continue;
        }
        let new_name = format!("{ts}-{pet_id}.jpg");
        let src = dir.join(&name);
        let dest = dir.join(&new_name);
        if dest.exists() {
            continue; // already associated (e.g. a repeated track tick) -- never overwrite
        }
        fs::rename(&src, &dest)?;
        let _ = fs::rename(embedding_path_for(&src), embedding_path_for(&dest));
        let _ = fs::rename(guess_path_for(&src), guess_path_for(&dest));
        renamed.push(new_name);
    }
    Ok(renamed)
}

/// Wires [`associate_track_in`] to the real pending directory and marks the push channel dirty
/// when anything actually changed -- called from `ai.rs`'s poll loop on every new track event.
pub fn associate_track(pet_id: u32, start_time: u64) -> io::Result<Vec<String>> {
    let renamed = associate_track_in(Path::new(PENDING_DIR), pet_id, start_time)?;
    if !renamed.is_empty() {
        mark_faces_changed();
    }
    Ok(renamed)
}

/// One permanently-labelled crop, as found under a `FACES_ROOT/<cat>/` directory.
#[derive(Debug, Clone)]
pub struct LabelledCrop {
    pub cat: String,
    pub name: String,
    pub jpg_path: PathBuf,
    pub mtime: SystemTime,
}

/// Every labelled crop across every cat directory (excluding `pending/` and reserved buckets --
/// see [`RESERVED_BUCKETS`]). Missing `FACES_ROOT` is an empty list, not an error (nothing
/// labelled yet).
pub fn list_labelled() -> io::Result<Vec<LabelledCrop>> {
    list_labelled_in(Path::new(FACES_ROOT))
}

fn list_labelled_in(faces_root: &Path) -> io::Result<Vec<LabelledCrop>> {
    let read = match fs::read_dir(faces_root) {
        Ok(r) => r,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut out = Vec::new();
    for entry in read.filter_map(|e| e.ok()) {
        let Ok(file_type) = entry.file_type() else { continue };
        if !file_type.is_dir() {
            continue;
        }
        let Ok(cat) = entry.file_name().into_string() else { continue };
        if cat == "pending" || is_reserved_bucket(&cat) {
            continue;
        }
        let cat_dir = entry.path();
        let Ok(cat_read) = fs::read_dir(&cat_dir) else { continue };
        for crop in cat_read.filter_map(|e| e.ok()) {
            let path = crop.path();
            if path.extension().and_then(|e| e.to_str()) != Some("jpg") {
                continue;
            }
            let Ok(meta) = crop.metadata() else { continue };
            let Ok(mtime) = meta.modified() else { continue };
            let Ok(name) = crop.file_name().into_string() else { continue };
            out.push(LabelledCrop { cat: cat.clone(), name, jpg_path: path, mtime });
        }
    }
    Ok(out)
}

/// One labelled sample as `GET /faces/samples/<cat>` reports it: name plus capture time, parsed
/// from the filename exactly like a pending crop's ([`parse_pending_name`]) -- labelling
/// preserves the filename, so the same parser applies.
#[derive(Debug, Clone, PartialEq)]
pub struct Sample {
    pub name: String,
    pub ts: u64,
}

/// `GET /faces/samples/<cat>`: every labelled crop under that cat's directory, oldest first.
/// `cat` is validated exactly like [`label`]'s -- no traversal, and the `pending` staging
/// directory itself is never a valid `cat` (mirrors [`list_enrolled_cats_in`]'s guard against the
/// same phantom-cat bug). An unknown `cat` is [`FaceError::NotFound`].
pub fn list_samples(cat: &str) -> Result<Vec<Sample>, FaceError> {
    list_samples_in(Path::new(FACES_ROOT), Path::new(PENDING_DIR), cat)
}

fn list_samples_in(faces_root: &Path, pending_dir: &Path, cat: &str) -> Result<Vec<Sample>, FaceError> {
    if !is_safe_name(cat) {
        return Err(FaceError::InvalidCat(cat.to_string()));
    }
    let pending_name = pending_dir.file_name().and_then(|s| s.to_str()).unwrap_or("pending");
    if cat == pending_name {
        return Err(FaceError::NotFound);
    }
    let read = match fs::read_dir(faces_root.join(cat)) {
        Ok(r) => r,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Err(FaceError::NotFound),
        Err(e) => return Err(FaceError::Io(e)),
    };
    let mut samples: Vec<Sample> = read
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let name = e.file_name().into_string().ok()?;
            let ts = sample_ts(&name)?;
            Some(Sample { name, ts })
        })
        .collect();
    samples.sort_by_key(|s| s.ts);
    Ok(samples)
}

/// A labelled sample's timestamp from its own name: a feeder capture's `{ts}-…jpg`, or an
/// uploaded reference photo's `upload-{unix_ms}.jpg` (whole seconds). Sidecars and anything
/// else in the folder are `None`.
fn sample_ts(name: &str) -> Option<u64> {
    if let Some(ms) = name.strip_prefix(UPLOAD_PREFIX).and_then(|rest| rest.strip_suffix(".jpg")) {
        return ms.parse::<u64>().ok().map(|ms| ms / 1000);
    }
    parse_pending_name(name).map(|(ts, _)| ts)
}

/// `GET /faces/samples/<cat>/<name>`: the raw JPEG bytes. Same path-safety rules as
/// [`read_pending`] for `name`, and the same `cat` rules as [`list_samples`].
pub fn read_sample(cat: &str, name: &str) -> Result<Vec<u8>, FaceError> {
    read_sample_in(Path::new(FACES_ROOT), Path::new(PENDING_DIR), cat, name)
}

fn read_sample_in(faces_root: &Path, pending_dir: &Path, cat: &str, name: &str) -> Result<Vec<u8>, FaceError> {
    if !is_safe_name(cat) {
        return Err(FaceError::InvalidCat(cat.to_string()));
    }
    if !is_safe_name(name) {
        return Err(FaceError::InvalidName);
    }
    let pending_name = pending_dir.file_name().and_then(|s| s.to_str()).unwrap_or("pending");
    if cat == pending_name {
        return Err(FaceError::NotFound);
    }
    match fs::read(faces_root.join(cat).join(name)) {
        Ok(b) => Ok(b),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Err(FaceError::NotFound),
        Err(e) => Err(FaceError::Io(e)),
    }
}

/// The single most recently labelled crop across every (non-reserved) cat, if any -- the
/// fallback source for both [`review_target`] and [`identify_target`] once the pending queue is
/// empty: there is no *new* visit to show, but there is still a most-recent *known* one.
fn newest_labelled(faces_root: &Path) -> Option<LabelledCrop> {
    list_labelled_in(faces_root).ok()?.into_iter().max_by_key(|c| c.mtime)
}

/// What [`review_target`]/[`identify_target`] resolved to, and where its bytes live.
#[derive(Debug, Clone, PartialEq)]
pub enum FaceTarget {
    /// `PENDING_DIR/<name>`, awaiting a human label.
    Pending { name: String },
    /// `FACES_ROOT/<cat>/<name>`, already labelled.
    Labelled { cat: String, name: String },
}

impl FaceTarget {
    pub fn jpg_path(&self) -> PathBuf {
        match self {
            FaceTarget::Pending { name } => Path::new(PENDING_DIR).join(name),
            FaceTarget::Labelled { cat, name } => Path::new(FACES_ROOT).join(cat).join(name),
        }
    }

    pub fn name(&self) -> &str {
        match self {
            FaceTarget::Pending { name } | FaceTarget::Labelled { name, .. } => name,
        }
    }
}

/// The crop to show a human for review: the *oldest* pending crop (a FIFO backlog, so an old
/// unreviewed visit is never buried by newer ones), or the most recently labelled crop if the
/// queue is empty (so the picture is never blank once caught up). Backs `GET /faces/current` /
/// the HA `image.*_pending_face` entity.
pub fn review_target() -> io::Result<Option<FaceTarget>> {
    review_target_in(Path::new(PENDING_DIR), Path::new(FACES_ROOT))
}

fn review_target_in(pending_dir: &Path, faces_root: &Path) -> io::Result<Option<FaceTarget>> {
    if let Some(name) = list_pending_in(pending_dir)?.into_iter().next() {
        return Ok(Some(FaceTarget::Pending { name }));
    }
    Ok(newest_labelled(faces_root).map(|c| FaceTarget::Labelled { cat: c.cat, name: c.name }))
}

/// The crop to identify "who was just here": the *newest* pending crop (the freshest unlabelled
/// visit), or the most recently labelled crop if the queue is empty (a human already answered
/// this question for the freshest visit). Backs `GET /identify`.
pub fn identify_target() -> io::Result<Option<FaceTarget>> {
    identify_target_in(Path::new(PENDING_DIR), Path::new(FACES_ROOT))
}

fn identify_target_in(pending_dir: &Path, faces_root: &Path) -> io::Result<Option<FaceTarget>> {
    if let Some(name) = list_pending_in(pending_dir)?.into_iter().next_back() {
        return Ok(Some(FaceTarget::Pending { name }));
    }
    Ok(newest_labelled(faces_root).map(|c| FaceTarget::Labelled { cat: c.cat, name: c.name }))
}

/// Prefix of a reference photo a human uploaded ([`Gallery::upload_sample`]) -- the one kind of
/// labelled crop that is *not* evidence the cat was at the feeder.
pub const UPLOAD_PREFIX: &str = "upload-";

/// Every feeder-captured labelled crop as `(capture ts, cat)` -- uploads excluded, they are
/// not sightings. What `ai::Feed` uses to name `"face"` events after a restart.
pub fn labelled_cats_by_ts() -> Vec<(u64, String)> {
    list_labelled()
        .unwrap_or_default()
        .into_iter()
        .filter(|c| !c.name.starts_with(UPLOAD_PREFIX))
        .filter_map(|c| parse_pending_name(&c.name).map(|(ts, _)| (ts, c.cat)))
        .collect()
}

fn last_seen_by_cat() -> HashMap<String, u64> {
    last_seen_from(list_labelled().unwrap_or_default())
}

/// "Last here" per cat: the newest crop the *feeder captured* and a human (or the classifier)
/// put in that cat's folder. Uploaded reference photos are skipped -- their mtime is when the
/// photo was added, not a sighting, and counting them made a cat "last here 2 min ago" right
/// after someone uploaded its picture.
fn last_seen_from(crops: Vec<LabelledCrop>) -> HashMap<String, u64> {
    let mut out = HashMap::new();
    for crop in crops {
        if crop.name.starts_with(UPLOAD_PREFIX) {
            continue;
        }
        let ts = crop.mtime.duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        let entry = out.entry(crop.cat).or_insert(0);
        if ts > *entry {
            *entry = ts;
        }
    }
    out
}

/// Every labelled, non-reserved crop's cached embedding, re-read from disk -- the input
/// [`catid::derive_threshold`] needs. Deliberately not cached in memory: a threshold recompute
/// happens only on a human labelling action (rare), so re-reading a few hundred small `.emb`
/// files each time is cheaper than permanently doubling the classifier's RSS with a second copy
/// of every embedding it already summarised into centroids.
fn all_sample_embeddings() -> Vec<(String, [f32; embed::EMBED_DIM])> {
    list_labelled()
        .unwrap_or_default()
        .into_iter()
        .filter(|c| !is_reserved_bucket(&c.cat))
        .filter_map(|c| ensure_embedding(&c.jpg_path).ok().map(|feat| (c.cat, feat)))
        .collect()
}

/// Applies [`catid::derive_threshold`]'s result to `classifier.t_accept` when there is enough
/// data to measure one; otherwise leaves the existing threshold (starting from
/// [`catid::DEFAULT_T_ACCEPT`] until enough data exists) untouched and logs why, rather than
/// guessing. `docs/27-cat-id.md` documents this behaviour for Nitin.
fn apply_threshold(classifier: &mut Classifier, samples: &[(String, [f32; embed::EMBED_DIM])]) {
    match catid::derive_threshold(samples) {
        Ok(report) => classifier.t_accept = report.accept,
        Err(e) => {
            eprintln!("kibbled: faces: threshold not yet measurable ({e:?}); keeping current t_accept")
        }
    }
}

/// The live classifier plus the disk-backed labelled set it was built from -- one instance,
/// shared (behind an `Arc`, per `main.rs`) between the HTTP handler thread and `ai.rs`'s poller.
pub struct Gallery {
    inner: Mutex<Classifier>,
}

/// Every cat directory under [`FACES_ROOT`], including ones with no crops yet, excluding the
/// reserved buckets ([`RESERVED_BUCKETS`]) and the pending-crop staging directory. Used by
/// [`Gallery::load`] so an enrolled cat survives a restart before it has its first labelled
/// sample.
///
/// `pending` lives *inside* `FACES_ROOT` but is not a reserved bucket (it is a staging area, not
/// a label), so it has to be excluded explicitly -- otherwise it is enrolled as a cat named
/// "pending", which is exactly what happened the first time this function shipped.
fn list_enrolled_cats() -> Vec<String> {
    list_enrolled_cats_in(Path::new(FACES_ROOT), Path::new(PENDING_DIR))
}

fn list_enrolled_cats_in(faces_root: &Path, pending_dir: &Path) -> Vec<String> {
    let pending_name = pending_dir.file_name().and_then(|s| s.to_str()).unwrap_or("pending");
    let Ok(read) = fs::read_dir(faces_root) else { return Vec::new() };
    let mut out = Vec::new();
    for entry in read.filter_map(|e| e.ok()) {
        if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if is_safe_name(&name) && !is_reserved_bucket(&name) && name != pending_name {
            out.push(name);
        }
    }
    out.sort();
    out
}

/// The labelled sample whose embedding is nearest (highest cosine similarity) to `cat`'s current
/// centroid -- what `GET /cats`'s `"avatar"` shows: the crop that looks most like the cat's
/// trained identity, not merely the most recently labelled one. `None` when the cat has no
/// samples yet (an empty, [`Gallery::add_cat`]-enrolled directory) or nothing embeds cleanly.
fn nearest_sample(cat_model: &catid::CatModel, crops: &[LabelledCrop]) -> Option<String> {
    crops
        .iter()
        .filter(|c| c.cat == cat_model.name)
        .filter_map(|c| {
            let mut normalized = ensure_embedding(&c.jpg_path).ok()?;
            if !catid::l2_normalize(&mut normalized) {
                return None;
            }
            let score = cat_model.cosine_to(&normalized)?;
            Some((score, c.name.clone()))
        })
        .max_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
        .map(|(_, name)| name)
}

/// Below this, [`Gallery::upload_sample`] still stores and trains on the uploaded crop (a human
/// explicitly chose it) but flags `"low_quality":true` in the response. The contract's own
/// number, not a measurement -- unrelated to [`catid::Classifier::t_accept`]'s derived threshold,
/// which compares embeddings *between* cats; this instead is the embedding model's own single-
/// crop quality/liveness gate (`docs/27-cat-id.md`), read straight off `embed::Embedding::prob`.
pub const LOW_QUALITY_PROB: f32 = 0.2;

/// One successful `POST /faces/upload` -- see [`Gallery::upload_sample`].
#[derive(Debug, Clone, PartialEq)]
pub struct UploadedSample {
    pub name: String,
    pub samples: usize,
    pub low_quality: bool,
}

impl Gallery {
    /// Builds the classifier from every labelled crop on disk, computing (and caching) any
    /// embedding that isn't already cached, and re-registers every enrolled cat directory even
    /// when it holds no crops yet. Meant for startup; scans the whole tree, so it is not meant to
    /// be called on every request.
    ///
    /// The sample-less pass matters: a cat added via [`Gallery::add_cat`] before its first crop is
    /// only an empty directory, so rebuilding from crops alone silently forgot it on every restart
    /// (and `kibbled` restarts routinely). Enrolling your cats and then losing them to the next
    /// deploy is exactly the kind of quiet data loss this avoids.
    pub fn load() -> Gallery {
        let mut classifier = Classifier::new();
        for name in list_enrolled_cats() {
            classifier.ensure_cat(&name);
        }
        let crops = list_labelled().unwrap_or_default();
        let mut samples = Vec::with_capacity(crops.len());
        for crop in &crops {
            match ensure_embedding(&crop.jpg_path) {
                Ok(feat) => {
                    classifier.label(&crop.cat, &feat);
                    samples.push((crop.cat.clone(), feat));
                }
                Err(e) => eprintln!(
                    "kibbled: faces: could not embed {} on startup: {e}",
                    crop.jpg_path.display()
                ),
            }
        }
        apply_threshold(&mut classifier, &samples);
        Gallery { inner: Mutex::new(classifier) }
    }

    pub fn identify(&self, feat: &[f32; embed::EMBED_DIM]) -> catid::Verdict {
        self.inner.lock().unwrap().identify(feat)
    }

    /// `GET /cats`: every enrolled cat with at least one sample or explicitly pre-created via
    /// [`Gallery::add_cat`], sorted by name for a stable listing. `avatar` is [`nearest_sample`]'s
    /// pick, `null` for a sample-less cat.
    pub fn cats_json(&self) -> String {
        // Cloned out from under the lock before any filesystem work (`nearest_sample` reads
        // cached embeddings off disk) -- `CatModel` is cheap to clone and this keeps the mutex
        // held for microseconds instead of however long disk I/O takes.
        let mut cats: Vec<catid::CatModel> = {
            let classifier = self.inner.lock().unwrap();
            classifier.cats().cloned().collect()
        };
        cats.sort_by(|a, b| a.name.cmp(&b.name));
        let last_seen = last_seen_by_cat();
        let crops = list_labelled().unwrap_or_default();
        let items: Vec<String> = cats
            .iter()
            .map(|c| {
                let seen = last_seen.get(c.name.as_str()).map_or("null".to_string(), u64::to_string);
                let avatar = nearest_sample(c, &crops)
                    .map_or("null".to_string(), |n| format!("\"{}\"", n.escape_debug()));
                format!(
                    r#"{{"name":"{}","samples":{},"last_seen":{},"avatar":{}}}"#,
                    c.name.escape_debug(),
                    c.count,
                    seen,
                    avatar,
                )
            })
            .collect();
        format!("[{}]", items.join(","))
    }

    /// `POST /cats {"name": "..."}`: pre-register a cat with zero samples, so it appears in
    /// `GET /cats` (and the HA label select's options) before its first crop is ever labelled.
    /// Rejects a reserved bucket name or an unsafe one; idempotent otherwise.
    pub fn add_cat(&self, name: &str) -> Result<(), FaceError> {
        if !is_safe_name(name) || is_reserved_bucket(name) {
            return Err(FaceError::InvalidCat(name.to_string()));
        }
        fs::create_dir_all(Path::new(FACES_ROOT).join(name)).map_err(FaceError::Io)?;
        self.inner.lock().unwrap().ensure_cat(name);
        mark_faces_changed();
        Ok(())
    }

    /// Call after a crop is successfully moved into `cat`'s directory ([`label`] succeeded):
    /// updates the in-memory centroid and re-measures the accept threshold from the full,
    /// current disk state. `feat` is the crop's own embedding (already computed/cached by the
    /// caller via [`ensure_embedding`]). A no-op for a reserved bucket -- see [`RESERVED_BUCKETS`].
    pub fn on_labelled(&self, cat: &str, feat: &[f32; embed::EMBED_DIM]) {
        if is_reserved_bucket(cat) {
            return;
        }
        let mut classifier = self.inner.lock().unwrap();
        classifier.label(cat, feat);
        let samples = all_sample_embeddings();
        apply_threshold(&mut classifier, &samples);
    }

    /// Call after a crop is successfully moved out of `cat`'s directory ([`unlabel`] succeeded):
    /// the exact inverse of [`Gallery::on_labelled`].
    pub fn on_unlabelled(&self, cat: &str, feat: &[f32; embed::EMBED_DIM]) {
        if is_reserved_bucket(cat) {
            return;
        }
        let mut classifier = self.inner.lock().unwrap();
        classifier.unlabel(cat, feat);
        let samples = all_sample_embeddings();
        apply_threshold(&mut classifier, &samples);
    }

    /// `DELETE /cats/<name>`: removes a cat entirely -- its whole directory (every labelled
    /// sample and sidecar under it) and the classifier's in-memory model, together -- the
    /// inverse of [`Gallery::add_cat`]. `name` is validated exactly like `add_cat`'s (no
    /// traversal, not a reserved bucket -- those were never a real cat to begin with); an
    /// unknown but validly-named cat is [`FaceError::NotFound`].
    pub fn delete_cat(&self, name: &str) -> Result<(), FaceError> {
        if !is_safe_name(name) || is_reserved_bucket(name) {
            return Err(FaceError::InvalidCat(name.to_string()));
        }
        if name == pending_dir_name() {
            return Err(FaceError::NotFound);
        }
        let dir = Path::new(FACES_ROOT).join(name);
        if !dir.is_dir() {
            return Err(FaceError::NotFound);
        }
        fs::remove_dir_all(&dir).map_err(FaceError::Io)?;
        {
            let mut classifier = self.inner.lock().unwrap();
            classifier.remove_cat(name);
            let samples = all_sample_embeddings();
            apply_threshold(&mut classifier, &samples);
        }
        mark_faces_changed();
        Ok(())
    }

    /// `DELETE /faces/samples/<cat>/<name>`: permanently removes one labelled sample (and its
    /// `.emb`/`.guess` sidecars) and rebuilds `cat`'s centroid from every sample that remains.
    /// Unlike [`Gallery::on_unlabelled`]'s O(1) subtraction (the exact inverse of the one
    /// embedding just removed), this recomputes from scratch via
    /// [`catid::Classifier::recompute_cat`] by re-reading every remaining `.emb` sidecar --
    /// deletion is permanent, with no `POST /faces/label` path back the way an unlabel has, so
    /// there is no single embedding to subtract for a photo a human uploaded directly (see
    /// [`Gallery::upload_sample`]). `name`/`cat` are validated exactly like [`unlabel`]'s; a
    /// missing sample is [`FaceError::NotFound`].
    pub fn delete_sample(&self, cat: &str, name: &str) -> Result<(), FaceError> {
        if !is_safe_name(name) {
            return Err(FaceError::InvalidName);
        }
        if !is_safe_name(cat) {
            return Err(FaceError::InvalidCat(cat.to_string()));
        }
        if cat == pending_dir_name() {
            return Err(FaceError::NotFound);
        }
        let path = Path::new(FACES_ROOT).join(cat).join(name);
        if !path.is_file() {
            return Err(FaceError::NotFound);
        }
        fs::remove_file(&path).map_err(FaceError::Io)?;
        let _ = fs::remove_file(embedding_path_for(&path));
        let _ = fs::remove_file(guess_path_for(&path));
        if !is_reserved_bucket(cat) {
            let embeddings: Vec<[f32; embed::EMBED_DIM]> = list_labelled()
                .unwrap_or_default()
                .into_iter()
                .filter(|c| c.cat == cat)
                .filter_map(|c| ensure_embedding(&c.jpg_path).ok())
                .collect();
            let mut classifier = self.inner.lock().unwrap();
            classifier.recompute_cat(cat, &embeddings);
            let samples = all_sample_embeddings();
            apply_threshold(&mut classifier, &samples);
        }
        mark_faces_changed();
        Ok(())
    }

    /// `POST /faces/upload?cat=<name>`: ingests a browser-cropped 224x224 JPEG directly into
    /// `cat`'s permanent sample storage and its running centroid, in one step -- the manual
    /// counterpart to `POST /faces/label`, which can only promote a crop the vendor's own
    /// capture pipeline already wrote to [`PENDING_DIR`]. `cat` must already be enrolled
    /// ([`FaceError::NotFound`] otherwise -- unlike `label`, upload never creates a cat: a human
    /// adding reference photos has already gone through `POST /cats`). `bytes` is validated by
    /// [`validate_upload`] before anything touches disk. Saved as `upload-<unix_ms>.jpg`; the
    /// `upload-` prefix distinguishes an uploaded reference photo from a vendor-captured
    /// `<ts>-<petid|unknown>.jpg` one at a glance -- and is the reason a client deletes it via
    /// [`Gallery::delete_sample`], never `POST /faces/unlabel` (there is no vendor-side pending
    /// queue an uploaded photo could ever return to). `low_quality` mirrors the embedding
    /// model's own `prob` gate (`docs/27-cat-id.md`) falling under [`LOW_QUALITY_PROB`] -- still
    /// stored and trained on regardless (a human explicitly chose this photo), just flagged so
    /// the caller can say so. A failed embedding (helper crash, NPU unavailable) still leaves the
    /// crop stored -- exactly `POST /faces/label`'s own log-and-continue behaviour -- reported as
    /// `low_quality: false` (no measurement made, not a claim of high quality).
    pub fn upload_sample(&self, bytes: &[u8], cat: &str) -> Result<UploadedSample, FaceError> {
        if !is_safe_name(cat) {
            return Err(FaceError::InvalidCat(cat.to_string()));
        }
        validate_upload(bytes).map_err(FaceError::InvalidUpload)?;
        let dir = Path::new(FACES_ROOT).join(cat);
        if is_reserved_bucket(cat) || cat == pending_dir_name() || !dir.is_dir() {
            return Err(FaceError::NotFound);
        }
        let name = format!("{UPLOAD_PREFIX}{}.jpg", now_unix_millis());
        let dest = dir.join(&name);
        fs::write(&dest, bytes).map_err(FaceError::Io)?;
        let low_quality = match embed::extract(&dest) {
            Ok(Embedding { feat, prob }) => {
                let _ = save_embedding(&dest, &feat);
                self.on_labelled(cat, &feat);
                prob < LOW_QUALITY_PROB
            }
            Err(e) => {
                eprintln!("kibbled: faces: embed uploaded {}: {e}", dest.display());
                false
            }
        };
        mark_faces_changed();
        let samples = list_samples(cat).map(|v| v.len()).unwrap_or(0);
        Ok(UploadedSample { name, samples, low_quality })
    }
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

    /// A cat enrolled but not yet labelled is only an empty directory; it must still be listed,
    /// or `Gallery::load` forgets it on the next restart (observed live: two cats added, gone
    /// after the following deploy).
    #[test]
    fn enrolled_cats_include_sample_less_directories() {
        let root = temp_dir("enrolled");
        let pending = root.join("pending");
        fs::create_dir_all(root.join("Kitty")).unwrap();
        fs::create_dir_all(root.join("Pancake")).unwrap();
        fs::create_dir_all(&pending).unwrap();
        assert_eq!(list_enrolled_cats_in(&root, &pending), vec!["Kitty", "Pancake"]);
        let _ = fs::remove_dir_all(&root);
    }

    /// `pending` lives inside `FACES_ROOT` but is a staging area, not a label -- listing it
    /// enrolled a phantom cat named "pending", which shipped once and was caught in HA.
    #[test]
    fn enrolled_cats_exclude_pending_and_reserved_buckets() {
        let root = temp_dir("buckets");
        let pending = root.join("pending");
        fs::create_dir_all(&pending).unwrap();
        fs::create_dir_all(root.join(SKIP_BUCKET)).unwrap();
        fs::create_dir_all(root.join(NOT_A_CAT_BUCKET)).unwrap();
        fs::create_dir_all(root.join("Kitty")).unwrap();
        assert_eq!(list_enrolled_cats_in(&root, &pending), vec!["Kitty"]);
        let _ = fs::remove_dir_all(&root);
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

    #[test]
    fn label_moves_the_embedding_sidecar_alongside_the_crop_when_present() {
        let pending = temp_dir("label-sidecar-pending");
        let root = temp_dir("label-sidecar-root");
        fs::create_dir_all(&pending).unwrap();
        fs::write(pending.join("1-unknown.jpg"), b"crop").unwrap();
        fs::write(pending.join("1-unknown.emb"), b"fake-embedding-bytes").unwrap();
        label_in(&pending, &root, "1-unknown.jpg", "Rashy").unwrap();
        assert!(!pending.join("1-unknown.emb").exists());
        assert_eq!(
            fs::read(root.join("Rashy").join("1-unknown.emb")).unwrap(),
            b"fake-embedding-bytes"
        );
    }

    #[test]
    fn label_succeeds_with_no_sidecar_at_all() {
        let pending = temp_dir("label-no-sidecar-pending");
        let root = temp_dir("label-no-sidecar-root");
        fs::create_dir_all(&pending).unwrap();
        fs::write(pending.join("1-unknown.jpg"), b"crop").unwrap();
        assert!(label_in(&pending, &root, "1-unknown.jpg", "Rashy").is_ok());
        assert!(!root.join("Rashy").join("1-unknown.emb").exists());
    }

    #[test]
    fn unlabel_moves_a_labelled_crop_and_its_sidecar_back_to_pending() {
        let root = temp_dir("unlabel-root");
        let pending = temp_dir("unlabel-pending");
        fs::create_dir_all(root.join("Rashy")).unwrap();
        fs::write(root.join("Rashy").join("1-unknown.jpg"), b"crop").unwrap();
        fs::write(root.join("Rashy").join("1-unknown.emb"), b"emb-bytes").unwrap();
        unlabel_in(&root, &pending, "Rashy", "1-unknown.jpg").unwrap();
        assert!(!root.join("Rashy").join("1-unknown.jpg").exists());
        assert!(!root.join("Rashy").join("1-unknown.emb").exists());
        assert_eq!(fs::read(pending.join("1-unknown.jpg")).unwrap(), b"crop");
        assert_eq!(fs::read(pending.join("1-unknown.emb")).unwrap(), b"emb-bytes");
    }

    #[test]
    fn unlabel_reports_not_found_for_a_crop_that_was_never_labelled_into_that_cat() {
        let root = temp_dir("unlabel-missing-root");
        let pending = temp_dir("unlabel-missing-pending");
        fs::create_dir_all(&root).unwrap();
        assert!(matches!(
            unlabel_in(&root, &pending, "Rashy", "ghost.jpg"),
            Err(FaceError::NotFound)
        ));
    }

    #[test]
    fn unlabel_rejects_traversal_in_either_the_cat_or_the_name() {
        let root = temp_dir("unlabel-bad-root");
        let pending = temp_dir("unlabel-bad-pending");
        assert!(matches!(
            unlabel_in(&root, &pending, "../escape", "a.jpg"),
            Err(FaceError::InvalidCat(_))
        ));
        assert!(matches!(
            unlabel_in(&root, &pending, "Rashy", "../a.jpg"),
            Err(FaceError::InvalidName)
        ));
    }

    #[test]
    fn list_labelled_excludes_pending_and_reserved_buckets() {
        let root = temp_dir("list-labelled-root");
        fs::create_dir_all(root.join("pending")).unwrap();
        fs::write(root.join("pending").join("x.jpg"), b"x").unwrap();
        fs::create_dir_all(root.join(SKIP_BUCKET)).unwrap();
        fs::write(root.join(SKIP_BUCKET).join("y.jpg"), b"y").unwrap();
        fs::create_dir_all(root.join("Rashy")).unwrap();
        fs::write(root.join("Rashy").join("z.jpg"), b"z").unwrap();
        let found = list_labelled_in(&root).unwrap();
        assert_eq!(found.len(), 1, "{found:?}");
        assert_eq!(found[0].cat, "Rashy");
        assert_eq!(found[0].name, "z.jpg");
    }

    #[test]
    fn last_seen_ignores_uploaded_reference_photos() {
        let at = |secs: u64| UNIX_EPOCH + std::time::Duration::from_secs(secs);
        let crop = |cat: &str, name: &str, secs: u64| LabelledCrop {
            cat: cat.to_string(),
            name: name.to_string(),
            jpg_path: PathBuf::from(name),
            mtime: at(secs),
        };
        let seen = last_seen_from(vec![
            crop("Pancake", "1789600000-101321488.jpg", 1_789_600_000),
            crop("Pancake", "upload-1789609534401.jpg", 1_789_609_534),
            crop("Kitty", "upload-1789609470108.jpg", 1_789_609_470),
        ]);
        assert_eq!(seen.get("Pancake"), Some(&1_789_600_000));
        assert_eq!(seen.get("Kitty"), None, "an upload alone is not a sighting");
    }

    #[test]
    fn list_labelled_on_a_missing_root_is_empty_not_an_error() {
        let root = temp_dir("list-labelled-missing");
        assert_eq!(list_labelled_in(&root).unwrap().len(), 0);
    }

    #[test]
    fn review_target_prefers_the_oldest_pending_crop() {
        let pending = temp_dir("review-pending");
        let root = temp_dir("review-root");
        fs::create_dir_all(&pending).unwrap();
        for (name, secs) in [("old.jpg", 100u64), ("new.jpg", 200)] {
            let path = pending.join(name);
            fs::write(&path, b"x").unwrap();
            let mtime = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(secs);
            fs::File::open(&path).unwrap().set_modified(mtime).unwrap();
        }
        assert_eq!(
            review_target_in(&pending, &root).unwrap(),
            Some(FaceTarget::Pending { name: "old.jpg".to_string() })
        );
    }

    #[test]
    fn identify_target_prefers_the_newest_pending_crop() {
        let pending = temp_dir("identify-pending");
        let root = temp_dir("identify-root");
        fs::create_dir_all(&pending).unwrap();
        for (name, secs) in [("old.jpg", 100u64), ("new.jpg", 200)] {
            let path = pending.join(name);
            fs::write(&path, b"x").unwrap();
            let mtime = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(secs);
            fs::File::open(&path).unwrap().set_modified(mtime).unwrap();
        }
        assert_eq!(
            identify_target_in(&pending, &root).unwrap(),
            Some(FaceTarget::Pending { name: "new.jpg".to_string() })
        );
    }

    #[test]
    fn both_targets_fall_back_to_the_newest_labelled_crop_once_pending_is_empty() {
        let pending = temp_dir("targets-empty-pending");
        let root = temp_dir("targets-empty-root");
        fs::create_dir_all(&pending).unwrap();
        fs::create_dir_all(root.join("Rashy")).unwrap();
        fs::write(root.join("Rashy").join("only.jpg"), b"x").unwrap();
        let expect = Some(FaceTarget::Labelled { cat: "Rashy".to_string(), name: "only.jpg".to_string() });
        assert_eq!(review_target_in(&pending, &root).unwrap(), expect);
        assert_eq!(identify_target_in(&pending, &root).unwrap(), expect);
    }

    #[test]
    fn both_targets_are_none_when_nothing_has_ever_been_captured() {
        let pending = temp_dir("targets-nothing-pending");
        let root = temp_dir("targets-nothing-root");
        assert_eq!(review_target_in(&pending, &root).unwrap(), None);
        assert_eq!(identify_target_in(&pending, &root).unwrap(), None);
    }

    #[test]
    fn is_reserved_bucket_matches_only_the_two_reserved_names() {
        assert!(is_reserved_bucket(SKIP_BUCKET));
        assert!(is_reserved_bucket(NOT_A_CAT_BUCKET));
        assert!(!is_reserved_bucket("Rashy"));
    }

    #[test]
    fn parse_pending_name_parses_ts_and_vendor_pet_id() {
        assert_eq!(parse_pending_name("1700000000-101321488.jpg"), Some((1700000000, Some(101321488))));
        assert_eq!(parse_pending_name("1700000000-unknown.jpg"), Some((1700000000, None)));
    }

    #[test]
    fn parse_pending_name_rejects_anything_that_does_not_match_the_convention() {
        assert_eq!(parse_pending_name("not-a-crop.txt"), None);
        assert_eq!(parse_pending_name("nope.jpg"), None);
        assert_eq!(parse_pending_name("abc-unknown.jpg"), None);
    }

    #[test]
    fn list_pending_excludes_sidecar_files() {
        let dir = temp_dir("list-pending-sidecars");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("1-unknown.jpg"), b"jpg").unwrap();
        fs::write(dir.join("1-unknown.emb"), b"emb").unwrap();
        fs::write(dir.join("1-unknown.guess"), b"guess").unwrap();
        assert_eq!(list_pending_in(&dir).unwrap(), vec!["1-unknown.jpg".to_string()]);
    }

    #[test]
    fn list_pending_full_reports_ts_vendor_pet_id_and_guess() {
        let dir = temp_dir("pending-full");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("100-7.jpg"), b"x").unwrap();
        save_guess_file(&dir.join("100-7.jpg"), &Guess { cat: "Pancake".to_string(), score: 0.83 }).unwrap();
        fs::write(dir.join("200-unknown.jpg"), b"x").unwrap();
        let crops = list_pending_full_in(&dir).unwrap();
        assert_eq!(crops.len(), 2);
        assert_eq!(crops[0].name, "100-7.jpg");
        assert_eq!(crops[0].ts, 100);
        assert_eq!(crops[0].vendor_pet_id, Some(7));
        assert_eq!(crops[0].guess, Some(Guess { cat: "Pancake".to_string(), score: 0.83 }));
        assert_eq!(crops[1].name, "200-unknown.jpg");
        assert_eq!(crops[1].vendor_pet_id, None);
        assert_eq!(crops[1].guess, None);
    }

    #[test]
    fn list_pending_full_sorts_newest_last_by_ts_regardless_of_write_order() {
        let dir = temp_dir("pending-full-sort");
        fs::create_dir_all(&dir).unwrap();
        // Written newest-first on disk; the returned order must still be ts-ascending.
        fs::write(dir.join("300-unknown.jpg"), b"x").unwrap();
        fs::write(dir.join("100-unknown.jpg"), b"x").unwrap();
        fs::write(dir.join("200-unknown.jpg"), b"x").unwrap();
        let names: Vec<String> = list_pending_full_in(&dir).unwrap().into_iter().map(|c| c.name).collect();
        assert_eq!(names, vec!["100-unknown.jpg", "200-unknown.jpg", "300-unknown.jpg"]);
    }

    #[test]
    fn label_moves_the_guess_sidecar_alongside_the_crop_when_present() {
        let pending = temp_dir("label-guess-pending");
        let root = temp_dir("label-guess-root");
        fs::create_dir_all(&pending).unwrap();
        fs::write(pending.join("1-unknown.jpg"), b"crop").unwrap();
        save_guess_file(&pending.join("1-unknown.jpg"), &Guess { cat: "Kitty".to_string(), score: 0.9 }).unwrap();
        label_in(&pending, &root, "1-unknown.jpg", "Rashy").unwrap();
        assert!(!pending.join("1-unknown.guess").exists());
        assert_eq!(
            load_guess(&root.join("Rashy").join("1-unknown.jpg")),
            Some(Guess { cat: "Kitty".to_string(), score: 0.9 })
        );
    }

    #[test]
    fn unlabel_drops_the_guess_sidecar_without_restoring_it_to_pending() {
        let root = temp_dir("unlabel-guess-root");
        let pending = temp_dir("unlabel-guess-pending");
        fs::create_dir_all(root.join("Rashy")).unwrap();
        fs::write(root.join("Rashy").join("1-unknown.jpg"), b"crop").unwrap();
        save_guess_file(
            &root.join("Rashy").join("1-unknown.jpg"),
            &Guess { cat: "Kitty".to_string(), score: 0.9 },
        )
        .unwrap();
        unlabel_in(&root, &pending, "Rashy", "1-unknown.jpg").unwrap();
        assert!(!root.join("Rashy").join("1-unknown.guess").exists());
        assert!(pending.join("1-unknown.jpg").exists());
        assert_eq!(load_guess(&pending.join("1-unknown.jpg")), None);
    }

    #[test]
    fn eviction_counts_only_jpg_crops_and_removes_their_sidecars() {
        let dir = temp_dir("evict-sidecars");
        fs::create_dir_all(&dir).unwrap();
        let mut names = Vec::new();
        for i in 0..5u64 {
            let name = format!("{i}-unknown.jpg");
            let path = dir.join(&name);
            fs::write(&path, b"x").unwrap();
            save_embedding(&path, &[0.0f32; embed::EMBED_DIM]).unwrap();
            save_guess_file(&path, &Guess { cat: "Rashy".to_string(), score: 0.5 }).unwrap();
            let mtime = SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000 + i);
            fs::File::open(&path).unwrap().set_modified(mtime).unwrap();
            names.push(name);
        }
        evict_oldest_if_over_cap(&dir, 3).unwrap();
        let remaining = list_pending_in(&dir).unwrap();
        assert_eq!(remaining.len(), 3, "{remaining:?}");
        // The two oldest crops, and only their sidecars, are gone.
        for name in &names[..2] {
            let stem = name.strip_suffix(".jpg").unwrap();
            assert!(!dir.join(format!("{stem}.emb")).exists(), "{name} .emb should be gone");
            assert!(!dir.join(format!("{stem}.guess")).exists(), "{name} .guess should be gone");
        }
        // The three newest crops keep their sidecars.
        for name in &names[2..] {
            let stem = name.strip_suffix(".jpg").unwrap();
            assert!(dir.join(format!("{stem}.emb")).exists(), "{name} .emb should survive");
            assert!(dir.join(format!("{stem}.guess")).exists(), "{name} .guess should survive");
        }
    }

    #[test]
    fn associate_track_renames_a_crop_exactly_at_the_window_boundary() {
        let dir = temp_dir("track-boundary-in");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("1000-unknown.jpg"), b"x").unwrap();
        // start_time - ts == TRACK_ASSOCIATION_WINDOW_SECS exactly -- still within the window.
        let renamed = associate_track_in(&dir, 42, 1000 + TRACK_ASSOCIATION_WINDOW_SECS).unwrap();
        assert_eq!(renamed, vec![format!("1000-42.jpg")]);
        assert!(dir.join("1000-42.jpg").exists());
        assert!(!dir.join("1000-unknown.jpg").exists());
    }

    #[test]
    fn associate_track_leaves_a_crop_one_second_outside_the_window_alone() {
        let dir = temp_dir("track-boundary-out");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("1000-unknown.jpg"), b"x").unwrap();
        let renamed = associate_track_in(&dir, 42, 1000 + TRACK_ASSOCIATION_WINDOW_SECS + 1).unwrap();
        assert!(renamed.is_empty());
        assert!(dir.join("1000-unknown.jpg").exists());
    }

    #[test]
    fn associate_track_accepts_a_track_landing_before_the_crops_own_timestamp_too() {
        let dir = temp_dir("track-symmetric");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("1000-unknown.jpg"), b"x").unwrap();
        let renamed = associate_track_in(&dir, 42, 1000 - 100).unwrap();
        assert_eq!(renamed, vec!["1000-42.jpg".to_string()]);
    }

    #[test]
    fn associate_track_moves_sidecars_along_with_the_renamed_crop() {
        let dir = temp_dir("track-sidecars");
        fs::create_dir_all(&dir).unwrap();
        let src = dir.join("1000-unknown.jpg");
        fs::write(&src, b"x").unwrap();
        save_embedding(&src, &[0.0f32; embed::EMBED_DIM]).unwrap();
        save_guess_file(&src, &Guess { cat: "Kitty".to_string(), score: 0.7 }).unwrap();
        associate_track_in(&dir, 42, 1000).unwrap();
        assert!(dir.join("1000-42.emb").exists());
        assert_eq!(
            load_guess(&dir.join("1000-42.jpg")),
            Some(Guess { cat: "Kitty".to_string(), score: 0.7 })
        );
    }

    #[test]
    fn associate_track_ignores_crops_already_associated_with_a_real_pet_id() {
        let dir = temp_dir("track-already-known");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("1000-9.jpg"), b"x").unwrap();
        let renamed = associate_track_in(&dir, 42, 1000).unwrap();
        assert!(renamed.is_empty());
        assert!(dir.join("1000-9.jpg").exists());
    }

    #[test]
    fn associate_track_never_overwrites_an_existing_destination() {
        let dir = temp_dir("track-collision");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("1000-unknown.jpg"), b"source").unwrap();
        fs::write(dir.join("1000-42.jpg"), b"already there").unwrap();
        let renamed = associate_track_in(&dir, 42, 1000).unwrap();
        assert!(renamed.is_empty());
        assert_eq!(fs::read(dir.join("1000-unknown.jpg")).unwrap(), b"source");
        assert_eq!(fs::read(dir.join("1000-42.jpg")).unwrap(), b"already there");
    }

    #[test]
    fn list_samples_returns_a_cats_crops_sorted_by_ts() {
        let root = temp_dir("samples-root");
        let pending = temp_dir("samples-pending");
        fs::create_dir_all(root.join("Rashy")).unwrap();
        fs::write(root.join("Rashy").join("200-unknown.jpg"), b"x").unwrap();
        fs::write(root.join("Rashy").join("100-unknown.jpg"), b"x").unwrap();
        let samples = list_samples_in(&root, &pending, "Rashy").unwrap();
        let names: Vec<String> = samples.into_iter().map(|s| s.name).collect();
        assert_eq!(names, vec!["100-unknown.jpg".to_string(), "200-unknown.jpg".to_string()]);
    }

    #[test]
    fn list_samples_includes_uploaded_reference_photos() {
        let root = temp_dir("samples-uploads-root");
        let pending = temp_dir("samples-uploads-pending");
        fs::create_dir_all(root.join("Kitty")).unwrap();
        fs::write(root.join("Kitty").join("upload-1789609470108.jpg"), b"x").unwrap();
        fs::write(root.join("Kitty").join("upload-1789609470108.emb"), b"x").unwrap();
        fs::write(root.join("Kitty").join("1789595577-unknown.jpg"), b"x").unwrap();
        let names: Vec<(String, u64)> = list_samples_in(&root, &pending, "Kitty")
            .unwrap()
            .into_iter()
            .map(|s| (s.name, s.ts))
            .collect();
        assert_eq!(
            names,
            vec![("1789595577-unknown.jpg".to_string(), 1789595577), ("upload-1789609470108.jpg".to_string(), 1789609470)]
        );
    }

    #[test]
    fn list_samples_reports_not_found_for_an_unknown_cat() {
        let root = temp_dir("samples-missing-root");
        let pending = temp_dir("samples-missing-pending");
        fs::create_dir_all(&root).unwrap();
        assert!(matches!(list_samples_in(&root, &pending, "Ghost"), Err(FaceError::NotFound)));
    }

    #[test]
    fn list_samples_rejects_the_pending_staging_directory_as_a_cat() {
        let root = temp_dir("samples-pending-guard-root");
        let pending = root.join("pending");
        fs::create_dir_all(&pending).unwrap();
        fs::write(pending.join("1-unknown.jpg"), b"x").unwrap();
        assert!(matches!(list_samples_in(&root, &pending, "pending"), Err(FaceError::NotFound)));
    }

    #[test]
    fn read_sample_returns_the_raw_bytes() {
        let root = temp_dir("read-sample-root");
        let pending = temp_dir("read-sample-pending");
        fs::create_dir_all(root.join("Rashy")).unwrap();
        fs::write(root.join("Rashy").join("1-unknown.jpg"), b"crop bytes").unwrap();
        assert_eq!(read_sample_in(&root, &pending, "Rashy", "1-unknown.jpg").unwrap(), b"crop bytes");
    }

    #[test]
    fn read_sample_rejects_traversal_in_either_the_cat_or_the_name() {
        let root = temp_dir("read-sample-bad-root");
        let pending = temp_dir("read-sample-bad-pending");
        assert!(matches!(
            read_sample_in(&root, &pending, "../escape", "a.jpg"),
            Err(FaceError::InvalidCat(_))
        ));
        assert!(matches!(
            read_sample_in(&root, &pending, "Rashy", "../a.jpg"),
            Err(FaceError::InvalidName)
        ));
    }

    #[test]
    fn read_sample_reports_not_found_for_a_missing_file() {
        let root = temp_dir("read-sample-missing-root");
        let pending = temp_dir("read-sample-missing-pending");
        fs::create_dir_all(root.join("Rashy")).unwrap();
        assert!(matches!(
            read_sample_in(&root, &pending, "Rashy", "ghost.jpg"),
            Err(FaceError::NotFound)
        ));
    }

    #[test]
    fn nearest_sample_picks_the_crop_closest_to_the_cats_centroid() {
        let root = temp_dir("nearest-sample");
        fs::create_dir_all(root.join("Rashy")).unwrap();
        let a = root.join("Rashy").join("1-unknown.jpg");
        let b = root.join("Rashy").join("2-unknown.jpg");
        fs::write(&a, b"a").unwrap();
        fs::write(&b, b"b").unwrap();
        let mut feat_a = [0.0f32; embed::EMBED_DIM];
        feat_a[0] = 1.0;
        let mut feat_b = [0.0f32; embed::EMBED_DIM];
        feat_b[0] = 1.0;
        feat_b[1] = 1.0;
        save_embedding(&a, &feat_a).unwrap();
        save_embedding(&b, &feat_b).unwrap();

        // Centroid is built from `a` alone, so `a` is an exact match (cosine 1.0) and `b` -- a
        // different direction -- scores lower.
        let mut classifier = catid::Classifier::new();
        classifier.label("Rashy", &feat_a);
        let cat_model = classifier.cats().next().expect("just labelled");

        let crops = vec![
            LabelledCrop {
                cat: "Rashy".to_string(),
                name: "1-unknown.jpg".to_string(),
                jpg_path: a.clone(),
                mtime: SystemTime::now(),
            },
            LabelledCrop {
                cat: "Rashy".to_string(),
                name: "2-unknown.jpg".to_string(),
                jpg_path: b.clone(),
                mtime: SystemTime::now(),
            },
        ];
        assert_eq!(nearest_sample(cat_model, &crops), Some("1-unknown.jpg".to_string()));
    }

    // --- POST /faces/upload body validation ---------------------------------------------------

    #[test]
    fn validate_upload_accepts_a_well_formed_small_jpeg() {
        let mut bytes = vec![0xFF, 0xD8, 0xFF, 0xE0];
        bytes.extend(std::iter::repeat(0u8).take(100));
        assert!(validate_upload(&bytes).is_ok());
    }

    #[test]
    fn validate_upload_rejects_missing_jpeg_magic() {
        let bytes = vec![0x89, 0x50, 0x4E, 0x47]; // PNG magic, not JPEG
        assert!(matches!(validate_upload(&bytes), Err(UploadError::NotJpeg)));
    }

    #[test]
    fn validate_upload_rejects_an_empty_body() {
        assert!(matches!(validate_upload(&[]), Err(UploadError::NotJpeg)));
    }

    #[test]
    fn validate_upload_accepts_exactly_at_the_size_cap() {
        let mut bytes = vec![0xFF, 0xD8, 0xFF];
        bytes.resize(MAX_UPLOAD_BYTES, 0);
        assert!(validate_upload(&bytes).is_ok());
    }

    #[test]
    fn validate_upload_rejects_a_body_one_byte_over_the_size_cap() {
        let mut bytes = vec![0xFF, 0xD8, 0xFF];
        bytes.resize(MAX_UPLOAD_BYTES + 1, 0);
        assert!(matches!(validate_upload(&bytes), Err(UploadError::TooLarge(n)) if n == MAX_UPLOAD_BYTES + 1));
    }

    #[test]
    fn validate_upload_checks_size_before_magic_when_both_are_wrong() {
        // An oversized non-JPEG body is still, unambiguously, "too large" -- checking size first
        // means the caller never has to wonder whether a wrong-format body was even measured.
        let bytes = vec![0u8; MAX_UPLOAD_BYTES + 1];
        assert!(matches!(validate_upload(&bytes), Err(UploadError::TooLarge(_))));
    }
}
