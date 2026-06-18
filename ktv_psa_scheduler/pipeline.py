"""
Data Pipeline: Ingestion, Validation, and Temporal Realignment

This module loads raw CSV data from the `data/` directory, validates schemas,
normalizes timestamps to rolling-window minute offsets, filters to active
planning horizons, and produces compact Python objects ready for the Rust
pruning engine and Python-MIP model layer.

Key Responsibilities:
- Load freight, passenger, infrastructure, and route data from CSV files
- Establish system start epoch (T0) and convert all timestamps to relative minutes
- Validate required fields and data types
- Filter records to active rolling-horizon windows
- Produce structured outputs (dataframes or dataclasses) for downstream modules
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import polars as pl
from datetime import datetime, timedelta



# Data Contracts: Structured Output Classes


@dataclass
class FreightLoad:
    """Represents a freight train journey log (tracking record from historical data)."""
    load_id: str  # Unique identifier for loaded train journey
    rake_id: str  # Physical wagon formation identifier
    source: str  # Origin station code
    destination: str  # Final destination station code
    load_type: str  # Wagon design category (BOXNHL, BOXN, BCN, BLC, etc.)
    total_km: float  # Total planned journey distance (km)
    block_section: str  # Current block section being traversed
    block_hrs: float  # Duration spent on block section (hours)
    speed: float  # Average speed on block section (km/h)
    commodity: str  # Commodity code (IORE, PHC, IMCL, CONT, IS, etc.)
    description: str  # Natural name of cargo (IRON ORE, POWER HOUSE COAL, etc.)
    station: str  # Current reporting station code
    arrival_time: str  # Timestamp of arrival at station (DD/MM/YYYY HH:MM)
    depart_time: Optional[str]  # Timestamp of departure from station (DD/MM/YYYY HH:MM)


@dataclass
class PassengerTrain:
    """Represents a scheduled passenger train with speed/priority profile."""
    train_id: str  # Unique passenger train number/id
    train_name: str  # Train name (e.g., "Vande Bharat Express")
    train_type: str  # Type: SUF (Superfast), MEX/PEXP (Express), VNDB (Vande Bharat), PAS/MEMU (Local)
    priority_class: int  # Priority tier (1 = Ultra-high, e.g., Vande Bharat; 2+ = lower priority)


@dataclass
class PassengerScheduleEntry:
    """Represents a single passenger train passage through a block section."""
    train_id: str
    block_id: str
    arrival_seconds: int  # Integer seconds from base midnight (relative to planning epoch)
    departure_seconds: int  # Integer seconds from base midnight
    
    @property
    def arrival_minutes(self) -> int:
        """Convert arrival seconds to minutes from epoch."""
        return self.arrival_seconds // 60
    
    @property
    def departure_minutes(self) -> int:
        """Convert departure seconds to minutes from epoch."""
        return self.departure_seconds // 60
    
    @property
    def dwell_minutes(self) -> int:
        """Calculate dwell time in minutes at the block/station."""
        return (self.departure_seconds - self.arrival_seconds) // 60


@dataclass
class Station:
    """Infrastructure: station metadata and capacity."""
    station_id: str
    station_name: str
    loop_count: int  # total loop tracks available
    mancsr: float  # Clear Standing Resonance (meters)


@dataclass
class BlockSection:
    """Infrastructure: block section (directional segment) metadata."""
    block_id: str
    from_station: str
    to_station: str
    signalling_type: str  # AT (Automatic Track, 3 lines) or AB (Automatic Block, 2 lines)
    running_lines: int  # Total running tracks on this direction (MANNUMBLINES)
    length_km: float  # distance in kilometers
    headway_minutes: int  # safety headway requirement (minutes, typically 3-5)


@dataclass
class PipelineOutput:
    """Compact output from the preprocessing pipeline."""
    freight_loads: List[FreightLoad]  # Historical/active freight tracking logs
    passenger_trains: List[PassengerTrain]  # Passenger train definitions
    passenger_schedule_entries: List[PassengerScheduleEntry]  # Block-level schedule entries
    stations: Dict[str, Station]
    blocks: Dict[str, BlockSection]
    epoch_t0: datetime  # system start time
    window_start_minutes: int  # relative minute start of active window
    window_end_minutes: int  # relative minute end of active window
    horizon_size_minutes: int  # total planning horizon (e.g., 360 for 6 hours)


# ============================================================================
# Pipeline Functions
# ============================================================================

def load_freight_data(data_dir: Path, window_start: int, horizon_size: int) -> pl.DataFrame:
    """
    Load and preprocess freight train tracking data.
    
    Args:
        data_dir: Path to `data/Freight/` directory
        window_start: Start minute of active planning window (relative to T0)
        horizon_size: Size of planning horizon in minutes (e.g., 360)
    
    Returns:
        Polars DataFrame with freight records, filtered to active window
    """
    freight_file = data_dir / "Freight" / "WAT_GOODS_TRAIN_AUG25_01082025_27082025 - WAT_GOODS_TRAIN_AUG25_01082025_27082025.csv"
    
    if not freight_file.exists():
        raise FileNotFoundError(f"Freight data not found at {freight_file}")
    
    # Load CSV with Polars
    df = pl.read_csv(freight_file)
    
    # Strip whitespace from column names just in case
    df.columns = [c.strip() for c in df.columns]
    
    # Validate required columns (actual freight tracking schema)
    required_cols = ["LoadId", "RakeId", "Source", "Destination", "Load Type", 
                     "Total Km", "Block Section", "Block Hrs", "Speed", 
                     "Commodity", "Description", "Sttn", "Arrival Time", "Depart Time"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Freight CSV missing columns: {missing}")
    
    # Filter out empty or header rows
    df = df.filter(
        (pl.col("LoadId").is_not_null()) & 
        (pl.col("LoadId") != "LoadId")
    )
    
    # Ensure numeric types
    df = df.with_columns([
        pl.col("Total Km").cast(pl.Float32, strict=False).fill_null(0.0),
        pl.col("Block Hrs").cast(pl.Float32, strict=False).fill_null(0.0),
        pl.col("Speed").cast(pl.Float32, strict=False).fill_null(0.0),
    ])
    
    return df


def load_passenger_data(data_dir: Path, window_start: int, horizon_size: int) -> Tuple[List[PassengerTrain], List[PassengerScheduleEntry]]:
    """
    Load and preprocess passenger schedule data.
    
    Loads two components:
    1. Train metadata (train_id, name, type, priority) from TRAIN-KTV-PSA.csv
    2. Schedule entries (block-level arrivals/departures in seconds) from SCHEDULE-KTV-PSA.csv
    
    Args:
        data_dir: Path to `data/` directory
        window_start: Start minute of active planning window
        horizon_size: Size of planning horizon in minutes
    
    Returns:
        Tuple of (list of PassengerTrain, list of PassengerScheduleEntry)
    """
    # Load passenger train metadata
    train_files = [f for f in data_dir.glob("passenger/*.csv") if "TRAIN" in f.name.split(" - ")[-1].upper()]
    if not train_files:
        raise FileNotFoundError("No passenger train metadata CSV found in data/passenger/")
    
    train_meta_df = pl.read_csv(train_files[0])
    train_meta_df.columns = [c.strip().upper() for c in train_meta_df.columns]
    
    required_train_cols = ["TRAINID", "TRAIN_NAME", "TRAIN_TYPE"]
    missing = [c for c in required_train_cols if c not in train_meta_df.columns]
    if missing:
        raise ValueError(f"Passenger train metadata CSV missing columns: {missing}")
    
    # Convert to PassengerTrain objects
    trains = {}
    for row in train_meta_df.iter_rows(named=True):
        ttype = str(row.get("TRAIN_TYPE", "")).strip().upper()
        if ttype in ["VNDB", "DRNT"]:
            priority = 1
        elif ttype in ["SUF", "GBR"]:
            priority = 2
        elif ttype in ["MEX", "PEXP", "TOD"]:
            priority = 3
        else:
            priority = 4
            
        train = PassengerTrain(
            train_id=str(row["TRAINID"]),
            train_name=str(row.get("TRAIN_NAME", "")),
            train_type=ttype,
            priority_class=priority
        )
        trains[train.train_id] = train
    
    # Load passenger schedule entries
    sched_files = [f for f in data_dir.glob("passenger/*.csv") if "SCHEDULE" in f.name.split(" - ")[-1].upper()]
    if not sched_files:
        raise FileNotFoundError("No passenger schedule CSV found in data/passenger/")
    
    sched_df = pl.read_csv(sched_files[0])
    sched_df.columns = [c.strip().upper() for c in sched_df.columns]
    
    required_sched_cols = ["TRAINID", "BLOCK_SCTN", "ARRIVAL", "DEPARTURE", "ROUTE_SEQ_NO"]
    missing = [c for c in required_sched_cols if c not in sched_df.columns]
    if missing:
        raise ValueError(f"Passenger schedule CSV missing columns: {missing}")
    
    # Ensure numeric types and convert seconds
    sched_df = sched_df.with_columns([
        pl.col("TRAINID").cast(pl.Utf8),
        pl.col("ROUTE_SEQ_NO").cast(pl.Int32),
        pl.col("ARRIVAL").cast(pl.Int32),
        pl.col("DEPARTURE").cast(pl.Int32),
    ])
    
    # Filter out entries with null block sections (typically terminal arrivals)
    sched_df = sched_df.filter(pl.col("BLOCK_SCTN").is_not_null())
    
    # Sort chronologically by train
    sched_df = sched_df.sort(["TRAINID", "ARRIVAL"])
    
    # Pair current row departure (occupancy start) with next row arrival (occupancy end)
    paired_df = sched_df.with_columns([
        pl.col("DEPARTURE").alias("start_seconds"),
        pl.col("ARRIVAL").shift(-1).over("TRAINID").alias("end_seconds")
    ]).filter(pl.col("end_seconds").is_not_null())
    
    # Filter to active window (using minutes-from-seconds)
    window_start_seconds = window_start * 60
    window_end_seconds = (window_start + horizon_size) * 60
    paired_df = paired_df.filter(
        (pl.col("start_seconds") < window_end_seconds) &
        (pl.col("end_seconds") >= window_start_seconds)
    )
    
    # Convert to PassengerScheduleEntry objects
    schedules = []
    for row in paired_df.iter_rows(named=True):
        entry = PassengerScheduleEntry(
            train_id=row["TRAINID"],
            block_id=row["BLOCK_SCTN"],
            arrival_seconds=int(row["start_seconds"]),
            departure_seconds=int(row["end_seconds"])
        )
        schedules.append(entry)
    
    return list(trains.values()), schedules


def load_infrastructure_data(data_dir: Path) -> Tuple[Dict[str, Station], Dict[str, BlockSection]]:
    """
    Load station and block section infrastructure metadata.
    
    Args:
        data_dir: Path to `data/` directory
    
    Returns:
        Tuple of (stations dict, blocks dict)
    """
    # 1. Load STTNLINE.csv to compute LoopCount and MANCSR per station
    sttnline_files = [f for f in data_dir.glob("infrastructure/*.csv") if "STTNLINE" in f.name.split(" - ")[-1].upper()]
    if not sttnline_files:
        raise FileNotFoundError("No STTNLINE CSV found in data/infrastructure/")
        
    sttnline_df = pl.read_csv(sttnline_files[0])
    sttnline_df.columns = [c.strip().upper() for c in sttnline_df.columns]
    
    # Clean and filter STTNLINE
    sttnline_df = sttnline_df.filter(
        (pl.col("MAVSTTNCODE").is_not_null()) & 
        (pl.col("MAVSTTNCODE") != "MAVSTTNCODE")
    )
    
    # Cast MANCSR to float
    sttnline_df = sttnline_df.with_columns([
        pl.col("MANCSR").cast(pl.Float32, strict=False).fill_null(0.0)
    ])
    
    # Calculate loop counts: count rows where MACLINECATEGORY == 'L' per station
    loop_counts_df = sttnline_df.filter(pl.col("MACLINECATEGORY") == "L").group_by("MAVSTTNCODE").len()
    loop_counts = {row["MAVSTTNCODE"]: row["len"] for row in loop_counts_df.iter_rows(named=True)}
    
    # Calculate MANCSR per station: max MANCSR for category 'L' loop lines, or max for any line
    mancsr_loops_df = sttnline_df.filter(pl.col("MACLINECATEGORY") == "L").group_by("MAVSTTNCODE").agg(pl.max("MANCSR").alias("max_csr"))
    mancsr_all_df = sttnline_df.group_by("MAVSTTNCODE").agg(pl.max("MANCSR").alias("max_csr"))
    
    mancsr_loops = {row["MAVSTTNCODE"]: row["max_csr"] for row in mancsr_loops_df.iter_rows(named=True)}
    mancsr_all = {row["MAVSTTNCODE"]: row["max_csr"] for row in mancsr_all_df.iter_rows(named=True)}

    # 2. Load stations (from STATION.csv)
    station_files = [f for f in data_dir.glob("infrastructure/*.csv") if "STATION" in f.name.split(" - ")[-1].upper()]
    if not station_files:
        raise FileNotFoundError("No station CSV found in data/infrastructure/")
    
    stations_df = pl.read_csv(station_files[0])
    stations_df.columns = [c.strip().upper() for c in stations_df.columns]
    
    # Clean stations
    stations_df = stations_df.filter(
        (pl.col("MAVSTTNCODE").is_not_null()) & 
        (pl.col("MAVSTTNCODE") != "MAVSTTNCODE")
    )
    
    stations = {}
    for row in stations_df.iter_rows(named=True):
        sttn_code = row["MAVSTTNCODE"]
        sttn_name = row.get("MAVSTTNNAME", sttn_code)
        
        loop_cnt = loop_counts.get(sttn_code, 0)
        csr_val = mancsr_loops.get(sttn_code, mancsr_all.get(sttn_code, 0.0))
        
        stations[sttn_code] = Station(
            station_id=sttn_code,
            station_name=sttn_name,
            loop_count=int(loop_cnt),
            mancsr=float(csr_val)
        )
    
    # 3. Load block sections (from BLOCKSCTN.csv)
    block_files = [f for f in data_dir.glob("infrastructure/*.csv") if "BLOCKSCTN" in f.name.split(" - ")[-1].upper()]
    if not block_files:
        raise FileNotFoundError("No block section CSV found in data/infrastructure/")
    
    blocks_df = pl.read_csv(block_files[0]).unique()
    blocks_df.columns = [c.strip().upper() for c in blocks_df.columns]
    
    # Clean blocks
    blocks_df = blocks_df.filter(
        (pl.col("MAVBLCKSCTN").is_not_null()) & 
        (pl.col("MAVFROMSTTNCODE").is_not_null()) &
        (pl.col("MAVBLCKSCTN") != "MAVBLCKSCTN")
    )
    
    blocks = {}
    for row in blocks_df.iter_rows(named=True):
        block_id = row["MAVBLCKSCTN"]
        
        sig_type = str(row.get("MAVSGNLTYPE", "")).strip().upper()
        if sig_type == "AT":
            headway = 3
        elif sig_type == "AB":
            headway = 5
        else:
            headway = 4
            
        try:
            running_lines = int(row["MANNUMBLINES"]) if row.get("MANNUMBLINES") is not None else 2
        except (ValueError, TypeError):
            running_lines = 2
            
        try:
            length_km = float(row["MANINTRDIST"]) if row.get("MANINTRDIST") is not None else 0.0
        except (ValueError, TypeError):
            length_km = 0.0
            
        blocks[block_id] = BlockSection(
            block_id=block_id,
            from_station=row["MAVFROMSTTNCODE"],
            to_station=row["MAVTOSTTNCODE"],
            signalling_type=sig_type,
            running_lines=running_lines,
            length_km=length_km,
            headway_minutes=headway
        )
    
    return stations, blocks


def load_route_data(data_dir: Path) -> Dict[str, List[str]]:
    """
    Load route mappings (sequence of stations per route).
    
    Args:
        data_dir: Path to `data/` directory
    
    Returns:
        Dictionary mapping route_id to ordered list of station codes
    """
    route_files = list(data_dir.glob("Route_Station/*.csv"))
    if not route_files:
        # Return empty dict if route data not available
        return {}
    
    routes = {}
    for route_file in route_files:
        df = pl.read_csv(route_file)
        if "route_id" in df.columns and "station_sequence" in df.columns:
            for row in df.iter_rows(named=True):
                # Assume station_sequence is comma-separated or similar
                seq = row["station_sequence"]
                if isinstance(seq, str):
                    routes[row["route_id"]] = [s.strip() for s in seq.split(",")]
    
    return routes


def establish_epoch(freight_df: Optional[pl.DataFrame] = None) -> datetime:
    """
    Establish the system start epoch (T0).
    
    Parses the earliest arrival time in the freight tracking records and rounds
    it down to the start of that day.
    
    Args:
        freight_df: Optional freight DataFrame to derive T0 from earliest record
    
    Returns:
        datetime object representing system start time
    """
    if freight_df is not None and len(freight_df) > 0:
        try:
            parsed = freight_df.select(
                pl.col("Arrival Time").str.to_datetime("%d/%m/%Y %H:%M", strict=False)
            ).filter(pl.col("Arrival Time").is_not_null())
            if len(parsed) > 0:
                min_time = parsed["Arrival Time"].min()
                if min_time:
                    return min_time.replace(hour=0, minute=0, second=0, microsecond=0)
        except Exception:
            pass
            
    # Fallback to August 1, 2025
    return datetime(2025, 8, 1, 0, 0, 0)


def run_pipeline(
    data_dir: Path,
    window_start_minutes: int = 0,
    horizon_size_minutes: int = 360,
    epoch_t0: Optional[datetime] = None,
) -> PipelineOutput:
    """
    Main pipeline orchestration: load, validate, align, and filter all data.
    
    Args:
        data_dir: Root path to `data/` directory
        window_start_minutes: Start of active planning window (minutes from T0)
        horizon_size_minutes: Size of planning horizon in minutes (default 6 hours)
        epoch_t0: Optional system start epoch; if None, derived from data
    
    Returns:
        PipelineOutput object containing preprocessed data for model and Rust layers
    """
    # Load freight tracking data first to derive epoch if needed
    try:
        freight_df = load_freight_data(data_dir, window_start_minutes, horizon_size_minutes)
    except FileNotFoundError as e:
        raise RuntimeError(f"Freight data load failed: {e}")
        
    # Establish epoch if not provided
    if epoch_t0 is None:
        epoch_t0 = establish_epoch(freight_df)
    
    # Load infrastructure (stations and blocks)
    try:
        stations, blocks = load_infrastructure_data(data_dir)
    except FileNotFoundError as e:
        raise RuntimeError(f"Infrastructure data load failed: {e}")
    
    # Load passenger train metadata and schedule entries
    try:
        passenger_trains, passenger_entries = load_passenger_data(data_dir, window_start_minutes, horizon_size_minutes)
    except FileNotFoundError as e:
        raise RuntimeError(f"Passenger data load failed: {e}")
    
    # Load route data (optional)
    try:
        routes = load_route_data(data_dir)
    except Exception as e:
        routes = {}
        print(f"Warning: Route data load failed: {e}. Continuing without route metadata.")
    
    # Parse and filter freight data using Polars
    window_end_minutes = window_start_minutes + horizon_size_minutes
    freight_df_parsed = freight_df.with_columns([
        pl.col("Arrival Time").str.to_datetime("%d/%m/%Y %H:%M", strict=False)
    ]).filter(pl.col("Arrival Time").is_not_null())
    
    # Parse Depart Time optionally
    if "Depart Time" in freight_df_parsed.columns:
        freight_df_parsed = freight_df_parsed.with_columns([
            pl.col("Depart Time").str.to_datetime("%d/%m/%Y %H:%M", strict=False)
        ])
        
    # Compute relative minutes
    epoch_t0_lit = pl.lit(epoch_t0)
    freight_df_parsed = freight_df_parsed.with_columns([
        ((pl.col("Arrival Time") - epoch_t0_lit).dt.total_minutes()).cast(pl.Int32).alias("arrival_minutes")
    ])
    
    # Filter to active window: arrivals up to window_end_minutes with 24h lookback
    # Group by LoadId and take the earliest record in this window to avoid duplicate entries for the same train
    freight_df_filtered = freight_df_parsed.filter(
        (pl.col("arrival_minutes") >= window_start_minutes - 1440) &
        (pl.col("arrival_minutes") < window_end_minutes)
    ).sort("arrival_minutes").group_by("LoadId", maintain_order=True).first()
    
    # Convert freight DataFrame rows into FreightLoad objects
    freight_loads = []
    for row in freight_df_filtered.iter_rows(named=True):
        # Format back to string to match data contract
        arr_dt = row["Arrival Time"]
        arr_str = arr_dt.strftime("%d/%m/%Y %H:%M") if isinstance(arr_dt, datetime) else str(arr_dt)
        
        dep_dt = row.get("Depart Time")
        dep_str = dep_dt.strftime("%d/%m/%Y %H:%M") if isinstance(dep_dt, datetime) else (str(dep_dt) if dep_dt is not None else None)
        
        load = FreightLoad(
            load_id=row["LoadId"],
            rake_id=row["RakeId"],
            source=row["Source"],
            destination=row["Destination"],
            load_type=row["Load Type"],
            total_km=float(row["Total Km"]),
            block_section=row["Block Section"],
            block_hrs=float(row["Block Hrs"]),
            speed=float(row["Speed"]),
            commodity=row["Commodity"],
            description=row.get("Description", ""),
            station=row["Sttn"],
            arrival_time=arr_str,
            depart_time=dep_str
        )
        freight_loads.append(load)
    
    return PipelineOutput(
        freight_loads=freight_loads,
        passenger_trains=passenger_trains,
        passenger_schedule_entries=passenger_entries,
        stations=stations,
        blocks=blocks,
        epoch_t0=epoch_t0,
        window_start_minutes=window_start_minutes,
        window_end_minutes=window_end_minutes,
        horizon_size_minutes=horizon_size_minutes,
    )


# ============================================================================
# Utility Functions
# ============================================================================

def seconds_to_minutes(seconds: float, epoch_seconds: float) -> int:
    """
    Convert absolute seconds-from-midnight to relative minutes from epoch.
    
    Args:
        seconds: Absolute seconds from midnight
        epoch_seconds: Epoch start in seconds from midnight
    
    Returns:
        Relative minute offset
    """
    return int((seconds - epoch_seconds) / 60)


def print_pipeline_summary(output: PipelineOutput) -> None:
    """
    Print a summary of loaded and preprocessed data.
    
    Args:
        output: PipelineOutput object from run_pipeline()
    """
    print("=" * 70)
    print("PIPELINE SUMMARY")
    print("=" * 70)
    print(f"Epoch (T0): {output.epoch_t0}")
    print(f"Active Window: [{output.window_start_minutes}, {output.window_end_minutes}) minutes")
    print(f"Horizon Size: {output.horizon_size_minutes} minutes")
    print()
    print(f"Freight Load Logs: {len(output.freight_loads)}")
    if output.freight_loads:
        commodities = {}
        for load in output.freight_loads:
            commodities[load.commodity] = commodities.get(load.commodity, 0) + 1
        print(f"  Unique commodities: {len(commodities)}")
        for commodity, count in sorted(commodities.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"    - {commodity}: {count}")
    print()
    print(f"Passenger Trains: {len(output.passenger_trains)}")
    if output.passenger_trains:
        by_type = {}
        for train in output.passenger_trains:
            by_type[train.train_type] = by_type.get(train.train_type, 0) + 1
        for train_type, count in sorted(by_type.items()):
            print(f"  - {train_type}: {count}")
    print()
    print(f"Passenger Schedule Entries: {len(output.passenger_schedule_entries)}")
    if output.passenger_schedule_entries:
        blocks_used = set(e.block_id for e in output.passenger_schedule_entries)
        print(f"  - Block sections with scheduled trains: {len(blocks_used)}")
    print()
    print(f"Stations: {len(output.stations)}")
    print(f"Block Sections: {len(output.blocks)}")
    print("=" * 70)
    print()


if __name__ == "__main__":
    """
    Example usage: load and preprocess corridor data for a 6-hour planning window.
    """
    import sys
    
    # Detect data directory (assumes script runs from project root or tests/)
    data_dir = Path(__file__).parent.parent / "data"
    if not data_dir.exists():
        data_dir = Path("data")
    
    if not data_dir.exists():
        print(f"Error: data directory not found at {data_dir}")
        sys.exit(1)
    
    # Run pipeline for the first 6-hour window
    try:
        output = run_pipeline(
            data_dir=data_dir,
            window_start_minutes=0,
            horizon_size_minutes=360,
        )
        print_pipeline_summary(output)
    except Exception as e:
        print(f"Pipeline error: {e}")
        sys.exit(1)

