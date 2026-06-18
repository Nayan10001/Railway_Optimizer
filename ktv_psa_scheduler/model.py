"""
TSN MIP Model Formulation and Solver Interface

This module constructs a Time-Space Network (TSN) graph for freight trains on the
KTV-PSA corridor, formulates the scheduling problem as a Mixed-Integer Linear Program (MILP),
solves it using the HiGHS solver backend, and extracts the optimal paths.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import polars as pl
import mip

from ktv_psa_scheduler.pipeline import PipelineOutput, FreightLoad, PassengerScheduleEntry, Station, BlockSection
from ktv_psa_scheduler import (
    ConflictMask,
    new_conflict_mask,
    compute_travel_time,
    batch_filter_edges,
)


@dataclass
class TSNEdge:
    edge_id: int              # unique edge index
    freight_id: str           # which freight train
    block_id: str             # block section string key
    from_station: str         # origin station code
    to_station: str           # destination station code  
    entry_minute: int         # when freight enters the block (relative to T0)
    exit_minute: int          # when freight exits the block
    travel_minutes: float     # physics-computed traversal time
    progress_reward: float    # block_length / total_freight_distance


@dataclass
class ScheduledPath:
    freight_id: str
    edges: List[TSNEdge]      # ordered by entry_minute
    total_progress: float     # sum of progress_reward for selected edges


@dataclass
class SolveResult:
    status: str               # "OPTIMAL", "FEASIBLE", "INFEASIBLE", "TIME_LIMIT", "ERROR"
    objective_value: float
    paths: List[ScheduledPath]
    solve_time_seconds: float
    num_variables: int
    num_constraints: int


# Time discretisation granularity (minutes)
# Coarser steps = fewer variables = faster solve; finer = more precise schedules.
TIME_STEP_MINUTES = 5

# Station sequence for DN and UP directions
DN_STATIONS = ['KTV', 'KPL', 'ALM', 'KUK', 'VZM', 'NML', 'GVI', 'CPP', 'BTVA', 'SGDM', 'PDU', 'DUSI', 'CHE', 'ULM', 'TIU', 'HCM', 'KBM', 'DGB', 'NWP', 'RMZ', 'PUN', 'PSA']
UP_STATIONS = list(reversed(DN_STATIONS))


def get_train_direction(load: FreightLoad) -> str:
    """Derive train running direction (DN or UP) based on routing metadata."""
    bs = load.block_section
    if bs and "-" in bs:
        parts = bs.split("-")
        if len(parts) == 2:
            u, v = parts[0].strip(), parts[1].strip()
            if u in DN_STATIONS and v in DN_STATIONS:
                if DN_STATIONS.index(u) < DN_STATIONS.index(v):
                    return "DN"
                else:
                    return "UP"
                    
    src, dest = load.source, load.destination
    if src in DN_STATIONS and dest in DN_STATIONS:
        if DN_STATIONS.index(src) < DN_STATIONS.index(dest):
            return "DN"
        else:
            return "UP"
            
    curr = load.station
    if curr in DN_STATIONS:
        if dest == "PSA" or dest in DN_STATIONS[DN_STATIONS.index(curr)+1:]:
            return "DN"
        if dest == "KTV" or dest in DN_STATIONS[:DN_STATIONS.index(curr)]:
            return "UP"
            
    return "DN"


def load_physics_profiles(data_dir: Path) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, float]]:
    """
    Load physical profiles (gradients, curves, PSRs) from passenger data folder.
    """
    gradients = {}
    curves = {}
    psrs = {}
    
    grad_files = [f for f in data_dir.glob("passenger/*.csv") if "GRADIENT" in f.name.split(" - ")[-1].upper()]
    curve_files = [f for f in data_dir.glob("passenger/*.csv") if "CURVATURE" in f.name.split(" - ")[-1].upper()]
    psr_files = [f for f in data_dir.glob("passenger/*.csv") if "PSR" in f.name.split(" - ")[-1].upper()]
    
    # Load Gradients
    for gf in grad_files:
        try:
            df = pl.read_csv(gf, infer_schema_length=0)
            for row in df.iter_rows(named=True):
                block = row.get("MAVBLCKSCTN")
                if not block or block == "MAVBLCKSCTN":
                    continue
                block = block.strip()
                
                try:
                    dist = float(row.get("MANDISTANCE") or 0)
                except:
                    dist = 0.0
                gtype = str(row.get("MAVGRADETYPE") or "").strip().upper()
                try:
                    val = float(row.get("MANGRADEVALUE") or 0)
                except:
                    val = 0.0
                    
                if gtype not in ["RISE", "FALL"]:
                    gtype = "RISE"
                    val = 0.0
                    
                if block not in gradients:
                    gradients[block] = []
                gradients[block].append({"dist_m": dist, "grade": gtype, "val": val})
        except Exception as e:
            print(f"Warning: Failed to load gradient file {gf}: {e}")
            
    # Load Curves
    for cf in curve_files:
        try:
            df = pl.read_csv(cf, infer_schema_length=0)
            for row in df.iter_rows(named=True):
                block = row.get("MAVBLCKSCTN")
                if not block or block == "MAVBLCKSCTN":
                    continue
                block = block.strip()
                
                try:
                    dist = float(row.get("MANDISTANCE") or 0)
                except:
                    dist = 0.0
                try:
                    rad = float(row.get("MANCURVERADIUS") or 9999.0)
                except:
                    rad = 9999.0
                    
                if block not in curves:
                    curves[block] = []
                curves[block].append({"dist_m": dist, "radius_m": rad})
        except Exception as e:
            print(f"Warning: Failed to load curvature file {cf}: {e}")
            
    # Load PSRs
    for pf in psr_files:
        try:
            df = pl.read_csv(pf, infer_schema_length=0)
            for row in df.iter_rows(named=True):
                block = row.get("MAVBLCKSCTN")
                if not block or block == "MAVBLCKSCTN":
                    continue
                block = block.strip()
                
                speed = -1.0
                for col in ["MANGDSSPEED", "MANPASSPEED"]:
                    val = row.get(col)
                    if val is not None:
                        try:
                            s = float(val)
                            if s > 0:
                                speed = s if speed < 0 else min(speed, s)
                        except:
                            pass
                if speed > 0:
                    if block not in psrs:
                        psrs[block] = speed
                    else:
                        psrs[block] = min(psrs[block], speed)
        except Exception as e:
            print(f"Warning: Failed to load PSR file {pf}: {e}")
            
    # Convert lists to JSON strings
    gradients_json = {}
    for k, v in gradients.items():
        gradients_json[k] = json.dumps(v)
    curves_json = {}
    for k, v in curves.items():
        curves_json[k] = json.dumps(v)
        
    return gradients_json, curves_json, psrs


def build_tsn_graph(output: PipelineOutput, data_dir: Path) -> List[TSNEdge]:
    """
    Generate feasible Time-Space Network edges for all active freight trains.
    """
    gradients_json, curves_json, psrs = load_physics_profiles(data_dir)
    
    # 1. Build Passenger Conflict Mask
    # Use safety headway of 5 minutes for passenger masking
    mask = new_conflict_mask(5)
    for entry in output.passenger_schedule_entries:
        mask.insert_interval(entry.block_id, entry.arrival_minutes, entry.departure_minutes, 1)
    mask.sort_all()
    
    # 2. Compile candidate edges
    candidates = []
    candidate_metadata = [] # stores (freight_id, block_id, from_sttn, to_sttn, entry_min, exit_min, travel_min, progress_reward)
    
    for load in output.freight_loads:
        # Determine direction
        direction = get_train_direction(load)
        sttn_list = DN_STATIONS if direction == "DN" else UP_STATIONS
        
        # Get start station index
        try:
            start_idx = sttn_list.index(load.station)
        except ValueError:
            # Station not in corridor sequence, skip this train
            continue
            
        # Parse arrival time at start station
        try:
            arr_dt = datetime.strptime(load.arrival_time.strip(), "%d/%m/%Y %H:%M")
            t_start = int((arr_dt - output.epoch_t0).total_seconds() / 60)
        except Exception:
            t_start = output.window_start_minutes
            
        t_min_arr = t_start
        
        # Expand sequentially through block sections
        for idx in range(start_idx, len(sttn_list) - 1):
            from_sttn = sttn_list[idx]
            to_sttn = sttn_list[idx + 1]
            block_id = f"{from_sttn}-{to_sttn}"
            
            if block_id not in output.blocks:
                break
                
            block = output.blocks[block_id]
            
            # Physics-based travel time
            psr = psrs.get(block_id, -1.0)
            grad_str = gradients_json.get(block_id, "[]")
            curve_str = curves_json.get(block_id, "[]")
            
            # Speed is km/h. Distance is km.
            speed = load.speed if load.speed > 0 else 60.0
            travel_min = compute_travel_time(block.length_km, speed, psr, grad_str, curve_str)
            
            # Early stop if start time is past horizon
            if t_min_arr >= output.window_end_minutes:
                break
                
            # Possible entry times into this block (coarsened by TIME_STEP)
            t_entry_start = max(output.window_start_minutes, t_min_arr)
            # Snap to the next TIME_STEP boundary
            t_entry_start = ((t_entry_start + TIME_STEP_MINUTES - 1) // TIME_STEP_MINUTES) * TIME_STEP_MINUTES
            
            for t_entry in range(t_entry_start, output.window_end_minutes, TIME_STEP_MINUTES):
                t_exit = int(t_entry + travel_min)
                candidates.append((block_id, t_entry, t_exit))
                
                # Compute progress reward
                reward = block.length_km / load.total_km if load.total_km > 0 else block.length_km
                candidate_metadata.append((
                    load.load_id, block_id, from_sttn, to_sttn, t_entry, t_exit, travel_min, reward
                ))
                
            # Optimistically update earliest arrival at next station
            t_min_arr = int(t_entry_start + travel_min)
            
    if not candidates:
        return []
        
    # 3. Filter candidates in batch via Rust engine
    # Extract lengths and speeds
    block_lengths = {bid: b.length_km for bid, b in output.blocks.items()}
    # Speed is derived per train, but batch_filter_edges takes block-level speeds for default physics check.
    # We pass a dummy dict since travel times are already checked or overridden.
    block_speeds = {bid: 60.0 for bid in output.blocks.keys()}
    
    masked_edges = batch_filter_edges(candidates, mask, block_lengths, block_speeds)
    
    # 4. Filter feasible edges and create TSNEdge objects
    tsn_edges = []
    edge_counter = 0
    for idx, me in enumerate(masked_edges):
        if me.feasible:
            meta = candidate_metadata[idx]
            edge = TSNEdge(
                edge_id=edge_counter,
                freight_id=meta[0],
                block_id=meta[1],
                from_station=meta[2],
                to_station=meta[3],
                entry_minute=meta[4],
                exit_minute=meta[5],
                travel_minutes=meta[6],
                progress_reward=meta[7]
            )
            tsn_edges.append(edge)
            edge_counter += 1
            
    return tsn_edges


def solve_model(
    output: PipelineOutput,
    edges: List[TSNEdge],
    time_limit_seconds: float = 120.0,
    mip_gap: float = 0.01,
    verbose: bool = False,
) -> SolveResult:
    """
    Formulates and solves the TSN Mixed-Integer Program.
    """
    import time as _time
    start_time = datetime.now()
    t_phase = _time.perf_counter()
    
    # 1. Initialize Model
    model = mip.Model(solver_name="highs")
    model.verbose = 0
    
    # 2. Variables
    # x[e] = 1 if edge is selected
    x = [model.add_var(var_type=mip.BINARY, name=f"x_{e.edge_id}") for e in edges]
    
    if verbose:
        print(f"    [build] {len(edges)} edge vars in {_time.perf_counter() - t_phase:.2f}s")
        t_phase = _time.perf_counter()
    
    # dwell[f, S, t] = 1 if train f is dwelling at station S at minute t
    dwell = {}
    
    # Group edges by train
    train_edges: Dict[str, List[TSNEdge]] = {}
    for e in edges:
        train_edges.setdefault(e.freight_id, []).append(e)
        
    # Pre-compute each train's earliest arrival time and route to trim dwell vars
    train_t_init: Dict[str, int] = {}
    for load in output.freight_loads:
        try:
            arr_dt = datetime.strptime(load.arrival_time.strip(), "%d/%m/%Y %H:%M")
            t_init = int((arr_dt - output.epoch_t0).total_seconds() / 60)
        except Exception:
            t_init = output.window_start_minutes
        train_t_init[load.load_id] = max(output.window_start_minutes, t_init)
    
    # Build dwell variables — only from t_init onwards, coarsened by TIME_STEP
    for load in output.freight_loads:
        fid = load.load_id
        direction = get_train_direction(load)
        sttn_list = DN_STATIONS if direction == "DN" else UP_STATIONS
        try:
            start_idx = sttn_list.index(load.station)
        except ValueError:
            continue
            
        route_sttns = sttn_list[start_idx:]
        t_start = train_t_init.get(fid, output.window_start_minutes)
        # Snap t_start down to TIME_STEP boundary so dwell covers the arrival slot
        t_start = (t_start // TIME_STEP_MINUTES) * TIME_STEP_MINUTES
        for sttn in route_sttns:
            for t in range(t_start, output.window_end_minutes, TIME_STEP_MINUTES):
                dwell[(fid, sttn, t)] = model.add_var(var_type=mip.BINARY, name=f"dwell_{fid}_{sttn}_{t}")
    
    if verbose:
        print(f"    [build] {len(dwell)} dwell vars in {_time.perf_counter() - t_phase:.2f}s")
        t_phase = _time.perf_counter()
    
    # skip[fid] = 1 means this train is NOT scheduled (absorbs the source flow)
    # This prevents infeasibility when corridor capacity is exhausted.
    skip: Dict[str, mip.Var] = {}
    for load in output.freight_loads:
        skip[load.load_id] = model.add_var(var_type=mip.BINARY, name=f"skip_{load.load_id}")
                
    # 3. Constraints
    
    # Pre-index edges for fast flow conservation (snap times to TIME_STEP grid)
    inflow_edges = {}
    outflow_edges = {}
    for e in edges:
        # Snap exit/entry to TIME_STEP grid for constraint matching
        exit_slot = (e.exit_minute // TIME_STEP_MINUTES) * TIME_STEP_MINUTES
        entry_slot = (e.entry_minute // TIME_STEP_MINUTES) * TIME_STEP_MINUTES
        inflow_edges.setdefault(e.freight_id, {}).setdefault(e.to_station, {}).setdefault(exit_slot, []).append(e.edge_id)
        outflow_edges.setdefault(e.freight_id, {}).setdefault(e.from_station, {}).setdefault(entry_slot, []).append(e.edge_id)

    # Pre-index dwell variables for fast loop capacity
    dwell_by_sttn_t = {}
    for (fid, sttn, t), var in dwell.items():
        dwell_by_sttn_t.setdefault((sttn, t), []).append(var)

    # C1: Flow Conservation
    for load in output.freight_loads:
        fid = load.load_id
        direction = get_train_direction(load)
        sttn_list = DN_STATIONS if direction == "DN" else UP_STATIONS
        try:
            start_idx = sttn_list.index(load.station)
            s_init = sttn_list[start_idx]
        except ValueError:
            continue
            
        route_sttns = sttn_list[start_idx:]
        
        # Parse initial arrival time at s_init
        try:
            arr_dt = datetime.strptime(load.arrival_time.strip(), "%d/%m/%Y %H:%M")
            t_init = int((arr_dt - output.epoch_t0).total_seconds() / 60)
        except Exception:
            t_init = output.window_start_minutes
            
        # Clip t_init to window start if it arrived earlier
        t_init = max(output.window_start_minutes, t_init)
        
        # We enforce flow conservation at each station S and minute t
        f_inflow = inflow_edges.get(fid, {})
        f_outflow = outflow_edges.get(fid, {})
        t_start = train_t_init.get(fid, output.window_start_minutes)
        
        for sttn in route_sttns:
            sttn_inflow = f_inflow.get(sttn, {})
            sttn_outflow = f_outflow.get(sttn, {})
            
            # Only iterate from t_start, coarsened by TIME_STEP
            t_start_snapped = (t_start // TIME_STEP_MINUTES) * TIME_STEP_MINUTES
            for t in range(t_start_snapped, output.window_end_minutes, TIME_STEP_MINUTES):
                # Traversal inflow to (sttn, t)
                inflow_traversal = [x[eid] for eid in sttn_inflow.get(t, [])]
                
                # Traversal outflow from (sttn, t)
                outflow_traversal = [x[eid] for eid in sttn_outflow.get(t, [])]
                
                # Inflow from preceding dwell
                inflow_dwell = []
                dwell_prev = dwell.get((fid, sttn, t - TIME_STEP_MINUTES))
                if dwell_prev is not None:
                    inflow_dwell = [dwell_prev]
                    
                # Outflow to next dwell
                dwell_cur = dwell.get((fid, sttn, t))
                outflow_dwell = [dwell_cur] if dwell_cur is not None else []
                
                # Source injection — two cases where flow enters the network:
                # Case A: train arrives INSIDE the window (t_init > window_start)
                # Case B: train arrived BEFORE the window (was already dwelling)
                # Both use (1 - skip[fid]) so skipped trains inject no flow
                t_init_slot = (t_init // TIME_STEP_MINUTES) * TIME_STEP_MINUTES
                is_source_a = (sttn == s_init and t == t_init_slot and t_init > output.window_start_minutes)
                is_source_b = (sttn == s_init and t_init <= output.window_start_minutes and t == t_start_snapped and dwell_prev is None)
                is_source_node = is_source_a or is_source_b
                
                if is_source_node:
                    source_inj = 1 - skip[fid]
                else:
                    source_inj = 0
                
                # Constraint: traversal inflow + dwell inflow + source == traversal outflow + dwell outflow
                # Only add constraint if there is any potential flow (avoid adding 0 == 0 constraints)
                if inflow_traversal or inflow_dwell or outflow_traversal or outflow_dwell or is_source_node:
                    model.add_constr(
                        sum(inflow_traversal) + sum(inflow_dwell) + source_inj == sum(outflow_traversal) + sum(outflow_dwell)
                    )
    
    if verbose:
        print(f"    [build] flow constraints in {_time.perf_counter() - t_phase:.2f}s")
        t_phase = _time.perf_counter()
                
    # C3: Loop Capacity
    # At each station S and each minute t, sum of dwelling trains <= loop_count
    for sttn_code, station in output.stations.items():
        loop_cnt = station.loop_count
        for t in range(output.window_start_minutes, output.window_end_minutes, TIME_STEP_MINUTES):
            dwell_vars = dwell_by_sttn_t.get((sttn_code, t), [])
            if dwell_vars:
                model.add_constr(sum(dwell_vars) <= loop_cnt)
                
    # C4: Headway Clique
    # Group edges by block_id
    block_edges: Dict[str, List[TSNEdge]] = {}
    for e in edges:
        block_edges.setdefault(e.block_id, []).append(e)
        
    for block_id, b_edges in block_edges.items():
        block = output.blocks.get(block_id)
        if not block:
            continue
        h_min = block.headway_minutes
        
        b_edges_by_t = {}
        for e in b_edges:
            b_edges_by_t.setdefault(e.entry_minute, []).append(x[e.edge_id])
            
        for t in range(output.window_start_minutes, output.window_end_minutes, TIME_STEP_MINUTES):
            clique = []
            for dt in range(0, h_min + TIME_STEP_MINUTES, TIME_STEP_MINUTES):
                clique.extend(b_edges_by_t.get(t + dt, []))
            if len(clique) > 1:
                model.add_constr(sum(clique) <= 1)

                
    if verbose:
        print(f"    [build] capacity+headway constraints in {_time.perf_counter() - t_phase:.2f}s")
        t_phase = _time.perf_counter()
    
    # 4. Objective: maximise progress reward MINUS heavy penalty for skipping trains
    # The skip penalty must be large enough that the solver always prefers scheduling
    # a train over skipping it. We use 10× the max possible single-train reward.
    skip_penalty = 10.0
    model.objective = mip.maximize(
        sum(x[e.edge_id] * e.progress_reward for e in edges)
        - skip_penalty * sum(skip[fid] for fid in skip)
    )
    
    if verbose:
        total_build = (datetime.now() - start_time).total_seconds()
        print(f"    [build] TOTAL model build: {total_build:.2f}s  "
              f"({model.num_cols} vars, {model.num_rows} constrs)")
    
    # 5. Optimize
    model.max_gap = mip_gap
    status = model.optimize(max_seconds=time_limit_seconds)
    
    solve_time = (datetime.now() - start_time).total_seconds()
    
    # Convert status to string
    status_str = "INFEASIBLE"
    if status == mip.OptimizationStatus.OPTIMAL:
        status_str = "OPTIMAL"
    elif status == mip.OptimizationStatus.FEASIBLE:
        status_str = "FEASIBLE"
    elif status == mip.OptimizationStatus.NO_SOLUTION_FOUND:
        status_str = "INFEASIBLE"
    elif status == mip.OptimizationStatus.INT_INFEASIBLE:
        status_str = "INFEASIBLE"
    
    # 6. Extract Paths
    paths: List[ScheduledPath] = []
    skipped_count = 0
    for load in output.freight_loads:
        fid = load.load_id
        
        # Check if this train was skipped
        skip_var = skip.get(fid)
        if skip_var is not None and skip_var.x is not None and skip_var.x >= 0.5:
            skipped_count += 1
            continue
        
        selected_edges = []
        for e in train_edges.get(fid, []):
            if x[e.edge_id].x is not None and x[e.edge_id].x >= 0.5:
                selected_edges.append(e)
                
        # Sort chronologically by entry_minute
        selected_edges.sort(key=lambda e: e.entry_minute)
        
        total_prog = sum(e.progress_reward for e in selected_edges)
        paths.append(ScheduledPath(freight_id=fid, edges=selected_edges, total_progress=total_prog))
    
    if verbose:
        scheduled = len([p for p in paths if p.edges])
        print(f"    [result] {scheduled} trains scheduled, {skipped_count} skipped")
        
    return SolveResult(
        status=status_str,
        objective_value=model.objective_value if model.objective_value is not None else 0.0,
        paths=paths,
        solve_time_seconds=solve_time,
        num_variables=model.num_cols,
        num_constraints=model.num_rows
    )
