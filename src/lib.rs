/// KTV-PSA Scheduler — Rust extension module exposed to Python via PyO3.
///
/// This module exports the native scheduler API under the `_native` Python
/// module name. Python imports it as:
///
/// ```python
/// from ktv_psa_scheduler import _native as native
/// mask = native.new_conflict_mask(5)
/// t    = native.compute_travel_time(7.74, 60.0, -1.0, "[]", "[]")
/// ```
///
/// ## Exported symbols
///
/// ### Classes
/// - `PyConflictMask` (as `ConflictMask`) — pre-built passenger conflict lookup
/// - `PyMaskedEdge`   (as `MaskedEdge`)   — result of a filtered candidate edge
///
/// ### Functions
/// - `new_conflict_mask(headway_minutes) → ConflictMask`
/// - `compute_travel_time(length_km, base_speed, psr_speed, gradient_json, curve_json) → float`
/// - `batch_filter_edges(candidates, mask, block_lengths, block_speeds) → list[MaskedEdge]`
///
/// ## Design notes
/// - Gradient and curvature data are passed as JSON strings from Python.
///   This avoids complex PyO3 object mappings and keeps the Python side simple.
///   `serde_json` deserializes them inside Rust on each call.
/// - `batch_filter_edges` uses `rayon` for parallel edge evaluation across the
///   candidate set. Python receives a `Vec<MaskedEdge>` back.
/// - All errors propagate as `PyErr` (`ValueError` or `RuntimeError`) via the
///   `From<SchedulerError> for PyErr` implementation in `error.rs`.
use pyo3::prelude::*;
use rayon::prelude::*;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

mod error;
mod masking;
mod physics;

use masking::ConflictMask;
use physics::{compute_travel_time, parse_curve_json, parse_gradient_json};

// ── Python class: ConflictMask ───────────────────────────────────────────────

/// Python-accessible wrapper around the Rust `ConflictMask`.
///
/// Usage from Python:
/// ```python
/// mask = native.new_conflict_mask(5)
/// mask.insert_interval("KTV-KPL", 100, 140, 1)
/// mask.sort_all()
/// assert mask.conflicts("KTV-KPL", 110, 130) == True
/// ```
#[pyclass(name = "ConflictMask")]
pub struct PyConflictMask {
    inner: Arc<Mutex<ConflictMask>>,
}

#[pymethods]
impl PyConflictMask {
    /// Insert one passenger occupancy interval.
    ///
    /// Args:
    ///     block_id: Block section string key (e.g. "KTV-KPL")
    ///     entry_min: Minute when passenger train enters the block (from T0)
    ///     exit_min: Minute when passenger train exits the block (from T0)
    ///     priority: Priority class (1 = highest, e.g. Vande Bharat)
    pub fn insert_interval(
        &self,
        block_id: &str,
        entry_min: i32,
        exit_min: i32,
        priority: u8,
    ) -> PyResult<()> {
        let mut guard = self.inner.lock().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        guard.insert_interval(block_id, entry_min, exit_min, priority);
        Ok(())
    }

    /// Sort all per-block interval lists.
    ///
    /// Must be called once after all `insert_interval()` calls and before
    /// any `conflicts()` call. O(P log P) total where P = total intervals.
    pub fn sort_all(&self) -> PyResult<()> {
        let mut guard = self.inner.lock().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        guard.sort_all();
        Ok(())
    }

    /// Check whether a freight traversal window conflicts with any passenger train.
    ///
    /// Args:
    ///     block_id: Block section string key
    ///     entry_min: Minute when freight enters the block
    ///     exit_min: Minute when freight exits the block
    ///
    /// Returns:
    ///     True if conflict detected (passenger traffic + headway buffer overlap)
    pub fn conflicts(&self, block_id: &str, entry_min: i32, exit_min: i32) -> PyResult<bool> {
        let guard = self.inner.lock().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        Ok(guard.conflicts(block_id, entry_min, exit_min))
    }

    /// Return the number of block sections in this mask.
    pub fn block_count(&self) -> PyResult<usize> {
        let guard = self.inner.lock().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        Ok(guard.block_count())
    }

    /// Return the total number of passenger intervals across all blocks.
    pub fn interval_count(&self) -> PyResult<usize> {
        let guard = self.inner.lock().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        Ok(guard.interval_count())
    }

    /// Return the headway buffer in minutes.
    pub fn headway_minutes(&self) -> PyResult<i32> {
        let guard = self.inner.lock().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        Ok(guard.headway_minutes())
    }

    pub fn __repr__(&self) -> PyResult<String> {
        let guard = self.inner.lock().map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("Lock poisoned: {}", e))
        })?;
        Ok(format!(
            "ConflictMask(blocks={}, intervals={}, headway={})",
            guard.block_count(),
            guard.interval_count(),
            guard.headway_minutes()
        ))
    }
}

// ── Python class: MaskedEdge ─────────────────────────────────────────────────

/// A candidate freight edge after conflict masking and travel-time computation.
///
/// Returned by `batch_filter_edges()`. The Python model layer uses `feasible`
/// to decide which edges become decision variables in the MIP formulation.
///
/// Attributes:
///     block_id: Block section string key
///     entry_minute: Minute when freight train enters the block
///     exit_minute: Minute when freight train exits the block
///     travel_minutes: Computed traversal time from the physics engine (float)
///     feasible: False if a passenger conflict was detected, True otherwise
#[pyclass(name = "MaskedEdge", from_py_object)]
#[derive(Debug, Clone)]
pub struct PyMaskedEdge {
    #[pyo3(get)]
    pub block_id: String,
    #[pyo3(get)]
    pub entry_minute: i32,
    #[pyo3(get)]
    pub exit_minute: i32,
    #[pyo3(get)]
    pub travel_minutes: f32,
    #[pyo3(get)]
    pub feasible: bool,
}

#[pymethods]
impl PyMaskedEdge {
    pub fn __repr__(&self) -> String {
        format!(
            "MaskedEdge(block='{}', [{}, {}], t={:.2}min, feasible={})",
            self.block_id, self.entry_minute, self.exit_minute, self.travel_minutes, self.feasible
        )
    }
}

// ── Exported Python functions ────────────────────────────────────────────────

/// Create a new empty `ConflictMask` with the given headway buffer.
///
/// Args:
///     headway_minutes: Safety headway applied symmetrically around each
///                      freight traversal window (typically 3–5 minutes).
///
/// Returns:
///     An empty ConflictMask ready for `insert_interval()` calls.
#[pyfunction]
fn new_conflict_mask(headway_minutes: i32) -> PyConflictMask {
    PyConflictMask {
        inner: Arc::new(Mutex::new(ConflictMask::new(headway_minutes))),
    }
}

/// Compute freight traversal time over one block section (minutes).
///
/// This is the single-call physics API. For batch processing, use
/// `batch_filter_edges()` which also applies conflict masking.
///
/// Args:
///     length_km: Block section length in km
///     base_speed_kmh: Freight train speed (km/h); from FreightLoad or BlockSection max speed
///     psr_speed_kmh: PSR goods-train speed limit (km/h). Pass -1.0 for no restriction.
///     gradient_json: JSON string of gradient segments, e.g.:
///                    '[{"dist_m": 650.0, "grade": "RISE", "val": 350.0}]'
///                    Empty array '[]' means no gradient data for this block.
///     curve_json: JSON string of curvature segments, e.g.:
///                 '[{"dist_m": 402.0, "radius_m": 1750.0}]'
///                 Empty array '[]' means no curvature data for this block.
///
/// Returns:
///     Travel time in minutes (clamped to [1.0, 480.0]).
///
/// Raises:
///     ValueError: If JSON parsing fails.
///     RuntimeError: If physics produces an invalid result.
#[pyfunction]
fn compute_travel_time_py(
    length_km: f32,
    base_speed_kmh: f32,
    psr_speed_kmh: f32,
    gradient_json: &str,
    curve_json: &str,
) -> PyResult<f32> {
    // Sentinel: -1.0 means "no PSR restriction"
    let psr = if psr_speed_kmh < 0.0 {
        None
    } else {
        Some(psr_speed_kmh)
    };

    let gradients = parse_gradient_json(gradient_json)?;
    let curves = parse_curve_json(curve_json)?;

    let t = compute_travel_time(length_km, base_speed_kmh, psr, &gradients, &curves)?;
    Ok(t)
}

/// Batch-filter candidate freight edges against the conflict mask.
///
/// For each candidate `(block_id, entry_minute, exit_minute)`:
///   1. Check if the traversal window conflicts with any passenger train
///      (via `ConflictMask::conflicts()`).
///   2. Compute the physics travel time using block length and speed from
///      the provided lookup dicts. Gradient and curvature data are not
///      applied here (pass pre-computed speeds from the pipeline layer,
///      or call `compute_travel_time_py()` per block for full physics).
///   3. Return a `MaskedEdge` with `feasible=True/False` and the travel time.
///
/// This function uses `rayon` for parallel evaluation across the candidate list.
///
/// Args:
///     candidates: List of `(block_id, entry_minute, exit_minute)` tuples
///     mask: A populated and sorted `ConflictMask`
///     block_lengths: Dict mapping block_id → length_km
///     block_speeds: Dict mapping block_id → effective speed (km/h)
///                   (Pre-computed by Python pipeline after applying PSR/physics)
///
/// Returns:
///     List of `MaskedEdge` objects, one per candidate (order preserved).
///
/// Raises:
///     RuntimeError: If the mask lock cannot be acquired.
#[pyfunction]
fn batch_filter_edges(
    candidates: Vec<(String, i32, i32)>,
    mask: &PyConflictMask,
    block_lengths: HashMap<String, f32>,
    block_speeds: HashMap<String, f32>,
) -> PyResult<Vec<PyMaskedEdge>> {
    // Take a snapshot of the conflict mask state for parallel read access
    let mask_guard = mask.inner.lock().map_err(|e| {
        pyo3::exceptions::PyRuntimeError::new_err(format!("ConflictMask lock poisoned: {}", e))
    })?;

    // Process all candidates in parallel using rayon
    let edges: Vec<PyMaskedEdge> = candidates
        .par_iter()
        .map(|(block_id, entry_min, exit_min)| {
            // 1. Conflict check
            let has_conflict = mask_guard.conflicts(block_id, *entry_min, *exit_min);

            // 2. Travel time from pre-computed block speed
            let length_km = block_lengths.get(block_id.as_str()).copied().unwrap_or(0.0);
            let speed_kmh = block_speeds.get(block_id.as_str()).copied().unwrap_or(0.0);

            let travel_minutes = if speed_kmh > 0.0 && length_km > 0.0 {
                // Simple time = distance / speed (physics already applied by Python)
                let raw = (length_km / speed_kmh) * 60.0;
                raw.clamp(1.0, 480.0)
            } else {
                // Unknown block or zero speed → use window duration as fallback
                (exit_min - entry_min).max(1) as f32
            };

            PyMaskedEdge {
                block_id: block_id.clone(),
                entry_minute: *entry_min,
                exit_minute: *exit_min,
                travel_minutes,
                feasible: !has_conflict,
            }
        })
        .collect();

    Ok(edges)
}

// ── PyO3 module registration ─────────────────────────────────────────────────

/// The native Rust extension module for the KTV-PSA scheduler.
///
/// Exposed as `ktv_psa_scheduler._native`. Python imports:
/// ```python
/// from ktv_psa_scheduler import _native as native
/// ```
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Register Python-accessible classes
    m.add_class::<PyConflictMask>()?;
    m.add_class::<PyMaskedEdge>()?;

    // Register Python-callable functions
    m.add_function(wrap_pyfunction!(new_conflict_mask, m)?)?;
    m.add_function(wrap_pyfunction!(compute_travel_time_py, m)?)?;
    m.add_function(wrap_pyfunction!(batch_filter_edges, m)?)?;

    // Module metadata
    m.add("__version__", "0.1.0")?;
    m.add("__doc__", "KTV-PSA Scheduler native Rust engine (physics + masking)")?;

    Ok(())
}