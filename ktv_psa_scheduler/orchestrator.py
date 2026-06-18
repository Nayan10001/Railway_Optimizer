"""
Rolling-Horizon Orchestrator

This module coordinates the full scheduling loop:
  1. Load data for a planning window via the pipeline
  2. Inject carried-forward trains from the previous frozen slice
  3. Build the TSN graph and solve the MILP
  4. Freeze near-term decisions and extract committed paths
  5. Determine train positions at the freeze boundary (state handoff)
  6. Advance the window and repeat until the total horizon is exhausted

Usage:
    from ktv_psa_scheduler.orchestrator import run_rolling_horizon, OrchestratorConfig
    result = run_rolling_horizon(Path("data"), OrchestratorConfig())
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from ktv_psa_scheduler.pipeline import (
    FreightLoad,
    PipelineOutput,
    run_pipeline,
)
from ktv_psa_scheduler.model import (
    DN_STATIONS,
    UP_STATIONS,
    ScheduledPath,
    SolveResult,
    TSNEdge,
    build_tsn_graph,
    get_train_direction,
    solve_model,
)


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class OrchestratorConfig:
    """Tuning knobs for the rolling-horizon loop."""

    plan_horizon_minutes: int = 360     # 6-hour solve window
    freeze_horizon_minutes: int = 120   # freeze first 2 hours of each solution
    total_horizon_minutes: int = 1440   # 24 hours total planning day
    solve_time_limit: float = 120.0     # per-window solver time limit (seconds)
    mip_gap: float = 0.01              # 1% optimality gap is good enough


@dataclass
class TrainState:
    """Snapshot of a train's position at the freeze boundary."""

    load_id: str
    station: str              # station where the train sits at the freeze boundary
    arrival_minute: int       # minute it arrived at that station (relative to epoch)
    remaining_km: float       # remaining journey distance from this station
    speed: float              # average speed (km/h)
    direction: str            # "DN" or "UP"
    original_load: FreightLoad  # reference to source freight load


@dataclass
class FrozenSlice:
    """Committed schedule decisions from one planning window."""

    window_index: int
    window_start: int
    freeze_end: int
    paths: List[ScheduledPath]
    solve_status: str
    solve_time: float
    num_variables: int
    num_constraints: int


@dataclass
class OrchestratorResult:
    """Aggregate output from the full rolling-horizon run."""

    frozen_slices: List[FrozenSlice]
    total_solve_time: float
    windows_solved: int
    total_trains_scheduled: int
    total_edges_committed: int


# ============================================================================
# Helper Functions
# ============================================================================


def format_minute_as_datetime(minute: int, epoch_t0: datetime) -> str:
    """Convert a relative minute offset back to DD/MM/YYYY HH:MM string."""
    dt = epoch_t0 + timedelta(minutes=minute)
    return dt.strftime("%d/%m/%Y %H:%M")


def freeze_paths(
    paths: List[ScheduledPath],
    window_start: int,
    freeze_end: int,
) -> List[ScheduledPath]:
    """
    Extract the frozen portion of each path.

    An edge is frozen if its entry_minute < freeze_end (i.e., the train
    committed to entering the block before the freeze boundary).
    """
    frozen = []
    for path in paths:
        frozen_edges = [
            e for e in path.edges
            if e.entry_minute >= window_start and e.entry_minute < freeze_end
        ]
        if frozen_edges:
            total_prog = sum(e.progress_reward for e in frozen_edges)
            frozen.append(ScheduledPath(
                freight_id=path.freight_id,
                edges=frozen_edges,
                total_progress=total_prog,
            ))
    return frozen


def extract_train_states(
    paths: List[ScheduledPath],
    freeze_end: int,
    output: PipelineOutput,
) -> List[TrainState]:
    """
    Determine each train's position at the freeze boundary.

    For trains that have frozen edges but haven't reached their destination,
    create a TrainState at the last station they reached before freeze_end.
    """
    # Build a lookup from load_id to FreightLoad
    load_lookup: Dict[str, FreightLoad] = {
        load.load_id: load for load in output.freight_loads
    }

    carried = []
    for path in paths:
        if not path.edges:
            continue

        load = load_lookup.get(path.freight_id)
        if load is None:
            continue

        # Find frozen edges: edges that started before freeze_end
        frozen_edges = [e for e in path.edges if e.entry_minute < freeze_end]
        if not frozen_edges:
            # Train didn't start in this window — it will be re-loaded from
            # pipeline in the next window if it's still active
            continue

        # Last frozen edge determines handoff position
        last_edge = frozen_edges[-1]
        handoff_station = last_edge.to_station
        handoff_minute = last_edge.exit_minute

        # Check if the train has reached its destination
        direction = get_train_direction(load)
        sttn_list = DN_STATIONS if direction == "DN" else UP_STATIONS

        # If the handoff station is the destination (or last station in corridor
        # for that direction), the train is done — don't carry forward
        if handoff_station == load.destination:
            continue
        if handoff_station == sttn_list[-1]:
            continue

        # Calculate remaining distance
        try:
            handoff_idx = sttn_list.index(handoff_station)
        except ValueError:
            continue

        remaining_km = 0.0
        for idx in range(handoff_idx, len(sttn_list) - 1):
            u = sttn_list[idx]
            v = sttn_list[idx + 1]
            block_id = f"{u}-{v}"
            if block_id in output.blocks:
                remaining_km += output.blocks[block_id].length_km
            else:
                break
            # Stop counting at destination
            if v == load.destination:
                break

        if remaining_km <= 0:
            continue

        carried.append(TrainState(
            load_id=load.load_id,
            station=handoff_station,
            arrival_minute=handoff_minute,
            remaining_km=remaining_km,
            speed=load.speed if load.speed > 0 else 60.0,
            direction=direction,
            original_load=load,
        ))

    return carried


def inject_carried_trains(
    output: PipelineOutput,
    carried_trains: List[TrainState],
    epoch_t0: datetime,
) -> PipelineOutput:
    """
    Add carried-forward trains into the PipelineOutput's freight_loads.

    Each TrainState becomes a synthetic FreightLoad positioned at the
    station where the train was handed off, with updated arrival time
    and remaining distance.
    """
    # Collect existing load_ids to avoid duplicates
    existing_ids = {load.load_id for load in output.freight_loads}

    injected_loads = []
    for ts in carried_trains:
        if ts.load_id in existing_ids:
            # The pipeline already picked up this train from the CSV data
            # for this window — skip the synthetic injection
            continue

        synthetic = FreightLoad(
            load_id=ts.load_id,
            rake_id=ts.original_load.rake_id,
            source=ts.original_load.source,
            destination=ts.original_load.destination,
            load_type=ts.original_load.load_type,
            total_km=ts.remaining_km,
            block_section=f"{ts.station}-",  # partial; model uses station field
            block_hrs=0.0,
            speed=ts.speed,
            commodity=ts.original_load.commodity,
            description=ts.original_load.description,
            station=ts.station,
            arrival_time=format_minute_as_datetime(ts.arrival_minute, epoch_t0),
            depart_time=None,
        )
        injected_loads.append(synthetic)
        existing_ids.add(ts.load_id)

    # Return a new PipelineOutput with the extra trains appended
    return PipelineOutput(
        freight_loads=output.freight_loads + injected_loads,
        passenger_trains=output.passenger_trains,
        passenger_schedule_entries=output.passenger_schedule_entries,
        stations=output.stations,
        blocks=output.blocks,
        epoch_t0=output.epoch_t0,
        window_start_minutes=output.window_start_minutes,
        window_end_minutes=output.window_end_minutes,
        horizon_size_minutes=output.horizon_size_minutes,
    )


# ============================================================================
# Main Orchestration Loop
# ============================================================================


def run_rolling_horizon(
    data_dir: Path,
    config: Optional[OrchestratorConfig] = None,
    epoch_t0: Optional[datetime] = None,
    verbose: bool = True,
) -> OrchestratorResult:
    """
    Execute the full rolling-horizon scheduling loop.

    Args:
        data_dir: Root path to the ``data/`` directory.
        config: Orchestrator tuning parameters. Uses defaults if None.
        epoch_t0: System epoch. Derived from data if None.
        verbose: Print progress messages.

    Returns:
        OrchestratorResult with all frozen slices and aggregate metrics.
    """
    if config is None:
        config = OrchestratorConfig()

    frozen_slices: List[FrozenSlice] = []
    carried_trains: List[TrainState] = []
    window_start = 0
    window_index = 0
    total_solve_time = 0.0
    total_trains = 0
    total_edges = 0

    while window_start < config.total_horizon_minutes:
        t_wall = time.time()
        window_end = min(
            window_start + config.plan_horizon_minutes,
            config.total_horizon_minutes,
        )
        freeze_end = min(
            window_start + config.freeze_horizon_minutes,
            config.total_horizon_minutes,
        )

        if verbose:
            print(f"\n{'='*60}")
            print(f"Window {window_index}: "
                  f"plan [{window_start}, {window_end}) min, "
                  f"freeze [{window_start}, {freeze_end}) min")
            print(f"  Carried-forward trains: {len(carried_trains)}")

        # 1. Load data for this window
        try:
            output = run_pipeline(
                data_dir=data_dir,
                window_start_minutes=window_start,
                horizon_size_minutes=config.plan_horizon_minutes,
                epoch_t0=epoch_t0,
            )
        except Exception as e:
            if verbose:
                print(f"  Pipeline error: {e}")
            # Record an empty frozen slice so the loop can continue
            frozen_slices.append(FrozenSlice(
                window_index=window_index,
                window_start=window_start,
                freeze_end=freeze_end,
                paths=[],
                solve_status="ERROR",
                solve_time=0.0,
                num_variables=0,
                num_constraints=0,
            ))
            window_start += config.freeze_horizon_minutes
            window_index += 1
            continue

        # Capture epoch from first successful pipeline run
        if epoch_t0 is None:
            epoch_t0 = output.epoch_t0

        # 2. Inject carried-forward trains
        if carried_trains:
            output = inject_carried_trains(output, carried_trains, epoch_t0)

        if verbose:
            print(f"  Freight trains in window: {len(output.freight_loads)}")

        # 3. Build TSN graph
        edges = build_tsn_graph(output, data_dir)

        if verbose:
            print(f"  Feasible TSN edges: {len(edges)}")

        if not edges:
            if verbose:
                print("  No feasible edges — skipping solve.")
            frozen_slices.append(FrozenSlice(
                window_index=window_index,
                window_start=window_start,
                freeze_end=freeze_end,
                paths=[],
                solve_status="NO_EDGES",
                solve_time=0.0,
                num_variables=0,
                num_constraints=0,
            ))
            carried_trains = []
            window_start += config.freeze_horizon_minutes
            window_index += 1
            continue

        # 4. Solve
        result = solve_model(
            output, edges,
            time_limit_seconds=config.solve_time_limit,
            mip_gap=config.mip_gap,
            verbose=verbose,
        )
        total_solve_time += result.solve_time_seconds

        if verbose:
            print(f"  Solve: {result.status} in {result.solve_time_seconds:.2f}s "
                  f"(vars={result.num_variables}, constrs={result.num_constraints})")

        # 5. Freeze paths
        frozen_paths = freeze_paths(result.paths, window_start, freeze_end)
        frozen_edge_count = sum(len(p.edges) for p in frozen_paths)
        frozen_train_count = len([p for p in frozen_paths if p.edges])

        if verbose:
            print(f"  Frozen: {frozen_train_count} trains, {frozen_edge_count} edges")

        total_trains += frozen_train_count
        total_edges += frozen_edge_count

        frozen_slices.append(FrozenSlice(
            window_index=window_index,
            window_start=window_start,
            freeze_end=freeze_end,
            paths=frozen_paths,
            solve_status=result.status,
            solve_time=result.solve_time_seconds,
            num_variables=result.num_variables,
            num_constraints=result.num_constraints,
        ))

        # 6. State handoff
        carried_trains = extract_train_states(result.paths, freeze_end, output)

        if verbose:
            print(f"  State handoff: {len(carried_trains)} trains carried forward")
            print(f"  Wall time: {time.time() - t_wall:.2f}s")

        # 7. Advance
        window_start += config.freeze_horizon_minutes
        window_index += 1

    if verbose:
        print(f"\n{'='*60}")
        print(f"ORCHESTRATOR COMPLETE")
        print(f"  Windows solved: {window_index}")
        print(f"  Total solve time: {total_solve_time:.2f}s")
        print(f"  Total trains scheduled: {total_trains}")
        print(f"  Total edges committed: {total_edges}")
        print(f"{'='*60}\n")

    return OrchestratorResult(
        frozen_slices=frozen_slices,
        total_solve_time=total_solve_time,
        windows_solved=window_index,
        total_trains_scheduled=total_trains,
        total_edges_committed=total_edges,
    )
