/// Scheduler error types, bridged cleanly to Python exceptions via PyO3.
///
/// All public Rust functions return `Result<T, SchedulerError>` and PyO3
/// automatically converts `SchedulerError` into a Python exception through
/// the `From<SchedulerError> for pyo3::PyErr` implementation below.
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::PyErr;

#[derive(Debug)]
pub enum SchedulerError {
    /// A block_id string was supplied that does not exist in the physics context.
    InvalidBlockId(String),

    /// Physics computation produced a non-positive or infinite travel time.
    NegativeTravelTime { block_id: String, value: f32 },

    /// The JSON payload for gradient or curvature segments could not be parsed.
    BadSegmentJson { field: String, detail: String },

    /// Conflict check failed due to an internal inconsistency.
    ConflictCheckFailed(String),
}

impl std::fmt::Display for SchedulerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SchedulerError::InvalidBlockId(id) => {
                write!(f, "InvalidBlockId: block '{}' not found in context", id)
            }
            SchedulerError::NegativeTravelTime { block_id, value } => write!(
                f,
                "NegativeTravelTime: block '{}' produced travel time {:.3} min",
                block_id, value
            ),
            SchedulerError::BadSegmentJson { field, detail } => {
                write!(f, "BadSegmentJson in '{}': {}", field, detail)
            }
            SchedulerError::ConflictCheckFailed(msg) => {
                write!(f, "ConflictCheckFailed: {}", msg)
            }
        }
    }
}

/// Map SchedulerError variants to appropriate Python built-in exceptions.
/// - `InvalidBlockId` and `BadSegmentJson` → `ValueError` (caller supplied bad input)
/// - `NegativeTravelTime` and `ConflictCheckFailed` → `RuntimeError` (internal logic failure)
impl From<SchedulerError> for PyErr {
    fn from(e: SchedulerError) -> PyErr {
        match e {
            SchedulerError::InvalidBlockId(_) | SchedulerError::BadSegmentJson { .. } => {
                PyValueError::new_err(e.to_string())
            }
            SchedulerError::NegativeTravelTime { .. }
            | SchedulerError::ConflictCheckFailed(_) => {
                PyRuntimeError::new_err(e.to_string())
            }
        }
    }
}
