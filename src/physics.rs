/// Traction physics calculations for freight traversal time.
///
/// This module computes the effective travel time for a freight train over a
/// block section, accounting for three real-world factors drawn from the
/// KTV-PSA infrastructure CSVs:
///
/// 1. **PSR (Permanent Speed Restriction)** — `MANGDSSPEED` from PSR CSV caps
///    the goods-train speed on specific block sub-sections.
/// 2. **Gradient resistance** — `MANGRADEVALUE` stored as 1-in-N integers from
///    the GRADIENT CSV. The Indian Railways formula is:
///      `GR (kg-force/tonne) = 1000 / N`
///    Rise grades add resistance; fall grades provide assistance (modelled as
///    a partial offset, not a full deduction, since braking limits apply).
/// 3. **Curvature resistance** — `MANCURVERADIUS` in metres from the CURVATURE
///    CSV. The IRS formula is:
///      `CR (kg-force/tonne) = 13.5 / (R - 6.0)`
///    Both factors are accumulated as fractional reductions to effective speed.
///
/// All speeds are in km/h. All distances are in km (converted from metres for
/// segment data). Travel time is returned in minutes.
use serde::Deserialize;

use crate::error::SchedulerError;

// ── Grade direction enum ─────────────────────────────────────────────────────

/// Grade type from the GRADIENT CSV (`MAVGRADETYPE` field).
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum GradeType {
    Rise,
    Fall,
    Level,
}

// ── Segment data structs (deserialized from JSON passed by Python) ───────────

/// A single gradient sub-segment of a block section.
/// Passed from Python as a JSON array element.
///
/// JSON field names match what Python constructs from the GRADIENT CSV:
/// `{"dist_m": 650.0, "grade": "RISE", "val": 350}`
#[derive(Debug, Clone, Deserialize)]
pub struct GradientSegment {
    /// Segment length in metres (`MANDISTANCE` from CSV)
    pub dist_m: f32,
    /// Grade direction: "RISE", "FALL", or "LEVEL"
    pub grade: GradeType,
    /// Grade value as 1-in-N integer (`MANGRADEVALUE`)
    pub val: f32,
}

/// A single curvature sub-segment of a block section.
/// JSON field names match what Python constructs from the CURVATURE CSV:
/// `{"dist_m": 402.0, "radius_m": 1750.0}`
#[derive(Debug, Clone, Deserialize)]
pub struct CurveSegment {
    /// Segment length in metres (`MANDISTANCE` from CSV)
    pub dist_m: f32,
    /// Curve radius in metres (`MANCURVERADIUS` from CSV)
    pub radius_m: f32,
}

// ── Constants ────────────────────────────────────────────────────────────────

/// Minimum travel time in minutes (1 minute per block section).
const MIN_TRAVEL_MINUTES: f32 = 1.0;

/// Maximum travel time in minutes (8 hours; guards against near-zero speeds).
const MAX_TRAVEL_MINUTES: f32 = 480.0;

/// Conversion factor from (kg-force/tonne) resistance to fractional speed loss.
/// Indian Railways empirical: full GR of 10 kgf/t corresponds to ~12.5% speed loss.
/// So: speed_penalty_fraction = GR / 80.0
const GR_TO_SPEED_PENALTY: f32 = 80.0;

/// Fall grades provide only partial assistance (braking limits the benefit).
/// We model recovery at 40% of the RISE penalty magnitude.
const FALL_RECOVERY_FACTOR: f32 = 0.4;

// ── Core physics functions ───────────────────────────────────────────────────

/// Compute weighted gradient resistance (kg-force/tonne) across all segments.
///
/// Each segment's contribution is weighted by its fraction of total block length.
/// RISE adds positive resistance; FALL subtracts at `FALL_RECOVERY_FACTOR`.
fn weighted_gradient_resistance(
    gradient_segments: &[GradientSegment],
    total_length_m: f32,
) -> f32 {
    if total_length_m <= 0.0 || gradient_segments.is_empty() {
        return 0.0;
    }
    let mut weighted_gr = 0.0f32;
    for seg in gradient_segments {
        if seg.val <= 0.0 || seg.dist_m <= 0.0 {
            continue;
        }
        let gr = 1000.0 / seg.val; // kg-force per tonne
        let weight = seg.dist_m / total_length_m;
        match seg.grade {
            GradeType::Rise => weighted_gr += gr * weight,
            GradeType::Fall => weighted_gr -= gr * weight * FALL_RECOVERY_FACTOR,
            GradeType::Level => {}
        }
    }
    weighted_gr.max(0.0) // Net resistance cannot be negative (fall-only sections OK)
}

/// Compute weighted curvature resistance (kg-force/tonne) across all segments.
///
/// Uses the IRS formula: CR = 13.5 / (R - 6.0) where R is radius in metres.
/// Weighted by each segment's fraction of total block length.
fn weighted_curvature_resistance(
    curve_segments: &[CurveSegment],
    total_length_m: f32,
) -> f32 {
    if total_length_m <= 0.0 || curve_segments.is_empty() {
        return 0.0;
    }
    let mut weighted_cr = 0.0f32;
    for seg in curve_segments {
        // Guard: radius must be > 6.0 m (IRS formula denominator)
        if seg.radius_m <= 6.0 || seg.dist_m <= 0.0 {
            continue;
        }
        let cr = 13.5 / (seg.radius_m - 6.0); // kg-force per tonne
        let weight = seg.dist_m / total_length_m;
        weighted_cr += cr * weight;
    }
    weighted_cr.max(0.0)
}

/// Compute freight train travel time over one block section.
///
/// # Arguments
/// * `length_km` — block section length in km
/// * `base_speed_kmh` — freight train speed attribute (km/h); from FreightLoad or BlockSection
/// * `psr_speed_kmh` — optional PSR goods-train speed limit (km/h); `None` = no restriction
/// * `gradient_segments` — ordered list of gradient sub-segments for this block
/// * `curve_segments` — ordered list of curvature sub-segments for this block
///
/// # Returns
/// Travel time in minutes, clamped to `[MIN_TRAVEL_MINUTES, MAX_TRAVEL_MINUTES]`.
pub fn compute_travel_time(
    length_km: f32,
    base_speed_kmh: f32,
    psr_speed_kmh: Option<f32>,
    gradient_segments: &[GradientSegment],
    curve_segments: &[CurveSegment],
) -> Result<f32, SchedulerError> {
    // 1. Apply PSR cap: effective speed cannot exceed PSR limit
    let psr_limited = match psr_speed_kmh {
        Some(psr) if psr > 0.0 => base_speed_kmh.min(psr),
        _ => base_speed_kmh,
    };

    // Guard: base speed must be positive
    if psr_limited <= 0.0 {
        return Ok(MAX_TRAVEL_MINUTES); // Effectively stationary — return max allowed time
    }

    // 2. Compute gradient and curvature resistance components
    let total_length_m = length_km * 1000.0;
    let gr = weighted_gradient_resistance(gradient_segments, total_length_m);
    let cr = weighted_curvature_resistance(curve_segments, total_length_m);

    // 3. Convert combined resistance to a fractional speed penalty
    //    GR_TO_SPEED_PENALTY = 80.0 kgf/t corresponds to 100% speed reduction
    //    (i.e., the train stalls). Cap total penalty at 50% to avoid absurd results.
    let total_resistance = gr + cr;
    let speed_penalty = (total_resistance / GR_TO_SPEED_PENALTY).min(0.50);

    // 4. Effective speed after physics penalties
    let effective_speed = psr_limited * (1.0 - speed_penalty);

    // 5. Travel time in minutes
    if effective_speed <= 0.0 {
        return Ok(MAX_TRAVEL_MINUTES);
    }
    let raw_minutes = (length_km / effective_speed) * 60.0;

    // 6. Validate and clamp
    if raw_minutes.is_nan() || raw_minutes.is_infinite() || raw_minutes < 0.0 {
        return Err(SchedulerError::NegativeTravelTime {
            block_id: String::from("<unknown>"),
            value: raw_minutes,
        });
    }

    Ok(raw_minutes.clamp(MIN_TRAVEL_MINUTES, MAX_TRAVEL_MINUTES))
}

// ── JSON parsing helpers (called from lib.rs PyO3 wrappers) ─────────────────

/// Parse a JSON string into a `Vec<GradientSegment>`.
/// Expected format: `[{"dist_m": 650.0, "grade": "RISE", "val": 350.0}, ...]`
pub fn parse_gradient_json(json: &str) -> Result<Vec<GradientSegment>, SchedulerError> {
    serde_json::from_str(json).map_err(|e| SchedulerError::BadSegmentJson {
        field: "gradient".to_string(),
        detail: e.to_string(),
    })
}

/// Parse a JSON string into a `Vec<CurveSegment>`.
/// Expected format: `[{"dist_m": 402.0, "radius_m": 1750.0}, ...]`
pub fn parse_curve_json(json: &str) -> Result<Vec<CurveSegment>, SchedulerError> {
    serde_json::from_str(json).map_err(|e| SchedulerError::BadSegmentJson {
        field: "curvature".to_string(),
        detail: e.to_string(),
    })
}

// ── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn no_segments() -> (Vec<GradientSegment>, Vec<CurveSegment>) {
        (vec![], vec![])
    }

    #[test]
    fn test_basic_travel_time() {
        // 10 km at 60 km/h with no restrictions → exactly 10 minutes
        let (grads, curves) = no_segments();
        let t = compute_travel_time(10.0, 60.0, None, &grads, &curves).unwrap();
        assert!((t - 10.0).abs() < 0.01, "Expected ~10.0 min, got {}", t);
    }

    #[test]
    fn test_psr_speed_limit_applied() {
        // 10 km at base 80 km/h, but PSR caps at 40 km/h → ~15 minutes
        let (grads, curves) = no_segments();
        let t = compute_travel_time(10.0, 80.0, Some(40.0), &grads, &curves).unwrap();
        assert!((t - 15.0).abs() < 0.1, "Expected ~15.0 min (PSR limited), got {}", t);
    }

    #[test]
    fn test_psr_higher_than_base_has_no_effect() {
        // PSR at 100 km/h but base is 60 km/h → PSR doesn't slow us down
        let (grads, curves) = no_segments();
        let t_no_psr = compute_travel_time(10.0, 60.0, None, &grads, &curves).unwrap();
        let t_high_psr = compute_travel_time(10.0, 60.0, Some(100.0), &grads, &curves).unwrap();
        assert!((t_no_psr - t_high_psr).abs() < 0.01);
    }

    #[test]
    fn test_steep_rise_slows_train() {
        // 1-in-100 rise (GR = 10 kgf/t) over 10 km at 60 km/h → slower than baseline
        let (_, curves) = no_segments();
        let grads = vec![GradientSegment {
            dist_m: 10_000.0,
            grade: GradeType::Rise,
            val: 100.0,
        }];
        let t_flat = compute_travel_time(10.0, 60.0, None, &[], &curves).unwrap();
        let t_rise = compute_travel_time(10.0, 60.0, None, &grads, &curves).unwrap();
        assert!(t_rise > t_flat, "RISE grade should increase travel time");
    }

    #[test]
    fn test_fall_grade_reduces_penalty() {
        // A fall grade should produce less time than level (partial recovery)
        let curves: Vec<CurveSegment> = vec![];
        let grads_fall = vec![GradientSegment {
            dist_m: 10_000.0,
            grade: GradeType::Fall,
            val: 100.0,
        }];
        let t_flat = compute_travel_time(10.0, 60.0, None, &[], &curves).unwrap();
        let t_fall = compute_travel_time(10.0, 60.0, None, &grads_fall, &curves).unwrap();
        // Fall grade: net resistance clamped to 0.0, so same as flat
        assert!(
            t_fall <= t_flat + 0.01,
            "FALL grade should not increase travel time above flat"
        );
    }

    #[test]
    fn test_curvature_adds_time() {
        // Tight curve (R=200m) should add resistance and increase travel time
        let grads: Vec<GradientSegment> = vec![];
        let curves_tight = vec![CurveSegment {
            dist_m: 5_000.0,
            radius_m: 200.0,
        }];
        let t_straight = compute_travel_time(10.0, 60.0, None, &grads, &[]).unwrap();
        let t_curved = compute_travel_time(10.0, 60.0, None, &grads, &curves_tight).unwrap();
        assert!(t_curved > t_straight, "Tight curve should increase travel time");
    }

    #[test]
    fn test_min_clamp() {
        // Very short block (0.01 km) at high speed should still return ≥ 1.0 min
        let (grads, curves) = no_segments();
        let t = compute_travel_time(0.01, 120.0, None, &grads, &curves).unwrap();
        assert!(t >= MIN_TRAVEL_MINUTES, "Travel time must be >= 1.0 min");
    }

    #[test]
    fn test_max_clamp() {
        // Effectively zero speed → should return MAX_TRAVEL_MINUTES
        let (grads, curves) = no_segments();
        let t = compute_travel_time(100.0, 0.0, None, &grads, &curves).unwrap();
        assert_eq!(t, MAX_TRAVEL_MINUTES);
    }

    #[test]
    fn test_parse_gradient_json_valid() {
        let json = r#"[{"dist_m": 650.0, "grade": "RISE", "val": 350.0}]"#;
        let segs = parse_gradient_json(json).unwrap();
        assert_eq!(segs.len(), 1);
        assert_eq!(segs[0].grade, GradeType::Rise);
        assert!((segs[0].val - 350.0).abs() < 0.01);
    }

    #[test]
    fn test_parse_gradient_json_invalid() {
        let json = r#"[{"dist_m": "bad"}]"#;
        assert!(parse_gradient_json(json).is_err());
    }

    #[test]
    fn test_parse_curve_json_valid() {
        let json = r#"[{"dist_m": 402.0, "radius_m": 1750.0}]"#;
        let segs = parse_curve_json(json).unwrap();
        assert_eq!(segs.len(), 1);
        assert!((segs[0].radius_m - 1750.0).abs() < 0.01);
    }

    #[test]
    fn test_empty_json_arrays() {
        let grads = parse_gradient_json("[]").unwrap();
        let curves = parse_curve_json("[]").unwrap();
        let t = compute_travel_time(7.74, 60.0, None, &grads, &curves).unwrap();
        // 7.74 km / 60 km/h * 60 = 7.74 min
        assert!((t - 7.74).abs() < 0.05, "Expected ~7.74 min, got {}", t);
    }
}
