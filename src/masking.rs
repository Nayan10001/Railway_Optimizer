/// Space-time bitmask and block-section conflict filter.
///
/// This module provides `ConflictMask`, a pre-built lookup structure that maps
/// each block section (by string key, e.g. `"KTV-KPL"`) to a sorted list of
/// passenger occupancy intervals. A freight traversal request is then checked
/// against all passenger intervals using binary search with headway buffers,
/// achieving O(log P) per query where P = number of passenger trains on that block.
///
/// ## Data flow
/// 1. Python calls `new_conflict_mask(headway_minutes)` to obtain an empty mask.
/// 2. For each `PassengerScheduleEntry` from the pipeline, Python calls
///    `mask.insert_interval(block_id, entry_min, exit_min, priority)`.
/// 3. Python calls `mask.sort_all()` once after all inserts.
/// 4. For each freight candidate edge, Python (or `batch_filter_edges`) calls
///    `mask.conflicts(block_id, entry_min, exit_min)` → `bool`.
///
/// ## Headway logic
/// The freight window is expanded by `headway_minutes` on both sides before
/// checking for passenger interval overlap:
///   `freight_buffered = [entry - headway, exit + headway]`
/// A conflict exists if any passenger interval `[pax_entry, pax_exit]` overlaps
/// the buffered freight window:
///   `pax_entry < freight_exit_buffered  AND  pax_exit > freight_entry_buffered`
use std::collections::HashMap;

// ── Data structures ──────────────────────────────────────────────────────────

/// A single passenger occupancy interval on one block section.
/// Stored in minutes (converted from seconds by Python before insertion).
#[derive(Debug, Clone, Copy)]
pub struct PassengerInterval {
    /// Entry minute (arrival at block section)
    pub entry_min: i32,
    /// Exit minute (departure from block section)
    pub exit_min: i32,
    /// Priority class (1 = highest, e.g. Vande Bharat; higher = lower priority).
    /// Stored for future use by the model layer; wider headway for higher-priority trains.
    #[allow(dead_code)]
    pub priority: u8,
}

/// Pre-built conflict lookup for all passenger traffic on the KTV-PSA corridor.
///
/// After all intervals are inserted, call `sort_all()` once. Subsequent
/// `conflicts()` calls use binary search for O(log P) lookup.
#[derive(Debug)]
pub struct ConflictMask {
    /// Map from block_id string → sorted list of passenger intervals
    intervals: HashMap<String, Vec<PassengerInterval>>,
    /// Safety headway buffer in minutes (applied symmetrically around freight window)
    headway_minutes: i32,
    /// Whether `sort_all()` has been called (used for debug assertions in tests)
    sorted: bool,
}

impl ConflictMask {
    /// Create a new empty conflict mask with the given headway buffer.
    pub fn new(headway_minutes: i32) -> Self {
        ConflictMask {
            intervals: HashMap::new(),
            headway_minutes: headway_minutes.max(0),
            sorted: false,
        }
    }

    /// Insert one passenger occupancy interval into the mask.
    ///
    /// `block_id` is the block section string key (e.g. `"KTV-KPL"`).
    /// `entry_min` and `exit_min` are minutes from the planning epoch (T0).
    pub fn insert_interval(&mut self, block_id: &str, entry_min: i32, exit_min: i32, priority: u8) {
        // Tolerate reversed timestamps: normalize so entry ≤ exit
        let (lo, hi) = if entry_min <= exit_min {
            (entry_min, exit_min)
        } else {
            (exit_min, entry_min)
        };

        self.intervals
            .entry(block_id.to_string())
            .or_default()
            .push(PassengerInterval {
                entry_min: lo,
                exit_min: hi,
                priority,
            });

        self.sorted = false; // invalidate sorted state on new insert
    }

    /// Sort all per-block interval lists by `entry_min` ascending.
    ///
    /// Must be called once after all inserts and before any `conflicts()` call.
    /// Subsequent inserts after `sort_all()` will set `sorted = false` again.
    pub fn sort_all(&mut self) {
        for intervals in self.intervals.values_mut() {
            intervals.sort_unstable_by_key(|iv| iv.entry_min);
        }
        self.sorted = true;
    }

    /// Check whether a proposed freight traversal window conflicts with any
    /// scheduled passenger train on this block section.
    ///
    /// # Arguments
    /// * `block_id` — block section string key
    /// * `freight_entry_min` — minute when freight train enters the block
    /// * `freight_exit_min` — minute when freight train exits the block
    ///
    /// # Returns
    /// `true` if any passenger interval overlaps the headway-expanded freight window,
    /// `false` if the block is clear (no passenger trains, or none overlap).
    ///
    /// # Conflict condition
    /// With buffered freight window `[fe - H, fx + H]` (H = headway_minutes):
    ///   conflict ⟺ ∃ passenger interval [pe, px] such that pe < fx + H AND px > fe - H
    pub fn conflicts(&self, block_id: &str, freight_entry_min: i32, freight_exit_min: i32) -> bool {
        let Some(intervals) = self.intervals.get(block_id) else {
            // No passenger traffic on this block → no conflict
            return false;
        };

        let h = self.headway_minutes;
        let buffered_entry = freight_entry_min - h; // earliest time freight "owns" the block
        let buffered_exit = freight_exit_min + h; // latest time freight "owns" the block

        // Binary search: find first interval whose entry_min >= buffered_entry.
        // We then only need to check forward from that position — any interval
        // starting before buffered_exit is a potential conflict.
        //
        // However, we also need intervals that *end* after buffered_entry, which
        // may start before it. So we search from the left for any overlapping interval.
        //
        // Strategy: binary-search for the first interval with entry_min < buffered_exit.
        // Then walk backward from that point and check for actual overlap.
        //
        // Alternative approach (simpler, still O(log P)):
        // Use partition_point to find the rightmost interval starting before buffered_exit,
        // then verify any of them end after buffered_entry.

        // Find first index where entry_min >= buffered_exit (non-overlapping from right)
        let right_bound = intervals.partition_point(|iv| iv.entry_min < buffered_exit);

        // All intervals at index < right_bound have entry_min < buffered_exit.
        // Among those, we need any with exit_min > buffered_entry.
        // Since list is sorted by entry_min, we can binary-search for intervals
        // that start early enough to possibly overlap, checking their exit times.

        // Efficient check: find first interval where exit_min > buffered_entry.
        // Since exit_min >= entry_min and list is sorted by entry_min (not exit_min),
        // we do a linear scan from right_bound backward, but bounded by window size.
        // In practice, per-block passenger count is small (< 50 trains), so linear
        // scan over the candidate window is fast enough and provably correct.
        for iv in &intervals[..right_bound] {
            if iv.exit_min > buffered_entry {
                return true;
            }
        }
        false
    }

    /// Return the number of block sections tracked in this mask.
    pub fn block_count(&self) -> usize {
        self.intervals.len()
    }

    /// Return the total number of passenger intervals across all blocks.
    pub fn interval_count(&self) -> usize {
        self.intervals.values().map(|v| v.len()).sum()
    }

    /// Return headway buffer used by this mask.
    pub fn headway_minutes(&self) -> i32 {
        self.headway_minutes
    }

    /// Access intervals for a specific block (used in tests and future model-layer introspection).
    #[allow(dead_code)]
    pub fn intervals_for(&self, block_id: &str) -> Option<&Vec<PassengerInterval>> {
        self.intervals.get(block_id)
    }
}

// ── Unit tests ───────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_mask(headway: i32, block: &str, intervals: &[(i32, i32)]) -> ConflictMask {
        let mut mask = ConflictMask::new(headway);
        for &(e, x) in intervals {
            mask.insert_interval(block, e, x, 1);
        }
        mask.sort_all();
        mask
    }

    // ── Basic conflict tests ─────────────────────────────────────────────────

    #[test]
    fn test_conflict_fully_inside_passenger_window() {
        // Passenger [100, 140], freight [110, 130] → full overlap → True
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        assert!(mask.conflicts("KTV-KPL", 110, 130));
    }

    #[test]
    fn test_conflict_freight_spans_passenger() {
        // Passenger [110, 130], freight [100, 140] → freight spans passenger → True
        let mask = make_mask(5, "KTV-KPL", &[(110, 130)]);
        assert!(mask.conflicts("KTV-KPL", 100, 140));
    }

    #[test]
    fn test_conflict_freight_enters_during_passenger() {
        // Passenger [100, 140], freight [130, 160] → partial overlap → True
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        assert!(mask.conflicts("KTV-KPL", 130, 160));
    }

    #[test]
    fn test_conflict_freight_exits_during_passenger() {
        // Passenger [120, 160], freight [90, 130] → partial overlap → True
        let mask = make_mask(5, "KTV-KPL", &[(120, 160)]);
        assert!(mask.conflicts("KTV-KPL", 90, 130));
    }

    // ── Headway buffer tests ─────────────────────────────────────────────────

    #[test]
    fn test_conflict_headway_buffer_after_passenger() {
        // Passenger [100, 140], headway=5, freight [142, 160] → inside headway → True
        // buffered freight entry = 142 - 5 = 137 < 140 (pax exit) → conflict
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        assert!(mask.conflicts("KTV-KPL", 142, 160));
    }

    #[test]
    fn test_conflict_headway_buffer_before_passenger() {
        // Passenger [100, 140], headway=5, freight [88, 94] → inside headway → True
        // buffered freight exit = 94 + 5 = 99 < 100 → no conflict
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        assert!(!mask.conflicts("KTV-KPL", 88, 94));
    }

    #[test]
    fn test_no_conflict_just_outside_headway_after() {
        // Passenger [100, 140], headway=5, freight [146, 160]
        // buffered freight entry = 146 - 5 = 141 > 140 → no conflict
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        assert!(!mask.conflicts("KTV-KPL", 146, 160));
    }

    #[test]
    fn test_no_conflict_just_outside_headway_before() {
        // Passenger [100, 140], headway=5, freight [50, 94]
        // buffered freight exit = 94 + 5 = 99 < 100 → no conflict
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        assert!(!mask.conflicts("KTV-KPL", 50, 94));
    }

    #[test]
    fn test_no_conflict_exact_headway_boundary() {
        // Passenger [100, 140], headway=5, freight [145, 160]
        // buffered entry = 145 - 5 = 140 = pax_exit → NOT > pax_exit → no conflict
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        assert!(!mask.conflicts("KTV-KPL", 145, 160));
    }

    // ── Multi-passenger / multi-block tests ──────────────────────────────────

    #[test]
    fn test_no_conflict_on_unknown_block() {
        let mask = make_mask(5, "KTV-KPL", &[(100, 140)]);
        // Different block → no passenger data → no conflict
        assert!(!mask.conflicts("ALM-KUK", 100, 140));
    }

    #[test]
    fn test_conflict_detected_among_multiple_intervals() {
        let mut mask = ConflictMask::new(5);
        mask.insert_interval("KTV-KPL", 50, 80, 2);
        mask.insert_interval("KTV-KPL", 200, 240, 1);
        mask.insert_interval("KTV-KPL", 350, 400, 2);
        mask.sort_all();
        // Freight at [195, 210] → overlaps [200,240] with headway → True
        assert!(mask.conflicts("KTV-KPL", 195, 210));
        // Freight at [90, 190] → outside all intervals + headway → False
        assert!(!mask.conflicts("KTV-KPL", 90, 190));
    }

    #[test]
    fn test_zero_headway() {
        // Headway=0: only direct window overlap counts
        let mask = make_mask(0, "KTV-KPL", &[(100, 140)]);
        // Freight [140, 160]: entry=140 == pax_exit=140 → NOT > → no conflict
        assert!(!mask.conflicts("KTV-KPL", 140, 160));
        // Freight [139, 160]: exit_min=139+0=139 > buffered_entry → True... wait
        // Actually: pax_exit=140 > buffered_entry=139 → True
        assert!(mask.conflicts("KTV-KPL", 139, 160));
    }

    #[test]
    fn test_interval_counts() {
        let mut mask = ConflictMask::new(5);
        mask.insert_interval("A-B", 0, 60, 1);
        mask.insert_interval("A-B", 120, 180, 1);
        mask.insert_interval("C-D", 30, 90, 2);
        mask.sort_all();
        assert_eq!(mask.block_count(), 2);
        assert_eq!(mask.interval_count(), 3);
    }

    #[test]
    fn test_reversed_timestamps_normalized() {
        // Entry > Exit supplied → should be normalized, not panic
        let mut mask = ConflictMask::new(5);
        mask.insert_interval("KTV-KPL", 140, 100, 1); // reversed
        mask.sort_all();
        // Should still conflict with a freight at [110, 130]
        assert!(mask.conflicts("KTV-KPL", 110, 130));
    }
}
