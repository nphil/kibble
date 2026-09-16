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
//! Crops arrive from `ai.rs`'s poller, which watches the vendor's own `/tmp/saveFace.jpg` for
//! changes -- see that module's doc for why the on-device `pet_id` this crop was associated with
//! is not attached (it lives in the same unreachable bus message).
//!
//! Layout: `PENDING_DIR/<unix>-<label>.jpg` (label is `pet_id` if one is ever available, else
//! `"unknown"`) plus a same-name `.emb` sidecar once its embedding has been computed (eagerly at
//! capture time by `ai.rs`, or lazily by [`ensure_embedding`] the first time anything needs one),
//! capped at [`MAX_PENDING`] files with the oldest evicted by mtime. A human names a pending crop
//! with `POST /faces/label` ([`label`]), which moves both files to `<FACES_ROOT>/<cat>/<same
//! filename>` -- permanent storage, outside the cap, one directory per label -- and feeds the
//! embedding into that cat's running centroid ([`Gallery::on_labelled`]). [`unlabel`] is the exact
//! inverse, for a mistaken label or the first half of a re-label. [`SKIP_BUCKET`]/
//! [`NOT_A_CAT_BUCKET`] are two reserved `cat` values (this module's own doc already anticipated
//! both, before any of this existed): a crop labelled into either still leaves the review queue
//! and keeps its embedding on record, but is never counted as "a cat" -- excluded from
//! `GET /cats` and never fed to the classifier.

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
    let dest_dir = faces_root.join(cat);
    fs::create_dir_all(&dest_dir).map_err(FaceError::Io)?;
    let dest = dest_dir.join(name);
    fs::rename(&src, &dest).map_err(FaceError::Io)?;
    if src_emb.is_file() {
        let _ = fs::rename(&src_emb, embedding_path_for(&dest));
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

/// `POST /faces/unlabel {"name": "...", "cat": "..."}`: the exact inverse of [`label`] -- moves a
/// previously-labelled crop (and its `.emb` sidecar, if any) back into the pending review queue.
/// A full re-label is this followed by [`label`] into the correct cat.
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
    fs::create_dir_all(pending_dir).map_err(FaceError::Io)?;
    let dest = pending_dir.join(name);
    fs::rename(&src, &dest).map_err(FaceError::Io)?;
    if src_emb.is_file() {
        let _ = fs::rename(&src_emb, embedding_path_for(&dest));
    }
    Ok(())
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

fn last_seen_by_cat() -> HashMap<String, u64> {
    let mut out = HashMap::new();
    for crop in list_labelled().unwrap_or_default() {
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
    /// [`Gallery::add_cat`], sorted by name for a stable listing.
    pub fn cats_json(&self) -> String {
        let classifier = self.inner.lock().unwrap();
        let mut cats: Vec<&catid::CatModel> = classifier.cats().collect();
        cats.sort_by(|a, b| a.name.cmp(&b.name));
        let last_seen = last_seen_by_cat();
        let items: Vec<String> = cats
            .iter()
            .map(|c| {
                let seen = last_seen.get(c.name.as_str()).map_or("null".to_string(), u64::to_string);
                format!(
                    r#"{{"name":"{}","samples":{},"last_seen":{}}}"#,
                    c.name.escape_debug(),
                    c.count,
                    seen
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
}
