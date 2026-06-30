import os
import sys
import traceback

import numpy as np

WORK_DIR = "work"

# ---------------------------------------------------------------------------
# test_hspice_read.py
#
# Post-implementation regression test for the Python hspice_read() function.
# Must be run from the sims/ directory after simulations have been generated.
#
# Imports hspice_read from PySpice.Spice.HSpice.hspicefile and asserts the
# full return structure for every binary file in the simulation set.
#
# Return structure (from C implementation):
#   hspice_read(filename) -> list[result]
#   result = tuple(sweeps, scale_name, None, title, date, None)
#   sweeps = tuple(sweep_var_name_or_None, sweep_values_ndarray_or_None, data_list)
#   data_list = list[dict{ var_name: numpy.ndarray, ... }]
#
# Variable name normalisation (applied by the C parser):
#   - All names lowercased.
#   - "v(node_a)" -> "node_a"  (v( prefix and ) suffix stripped)
#   - "i(v1)"     -> "i(v1"    (i( prefix retained, closing paren not stripped)
#   - Scale names (TIME, HERTZ, VOLTS, DEG_C, rval...) returned as-is lowercased.
#
# Arrays are numpy.ndarray:
#   - Real variables:    dtype float64 (NPY_DOUBLE)
#   - Complex variables: dtype complex128 (NPY_CDOUBLE)
#
# Logs to logs/test_hspice_read.log.
# ---------------------------------------------------------------------------

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))
)

from PySpice.Spice.HSpice.hspicefile import hspice_read

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _close(a, b, rtol=1e-3):
    """Relative tolerance float comparison."""
    if b == 0:
        return abs(a) < 1e-30
    return abs(a - b) / abs(b) < rtol


def _find(data_dict, partial):
    """Find a key that equals or starts with `partial` (handles truncated names)."""
    for k in data_dict:
        if k == partial or k.startswith(partial):
            return k
    return None


def _unpack(result):
    """
    Unpack the hspice_read return value into a convenient structure.

    Returns:
      title:       str
      date:        str
      scale_name:  str  (the independent variable name, lowercased)
      sweep_name:  str or None  (outer sweep variable, e.g. 'rval')
      sweep_vals:  numpy.ndarray or None  (outer sweep point values)
      data_list:   list[dict]  (one dict per sweep point)
    """
    assert (
        isinstance(result, list) and len(result) == 1
    ), f"Expected list of length 1, got {type(result)} len={len(result)}"
    entry = result[0]
    assert (
        isinstance(entry, tuple) and len(entry) == 6
    ), f"Expected 6-tuple, got {type(entry)} len={len(entry)}"
    sweeps, scale_name, _, title, date, _ = entry
    assert (
        isinstance(sweeps, tuple) and len(sweeps) == 3
    ), f"Expected 3-tuple sweeps, got {type(sweeps)} len={len(sweeps)}"
    sweep_name, sweep_vals, data_list = sweeps
    assert isinstance(
        data_list, list
    ), f"data_list should be list, got {type(data_list)}"
    return title, date, scale_name, sweep_name, sweep_vals, data_list


# ---------------------------------------------------------------------------
# Test case factory
# ---------------------------------------------------------------------------


def make_tests():
    """
    Return list of (filename, case_name, assertion_fn).
    assertion_fn(title, date, scale_name, sweep_name, sweep_vals, data_list)
      -> list[str] of error messages (empty = PASS)
    """
    tests = []

    # ------------------------------------------------------------------
    # DC BASIC: v1 swept 1..5 step 1 => 1 table, 5 rows, scale=VOLTS
    # data_list[0] has keys: scale_name, 'node_a', possibly '0'
    # ------------------------------------------------------------------
    def dc_basic(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()

        if sn_lower in t:
            sn = sn_lower
        elif sn_upper in t:
            sn = sn_upper
        else:
            errs.append(f"Scale '{scale_name}' not found (keys={list(t.keys())})")
            return errs

        arr = t[sn]
        if not isinstance(arr, np.ndarray):
            errs.append(f"Scale array is {type(arr)}, expected numpy.ndarray")
        if len(arr) != 5:
            errs.append(f"Expected 5 rows, got {len(arr)}")
        if arr.dtype not in (np.float64, np.float32):
            errs.append(f"Scale dtype {arr.dtype}, expected float64")
        if not _close(float(arr[0]), 1.0):
            errs.append(f"arr[0]={arr[0]}, expected 1.0")
        if not _close(float(arr[-1]), 5.0):
            errs.append(f"arr[-1]={arr[-1]}, expected 5.0")
        node_a = _find(t, "node_a")
        if node_a is None:
            errs.append(f"'node_a' variable not found in keys={list(t.keys())}")
        else:
            if not _close(float(t[node_a][0]), 1.0, rtol=1e-4):
                errs.append(f"v(node_a)[0]={t[node_a][0]}, expected ~1.0")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"dc_basic_{version}.sw0", f"dc_basic_{version}", dc_basic))

    # ------------------------------------------------------------------
    # DC SWEEP TEMP: .dc temp -40 120 40 => 1 table, 5 rows, scale=deg_c
    # Temperature is the primary DC sweep axis, not an outer parameter.
    # ------------------------------------------------------------------
    def dc_sweep_temp(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()

        if sn_lower in t:
            sn = sn_lower
        elif sn_upper in t:
            sn = sn_upper
        else:
            errs.append(f"Scale '{scale_name}' not found (keys={list(t.keys())})")
            return errs

        arr = t[sn]
        if len(arr) != 5:
            errs.append(f"Expected 5 rows, got {len(arr)}")
        if not _close(float(arr[0]), -40.0, rtol=1e-3):
            errs.append(f"arr[0]={arr[0]}, expected -40.0")
        if not _close(float(arr[-1]), 120.0, rtol=1e-3):
            errs.append(f"arr[-1]={arr[-1]}, expected 120.0")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (f"dc_sweep_temp_{version}.sw0", f"dc_sweep_temp_{version}", dc_sweep_temp)
        )

    # ------------------------------------------------------------------
    # DC SWEEP PARAM: .dc rval 10 50 10 => 1 table, 5 rows, scale=rval
    # rval is the primary DC sweep axis, not an outer parameter.
    # ------------------------------------------------------------------
    def dc_sweep_param(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()

        if sn_lower in t:
            sn = sn_lower
        elif sn_upper in t:
            sn = sn_upper
        else:
            errs.append(f"Scale '{scale_name}' not found (keys={list(t.keys())})")
            return errs

        arr = t[sn]
        if len(arr) != 5:
            errs.append(f"Expected 5 rows, got {len(arr)}")
        if not _close(float(arr[0]), 10.0, rtol=1e-3):
            errs.append(f"arr[0]={arr[0]}, expected 10.0")
        if not _close(float(arr[-1]), 50.0, rtol=1e-3):
            errs.append(f"arr[-1]={arr[-1]}, expected 50.0")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_sweep_param_{version}.sw0",
                f"dc_sweep_param_{version}",
                dc_sweep_param,
            )
        )

    # ------------------------------------------------------------------
    # DC MONTE: 5 Monte Carlo runs, each with 5 rows (v1 1..5 step 1)
    # data_list has 5 entries (one per Monte Carlo sample)
    # ------------------------------------------------------------------
    def dc_monte(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 5:
            errs.append(f"Expected 5 Monte Carlo tables, got {len(data_list)}")
            return errs
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()

        for i, t in enumerate(data_list):
            if sn_lower in t:
                sn = sn_lower
            elif sn_upper in t:
                sn = sn_upper
            else:
                errs.append(f"Scale '{scale_name}' not found (keys={list(t.keys())})")
                return errs

            arr = t[sn]
            if not isinstance(arr, np.ndarray):
                errs.append(f"Table {i}: scale is {type(arr)}, expected ndarray")
            if len(arr) != 5:
                errs.append(f"Table {i}: expected 5 rows, got {len(arr)}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"dc_monte_{version}.sw0", f"dc_monte_{version}", dc_monte))

    # ------------------------------------------------------------------
    # DC NESTED SWEEP: .dc v1 1 5 1 sweep rval 10 30 10
    # => sweeps tuple has sweep_name='rval', sweep_vals=[10,20,30]
    #    data_list has 3 entries, each with 5 rows
    # ------------------------------------------------------------------
    def dc_nested_sweep(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if sweep_name is None:
            errs.append("Expected outer sweep variable, got None")
        else:
            if sweep_name.lower() != "rval":
                errs.append(f"Expected sweep_name='rval', got '{sweep_name}'")
        if sweep_vals is None:
            errs.append("Expected sweep_vals array, got None")
        else:
            if not isinstance(sweep_vals, np.ndarray):
                errs.append(f"sweep_vals is {type(sweep_vals)}, expected ndarray")
            elif len(sweep_vals) != 3:
                errs.append(f"Expected 3 sweep_vals, got {len(sweep_vals)}")
            else:
                for i, ev in enumerate([10.0, 20.0, 30.0]):
                    if not _close(float(sweep_vals[i]), ev, rtol=1e-3):
                        errs.append(f"sweep_vals[{i}]={sweep_vals[i]}, expected {ev}")
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables (one per rval), got {len(data_list)}")
            return errs
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()

        for i, t in enumerate(data_list):
            if sn_lower in t:
                sn = sn_lower
            elif sn_upper in t:
                sn = sn_upper
            else:
                errs.append(f"Table {i}: scale '{sn}' missing")
                continue
            if len(t[sn]) != 5:
                errs.append(f"Table {i}: expected 5 rows (v1 1..5), got {len(t[sn])}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_nested_sweep_{version}.sw0",
                f"dc_nested_sweep_{version}",
                dc_nested_sweep,
            )
        )

    # ------------------------------------------------------------------
    # DC PROBES ONLY: no sweep, explicit probes:
    #   v(node_a), v(node_b), v(node_a,node_b), i(v1), i(r1)
    # scale=VOLTS, 1 table, 5 rows
    # C parser strips v(...) => "node_a", "node_b", "node_a,node_b"
    # ------------------------------------------------------------------
    def dc_probes_only(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        for p in ["node_a", "node_b", "i(v1", "i(r1"]:
            if _find(t, p) is None:
                errs.append(f"Expected probe '{p}' not found. Keys: {list(t.keys())}")
        for k, v in t.items():
            if not isinstance(v, np.ndarray):
                errs.append(f"'{k}' is {type(v)}, expected ndarray")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_probes_only_{version}.sw0",
                f"dc_probes_only_{version}",
                dc_probes_only,
            )
        )

    # ------------------------------------------------------------------
    # DC PROBE AND SWEEP: rval sweep 10..30 step 10 + probes v(node_a), v(node_b)
    # => 3 outer sweep tables; each dict has 'node_a', 'node_b'
    # ------------------------------------------------------------------
    def dc_probe_and_sweep(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        for i, t in enumerate(data_list):
            for p in ["node_a", "node_b"]:
                if _find(t, p) is None:
                    errs.append(f"Table {i}: '{p}' not found. Keys: {list(t.keys())}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_probe_and_sweep_{version}.sw0",
                f"dc_probe_and_sweep_{version}",
                dc_probe_and_sweep,
            )
        )

    # ------------------------------------------------------------------
    # AC BASIC: dec 5 100 10k, no sweep => 1 table, complex circuit vars
    # scale=hertz (or 'freq'), freq range 100..10000 Hz
    # ------------------------------------------------------------------
    def ac_basic(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()

        if sn_lower in t:
            sn = sn_lower
        elif sn_upper in t:
            sn = sn_upper
        else:
            errs.append(f"Scale '{scale_name}' not found (keys={list(t.keys())})")
            return errs

        freqs = t[sn]
        if len(freqs) < 5:
            errs.append(f"Too few frequency points: {len(freqs)}")
        if not _close(float(freqs[0]), 100.0, rtol=0.02):
            errs.append(f"freqs[0]={freqs[0]}, expected ~100 Hz")
        if not _close(float(freqs[-1]), 10000.0, rtol=0.02):
            errs.append(f"freqs[-1]={freqs[-1]}, expected ~10000 Hz")
        # All circuit vars must be complex128
        for k, v in t.items():
            if k == sn:
                continue
            if not isinstance(v, np.ndarray):
                errs.append(f"'{k}' is {type(v)}, expected ndarray")
                continue
            if v.dtype != np.complex128:
                errs.append(f"AC var '{k}' dtype={v.dtype}, expected complex128")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"ac_basic_{version}.ac0", f"ac_basic_{version}", ac_basic))

    # ------------------------------------------------------------------
    # AC SWEEP PARAM: rval 10 30 10, 3 outer tables, each with freq points
    # ------------------------------------------------------------------
    def ac_sweep_param(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        if sweep_vals is None:
            errs.append("Expected sweep_vals, got None")
        elif len(sweep_vals) != 3:
            errs.append(f"Expected 3 sweep_vals, got {len(sweep_vals)}")
        else:
            for i, ev in enumerate([10.0, 20.0, 30.0]):
                if not _close(float(sweep_vals[i]), ev, rtol=1e-3):
                    errs.append(f"sweep_vals[{i}]={sweep_vals[i]}, expected {ev}")
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()
        for i, t in enumerate(data_list):
            if sn_lower in t:
                sn = sn_lower
            elif sn_upper in t:
                sn = sn_upper
            else:
                errs.append(f"Table {i}: scale '{sn}' missing")
                continue
            if len(t[sn]) < 5:
                errs.append(f"Table {i}: too few freq points ({len(t[sn])})")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_sweep_param_{version}.ac0",
                f"ac_sweep_param_{version}",
                ac_sweep_param,
            )
        )

    # ------------------------------------------------------------------
    # AC SWEEP TEMP: temp -40 120 40, 5 outer tables, complex circuit vars
    # ------------------------------------------------------------------
    def ac_sweep_temp(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 5:
            errs.append(f"Expected 5 tables, got {len(data_list)}")
            return errs
        if sweep_vals is not None and len(sweep_vals) == 5:
            for i, ev in enumerate([-40.0, 0.0, 40.0, 80.0, 120.0]):
                if not _close(float(sweep_vals[i]), ev, rtol=1e-3):
                    errs.append(f"sweep_vals[{i}]={sweep_vals[i]}, expected {ev}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (f"ac_sweep_temp_{version}.ac0", f"ac_sweep_temp_{version}", ac_sweep_temp)
        )

    # ------------------------------------------------------------------
    # AC PROBES ONLY: explicit probes, 1 table, all circuit vars complex128
    # ------------------------------------------------------------------
    def ac_probes_only(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()
        for k, v in t.items():
            if k == sn_lower or k == sn_upper:
                continue
            if isinstance(v, np.ndarray) and v.dtype != np.complex128:
                errs.append(f"AC probe '{k}' dtype={v.dtype}, expected complex128")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_probes_only_{version}.ac0",
                f"ac_probes_only_{version}",
                ac_probes_only,
            )
        )

    # ------------------------------------------------------------------
    # TRAN BASIC: time 0..20ns step 1ns, 1 table, real (float64) vars
    # ------------------------------------------------------------------
    def tran_basic(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()
        if sn_lower in t:
            sn = sn_lower
        elif sn_upper in t:
            sn = sn_upper
        else:
            errs.append(f"Scale '{sn}' not found (keys={list(t.keys())})")
            return errs
        times = t[sn]
        if len(times) < 5:
            errs.append(f"Too few time points: {len(times)}")
        if float(times[0]) > 1e-12:
            errs.append(f"times[0]={times[0]}, expected ~0")
        if not _close(float(times[-1]), 20e-9, rtol=0.01):
            errs.append(f"times[-1]={times[-1]:.4e}, expected ~20ns")
        for k, v in t.items():
            if k == sn:
                continue
            if isinstance(v, np.ndarray) and v.dtype == np.complex128:
                errs.append(f"Tran var '{k}' is complex; should be real float64")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"tran_basic_{version}.tr0", f"tran_basic_{version}", tran_basic))

    # ------------------------------------------------------------------
    # TRAN SWEEP TEMP: temp -40..120 step 40, 5 outer tables
    # ------------------------------------------------------------------
    def tran_sweep_temp(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 5:
            errs.append(f"Expected 5 tables, got {len(data_list)}")
            return errs
        if sweep_vals is not None and len(sweep_vals) == 5:
            for i, ev in enumerate([-40.0, 0.0, 40.0, 80.0, 120.0]):
                if not _close(float(sweep_vals[i]), ev, rtol=1e-3):
                    errs.append(f"sweep_vals[{i}]={sweep_vals[i]}, expected {ev}")
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()
        for i, t in enumerate(data_list):
            if sn_lower in t:
                sn = sn_lower
            elif sn_upper in t:
                sn = sn_upper
            else:
                errs.append(f"Table {i}: scale '{scale_name}' missing")
                continue
            if len(t[sn]) < 2:
                errs.append(f"Table {i}: too few time points")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_sweep_temp_{version}.tr0",
                f"tran_sweep_temp_{version}",
                tran_sweep_temp,
            )
        )

    # ------------------------------------------------------------------
    # TRAN SWEEP PARAM: rval 10..30 step 10, 3 outer tables
    # ------------------------------------------------------------------
    def tran_sweep_param(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        if sweep_vals is not None and len(sweep_vals) == 3:
            for i, ev in enumerate([10.0, 20.0, 30.0]):
                if not _close(float(sweep_vals[i]), ev, rtol=1e-3):
                    errs.append(f"sweep_vals[{i}]={sweep_vals[i]}, expected {ev}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_sweep_param_{version}.tr0",
                f"tran_sweep_param_{version}",
                tran_sweep_param,
            )
        )

    # ------------------------------------------------------------------
    # TRAN SWEEP SOURCE: v2 1..3 step 1, 3 outer tables
    # ------------------------------------------------------------------
    def tran_sweep_source(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        if sweep_vals is not None and len(sweep_vals) == 3:
            for i, ev in enumerate([1.0, 2.0, 3.0]):
                if not _close(float(sweep_vals[i]), ev, rtol=1e-3):
                    errs.append(f"sweep_vals[{i}]={sweep_vals[i]}, expected {ev}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_sweep_source_{version}.tr0",
                f"tran_sweep_source_{version}",
                tran_sweep_source,
            )
        )

    # ------------------------------------------------------------------
    # TRAN PROBE AND SWEEP: rval 10..30 step 10 + probes v(node_a) v(node_b)
    # => 3 tables, each dict has 'node_a', 'node_b' keys
    # ------------------------------------------------------------------
    def tran_probe_and_sweep(
        title, date, scale_name, sweep_name, sweep_vals, data_list
    ):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        for i, t in enumerate(data_list):
            for p in ["node_a", "node_b"]:
                if _find(t, p) is None:
                    errs.append(f"Table {i}: '{p}' not found. Keys: {list(t.keys())}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_probe_and_sweep_{version}.tr0",
                f"tran_probe_and_sweep_{version}",
                tran_probe_and_sweep,
            )
        )

    # ------------------------------------------------------------------
    # TRAN MONTE: 5 Monte Carlo runs, each with time-domain real data
    # ------------------------------------------------------------------
    def tran_monte(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 5:
            errs.append(f"Expected 5 Monte Carlo tables, got {len(data_list)}")
            return errs
        sn_lower = scale_name.lower()
        sn_upper = scale_name.upper()
        for i, t in enumerate(data_list):
            if sn_lower in t:
                sn = sn_lower
            elif sn_upper in t:
                sn = sn_upper
            else:
                errs.append(f"Table {i}: scale '{scale_name}' missing")
                continue
            if len(t[sn]) < 2:
                errs.append(f"Table {i}: too few time points ({len(t[sn])})")
            for k, v in t.items():
                if k == sn:
                    continue
                if isinstance(v, np.ndarray) and v.dtype == np.complex128:
                    errs.append(f"Table {i}: var '{k}' is complex; should be real")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"tran_monte_{version}.tr0", f"tran_monte_{version}", tran_monte))

    # ------------------------------------------------------------------
    # AC SWEEP TEMP: 5 temperature sweep points (-40 to 120 step 40)
    # ------------------------------------------------------------------
    def ac_sweep_temp(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 5:
            errs.append(f"Expected 5 tables, got {len(data_list)}")
            return errs
        for i, t in enumerate(data_list):
            sn = _find(t, scale_name)
            if sn is None:
                errs.append(f"Table {i}: scale '{scale_name}' missing")
                continue
            if len(t[sn]) != 11:
                errs.append(f"Table {i}: expected 11 frequency points, got {len(t[sn])}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_sweep_temp_{version}.ac0",
                f"ac_sweep_temp_{version}",
                ac_sweep_temp,
            )
        )

    # ------------------------------------------------------------------
    # AC PROBE AND SWEEP: 3 parameter sweep points (rval 10..30 step 10)
    # ------------------------------------------------------------------
    def ac_probe_and_sweep(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        for i, t in enumerate(data_list):
            sn = _find(t, scale_name)
            if sn is None:
                errs.append(f"Table {i}: scale '{scale_name}' missing")
                continue
            if len(t[sn]) != 11:
                errs.append(f"Table {i}: expected 11 frequency points, got {len(t[sn])}")
            for p in ["node_a", "node_b"]:
                pn = _find(t, p)
                if pn is None:
                    errs.append(f"Table {i}: '{p}' probe missing")
                elif t[pn].dtype != np.complex128:
                    errs.append(f"Table {i}: '{pn}' expected complex128, got {t[pn].dtype}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_probe_and_sweep_{version}.ac0",
                f"ac_probe_and_sweep_{version}",
                ac_probe_and_sweep,
            )
        )

    # ------------------------------------------------------------------
    # TRAN PROBES ONLY: transient analysis with probe statements only
    # ------------------------------------------------------------------
    def tran_probes_only(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        sn = _find(t, scale_name)
        if sn is None:
            errs.append(f"Scale '{scale_name}' missing")
        if len(t) < 3:
            errs.append(f"Expected multiple probes, got keys {list(t.keys())}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_probes_only_{version}.tr0",
                f"tran_probes_only_{version}",
                tran_probes_only,
            )
        )

    # ------------------------------------------------------------------
    # DC MEAS / SWEEP MEAS: files containing measurements
    # ------------------------------------------------------------------
    def dc_meas(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        if len(t) < 2:
            errs.append("No circuit variables found")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"dc_meas_{version}.sw0", f"dc_meas_{version}", dc_meas))

    def ac_meas(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        if len(t) < 2:
            errs.append("No circuit variables found")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"ac_meas_{version}.ac0", f"ac_meas_{version}", ac_meas))

    def tran_meas(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 1:
            errs.append(f"Expected 1 table, got {len(data_list)}")
            return errs
        t = data_list[0]
        if len(t) < 2:
            errs.append("No circuit variables found")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append((f"tran_meas_{version}.tr0", f"tran_meas_{version}", tran_meas))

    def dc_meas_sweep(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_meas_sweep_{version}.sw0",
                f"dc_meas_sweep_{version}",
                dc_meas_sweep,
            )
        )

    def ac_meas_sweep(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_meas_sweep_{version}.ac0",
                f"ac_meas_sweep_{version}",
                ac_meas_sweep,
            )
        )

    def tran_meas_sweep(title, date, scale_name, sweep_name, sweep_vals, data_list):
        errs = []
        if len(data_list) != 3:
            errs.append(f"Expected 3 tables, got {len(data_list)}")
            return errs
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_meas_sweep_{version}.tr0",
                f"tran_meas_sweep_{version}",
                tran_meas_sweep,
            )
        )

    return tests


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main():
    logs_dir = "logs"
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "test_hspice_read.log")

    with open(log_path, "w") as lf:

        def log(msg):
            print(msg)
            lf.write(msg + "\n")
            lf.flush()

        log("=" * 70)
        log("hspice_read() Post-Implementation Test Suite")
        log("=" * 70)
        log(f"Work directory: {os.path.abspath(WORK_DIR)}")

        tests = make_tests()
        passed = 0
        failed = 0
        skipped = 0

        for filename, case_name, assertion_fn in tests:
            log(f"\n--- {case_name} ---")
            log(f"    File: {filename}")

            full_path = os.path.join(WORK_DIR, filename)
            if not os.path.exists(full_path):
                log(f"    SKIP: file not found ({full_path})")
                skipped += 1
                continue

            try:
                result = hspice_read(full_path)
            except Exception as e:
                log(f"    FAIL: hspice_read raised {type(e).__name__}: {e}")
                log(traceback.format_exc())
                failed += 1
                continue

            try:
                title, date, scale_name, sweep_name, sweep_vals, data_list = _unpack(
                    result
                )
            except AssertionError as e:
                log(f"    FAIL: return structure wrong: {e}")
                failed += 1
                continue

            log(f"    title={title.strip()!r}")
            log(
                f"    scale={scale_name!r}  sweep={sweep_name!r}"
                f"  sweep_vals={sweep_vals}  tables={len(data_list)}"
            )
            if data_list:
                log(f"    table[0] keys: {list(data_list[0].keys())}")

            try:
                errs = assertion_fn(
                    title, date, scale_name, sweep_name, sweep_vals, data_list
                )
            except Exception as e:
                log(f"    FAIL: assertion raised {type(e).__name__}: {e}")
                log(traceback.format_exc())
                failed += 1
                continue

            if errs:
                for e in errs:
                    log(f"    ASSERTION FAILED: {e}")
                failed += 1
            else:
                log(f"    PASS")
                passed += 1

        log("\n" + "=" * 70)
        log(
            f"Summary: {passed} passed, {failed} failed, {skipped} skipped"
            f" out of {len(tests)} cases"
        )
        log("=" * 70)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
