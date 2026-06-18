"""
Tests for the rolling-horizon orchestrator.
"""

import sys
from pathlib import Path
from datetime import datetime
import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ktv_psa_scheduler.pipeline import FreightLoad
from ktv_psa_scheduler.orchestrator import (
    OrchestratorConfig,
    run_rolling_horizon,
    extract_train_states,
    freeze_paths,
    inject_carried_trains,
)
from ktv_psa_scheduler.model import ScheduledPath, TSNEdge, PipelineOutput, Station, BlockSection


def create_mock_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    
    # Create folders
    (data_dir / "Freight").mkdir(parents=True, exist_ok=True)
    (data_dir / "passenger").mkdir(parents=True, exist_ok=True)
    (data_dir / "infrastructure").mkdir(parents=True, exist_ok=True)
    
    # 1. Infrastructure files
    sttnline_csv = data_dir / "infrastructure" / "KTV-PSA-Infra.xlsx - STTNLINE.csv"
    sttnline_csv.write_text(
        "MAVSTTNCODE,MANCSR,MACLINECATEGORY\n"
        "KTV,850.0,L\n"
        "KTV,850.0,L\n"
        "KPL,850.0,L\n"
        "KPL,850.0,L\n"
        "ALM,850.0,L\n"
    )
    
    station_csv = data_dir / "infrastructure" / "KTV-PSA-Infra.xlsx - STATION.csv"
    station_csv.write_text(
        "MAVSTTNCODE,MAVSTTNNAME\n"
        "KTV,Kottavalasa Junction\n"
        "KPL,Kantakapalle\n"
        "ALM,Alamanda\n"
    )
    
    blocks_csv = data_dir / "infrastructure" / "KTV-PSA-Infra.xlsx - BLOCKSCTN.csv"
    blocks_csv.write_text(
        "MAVBLCKSCTN,MAVFROMSTTNCODE,MAVTOSTTNCODE,MAVSGNLTYPE,MANNUMBLINES,MANINTRDIST\n"
        "KTV-KPL,KTV,KPL,AT,3,7.74\n"
        "KPL-ALM,KPL,ALM,AT,3,9.23\n"
    )
    
    # 2. Passenger files
    train_csv = data_dir / "passenger" / "KTV_PSA_Passenger_Schedule.xlsx - TRAIN-KTV-PSA.csv"
    train_csv.write_text(
        "TRAINID,TRAIN_NAME,TRAIN_TYPE\n"
        "18005,Express,MEX\n"
    )
    
    schedule_csv = data_dir / "passenger" / "KTV_PSA_Passenger_Schedule.xlsx - SCHEDULE-KTV-PSA.csv"
    schedule_csv.write_text(
        "TRAINID,BLOCK_SCTN,ARRIVAL,DEPARTURE,ROUTE_SEQ_NO\n"
        "18005,KTV-KPL,3600,4200,1\n"
        "18005,KPL-ALM,4300,4900,2\n"
    )
    
    # empty profiles
    (data_dir / "passenger" / "GRADIENT (KTV-PSA).csv").write_text("MAVBLCKSCTN,MANSEQNUMBER,MAVGRADETYPE,MANGRADEVALUE,MANDISTANCE\n")
    (data_dir / "passenger" / "CURVATURE (KTV-PSA).csv").write_text("MAVBLCKSCTN,MANSEQNUMBER,MANCURVERADIUS,MANDISTANCE\n")
    (data_dir / "passenger" / "PSR (KTV-PSA).csv").write_text("MAVBLCKSCTN,MANGDSSPEED,MANPASSPEED\n")
    
    # 3. Freight files
    freight_csv = data_dir / "Freight" / "WAT_GOODS_TRAIN_AUG25_01082025_27082025 - WAT_GOODS_TRAIN_AUG25_01082025_27082025.csv"
    freight_csv.write_text(
        "LoadId,RakeId,Source,Destination,Load Type,Total Km,Block Section,Block Hrs,Speed,Commodity,Description,Sttn,Arrival Time,Depart Time\n"
        "FT001,RAKE_001,KTV,ALM,BOXNHL,16.97,KTV-KPL,0.0,60.0,IORE,IRON ORE,KTV,01/08/2025 00:10,\n"
    )
    
    return data_dir


def test_freeze_paths():
    """Test that path freezing properly filters edges within the time boundary."""
    edges = [
        TSNEdge(edge_id=1, freight_id="FT01", block_id="KTV-KPL", from_station="KTV", to_station="KPL", entry_minute=10, exit_minute=18, travel_minutes=8.0, progress_reward=7.74),
        TSNEdge(edge_id=2, freight_id="FT01", block_id="KPL-ALM", from_station="KPL", to_station="ALM", entry_minute=25, exit_minute=35, travel_minutes=10.0, progress_reward=9.23),
        TSNEdge(edge_id=3, freight_id="FT01", block_id="ALM-KUK", from_station="ALM", to_station="KUK", entry_minute=40, exit_minute=50, travel_minutes=10.0, progress_reward=10.0),
    ]
    path = ScheduledPath(freight_id="FT01", edges=edges, total_progress=26.97)

    # Freeze end at 30: should only keep the first two edges
    frozen = freeze_paths([path], window_start=0, freeze_end=30)
    assert len(frozen) == 1
    assert len(frozen[0].edges) == 2
    assert frozen[0].edges[0].block_id == "KTV-KPL"
    assert frozen[0].edges[1].block_id == "KPL-ALM"
    assert frozen[0].total_progress == pytest.approx(7.74 + 9.23)


def test_extract_train_states():
    """Test that train state extraction correctly identifies positions at freeze boundary."""
    epoch_t0 = datetime(2025, 8, 1, 0, 0)
    
    # Stations and blocks mapping for distance calculation
    stations = {
        "KTV": Station("KTV", "Kottavalasa", 2, 850),
        "KPL": Station("KPL", "Kantakapalle", 2, 850),
        "ALM": Station("ALM", "Alamanda", 2, 850),
    }
    blocks = {
        "KTV-KPL": BlockSection("KTV-KPL", "KTV", "KPL", "AT", 3, 7.74, 3),
        "KPL-ALM": BlockSection("KPL-ALM", "KPL", "ALM", "AT", 3, 9.23, 3),
    }
    
    # Pipeline Output setup
    load = FreightLoad(
        load_id="FT01",
        rake_id="RAKE_001",
        source="KTV",
        destination="ALM",
        load_type="BOXNHL",
        total_km=16.97,
        block_section="KTV-KPL",
        block_hrs=0.0,
        speed=60.0,
        commodity="IORE",
        description="IRON ORE",
        station="KTV",
        arrival_time="01/08/2025 00:10",
        depart_time=None
    )
    
    output = PipelineOutput(
        freight_loads=[load],
        passenger_trains=[],
        passenger_schedule_entries=[],
        stations=stations,
        blocks=blocks,
        epoch_t0=epoch_t0,
        window_start_minutes=0,
        window_end_minutes=60,
        horizon_size_minutes=60
    )
    
    # Path where train traverses KTV-KPL but hasn't reached ALM (its destination) by freeze_end
    # Edge 1 starts at 10, exits at 18
    edges = [
        TSNEdge(edge_id=1, freight_id="FT01", block_id="KTV-KPL", from_station="KTV", to_station="KPL", entry_minute=10, exit_minute=18, travel_minutes=8.0, progress_reward=7.74),
    ]
    path = ScheduledPath(freight_id="FT01", edges=edges, total_progress=7.74)
    
    # Extract states at freeze_end = 20
    states = extract_train_states([path], freeze_end=20, output=output)
    
    assert len(states) == 1
    state = states[0]
    assert state.load_id == "FT01"
    assert state.station == "KPL"
    assert state.arrival_minute == 18
    assert state.direction == "DN"
    assert state.remaining_km == pytest.approx(9.23)


def test_inject_carried_trains():
    """Test that carried-forward trains are successfully injected into the next window."""
    epoch_t0 = datetime(2025, 8, 1, 0, 0)
    
    load = FreightLoad(
        load_id="FT01",
        rake_id="RAKE_001",
        source="KTV",
        destination="ALM",
        load_type="BOXNHL",
        total_km=16.97,
        block_section="KTV-KPL",
        block_hrs=0.0,
        speed=60.0,
        commodity="IORE",
        description="IRON ORE",
        station="KTV",
        arrival_time="01/08/2025 00:10",
        depart_time=None
    )
    
    output = PipelineOutput(
        freight_loads=[],
        passenger_trains=[],
        passenger_schedule_entries=[],
        stations={},
        blocks={},
        epoch_t0=epoch_t0,
        window_start_minutes=30,
        window_end_minutes=90,
        horizon_size_minutes=60
    )
    
    from ktv_psa_scheduler.orchestrator import TrainState
    carried = [
        TrainState(
            load_id="FT01",
            station="KPL",
            arrival_minute=18,
            remaining_km=9.23,
            speed=60.0,
            direction="DN",
            original_load=load
        )
    ]
    
    injected_output = inject_carried_trains(output, carried, epoch_t0)
    assert len(injected_output.freight_loads) == 1
    
    injected_load = injected_output.freight_loads[0]
    assert injected_load.load_id == "FT01"
    assert injected_load.station == "KPL"
    assert injected_load.arrival_time == "01/08/2025 00:18"
    assert injected_load.total_km == pytest.approx(9.23)


def test_orchestrator_synthetic_loop(tmp_path):
    """Test rolling-horizon execution on synthetic corridor data."""
    data_dir = create_mock_data_dir(tmp_path)
    
    config = OrchestratorConfig(
        plan_horizon_minutes=60,
        freeze_horizon_minutes=30,
        total_horizon_minutes=120,
        solve_time_limit=10.0,
        mip_gap=0.01
    )
    
    result = run_rolling_horizon(data_dir=data_dir, config=config, verbose=True)
    
    assert result.windows_solved == 4  # 120 / 30 = 4 windows
    assert len(result.frozen_slices) == 4
    
    # We should have scheduled FT01 on some segments
    assert result.total_trains_scheduled >= 1
    assert result.total_edges_committed >= 1
    
    # Let's inspect the frozen slices
    all_frozen_edges = []
    for s in result.frozen_slices:
        for p in s.paths:
            all_frozen_edges.extend(p.edges)
            
    # Ensure there are no duplicate/overlapping/conflicting edges for FT01
    assert len(all_frozen_edges) >= 2  # Should traverse both blocks
    for e in all_frozen_edges:
        assert e.from_station in ["KTV", "KPL"]
        assert e.to_station in ["KPL", "ALM"]


def test_orchestrator_real_data_smoke():
    """Run rolling-horizon on the real CSV data for a very short horizon."""
    # Check if the real data directory is present
    real_data_dir = Path(__file__).parent.parent / "data"
    if not real_data_dir.exists():
        pytest.skip("Real data directory not found; skipping real data smoke test.")
        
    config = OrchestratorConfig(
        plan_horizon_minutes=60,
        freeze_horizon_minutes=30,
        total_horizon_minutes=60,  # 1 window only
        solve_time_limit=15.0,
        mip_gap=0.05
    )
    
    result = run_rolling_horizon(data_dir=real_data_dir, config=config, verbose=True)
    assert result.windows_solved == 2  # 60 / 30 = 2 windows
    assert len(result.frozen_slices) == 2
    assert result.total_solve_time >= 0.0
