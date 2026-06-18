r"""
Smoke test for the Rust _native extension module.
Run: .venv\Scripts\python.exe tests\test_native_smoke.py
"""
from ktv_psa_scheduler import _native as native

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def check(label, condition):
    status = PASS if condition else FAIL
    print(f"  {label:45s} {status}")
    return condition


all_ok = True

# ── ConflictMask ─────────────────────────────────────────────────────────────
print("=== ConflictMask ===")
mask = native.new_conflict_mask(5)
mask.insert_interval("KTV-KPL", 100, 140, 1)
mask.sort_all()

all_ok &= check("inside window [110,130] -> True",  mask.conflicts("KTV-KPL", 110, 130) is True)
all_ok &= check("before-headway [50,90]  -> False", mask.conflicts("KTV-KPL", 50, 90)   is False)
all_ok &= check("just past headway [146,160] -> False", mask.conflicts("KTV-KPL", 146, 160) is False)
all_ok &= check("inside headway [142,160] -> True", mask.conflicts("KTV-KPL", 142, 160) is True)
all_ok &= check("unknown block ALM-KUK   -> False", mask.conflicts("ALM-KUK", 100, 140) is False)
all_ok &= check("block_count() == 1",                mask.block_count() == 1)
all_ok &= check("interval_count() == 1",             mask.interval_count() == 1)
all_ok &= check("headway_minutes() == 5",            mask.headway_minutes() == 5)
print(f"  repr: {repr(mask)}")

# ── compute_travel_time ──────────────────────────────────────────────────────
# Alias so test calls match the real module symbol
compute_travel_time = native.compute_travel_time_py

print("\n=== compute_travel_time ===")
t1 = compute_travel_time(7.74, 60.0, -1.0, "[]", "[]")
t2 = compute_travel_time(10.0, 80.0, 40.0, "[]", "[]")
t3 = compute_travel_time(
    10.0, 60.0, -1.0,
    '[{"dist_m": 10000.0, "grade": "RISE", "val": 100.0}]',
    "[]"
)
t4 = compute_travel_time(0.01, 120.0, -1.0, "[]", "[]")
t5 = compute_travel_time(100.0, 0.0, -1.0, "[]", "[]")

all_ok &= check(f"7.74km @60kmh -> {t1:.3f} min  (expect ~7.74)",  7.0 < t1 < 8.5)
all_ok &= check(f"10km @80kmh PSR=40 -> {t2:.3f} min (expect ~15)", 14.5 < t2 < 15.5)
all_ok &= check(f"10km @60 RISE 1:100 -> {t3:.3f} min (expect >10)", t3 > 10.0)
all_ok &= check(f"0.01km @120 -> {t4:.3f} min (expect 1.0 clamp)", t4 == 1.0)
all_ok &= check(f"100km @0kmh -> {t5:.1f} min (expect 480.0 clamp)", t5 == 480.0)

# Curve resistance test
t6 = compute_travel_time(
    10.0, 60.0, -1.0, "[]",
    '[{"dist_m": 5000.0, "radius_m": 200.0}]'
)
t_straight = compute_travel_time(10.0, 60.0, -1.0, "[]", "[]")
all_ok &= check(f"tight curve R=200m -> {t6:.3f} > straight {t_straight:.3f}", t6 > t_straight)

# bad JSON
try:
    compute_travel_time(10.0, 60.0, -1.0, "[bad json]", "[]")
    all_ok &= check("bad gradient JSON raises ValueError", False)
except ValueError:
    all_ok &= check("bad gradient JSON raises ValueError", True)

# ── batch_filter_edges ───────────────────────────────────────────────────────
print("\n=== batch_filter_edges ===")
mask2 = native.new_conflict_mask(5)
mask2.insert_interval("KTV-KPL", 200, 250, 1)
mask2.sort_all()

candidates = [
    ("KTV-KPL", 100, 140),   # feasible — before passenger window
    ("KTV-KPL", 210, 240),   # conflict — inside passenger window
    ("KTV-KPL", 252, 270),   # conflict — inside headway buffer (252-5=247 < 250)
    ("KTV-KPL", 256, 280),   # feasible — just past headway (256-5=251 > 250)
    ("ALM-KUK",  50,  90),   # feasible — different block, no passengers
]
block_lengths = {"KTV-KPL": 7.74, "ALM-KUK": 7.11}
block_speeds  = {"KTV-KPL": 60.0, "ALM-KUK": 60.0}

edges = native.batch_filter_edges(candidates, mask2, block_lengths, block_speeds)

for i, e in enumerate(edges):
    print(f"  [{i}] {repr(e)}")

all_ok &= check("edges[0] feasible (before pax)", edges[0].feasible is True)
all_ok &= check("edges[1] conflict (inside pax)", edges[1].feasible is False)
all_ok &= check("edges[2] conflict (in headway)", edges[2].feasible is False)
all_ok &= check("edges[3] feasible (past headway)", edges[3].feasible is True)
all_ok &= check("edges[4] feasible (other block)", edges[4].feasible is True)
all_ok &= check("edges[0] travel_time > 0", edges[0].travel_minutes > 0)
all_ok &= check("edges[0] block_id == KTV-KPL", edges[0].block_id == "KTV-KPL")
all_ok &= check("len(edges) == 5", len(edges) == 5)

# ── Module metadata ──────────────────────────────────────────────────────────
print("\n=== Module metadata ===")
all_ok &= check("__version__ == '0.1.0'", native.__version__ == "0.1.0")

# ── Final result ─────────────────────────────────────────────────────────────
print()
if all_ok:
    print("\033[92m[OK] ALL SMOKE TESTS PASSED\033[0m")
else:
    import sys
    print("\033[91m[FAIL] SOME TESTS FAILED\033[0m")
    sys.exit(1)
