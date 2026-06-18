"""
Test suite for the data pipeline module.
Tests data loading, validation, and temporal alignment.
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
    run_pipeline,
    seconds_to_minutes,
)


class TestDataContracts:
    """Test data structure definitions."""
    
    def test_freight_train_creation(self):
        """Test FreightLoad dataclass instantiation."""
        load = FreightLoad(
            load_id="FT001",
            rake_id="RAKE_001",
            source="KTV",
            destination="PSA",
            load_type="BOXNHL",
            total_km=176.83,
            block_section="KTV-KPL",
            block_hrs=2.5,
            speed=85.0,
            commodity="IORE",
            description="IRON ORE",
            station="KTV",
            arrival_time="18/08/2025 06:00",
            depart_time="18/08/2025 08:30"
        )
        assert load.load_id == "FT001"
        assert load.commodity == "IORE"
        assert load.total_km == 176.83
    
    def test_passenger_schedule_creation(self):
        """Test PassengerScheduleEntry dataclass instantiation."""
        entry = PassengerScheduleEntry(
            train_id="VB01",
            block_id="5",
            arrival_seconds=28800,  # 8 hours in seconds
            departure_seconds=29400  # 8h 10m in seconds
        )
        assert entry.train_id == "VB01"
        assert entry.block_id == "5"
        assert entry.arrival_minutes == 480  # 28800 / 60
        assert entry.departure_minutes == 490
        assert entry.dwell_minutes == 10  # (29400 - 28800) / 60
    
    def test_passenger_train_creation(self):
        """Test PassengerTrain dataclass instantiation."""
        train = PassengerTrain(
            train_id="VB01",
            train_name="Vande Bharat Express",
            train_type="VNDB",
            priority_class=1
        )
        assert train.train_id == "VB01"
        assert train.priority_class == 1
        assert train.train_type == "VNDB"
    
    def test_station_creation(self):
        """Test Station dataclass instantiation."""
        station = Station(
            station_id="KTV",
            station_name="Kottavalasa Junction",
            loop_count=3,
            mancsr=850.0
        )
        assert station.station_id == "KTV"
        assert station.loop_count == 3
    
    def test_block_section_creation(self):
        """Test BlockSection dataclass instantiation."""
        block = BlockSection(
            block_id="1",
            from_station="KTV",
            to_station="KPL",
            signalling_type="AT",
            running_lines=3,
            length_km=7.74,
            headway_minutes=4
        )
        assert block.block_id == "1"
        assert block.running_lines == 3
        assert block.headway_minutes == 4
        assert block.signalling_type == "AT"


class TestUtilityFunctions:
    """Test utility functions."""
    
    def test_seconds_to_minutes(self):
        """Test seconds-to-minutes conversion."""
        # 3600 seconds = 60 minutes
        result = seconds_to_minutes(3600, 0)
        assert result == 60
        
        # With offset
        result = seconds_to_minutes(3600, 1800)  # 1800 offset
        assert result == 30
    
    def test_seconds_to_minutes_zero_offset(self):
        """Test zero-offset case."""
        result = seconds_to_minutes(0, 0)
        assert result == 0


class TestPipelineIntegration:
    """Integration tests for the pipeline."""
    
    def test_pipeline_initialization(self):
        """Test that pipeline can be initialized (even with missing data files)."""
        data_dir = Path(__file__).parent.parent / "data"
        
        # This test will fail gracefully if data is not present
        # In a real test suite, we'd mock the file I/O
        if not data_dir.exists():
            pytest.skip("Data directory not found; skipping full pipeline test")
            
    def test_pipeline_real_run(self):
        """Test run_pipeline on actual repository CSV files."""
        data_dir = Path(__file__).parent.parent / "data"
        if not data_dir.exists():
            pytest.skip("Data directory not found; skipping real run")
            
        output = run_pipeline(
            data_dir=data_dir,
            window_start_minutes=0,
            horizon_size_minutes=360
        )
        assert output is not None
        assert output.epoch_t0 == datetime(2025, 8, 1, 0, 0)
        assert output.horizon_size_minutes == 360
        
        # Verify we loaded actual stations and blocks
        assert len(output.stations) > 0
        assert len(output.blocks) > 0
        assert len(output.passenger_trains) > 0
        
        # Verify a key station and block from KTV-PSA corridor exist
        assert "KTV" in output.stations
        assert "PSA" in output.stations
        assert "KTV-KPL" in output.blocks or "KPL-KTV" in output.blocks
        
        # Check that LoopCounts were computed from STTNLINE
        assert output.stations["KTV"].loop_count > 0
        assert output.stations["PSA"].loop_count > 0
        assert output.stations["KTV"].mancsr > 500.0
    
    def test_pipeline_output_structure(self):
        """Test PipelineOutput structure."""
        output = PipelineOutput(
            freight_loads=[
                FreightLoad("FT001", "RAKE_001", "KTV", "PSA", "BOXNHL", 176.83, 
                           "KTV-KPL", 2.5, 85.0, "IORE", "IRON ORE", "KTV", 
                           "18/08/2025 06:00", "18/08/2025 08:30")
            ],
            passenger_trains=[
                PassengerTrain("VB01", "Vande Bharat", "VNDB", 1)
            ],
            passenger_schedule_entries=[
                PassengerScheduleEntry("VB01", "5", 28800, 29400)
            ],
            stations={"KTV": Station("KTV", "Kottavalasa", 3, 850)},
            blocks={"1": BlockSection("1", "KTV", "KPL", "AT", 3, 7.74, 4)},
            epoch_t0=datetime.now(),
            window_start_minutes=0,
            window_end_minutes=360,
            horizon_size_minutes=360,
        )
        
        assert len(output.freight_loads) == 1
        assert len(output.passenger_trains) == 1
        assert len(output.passenger_schedule_entries) == 1
        assert len(output.stations) == 1
        assert len(output.blocks) == 1
        assert output.horizon_size_minutes == 360


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
