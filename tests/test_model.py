"""
Test suite for TSN model formulation and HiGHS solving.
"""

import sys
import pytest
from pathlib import Path
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ktv_psa_scheduler.pipeline import (
    FreightLoad,
    PassengerTrain,
    PassengerScheduleEntry,
    Station,
    BlockSection,
    PipelineOutput,
)
from ktv_psa_scheduler.model import (
    TSNEdge,
    ScheduledPath,
    SolveResult,
    build_tsn_graph,
    solve_model,
)


def test_tsn_graph_building_and_solving(tmp_path):
    """Test TSN model graph construction and optimization on a synthetic 3-station corridor."""
    # Create mock/synthetic data directory for physics profile loader checks
    data_dir = tmp_path / "data"
    passenger_dir = data_dir / "passenger"
    passenger_dir.mkdir(parents=True, exist_ok=True)
    
    # Write empty CSV profiles so model loading doesn't warn or fail
    (passenger_dir / "GRADIENT (KTV-PSA).csv").write_text("MAVBLCKSCTN,MANSEQNUMBER,MAVGRADETYPE,MANGRADEVALUE,MANDISTANCE\n")
    (passenger_dir / "CURVATURE (KTV-PSA).csv").write_text("MAVBLCKSCTN,MANSEQNUMBER,MANCURVERADIUS,MANDISTANCE\n")
    (passenger_dir / "PSR (KTV-PSA).csv").write_text("MAVBLCKSCTN,MANGDSSPEED,MANPASSPEED\n")
    
    # 1. Setup corridor infrastructure (KTV -> KPL -> ALM)
    stations = {
        "KTV": Station(station_id="KTV", station_name="Kottavalasa Junction", loop_count=2, mancsr=850.0),
        "KPL": Station(station_id="KPL", station_name="Kantakapalle", loop_count=2, mancsr=850.0),
        "ALM": Station(station_id="ALM", station_name="Alamanda", loop_count=2, mancsr=850.0),
    }
    
    blocks = {
        "KTV-KPL": BlockSection(
            block_id="KTV-KPL",
            from_station="KTV",
            to_station="KPL",
            signalling_type="AT",
            running_lines=3,
            length_km=7.74,
            headway_minutes=3
        ),
        "KPL-ALM": BlockSection(
            block_id="KPL-ALM",
            from_station="KPL",
            to_station="ALM",
            signalling_type="AT",
            running_lines=3,
            length_km=9.23,
            headway_minutes=3
        )
    }
    
    # 2. Add 1 freight train starting at KTV at minute 0, destination ALM
    freight_loads = [
        FreightLoad(
            load_id="FT01",
            rake_id="RAKE01",
            source="KTV",
            destination="ALM",
            load_type="BOXNHL",
            total_km=16.97,
            block_section="KTV-KPL",
            block_hrs=0.0,
            speed=60.0,  # 60 km/h = 1 km/min
            commodity="IORE",
            description="IRON ORE",
            station="KTV",
            arrival_time="01/08/2025 00:00",
            depart_time=None
        )
    ]
    
    # 3. Add 1 passenger train that occupies KTV-KPL from minute 20 to minute 30
    passenger_trains = [
        PassengerTrain(train_id="VB01", train_name="Vande Bharat", train_type="VNDB", priority_class=1)
    ]
    
    passenger_schedule_entries = [
        PassengerScheduleEntry(
            train_id="VB01",
            block_id="KTV-KPL",
            arrival_seconds=20 * 60,   # minute 20
            departure_seconds=30 * 60  # minute 30
        )
    ]
    
    # Preprocessing pipeline output
    output = PipelineOutput(
        freight_loads=freight_loads,
        passenger_trains=passenger_trains,
        passenger_schedule_entries=passenger_schedule_entries,
        stations=stations,
        blocks=blocks,
        epoch_t0=datetime(2025, 8, 1, 0, 0),
        window_start_minutes=0,
        window_end_minutes=60,
        horizon_size_minutes=60
    )
    
    # 4. Build TSN Graph
    edges = build_tsn_graph(output, data_dir)
    
    # Verify we generated edges
    assert len(edges) > 0
    
    # Verify that edges overlapping with passenger window [20, 30] (with 5 min safety headway buffer, i.e., [15, 35]) are pruned.
    # A passenger train on KTV-KPL during [20, 30] blocks the interval [15, 35].
    # Traversal time of KTV-KPL is ~7.74 minutes.
    # An edge entering at minute 15 exits at 22.74, which overlaps with [15, 35] -> should be pruned.
    # An edge entering at minute 10 exits at 17.74, which overlaps with [15, 35] -> should be pruned.
    # An edge entering at minute 0 exits at 7.74 -> feasible.
    # An edge entering at minute 36 exits at 43.74 -> feasible.
    overlapping_edges = [e for e in edges if e.block_id == "KTV-KPL" and 10 <= e.entry_minute <= 25]
    assert len(overlapping_edges) == 0
    
    # 5. Solve the MILP model
    res = solve_model(output, edges)
    
    assert res.status in ["OPTIMAL", "FEASIBLE"]
    assert res.objective_value > 0.0
    
    # Verify path is scheduled for FT01
    ft01_paths = [p for p in res.paths if p.freight_id == "FT01"]
    assert len(ft01_paths) == 1
    
    path = ft01_paths[0]
    assert len(path.edges) == 2  # Must traverse both KTV-KPL and KPL-ALM
    
    # First edge must be KTV-KPL, second must be KPL-ALM
    edge1 = path.edges[0]
    edge2 = path.edges[1]
    assert edge1.block_id == "KTV-KPL"
    assert edge2.block_id == "KPL-ALM"
    assert edge1.to_station == "KPL"
    assert edge2.from_station == "KPL"
    
    # Departure times must avoid passenger block interval [15, 35]
    assert not (15 <= edge1.entry_minute <= 35)
    # The arrival at KPL must be before or equal to departure from KPL
    assert edge1.exit_minute <= edge2.entry_minute
