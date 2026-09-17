//! Nearest-centroid cat-identification classifier over the vendor face model's 512-float
//! embeddings (`embed.rs` extracts them; `docs/18-npu-confirmed.md` proves the model and its
//! output shape). Pure logic, no filesystem or process I/O -- `faces.rs` owns persistence and
//! wires this module to disk.
//!
//! ## Why nearest-centroid, not full k-NN
//!
//! Every enrolled cat gets one running centroid (the mean of its labelled samples' L2-normalised
//! embeddings). A query embedding is matched by cosine similarity against each cat's centroid;
//! the highest score wins if it clears [`Classifier::t_accept`], else the crop is `unknown`. This
//! is a degenerate k-NN with one prototype per class rather than a search over every historical
//! sample -- appropriate here because (a) it is O(1) per labelled sample to update and O(cats) to
//! query (microseconds, no crate, matches the project's own "tiny RSS, no dependencies" ethos),
//! and (b) a single frozen face-recognition backbone's embeddings for one individual cluster
//! tightly enough that one centroid is expected to represent them well (this is the standard
//! design for a frozen-embedding face-verification gallery, not novel here).
//!
//! ## Why a running *sum*, not a running *mean*
//!
//! `CatModel` accumulates the plain vector sum of every labelled sample (already L2-normalised)
//! rather than re-normalising after each addition. Cosine similarity is scale-invariant in its
//! second argument, so `cosine(query, normalize(sum))` and `cosine(query, sum)` (computed as
//! `dot(query, sum) / ||sum||`, i.e. only the query is pre-normalised) rank identically to
//! `cosine(query, mean)` for `mean = sum / count` -- dividing by the positive scalar `count`
//! never changes a vector's direction. This means [`CatModel::add`]/[`CatModel::remove`] are a
//! single elementwise add/subtract, no renormalisation needed on every update, and unlabelling a
//! sample (`POST /faces/unlabel`, a mistaken label, or a re-label) is an exact inverse of adding
//! it -- there is no "re-derive the mean from a running mean" rounding concern because the
//! running state *is* the exact sum.
//!
//! ## Threshold derivation ([`derive_threshold`])
//!
//! [`Classifier::t_accept`] starts at [`DEFAULT_T_ACCEPT`], a generic placeholder (see its own
//! doc comment for exactly how uncalibrated it is), and is expected to be replaced by
//! [`derive_threshold`]'s measured value as soon as there is enough labelled data to compute one.
//! The method is leave-one-out cross-validation: for every cat with >=2 samples, each sample's
//! cosine similarity is measured against a centroid built from *its own cat's other samples*
//! (a "genuine" score -- what a real future visit from this cat should score); every sample is
//! also scored against every *other* cat's full centroid (an "impostor" score -- what a
//! different cat visiting would score against this one). The accept threshold is the midpoint of
//! the tightest observed margin: `(min(genuine) + max(impostor)) / 2`. This needs at least two
//! cats, and at least one cat with two or more samples, to produce even one genuine score --
//! [`ThresholdError`] names exactly which precondition is missing when it can't run, and the
//! caller (`faces.rs`) is expected to report that honestly rather than inventing a number.

pub const EMBED_DIM: usize = 512;

/// Cosine-similarity accept threshold used until [`derive_threshold`] has enough labelled data to
/// measure a real one. **Not calibrated against this device's embeddings or this face model in
/// any way** -- it is simply the midpoint of cosine similarity's [-1, 1] range biased toward
/// "accept", a common starting point for L2-normalised embedding cosine similarity in face
/// verification generally. Treat any identification made while a `Classifier` is still on this
/// default as a guess, not a measurement -- `docs/27-cat-id.md` documents exactly when this
/// applies (the cold-start case).
pub const DEFAULT_T_ACCEPT: f32 = 0.5;

fn dot(a: &[f32; EMBED_DIM], b: &[f32; EMBED_DIM]) -> f32 {
    let mut s = 0.0f32;
    for i in 0..EMBED_DIM {
        s += a[i] * b[i];
    }
    s
}

fn l2_norm(v: &[f32; EMBED_DIM]) -> f32 {
    dot(v, v).sqrt()
}

/// Normalises `v` to unit length in place. Returns `false` (leaving `v` unchanged) for a
/// degenerate (all-zero, or too close to it to divide safely) vector -- a real face embedding is
/// never the zero vector, so this only guards against a corrupt/empty input rather than something
/// expected to occur in practice.
pub fn l2_normalize(v: &mut [f32; EMBED_DIM]) -> bool {
    let norm = l2_norm(v);
    if norm <= f32::EPSILON {
        return false;
    }
    for x in v.iter_mut() {
        *x /= norm;
    }
    true
}

fn sum_of(vectors: &[[f32; EMBED_DIM]]) -> [f32; EMBED_DIM] {
    let mut sum = [0.0f32; EMBED_DIM];
    for v in vectors {
        for i in 0..EMBED_DIM {
            sum[i] += v[i];
        }
    }
    sum
}

/// One enrolled cat's running centroid, as the sum of its labelled samples' L2-normalised
/// embeddings plus a count -- see the module doc for why a sum, not a mean.
#[derive(Debug, Clone)]
pub struct CatModel {
    pub name: String,
    sum: [f32; EMBED_DIM],
    pub count: u32,
}

impl CatModel {
    fn new(name: impl Into<String>) -> Self {
        Self { name: name.into(), sum: [0.0; EMBED_DIM], count: 0 }
    }

    fn add(&mut self, normalized: &[f32; EMBED_DIM]) {
        for i in 0..EMBED_DIM {
            self.sum[i] += normalized[i];
        }
        self.count += 1;
    }

    fn remove(&mut self, normalized: &[f32; EMBED_DIM]) {
        for i in 0..EMBED_DIM {
            self.sum[i] -= normalized[i];
        }
        self.count = self.count.saturating_sub(1);
    }

    /// Cosine similarity of an already-normalised query against this cat's centroid direction.
    /// `None` if this cat has no samples (nothing to compare against) or its sum is degenerate.
    /// `pub`: `faces.rs`'s `GET /cats` `"avatar"` selection (nearest labelled sample to the
    /// centroid) is the one caller outside this module, and the minimal accessor it needs.
    pub fn cosine_to(&self, query_normalized: &[f32; EMBED_DIM]) -> Option<f32> {
        if self.count == 0 {
            return None;
        }
        let norm = l2_norm(&self.sum);
        if norm <= f32::EPSILON {
            return None;
        }
        Some(dot(query_normalized, &self.sum) / norm)
    }
}

/// One cat's score in a ranking.
#[derive(Debug, Clone, PartialEq)]
pub struct Score {
    pub cat: String,
    pub score: f32,
}

/// The result of [`Classifier::identify`].
#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    /// Top score cleared [`Classifier::t_accept`].
    Known { cat: String, score: f32, second_best: Option<Score> },
    /// Either no cat is enrolled at all, or the best score didn't clear the threshold --
    /// `best_guess` carries that best (rejected) score for diagnostics, when one exists.
    Unknown { best_guess: Option<Score> },
}

/// A gallery of enrolled cats and the accept threshold to rank them with.
pub struct Classifier {
    cats: Vec<CatModel>,
    pub t_accept: f32,
}

impl Default for Classifier {
    fn default() -> Self {
        Self::new()
    }
}

impl Classifier {
    pub fn new() -> Self {
        Self { cats: Vec::new(), t_accept: DEFAULT_T_ACCEPT }
    }

    pub fn cats(&self) -> impl Iterator<Item = &CatModel> {
        self.cats.iter()
    }

    fn find_or_create(&mut self, name: &str) -> &mut CatModel {
        if let Some(i) = self.cats.iter().position(|c| c.name == name) {
            &mut self.cats[i]
        } else {
            self.cats.push(CatModel::new(name));
            self.cats.last_mut().expect("just pushed")
        }
    }

    /// Registers `name` with zero samples if it doesn't already exist -- lets a cat appear in
    /// [`Classifier::cats`] (and so HA's `GET /cats`/label-select options) before its first crop
    /// is ever labelled. A no-op if `name` is already known.
    pub fn ensure_cat(&mut self, name: &str) {
        self.find_or_create(name);
    }

    /// Feeds one more labelled sample into `cat`'s centroid. A degenerate (all-zero) embedding is
    /// silently ignored rather than poisoning the centroid -- see [`l2_normalize`].
    pub fn label(&mut self, cat: &str, embedding: &[f32; EMBED_DIM]) {
        let mut normalized = *embedding;
        if !l2_normalize(&mut normalized) {
            return;
        }
        self.find_or_create(cat).add(&normalized);
    }

    /// Removes one previously-labelled sample from `cat`'s centroid -- the exact inverse of
    /// [`Classifier::label`] with the same embedding, used for `POST /faces/unlabel` and as the
    /// first half of a re-label (unlabel from the old cat, then [`Classifier::label`] into the
    /// new one). Returns `false` if `cat` isn't known or already has zero samples; a cat whose
    /// count reaches zero is pruned entirely.
    pub fn unlabel(&mut self, cat: &str, embedding: &[f32; EMBED_DIM]) -> bool {
        let mut normalized = *embedding;
        if !l2_normalize(&mut normalized) {
            return false;
        }
        let Some(i) = self.cats.iter().position(|c| c.name == cat) else {
            return false;
        };
        if self.cats[i].count == 0 {
            return false;
        }
        self.cats[i].remove(&normalized);
        if self.cats[i].count == 0 {
            self.cats.remove(i);
        }
        true
    }

    /// Drops `name`'s entire model outright, regardless of its current sample count -- the
    /// classifier-side half of `DELETE /cats/<name>` (`faces.rs::Gallery::delete_cat` has already
    /// removed its directory by the time this runs). Distinct from repeatedly calling
    /// [`Classifier::unlabel`] one sample at a time: that only prunes a cat once an existing
    /// sample count reaches zero, so it can never remove an enrolled-but-sample-less cat (zero
    /// samples to subtract) the way a real deletion needs to. A no-op if `name` isn't known.
    pub fn remove_cat(&mut self, name: &str) {
        self.cats.retain(|c| c.name != name);
    }

    /// Rebuilds `name`'s centroid from scratch given every embedding it should now have --
    /// `DELETE /faces/samples/<cat>/<name>` uses this instead of a single [`Classifier::unlabel`]
    /// subtraction because deletion is permanent and the caller
    /// (`faces::Gallery::delete_sample`) already has to re-read every remaining `.emb` sidecar to
    /// answer "how many samples does this cat have now" -- recomputing from that same pass avoids
    /// any risk of the running sum ever drifting from what is actually still on disk. Registers
    /// `name` if it wasn't already known (mirrors [`Classifier::ensure_cat`]) rather than pruning
    /// it: a cat surviving with zero samples is the expected steady state after its last sample
    /// is deleted, not a phantom to clean up (see `Gallery::add_cat`'s own doc).
    pub fn recompute_cat(&mut self, name: &str, embeddings: &[[f32; EMBED_DIM]]) {
        let model = self.find_or_create(name);
        model.sum = [0.0; EMBED_DIM];
        model.count = 0;
        for raw in embeddings {
            let mut normalized = *raw;
            if l2_normalize(&mut normalized) {
                model.add(&normalized);
            }
        }
    }

    /// Ranks `embedding` against every enrolled cat's centroid.
    pub fn identify(&self, embedding: &[f32; EMBED_DIM]) -> Verdict {
        let mut normalized = *embedding;
        if !l2_normalize(&mut normalized) {
            return Verdict::Unknown { best_guess: None };
        }
        let mut scores: Vec<Score> = self
            .cats
            .iter()
            .filter_map(|c| c.cosine_to(&normalized).map(|score| Score { cat: c.name.clone(), score }))
            .collect();
        scores.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
        match scores.first() {
            None => Verdict::Unknown { best_guess: None },
            Some(top) if top.score >= self.t_accept => {
                let cat = top.cat.clone();
                let score = top.score;
                Verdict::Known { cat, score, second_best: scores.get(1).cloned() }
            }
            Some(_) => Verdict::Unknown { best_guess: scores.into_iter().next() },
        }
    }
}

/// What [`derive_threshold`] measured. See the module doc for the method.
#[derive(Debug, Clone, PartialEq)]
pub struct ThresholdReport {
    pub accept: f32,
    pub min_genuine: f32,
    pub max_impostor: f32,
    pub genuine_samples: usize,
    pub impostor_pairs: usize,
    /// `true` if every genuine score beat every impostor score, i.e. the data fully separates
    /// the classes at some threshold. `false` means the closest impostor scored higher than the
    /// hardest genuine case -- `accept` is still the best available midpoint, but expect some
    /// misclassification with the current sample size.
    pub separable: bool,
}

/// Why [`derive_threshold`] could not measure a threshold from the given samples.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ThresholdError {
    /// Fewer than two distinct cats have at least one sample -- there is no "someone else" yet
    /// to measure a false-match rate against.
    NotEnoughCats,
    /// At least two cats exist, but none has two or more samples, so leave-one-out validation
    /// cannot build a same-cat comparison centroid for even one genuine score.
    NotEnoughSamplesPerCat,
}

/// Measures a real accept threshold from labelled `(cat_name, raw_embedding)` pairs via
/// leave-one-out cross-validation -- see the module doc. Embeddings are normalised internally;
/// callers pass whatever `embed.rs`/`faces.rs` cached on disk.
pub fn derive_threshold(samples: &[(String, [f32; EMBED_DIM])]) -> Result<ThresholdReport, ThresholdError> {
    let mut by_cat: Vec<(String, Vec<[f32; EMBED_DIM]>)> = Vec::new();
    for (cat, embedding) in samples {
        let mut normalized = *embedding;
        if !l2_normalize(&mut normalized) {
            continue;
        }
        match by_cat.iter_mut().find(|(name, _)| name == cat) {
            Some((_, v)) => v.push(normalized),
            None => by_cat.push((cat.clone(), vec![normalized])),
        }
    }
    if by_cat.len() < 2 {
        return Err(ThresholdError::NotEnoughCats);
    }

    let mut genuine: Vec<f32> = Vec::new();
    for (_, own_samples) in &by_cat {
        if own_samples.len() < 2 {
            continue;
        }
        let full_sum = sum_of(own_samples);
        for sample in own_samples {
            let mut leave_one_out = full_sum;
            for i in 0..EMBED_DIM {
                leave_one_out[i] -= sample[i];
            }
            let norm = l2_norm(&leave_one_out);
            if norm > f32::EPSILON {
                genuine.push(dot(sample, &leave_one_out) / norm);
            }
        }
    }
    if genuine.is_empty() {
        return Err(ThresholdError::NotEnoughSamplesPerCat);
    }

    let mut impostor: Vec<f32> = Vec::new();
    for (i, (_, samples_i)) in by_cat.iter().enumerate() {
        for (j, (_, samples_j)) in by_cat.iter().enumerate() {
            if i == j {
                continue;
            }
            let sum_j = sum_of(samples_j);
            let norm_j = l2_norm(&sum_j);
            if norm_j <= f32::EPSILON {
                continue;
            }
            for sample in samples_i {
                impostor.push(dot(sample, &sum_j) / norm_j);
            }
        }
    }

    let min_genuine = genuine.iter().cloned().fold(f32::INFINITY, f32::min);
    let (accept, max_impostor, separable) = if impostor.is_empty() {
        (min_genuine, f32::NEG_INFINITY, true)
    } else {
        let max_impostor = impostor.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
        ((min_genuine + max_impostor) / 2.0, max_impostor, min_genuine > max_impostor)
    };

    Ok(ThresholdReport {
        accept,
        min_genuine,
        max_impostor,
        genuine_samples: genuine.len(),
        impostor_pairs: impostor.len(),
        separable,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A unit basis vector `e_i` (1.0 at index `i`, 0 elsewhere) -- trivially L2-normalised
    /// already, and any two distinct indices are exactly orthogonal (cosine 0), which makes
    /// expected scores easy to state exactly rather than approximately.
    fn basis(i: usize) -> [f32; EMBED_DIM] {
        let mut v = [0.0f32; EMBED_DIM];
        v[i] = 1.0;
        v
    }

    /// `basis(i)` rotated partway toward `basis(j)` by `weight` (0..1), then re-normalised --
    /// lets a test place a query at a controlled cosine distance from a pure basis vector.
    fn lean(i: usize, j: usize, weight: f32) -> [f32; EMBED_DIM] {
        let mut v = [0.0f32; EMBED_DIM];
        v[i] = 1.0 - weight;
        v[j] = weight;
        l2_normalize(&mut v);
        v
    }

    #[test]
    fn l2_normalize_scales_to_unit_length() {
        let mut v = [0.0f32; EMBED_DIM];
        v[0] = 3.0;
        v[1] = 4.0;
        assert!(l2_normalize(&mut v));
        assert!((v[0] - 0.6).abs() < 1e-6);
        assert!((v[1] - 0.8).abs() < 1e-6);
    }

    #[test]
    fn l2_normalize_rejects_the_zero_vector_and_leaves_it_unchanged() {
        let mut v = [0.0f32; EMBED_DIM];
        assert!(!l2_normalize(&mut v));
        assert_eq!(v, [0.0f32; EMBED_DIM]);
    }

    #[test]
    fn identify_is_unknown_with_no_cats_enrolled() {
        let c = Classifier::new();
        assert_eq!(c.identify(&basis(0)), Verdict::Unknown { best_guess: None });
    }

    #[test]
    fn label_then_identify_the_exact_same_vector_is_a_perfect_match() {
        let mut c = Classifier::new();
        c.label("Rashy", &basis(0));
        match c.identify(&basis(0)) {
            Verdict::Known { cat, score, second_best } => {
                assert_eq!(cat, "Rashy");
                assert!((score - 1.0).abs() < 1e-5, "expected ~1.0, got {score}");
                assert_eq!(second_best, None);
            }
            other => panic!("expected Known, got {other:?}"),
        }
    }

    #[test]
    fn identify_rejects_a_far_query_as_unknown_but_reports_the_best_guess() {
        let mut c = Classifier::new();
        c.label("Rashy", &basis(0));
        c.t_accept = 0.9;
        // Orthogonal to Rashy's only sample -- cosine 0, well under 0.9.
        match c.identify(&basis(1)) {
            Verdict::Unknown { best_guess: Some(Score { cat, score }) } => {
                assert_eq!(cat, "Rashy");
                assert!(score.abs() < 1e-5, "expected ~0.0, got {score}");
            }
            other => panic!("expected Unknown with a best guess, got {other:?}"),
        }
    }

    #[test]
    fn centroid_update_reflects_the_average_direction_of_multiple_samples() {
        let mut c = Classifier::new();
        // Two samples leaning opposite directions off e0 toward e1/e2 average back toward e0.
        c.label("Rashy", &lean(0, 1, 0.3));
        c.label("Rashy", &lean(0, 2, 0.3));
        c.t_accept = -1.0; // isolate the ranking/score math, not the accept cutoff
        let Verdict::Known { score, .. } = c.identify(&basis(0)) else {
            panic!("expected Known");
        };
        // Centroid direction is closer to e0 than either individual sample was (e0 component
        // reinforces, e1/e2 components partially cancel in direction only, not magnitude) --
        // so the score against pure e0 must exceed either single sample's own score of ~0.95.
        let single_sample_score = {
            let mut single = Classifier::new();
            single.label("Rashy", &lean(0, 1, 0.3));
            let Verdict::Known { score, .. } = single.identify(&basis(0)) else { panic!("expected Known") };
            score
        };
        assert!(
            score > single_sample_score,
            "averaged centroid ({score}) should score higher against e0 than one leaning sample ({single_sample_score})"
        );
    }

    #[test]
    fn second_best_is_reported_when_multiple_cats_are_enrolled() {
        let mut c = Classifier::new();
        c.label("Rashy", &basis(0));
        c.label("Ghost", &lean(0, 1, 0.2));
        c.t_accept = -1.0;
        match c.identify(&basis(0)) {
            Verdict::Known { cat, second_best: Some(Score { cat: second, .. }), .. } => {
                assert_eq!(cat, "Rashy");
                assert_eq!(second, "Ghost");
            }
            other => panic!("expected Known with a second_best, got {other:?}"),
        }
    }

    #[test]
    fn unlabel_prunes_a_cat_once_its_last_sample_is_removed() {
        let mut c = Classifier::new();
        c.label("Rashy", &basis(0));
        assert_eq!(c.cats().count(), 1);
        assert!(c.unlabel("Rashy", &basis(0)));
        assert_eq!(c.cats().count(), 0);
        assert_eq!(c.identify(&basis(0)), Verdict::Unknown { best_guess: None });
    }

    #[test]
    fn unlabel_of_an_unknown_cat_returns_false_and_changes_nothing() {
        let mut c = Classifier::new();
        c.label("Rashy", &basis(0));
        assert!(!c.unlabel("Ghost", &basis(0)));
        assert_eq!(c.cats().count(), 1);
    }

    #[test]
    fn relabelling_a_sample_from_one_cat_to_another_matches_labelling_it_there_directly() {
        let sample = lean(3, 7, 0.25);

        let mut relabelled = Classifier::new();
        relabelled.label("Rashy", &basis(3)); // unrelated existing sample, kept throughout
        relabelled.label("Rashy", &sample);
        assert!(relabelled.unlabel("Rashy", &sample));
        relabelled.label("Ghost", &sample);

        let mut direct = Classifier::new();
        direct.label("Rashy", &basis(3));
        direct.label("Ghost", &sample);

        relabelled.t_accept = -1.0;
        direct.t_accept = -1.0;
        let query = basis(7);
        assert_eq!(relabelled.identify(&query), direct.identify(&query));
    }

    #[test]
    fn ensure_cat_registers_a_zero_sample_cat_that_never_wins_identification() {
        let mut c = Classifier::new();
        c.ensure_cat("Ghost");
        assert_eq!(c.cats().map(|m| m.name.clone()).collect::<Vec<_>>(), vec!["Ghost"]);
        c.t_accept = -1.0;
        // A cat with zero samples has no centroid to score against -- must never be returned.
        assert_eq!(c.identify(&basis(0)), Verdict::Unknown { best_guess: None });
    }

    #[test]
    fn derive_threshold_reports_not_enough_cats_with_only_one() {
        let samples = vec![("Rashy".to_string(), basis(0)), ("Rashy".to_string(), basis(0))];
        assert_eq!(derive_threshold(&samples), Err(ThresholdError::NotEnoughCats));
    }

    #[test]
    fn derive_threshold_reports_not_enough_samples_per_cat_when_every_cat_has_exactly_one() {
        let samples = vec![("Rashy".to_string(), basis(0)), ("Ghost".to_string(), basis(1))];
        assert_eq!(derive_threshold(&samples), Err(ThresholdError::NotEnoughSamplesPerCat));
    }

    #[test]
    fn derive_threshold_separates_two_tight_far_apart_clusters() {
        // Rashy's samples all lean slightly off e0; Ghost's all lean slightly off e1. The two
        // clusters are far apart (e0 vs e1 are orthogonal) and each is tight (small lean), so
        // this must come back separable with a sensible midpoint threshold.
        let samples = vec![
            ("Rashy".to_string(), lean(0, 2, 0.05)),
            ("Rashy".to_string(), lean(0, 3, 0.05)),
            ("Rashy".to_string(), lean(0, 4, 0.05)),
            ("Ghost".to_string(), lean(1, 2, 0.05)),
            ("Ghost".to_string(), lean(1, 3, 0.05)),
            ("Ghost".to_string(), lean(1, 4, 0.05)),
        ];
        let report = derive_threshold(&samples).expect("enough data");
        assert!(report.separable, "{report:?}");
        assert!(report.min_genuine > 0.9, "{report:?}");
        assert!(report.max_impostor < 0.5, "{report:?}");
        assert!(report.accept > report.max_impostor && report.accept < report.min_genuine, "{report:?}");
        assert_eq!(report.genuine_samples, 6);
        assert_eq!(report.impostor_pairs, 6);
    }

    #[test]
    fn derive_threshold_flags_non_separable_when_two_cats_share_identical_samples() {
        let samples = vec![
            ("Rashy".to_string(), basis(0)),
            ("Rashy".to_string(), lean(0, 1, 0.01)),
            ("Ghost".to_string(), basis(0)),
            ("Ghost".to_string(), lean(0, 1, 0.01)),
        ];
        let report = derive_threshold(&samples).expect("enough data");
        assert!(!report.separable, "{report:?}");
    }
}
