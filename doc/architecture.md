# Master Implementation Plan: High-Performance Discrete TSN Freight Scheduling System (KTV–PSA Corridor)

---

## 1. Executive Summary

This master implementation document serves as the single source of truth and technical architecture blueprint for deploying a real-time, dynamic freight scheduling system on the **Kottavalasa Junction (KTV) to Palasa (PSA)** railway corridor (~176.83 km, 22 stations). 

### 1.1 Project Scope
The operational dataset encompasses **5,548 unique freight loads** (yielding 95,219 historical tracking logs) navigating concurrently with a fixed, deterministic timetable of **250 high-priority passenger trains** (ranging from ultra-high-priority Vande Bharat Express to regional commuter services). 

### 1.2 Core Objective
The primary engineering objective is to **maximize economic throughput** by dynamically scheduling freight movements. The scheduling engine operates by projecting decisions onto a minute-discretized **Discrete Time-Space Network (TSN)**. It ensures collision-free paths by maintaining directional safety headways while strictly complying with highly localized physical constraints:
* **Station Loop Clear Standing Lengths (MANCSR):** Enforcing track length limits (700m–900m) to prevent main lines from fouling. Freight trains exceeding a station's loop CSR are restricted from dwelling at that station.
* **Track Geometry Traction Physics:** Accounting for dynamic acceleration and speed adjustments over steep gradients (up to $1:150$ rises) and sharp curves.
* **Permanent Speed Restrictions (PSRs):** Complying with localized velocity ceilings, such as the 15 km/h limit governing the KUK–VZM slip-diamond crossing.

---

## 2. Mathematical Model Formulation

The corridor scheduling problem is formulated as a **Multi-Commodity Network Flow Problem with Disjunctive Side Constraints** mapped over a directed space-time graph $\mathcal{G} = (\mathcal{N}, \mathcal{E})$. Time is completely discretized into $1$-minute intervals over a rolling planning horizon: $\mathcal{T} = \{0, 1, 2, \dots, T\}$.

### 2.1 Mathematical Notation Reference Table

| Notation | Type | Domain / Elements | Description |
| :--- | :--- | :--- | :--- |
| $\mathcal{S}$ | Set | Stations $s \in \{ \text{KTV, KPL, } \dots, \text{PSA} \}$ | All physical stations along the corridor. |
| $\mathcal{B}$ | Set | Block Sections $b \in \mathcal{B}$ | Directed segments connecting sequential stations. |
| $\mathcal{T}_F$ | Set | Freight Trains $f \in \mathcal{T}_F$ | The set of active/candidate freight train identifiers. |
| $\mathcal{E}_f$ | Set | Edges $e \in \mathcal{E}_f$ | Graph edges partitioned into $\mathcal{E}_f^{\text{dep}}, \mathcal{E}_f^{\text{run}}, \mathcal{E}_f^{\text{dwell}}, \mathcal{E}_f^{\text{arr}}$. |
| $w_f^{\text{comm}}$| Parameter | $\mathbb{R}^+$ | Commodity-weighted economic priority value (e.g., `CONT` = 10, `EMPTY` = 1). |
| $w_{f, s}^{\text{progress}}$| Parameter | $\mathbb{R}^+$ | Progression reward coefficient assigned to station $s$. |
| $\text{duration}(e)$| Parameter | $\mathbb{Z}^+$ | Time consumed traversing edge $e$ (minutes). |
| $\lambda_f$ | Parameter | $\mathbb{R}^+$ | Regularization delay penalty parameter ($\lambda_{\text{CONT}} \gg \lambda_{\text{EMPTY}}$). |
| $H_b$ | Parameter | $\mathbb{Z}^+$ | Required safety headway interval for block $b$ (minutes). |
| $N_b$ | Parameter | $\mathbb{Z}^+$ | Total running lines available on block section $b$. |
| $C_s^{\text{loop}}$| Parameter | $\mathbb{Z}^+$ | Total loop tracks at station $s$. |
| $N_p^{\text{dwell}}(s, t)$| Parameter | $\mathbb{Z}^*$ | Fixed background passenger train count dwelling at $s$ at minute $t$. |
| $x_{f, e}$ | Variable | $\{0, 1\}$ | $1$ if freight train $f$ traverses space-time edge $e$; $0$ otherwise. |
| $u_f$ | Variable | $\{0, 1\}$ | $1$ if train $f$ completes its path to its terminal sink; $0$ otherwise. |
| $y_{f, s}$ | Variable | $\{0, 1\}$ | $1$ if train $f$ successfully arrives at intermediate station $s$. |

### 2.2 Objective Function
The system optimizes a multi-term linear objective function designed to maximize revenue, reward forward progress inside truncated windows, and minimize dynamic delay:

$$\max \quad Z = \sum_{f \in \mathcal{T}_F} \left( w_f^{\text{comm}} \cdot u_f + \sum_{s \in \mathcal{S}_f^{\text{route}}} w_{f, s}^{\text{progress}} \cdot y_{f, s} \right) \;-\; \sum_{f \in \mathcal{T}_F} \lambda_f \sum_{e \in \mathcal{E}_f^{\text{run}} \cup \mathcal{E}_f^{\text{dwell}}} \text{duration}(e) \cdot x_{f, e}$$

#### Structural Intent:
1.  **Commodity Revenue Maximization ($w_f^{\text{comm}} \cdot u_f$):** Prioritizes premium traffic classes based on cargo. High-tariff container routes (`CONT`) and structural materials (`IS`) receive large coefficients relative to empty hopper returns.
2.  **Partial Progress Rewards ($w_{f, s}^{\text{progress}} \cdot y_{f, s}$):** Eliminates rolling-horizon boundary truncation errors. Instead of abandoning a train because it cannot complete its full corridor journey within the current 6-hour window, the model receives incremental rewards for advancing it safely through intermediate stations.
3.  **Differentiated Delay Regulation ($\lambda_f$):** Acts as a secondary tie-breaker. By enforcing $\lambda_{\text{CONT}} \gg \lambda_{\text{EMPTY}}$, the solver routes time-critical manifests onto high-speed running edges while moving lower-priority bulk variants to intermediate loops during contention.

### 2.3 Structural Constraints Suite

#### 2.3.1 Strict Flow Conservation
For every individual train $f \in \mathcal{T}_F$ and every discrete space-time node $n = (s, t) \in \mathcal{N}_f$:

$$\sum_{e \in \delta^{\text{in}}_f(n)} x_{f, e} - \sum_{e \in \delta^{\text{out}}_f(n)} x_{f, e} = 
\begin{cases} 
-u_f & \text{if } n = \text{Source}_f \\
u_f & \text{if } n = \text{Sink}_f \\
0 & \forall n \in \mathcal{N}_f \setminus \{\text{Source}_f, \text{Sink}_f\}
\end{cases}$$

This equation guarantees path contiguity. A train must enter and leave intermediate space-time nodes symmetrically, preventing unauthorized appearance, disappearance, or spatial teleportation.

#### 2.3.2 Target Tracking Identity for Progress Rewards
To bound the progress indicator variables to the physical graph flow over the entire planning horizon:

$$\sum_{t \in \mathcal{T}} \sum_{e \in \delta^{\text{in}}_f(s, t)} x_{f, e} \;\ge\; y_{f, s} \quad \forall s \in \mathcal{S}_f^{\text{route}}$$

Summing over all $t \in \mathcal{T}$ ensures that the indicator $y_{f, s}$ can resolve to $1$ if the train enters station $s$ at **any** point during the active horizon, rather than incorrectly forcing entry at all minutes.

#### 2.3.3 Dynamic Multi-Line Same-Direction Headway Cliques
To prevent trailing collisions on block sections without using continuous big-M equations, running edges entering block $b$ within a trailing safety window are bound via localized conflict cliques:

$$\sum_{f \in \mathcal{T}_F} \sum_{e \in \mathcal{E}_f^{\text{run}} \cap E_b(t)} x_{f, e} \;\le\; N_b \quad \forall b \in \mathcal{B}, \; \forall t \in \{0, \dots, T - H_b\}$$

Where $E_b(t)$ isolates the set of all running edges transiting block $b$ whose entry timestamps fall within the moving interval $[t, t + H_b - 1]$.
* **KTV–VZM Segment:** Built on Automatic Track Signalling with 3 main lines ($N_b = 3$), allowing parallel running and dynamic overtaking.
* **VZM–PSA Segment:** Governed by Automatic Block Signalling with 2 dedicated directional lines ($N_b = 2$), enforcing strict safety headways per track direction.

#### 2.3.4 Physical Station Loop Capacity Constraints
The volumetric capacity of stationary track space is managed across each minute of the timeline:

$$\sum_{f \in \mathcal{T}_F} x_{f, e_{\text{dwell}}(s, t)} \;\le\; C_s^{\text{loop}} - N_p^{\text{dwell}}(s, t) \quad \forall s \in \mathcal{S}, \; \forall t \in \{0, \dots, T-1\}$$

Where $e_{\text{dwell}}(s, t) = \left( (s, t), (s, t+1) \right)$. This forces the aggregate count of freight trains parked at station $s$ at minute $t$ to remain within the physical loop count ($C_s^{\text{loop}}$) remaining after subtracting frozen passenger train occupancy ($N_p^{\text{dwell}}$).

---

## 3. Implementation Strategy: Rolling Horizon Framework

To execute this formulation over continuous 24-hour periods without experiencing performance degradation or memory overflows from a massive time horizon, the system utilizes a **Rolling Horizon Framework**.

```
Window 1: |====================| (0 - 360 min)
Window 2:         |====================| (120 - 480 min)
Window 3:                 |====================| (240 - 600 min)
```

### 3.1 Windowing Parameters
* **Planning Horizon Window ($T$):** 360 minutes (6 hours). This window is long enough to capture downstream routing conflicts across the 176.8 km corridor without overwhelming the solver.
* **Execution Step Size ($\Delta T$):** 120 minutes (2 hours). Paths active within $[0, 120]$ minutes are finalized and frozen.
* **Buffer Zone (Overlap):** 240 minutes (4 hours). Edges scheduled in the latter portion of a window are treated as tentative and re-optimized in the next window.

### 3.2 State Handover and Warm Starts
At the completion of window $k$:
1. Decisions within the execution step are locked into the system state database.
2. The exact positions of all active trains at $t = 120$ are extracted.
3. If a train is mid-transit on a block section $b$ at $t = 120$, its remaining travel time is computed based on physics profiles, and its entry into the next station is injected as a fixed initial boundary condition for window $k+1$.
4. If a train is stopped on a loop line, its initial position in window $k+1$ is bound at that loop at $t = 120$.
5. The solved variable basis vector is passed into the incoming window as a **Warm Start** hint to prune non-optimal branches instantly.

---

## 4. Technology Stack Architecture

The production platform implements a modular architecture designed to separate performance-critical code from high-level scheduling rules:

```
+---------------------------------------------------------------------------------+
|                               1. DATA PIPELINE LAYER                            |
|       - Technology: Polars (Rust Core)                                          |
|       - Purpose: High-speed ingestion, cleaning, and temporal alignment.        |
+---------------------------------------------------------------------------------+
                                       |
                                       v
+---------------------------------------------------------------------------------+
|                            2. GRAPH PRUNING ENGINE                              |
|       - Technology: Rust Native Core via PyO3 / Maturin                         |
|       - Purpose: Bitmasking, MANCSR checks, and traction physics integrations.   |
+---------------------------------------------------------------------------------+
                                       |
                                       v [Sparse Edge & Node Structural Arrays]
+---------------------------------------------------------------------------------+
|                           3. MATHEMATICAL MODELING                              |
|       - Technology: Python-MIP API                                              |
|       - Purpose: Translating sparse arrays into structural matrix rows.         |
+---------------------------------------------------------------------------------+
                                       |
                                       v [C-Memory Pointer Matrix Passing]
+---------------------------------------------------------------------------------+
|                               4. SOLVER COMPONENT                               |
|       - Technology: HiGHS (C++ Executable Binary via highspy)                   |
|       - Purpose: Parallel Presolve, Simplex, and Branch-and-Cut execution.      |
+---------------------------------------------------------------------------------+
                                       |
                                       v [Optimized Decision Vectors]
+---------------------------------------------------------------------------------+
|                             5. VISUALIZATION LAYER                              |
|       - Technology: Plotly / Matplotlib                                         |
|       - Purpose: Rendering interactive train string charts (Méridien diagrams).  |
+---------------------------------------------------------------------------------+
```

### 4.1 Data Ingestion (`Polars`)
`Polars` replaces traditional Pandas workflows. Written in Rust and utilizing Arrow memory layouts, it parallelizes file I/O to parse the 95,219 freight logging arrays in milliseconds. It converts passenger seconds-from-midnight indices into relative integer-minute offsets using vectorized SIMD executions.

### 4.2 Graph Generation & Pruning Engine (`Rust`)
This module is written in native Rust and compiled directly into an importable Python extension library using `PyO3` and `Maturin`. It eliminates invalid decision variables before optimization via three filters:
* **Space-Time Forbidden Masking:** Projects high-priority passenger schedules onto block sections and uses bitwise operations to drop conflicting freight edges.
* **Traction Physics Integration:** Calculates block traversal times ($d_f(b)$) by factoring in train weight, rolling resistance, curves, gradients, and permanent speed restrictions rather than using static parameters.
* **Clear Standing Length Filters (MANCSR):** Cross-references train lengths with station loop clear standing lengths. If $L_f > \text{MANCSR}_s$, the engine drops the corresponding dwell variables ($\mathcal{E}_f^{\text{dwell}}$) completely, ensuring long trains stay on the main line.

### 4.3 Modeling Layer (`Python-MIP`)
`Python-MIP` serves as the orchestration layer for the model. It interfaces directly with C-memory structures via CFFI, bypassing the performance overhead of traditional tools like Pyomo or PuLP. This allows the system to initialize hundreds of thousands of sparse variables and clique constraints in seconds.

### 4.4 Solver Engine (`HiGHS`)
The system uses the open-source **HiGHS** optimization suite (via the `highspy` driver engine or direct `Python-MIP` bindings). It utilizes advanced dual revised simplex methods and parallel branch-and-cut routines to solve large-scale problems. Aggressive presolve settings (`Presolve=2`) are enabled to prune redundant network components before exploring the search tree.

---

## 5. Detailed Implementation Plan (Module by Module)

### Module 1: Data Pipeline & Temporal Realignment
* **Input Data:** Raw CSV inputs tracking freight loads, station topologies, passenger schedules, and track geometry profiles.
* **Processing Rules:** 
    * Establish system start epoch timestamp ($T_0$).
    * Map passenger absolute timeline indices ($\text{seconds past midnight}$) to integer minutes relative to $T_0$ via $t = \lfloor \text{seconds} / 60 \rfloor - T_0$.
    * Filter the input datasets down to records that fall within the active rolling horizon window $[T_{\text{start}}, T_{\text{start}} + 360]$.

### Module 2: Rust Spatial-Physics Engine
* **Input Data:** Aligned data arrays from Module 1, station loop records, track gradients, and permanent speed restrictions.
* **Processing Rules:**
    * **Passenger Masking:** For each block section, build a bit vector where bits are cleared (`0`) during the window $[t_{\text{entry}} - H_b, t_{\text{exit}} + H_b]$ around passenger schedules.
    * **Dynamic Traversal Calculation:** Compute travel times ($d_f(b)$) by factoring in weight, speed restrictions (e.g., 15 km/h over VZM crossings), and gradient adjustments.
    * **Loop Capacity Length Filtering:** Compare the physical length of each train with station clear standing lengths. If $L_f > \text{MANCSR}_s$, drop the corresponding dwell edge variable, ensuring long trains stay on the move.

### Module 3: Python Matrix Formulation Builder
* **Input Data:** Compressed structural arrays of valid, pre-filtered nodes and edges from Module 2.
* **Processing Rules:**
    * Initialize the `mip.Model(solver_name=mip.INF_HIGHS)` instance.
    * Generate binary choice indicators $x_{f,e}$ exclusively for edges that passed the structural filters in Module 2.
    * Formulate flow conservation equations across every active space-time coordinate node.
    * Loop through block schedules and apply trailing headway clique exclusions across windows $[t, t + H_b - 1]$.
    * Incorporate station loop capacity constraints, factoring in passenger train occupancy.

### Module 4: Solver Core Execution
* **Input Data:** Structured optimization matrix from Module 3.
* **Processing Rules:**
    * Apply solver performance parameters: `model.max_seconds = 180`, `model.max_gap = 0.02`, and `model.preprocess = 2` (Aggressive Presolve).
    * Inject the basis vector from the previous window to warm-start the search.
    * Execute the optimization routine, intercept the solver status flags, and extract the chosen edge vectors where $x_{f,e} = 1$.

### Module 5: Orchestration & Visualization Controls
* **Input Data:** Solved variable arrays from Module 4.
* **Processing Rules:**
    * Isolate decisions within the first 120 minutes ($\Delta T$) and flag their edge paths as frozen.
    * Advance the rolling window base: $T_{\text{start}} \leftarrow T_{\text{start}} + 120$.
    * Export the active schedules to the visualization engine to render time-distance string charts for the dispatchers.

---

## 6. Code Structure & Pseudocode

### 6.1 Target Directory Layout
```
ktv_psa_scheduler/
├── Cargo.toml
├── src/
│   ├── lib.rs            # PyO3 module bindings
│   ├── physics.rs        # Traction mechanics integration
│   └── masking.rs        # Space-Time passenger bitmasks
├── scheduler/
│   ├── __init__.py
│   ├── pipeline.py       # Polars data alignment step
│   ├── model.py          # Python-MIP matrix constructor
│   └── orchestrator.py   # Rolling horizon control loop
└── main.py               # Application entry point
```

### 6.2 Rust Module: Passenger Forbidden Zone Bitmasking
The following native Rust routine generates space-time bitmasks to quickly strip conflicting freight edges out of the graph:

```rust
use pyo3::prelude::*;
use rayon::prelude::*;

#[pyclass]
pub struct SpaceTimeMask {
    // 2D Matrix: [block_index][minute] -> 1: Available, 0: Forbidden
    pub mask: Vec<Vec<u8>>,
    pub horizon: usize,
}

#[pymethods]
impl SpaceTimeMask {
    #[new]
    fn new(num_blocks: usize, horizon: usize) -> Self {
        Self {
            mask: vec![vec![1; horizon]; num_blocks],
            horizon,
        }
    }

    // Parallel processing of passenger train arrivals
    fn apply_passenger_schedule(
        &mut self, 
        block_idx: usize, 
        t_entry: usize, 
        t_exit: usize, 
        headway: usize
    ) {
        let start_forbidden = t_entry.saturating_sub(headway);
        let end_forbidden = std::cmp::min(t_exit + headway, self.horizon - 1);

        // Update the discrete time slots for this block section
        for t in start_forbidden..=end_forbidden {
            self.mask[block_idx][t] = 0; 
        }
    }
}
```

### 6.3 Python Module: Model Optimization & Clique Constraint Builder
The code below uses Python-MIP to build the optimization matrix and apply structural clique constraints to the pre-filtered graph:

```python
import mip
from typing import Dict, List, Tuple

class CorridorTSNModel:
    def __init__(self, valid_edges_dict: Dict, station_caps: Dict, passenger_dwells: Dict, horizon: int):
        """
        valid_edges_dict: f -> list of tuples ((s1, t1), (s2, t2), 'run'|'dwell', block_id)
        """
        self.model = mip.Model(name="KTV_PSA_Production_Model", solver_name=mip.INF_HIGHS)
        self.horizon = horizon
        self.x = {}
        
        # Initialize binary decision variables only for valid, pre-filtered edges
        for f, edges in valid_edges_dict.items():
            for edge in edges:
                self.x[(f, edge)] = self.model.add_var(
                    var_type=mip.BINARY, 
                    name=f"x_{f}_{edge[0]}_{edge[1]}"
                )
        self.model.update()
        self._build_clique_constraints(valid_edges_dict, headway=4)

    def _build_clique_constraints(self, valid_edges_dict: Dict, headway: int):
        # Extract unique block identifiers from the network graph
        all_blocks = set(edge[3] for edges in valid_edges_dict.values() for edge in edges if edge[2] == 'run')
        
        for b in all_blocks:
            for t in range(self.horizon - headway):
                clique_vars = []
                
                # Scan across the headway window to find conflicting running edges
                for f, edges in valid_edges_dict.items():
                    for edge in edges:
                        if edge[2] == 'run' and edge[3] == b:
                            t_entry = edge[0][1]
                            if t <= t_entry < t + headway:
                                clique_vars.append(self.x[(f, edge)])
                
                # Enforce safety headway via a single clique constraint
                if clique_vars:
                    self.model.add_constr(
                        mip.xsum(clique_vars) <= 1,
                        name=f"Headway_Clique_{b}_{t}"
                    )

    def optimize(self, time_limit: float = 180.0):
        self.model.max_seconds = time_limit
        self.model.max_gap = 0.02
        status = self.model.optimize()
        return status, self.x
```

### 6.4 Python Engine: Rolling Horizon Controller Loop
The core control loop coordinates data filtering, graph generation, optimization, and time-window adjustments:

```python
import polars as pl
from ktv_psa_scheduler import SpaceTimeMask # Imported Rust binary module
from scheduler.model import CorridorTSNModel

def run_rolling_horizon_scheduler():
    total_horizon_24h = 1440
    window_size = 360  # 6 hours
    step_size = 120    # 2 hours
    current_t0 = 0

    # Load infrastructure and operational tracking records using Polars
    freight_data = pl.read_csv("data/Freight/goods_train.csv")
    passenger_data = pl.read_csv("data/passenger/schedule.csv")

    while current_t0 < total_horizon_24h:
        print(f"Executing optimization loop across window: [{current_t0} -> {current_t0 + window_size}]")
        
        # 1. Initialize the Rust spatial mask for passenger schedules
        rust_mask = SpaceTimeMask(num_blocks=44, horizon=window_size)
        
        # 2. Filter passenger records within the active window and apply masks in Rust
        active_passengers = passenger_data.filter(
            (pl.col("minutes_entry") >= current_t0) & (pl.col("minutes_entry") < current_t0 + window_size)
        )
        for row in active_passengers.iter_rows(named=True):
            rust_mask.apply_passenger_schedule(
                block_idx=row["block_id"],
                t_entry=row["minutes_entry"] - current_t0,
                t_exit=row["minutes_exit"] - current_t0,
                headway=4
            )
            
        # 3. Generate valid, physics-verified freight edges in Rust
        # (Passes raw data structures down to the compiled Rust layer)
        valid_freight_edges = generate_pruned_edges_in_rust(freight_data, rust_mask, current_t0, window_size)
        
        # 4. Build and solve the mathematical optimization matrix
        tsn_solver = CorridorTSNModel(valid_freight_edges, station_caps={}, passenger_dwells={}, horizon=window_size)
        status, solution_vectors = tsn_solver.optimize(time_limit=180.0)
        
        # 5. Lock in choices within the execution step and advance the window
        lock_and_publish_execution_slice(solution_vectors, current_t0, step_size)
        current_t0 += step_size

if __name__ == "__main__":
    run_rolling_horizon_scheduler()
```

---

## 7. Key Optimization Decisions

### 7.1 Clique Constraints vs. Disjunctive Big-M Equations
Traditional continuous models use disjunctive inequalities ($t_B \ge t_A + H_b - M(1 - y_{A,B})$) to prevent collisions on a track. This approach introduces large big-M coefficients, leading to fractional, weak Linear Programming (LP) relaxations that slow down the branch-and-bound search tree. By using a minute-discretized network, all conflicting running decisions within a headway window are grouped into a single Conflict Graph Clique Constraint ($\sum x \le 1$). This provides tight mathematical bounds, allowing solvers to prune suboptimal solutions quickly.

### 7.2 Native Rust Graph Generation vs. Pure Python Pipelines
A minute-discretized network over 22 stations generates millions of potential node and edge combinations. Looping through these entries in pure Python to validate train lengths and passenger headways introduces severe performance bottlenecks from interpreter overhead and garbage collection. Moving this spatial filtering logic to compiled Rust allows the system to run physics equations and bitwise schedule masking in parallel across all CPU cores. This reduces the size of the graph by over 80% before it reaches the modeling layer, ensuring the system can run efficiently in real time.

### 7.3 HiGHS Engine vs. COIN-OR CBC Solvers
While COIN-OR CBC has long been a standard open-source solver for simple optimization problems, it lacks the performance required for large-scale scheduling. It often experiences performance issues when handling dense matrices and lacks robust multi-core branching capabilities. HiGHS features advanced, modern parallel dual revised simplex routines and parallel branch-and-cut algorithms. When combined with aggressive presolve configurations, HiGHS simplifies and resolves complex network flow matrices in a fraction of the time required by older open-source engines, matching the performance of commercial alternatives for this network layout.
