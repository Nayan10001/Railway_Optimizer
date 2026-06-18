"""
ktv_psa_scheduler — KTV-PSA Freight Scheduler Python Package.

Public API surface:
  - _native      : Rust extension (physics engine + conflict masking)
  - pipeline     : Data ingestion and preprocessing (Layer 1)
  - model        : TSN MIP formulation (Layer 3 — coming next)
  - orchestrator : Rolling-horizon controller (Layer 4 — coming next)
"""
from . import _native

# Re-export native classes at package level
from ._native import (
    ConflictMask,
    MaskedEdge,
    new_conflict_mask,
    batch_filter_edges,
)

# Expose compute_travel_time_py under the cleaner name compute_travel_time
from ._native import compute_travel_time_py as compute_travel_time

# Re-export model classes and functions
from .model import (
    TSNEdge,
    ScheduledPath,
    SolveResult,
    build_tsn_graph,
    solve_model,
)

# Re-export orchestrator classes and functions
from .orchestrator import (
    OrchestratorConfig,
    OrchestratorResult,
    FrozenSlice,
    run_rolling_horizon,
)

__all__ = [
    "_native",
    "ConflictMask",
    "MaskedEdge",
    "new_conflict_mask",
    "compute_travel_time",
    "batch_filter_edges",
    "TSNEdge",
    "ScheduledPath",
    "SolveResult",
    "build_tsn_graph",
    "solve_model",
    "OrchestratorConfig",
    "OrchestratorResult",
    "FrozenSlice",
    "run_rolling_horizon",
]

