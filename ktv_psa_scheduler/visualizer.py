"""
Plotly/Matplotlib train string chart rendering (Méridien diagrams)
This module visualizes the scheduled paths of freight trains along with passenger train
occupancies to verify conflict-free scheduling and analyze station dwell times.
"""

import plotly.graph_objects as go
from datetime import timedelta
from typing import Dict, List, TYPE_CHECKING

from ktv_psa_scheduler.pipeline import PipelineOutput
from ktv_psa_scheduler.model import SolveResult, DN_STATIONS

if TYPE_CHECKING:
    from ktv_psa_scheduler.orchestrator import OrchestratorResult


def build_station_distances(output: PipelineOutput) -> Dict[str, float]:
    """Compute cumulative distances for stations along the corridor."""
    station_distances = {DN_STATIONS[0]: 0.0}
    dist = 0.0
    for i in range(len(DN_STATIONS) - 1):
        u = DN_STATIONS[i]
        v = DN_STATIONS[i+1]
        block_id_dn = f"{u}-{v}"
        block_id_up = f"{v}-{u}"
        
        block_len = 0.0
        if block_id_dn in output.blocks:
            block_len = output.blocks[block_id_dn].length_km
        elif block_id_up in output.blocks:
            block_len = output.blocks[block_id_up].length_km
            
        dist += block_len
        station_distances[v] = dist
        
    return station_distances


def plot_train_string_chart(output: PipelineOutput, result: SolveResult, save_path: str = "train_string_chart.html"):
    """
    Generate an interactive Plotly Méridien diagram (train string chart).
    X-axis: Time (datetime)
    Y-axis: Station distance (km)
    """
    station_dist = build_station_distances(output)
    
    fig = go.Figure()
    
    # 1. Plot Passenger Trains (from pipeline output)
    # We need to group schedule entries by train
    passenger_paths: Dict[str, List] = {}
    for entry in output.passenger_schedule_entries:
        passenger_paths.setdefault(entry.train_id, []).append(entry)
        
    for train_id, entries in passenger_paths.items():
        # Sort by arrival
        entries.sort(key=lambda x: x.arrival_minutes)
        x_coords = []
        y_coords = []
        hover_texts = []
        
        for entry in entries:
            # block_id is e.g. "KTV-KPL"
            if "-" not in entry.block_id:
                continue
            u, v = entry.block_id.split("-")
            
            t_entry = output.epoch_t0 + timedelta(minutes=entry.arrival_minutes)
            t_exit = output.epoch_t0 + timedelta(minutes=entry.departure_minutes)
            
            if u in station_dist and v in station_dist:
                x_coords.extend([t_entry, t_exit, None])
                y_coords.extend([station_dist[u], station_dist[v], None])
                hover_texts.extend([f"{train_id} at {u}", f"{train_id} at {v}", None])
                
        if x_coords:
            fig.add_trace(go.Scatter(
                x=x_coords,
                y=y_coords,
                mode='lines',
                name=f"Pass {train_id}",
                line=dict(color='red', width=1.5, dash='dot'),
                hoverinfo='text',
                text=hover_texts,
                opacity=0.6
            ))
            
    # 2. Plot Scheduled Freight Trains
    for path in result.paths:
        if not path.edges:
            continue
            
        x_coords_cont = []
        y_coords_cont = []
        hover_cont = []
        
        for i, edge in enumerate(path.edges):
            t_entry = output.epoch_t0 + timedelta(minutes=edge.entry_minute)
            t_exit = output.epoch_t0 + timedelta(minutes=edge.exit_minute)
            y_from = station_dist.get(edge.from_station, 0)
            y_to = station_dist.get(edge.to_station, 0)
            
            # If not first edge, and there's a gap between previous exit and this entry, add a dwell segment
            if i > 0:
                prev_edge = path.edges[i-1]
                t_prev_exit = output.epoch_t0 + timedelta(minutes=prev_edge.exit_minute)
                y_prev_to = station_dist.get(prev_edge.to_station, 0)
                
                if t_entry > t_prev_exit:
                    # Dwell segment (horizontal line at station)
                    x_coords_cont.append(t_entry)
                    y_coords_cont.append(y_prev_to)
                    hover_cont.append(f"{path.freight_id} dwell end at {prev_edge.to_station}")
                    
                # If there's a spatial gap (e.g. teleporting, which shouldn't happen, but just in case)
                if y_prev_to != y_from:
                    x_coords_cont.append(None)
                    y_coords_cont.append(None)
                    hover_cont.append(None)
                    x_coords_cont.append(t_entry)
                    y_coords_cont.append(y_from)
                    hover_cont.append(f"{path.freight_id} resumes at {edge.from_station}")
            else:
                x_coords_cont.append(t_entry)
                y_coords_cont.append(y_from)
                hover_cont.append(f"{path.freight_id} departs {edge.from_station}")
                
            x_coords_cont.append(t_exit)
            y_coords_cont.append(y_to)
            hover_cont.append(f"{path.freight_id} arrives {edge.to_station}")
            
        if x_coords_cont:
            fig.add_trace(go.Scatter(
                x=x_coords_cont,
                y=y_coords_cont,
                mode='lines+markers',
                name=f"Frt {path.freight_id}",
                line=dict(color='blue', width=3),
                marker=dict(size=4),
                hoverinfo='text',
                text=hover_cont
            ))
            
    # Format axes
    # Use station names on Y axis
    y_vals = [station_dist[s] for s in DN_STATIONS if s in station_dist]
    y_texts = [s for s in DN_STATIONS if s in station_dist]
    
    fig.update_layout(
        title="KTV-PSA Freight Schedule - Méridien Diagram",
        xaxis_title="Time",
        yaxis_title="Station",
        yaxis=dict(
            tickmode='array',
            tickvals=y_vals,
            ticktext=y_texts
        ),
        hovermode="closest",
        template="plotly_white",
        height=800,
        legend=dict(
            yanchor="top",
            y=0.99,
            xanchor="left",
            x=1.01
        ),
        margin=dict(r=150)
    )
    
    # Save to file
    fig.write_html(save_path)
    print(f"Train string chart saved to {save_path}")
    return fig


def plot_orchestrator_string_chart(output: PipelineOutput, result: 'OrchestratorResult', save_path: str = "orchestrator_chart.html"):
    """
    Generate a string chart for the complete rolling-horizon result.
    Requires a master PipelineOutput (covering the full horizon) to plot passenger trains correctly.
    """
    # Create a synthetic SolveResult holding all frozen paths from all slices
    from ktv_psa_scheduler.model import SolveResult, ScheduledPath
    
    # Group paths by freight_id since a train might appear in multiple slices
    master_paths: Dict[str, ScheduledPath] = {}
    
    for slice_obj in result.frozen_slices:
        for path in slice_obj.paths:
            if path.freight_id not in master_paths:
                master_paths[path.freight_id] = ScheduledPath(
                    freight_id=path.freight_id,
                    edges=list(path.edges),
                    total_progress=path.total_progress
                )
            else:
                # Append the new edges to the existing path
                master_paths[path.freight_id].edges.extend(path.edges)
                master_paths[path.freight_id].total_progress += path.total_progress
                
    # Sort edges inside each path just in case
    for path in master_paths.values():
        path.edges.sort(key=lambda e: e.entry_minute)
        
    synthetic_result = SolveResult(
        status="ORCHESTRATED",
        objective_value=0.0,
        paths=list(master_paths.values()),
        solve_time_seconds=result.total_solve_time,
        num_variables=0,
        num_constraints=0
    )
    
    return plot_train_string_chart(output, synthetic_result, save_path)
