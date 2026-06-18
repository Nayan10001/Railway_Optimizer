# Data Dictionary - KTV-PSA Corridor Scheduler

This document describes the schema of the CSV datasets stored in the `data/` directory.

## 1. Freight Data (`data/Freight/`)
* **File:** `goods_train.csv` (historical logs and active loads)
* **Fields:**
  - `train_id`: String, Unique identifier for the freight train
  - `commodity_type`: String, e.g. `CONT` (Container), `EMPTY` (Empty Hoppers), `IS` (Iron/Steel)
  - `length`: Float, Train length in meters
  - `weight`: Float, Gross trailing weight in metric tons
  - `max_speed`: Float, Maximum permissible speed in km/h

## 2. Passenger Data (`data/passenger/`)
* **File:** `schedule.csv` (deterministic passenger timetables)
* **Fields:**
  - `train_id`: String, Unique passenger train number/id
  - `train_name`: String, Name (e.g. Vande Bharat Express)
  - `block_id`: Integer, Block section index being traversed
  - `minutes_entry`: Integer, Relative minute offset of block entry (relative to $T_0$)
  - `minutes_exit`: Integer, Relative minute offset of block exit (relative to $T_0$)
  - `priority_class`: Integer, Priority tier (1 = Highest, e.g., Vande Bharat)

## 3. Infrastructure Data (`data/infrastructure/`)
* **File:** `stations.csv`
  - `station_id`: String, Short code (e.g. KTV, VZM, PSA)
  - `station_name`: String, Full station name
  - `loop_count`: Integer, Number of loops ($C_s^{\text{loop}}$)
  - `mancsr`: Float, Station Loop Clear Standing Length ($CSR_s$ in meters)
* **File:** `blocks.csv`
  - `block_id`: Integer, Unique identifier
  - `from_station`: String, Origin station ID
  - `to_station`: String, Destination station ID
  - `running_lines`: Integer, Total running tracks ($N_b$)
  - `length`: Float, Distance in kilometers
  - `headway`: Integer, Safety headway interval ($H_b$ in minutes)

## 4. Route Maps (`data/Route_Station/`)
* **File:** `route_map.csv`
  - `route_id`: String, Route ID
  - `station_sequence`: String, Ordered list of station IDs traversed by this route
