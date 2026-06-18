## Plan: KTV-PSA Scheduler Implementation

Build the system in three explicit layers, in this order: Rust for fast pruning/physics, Python for preprocessing/model/orchestration, and HiGHS for solving. The goal is to turn the current stub-only repo into a rolling-horizon freight scheduler that can ingest corridor data, prefilter invalid decisions, formulate a TSN optimization model, solve it with HiGHS, and publish frozen execution slices.

**Steps**
1. Establish the project boundaries and data contracts first, using the current repository layout as the baseline. Keep `ktv_psa_scheduler/` as the Python package, `src/` as the Rust extension crate, and `tests/` for verification. Define the shared entities that all layers will use: freight train records, passenger timetable records, station metadata, block metadata, loop capacities, headway values, and minute-indexed time windows.
2. Implement the data preprocessing layer in Python before any solver work, because every later module depends on normalized inputs. Add loading, schema validation, and temporal alignment in `ktv_psa_scheduler/pipeline.py`. This layer should read the CSV inputs under `data/`, normalize field names, derive minute offsets relative to the rolling horizon start, filter to active windows, and produce compact Python objects or dataframes that are safe to pass into Rust and the model layer.
3. Implement the Rust layer next, because it is the performance-critical pruning and physics engine. Keep this layer in `src/lib.rs`, `src/masking.rs`, `src/physics.rs`, and `src/error.rs`. Expose the public API through the PyO3 module so Python can call Rust for masking and travel-time calculations.
4. Build the Python model layer after the Rust API is available. Implement `ktv_psa_scheduler/model.py` as the TSN model builder that turns preprocessed and Rust-pruned data into decision variables, flow constraints, progress reward constraints, loop-capacity constraints, and headway clique constraints.
5. Add HiGHS solving behavior inside the Python model layer once the formulation exists. Configure `mip` with the HiGHS backend, apply time limit and gap settings, and prepare warm-start support for rolling windows. Keep the solver interface isolated so the orchestration layer can swap or tune it later without rewriting the model.
6. Implement rolling-horizon orchestration in `ktv_psa_scheduler/orchestrator.py`. This module should coordinate one 6-hour planning window, freeze the first 2 hours of decisions, hand forward active train state, and loop until the daily horizon is exhausted.
7. Finish with visualization and entry points. Use `ktv_psa_scheduler/visualizer.py` for string-chart rendering, update `main.py` for CLI execution, update `app.py` if a dashboard entry point is needed, and correct `ktv_psa_scheduler/__init__.py` so the package exports the real scheduler API instead of placeholder symbols.
8. Add tests progressively with each layer, then run integration validation on a small corridor slice before trying the full dataset. Start with Rust unit-style tests, then Python preprocessing/model tests, then an end-to-end rolling-window test using the sample corridor data.

**Rust Part**
1. Replace the current stub in `src/lib.rs` with a real PyO3 module that exports the native scheduler API.
2. Implement `src/masking.rs` as the passenger conflict and block-availability engine.
3. Implement `src/physics.rs` as the traversal-time and speed-constraint calculator.
4. Implement `src/error.rs` for clean Rust-to-Python error propagation.
5. Define Rust-side data structures for blocks, passenger intervals, train attributes, and masked edge outputs so Python receives compact, solver-ready results.
6. Keep Rust focused on pure computation and pruning: no file I/O, no solver logic, no visualization.
7. Validate Rust independently by building the extension and checking that the Python import exposes the intended native functions and classes.

**Python Part**
1. Implement `ktv_psa_scheduler/pipeline.py` as the data ingestion and preprocessing boundary. This module should load freight, passenger, infrastructure, and route data; validate required fields; align times to rolling-window minutes; and shape the records for the Rust API.
2. Implement the graph and model assembly logic in `ktv_psa_scheduler/model.py`. This module should create binary variables only for valid edges, build the flow conservation constraints, encode station progress rewards, and add station loop capacity constraints and headway clique constraints.
3. Keep a clean data handoff structure between preprocessing and modeling. The pipeline should output one compact structure for freight candidates, one for passenger occupancy, and one for infrastructure capacities so the model does not re-parse raw CSVs.
4. Implement `ktv_psa_scheduler/orchestrator.py` as the rolling-window controller. It should call the pipeline, call Rust pruning, build the model, solve it, freeze the execution slice, and hand state into the next window.
5. Implement `ktv_psa_scheduler/visualizer.py` only after the core scheduling loop works. Use it to transform solved paths into corridor time-distance views.
6. Update `ktv_psa_scheduler/__init__.py` to export the scheduler-facing API objects and helper functions that users should import directly.
7. Keep Python responsible for coordination and modeling, not low-level bitmasking or traversal physics.

**HiGHS Part**
1. Use HiGHS as the only solver backend for the first implementation path, through `mip` with the HiGHS driver or `highspy`-compatible configuration.
2. Configure solver behavior centrally in the Python model layer so the orchestration layer only passes a time limit, gap target, and optional warm start.
3. Add the core solver settings needed for the corridor problem: aggressive presolve, bounded solve time, target optimality gap, and extraction of selected edge variables.
4. Define the warm-start strategy for rolling horizons. The solved basis or incumbent from one window should seed the next window after the frozen execution slice is removed.
5. Keep HiGHS logic strictly at the optimization boundary: no data loading, no preprocessing, and no visualization.
6. Add solver-focused tests that confirm the model can initialize with HiGHS, solve a tiny synthetic instance, and return a stable solution status.

**File Structure**
- `src/lib.rs`
- `src/masking.rs`
- `src/physics.rs`
- `src/error.rs`
- `ktv_psa_scheduler/pipeline.py`
- `ktv_psa_scheduler/model.py`
- `ktv_psa_scheduler/orchestrator.py`
- `ktv_psa_scheduler/visualizer.py`
- `ktv_psa_scheduler/__init__.py`
- `main.py`
- `app.py`
- `tests/`

**Implementation Order**
1. Build the preprocessing boundary in Python so all later layers receive normalized data.
2. Implement the Rust pruning and physics engine.
3. Build the Python TSN formulation on top of the Rust outputs.
4. Wire HiGHS into the model layer and verify solver behavior on a tiny test instance.
5. Implement the rolling-horizon orchestrator and state handoff.
6. Add visualization and entry points.
7. Expand tests from unit checks to end-to-end corridor validation.

**Verification**
1. Build the Rust extension and confirm Python imports succeed.
2. Run preprocessing tests on a small sample to verify time alignment and schema normalization.
3. Solve a tiny synthetic TSN instance through HiGHS and confirm the expected status and outputs.
4. Run one rolling-window integration test over a narrow slice of the corridor.
5. Confirm the final package exports and entry points expose scheduler functionality instead of the current hello stub.

If you want, the next step can be a more execution-ready version of this plan with estimated milestones and test criteria per phase.
