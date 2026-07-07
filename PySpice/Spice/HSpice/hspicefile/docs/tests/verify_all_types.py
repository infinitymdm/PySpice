import glob
import os
import re
import struct
import sys
import traceback

WORK_DIR = "work"

# ---------------------------------------------------------------------------
# verify_all_types.py
#
# Post-implementation test: parses every binary file produced by
# run_simulations.py and cross-checks numerical values against the matching
# ASCII golden reference file produced with .option post=2.
#
# Does NOT depend on PySpice or the hspice_read C extension — uses only the
# pure-Python binary parser validated by verify_spec.py.
#
# Logs to logs/verify_all_types.log.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Minimal binary parser (spec-compliant, verified by verify_spec.py)
# ---------------------------------------------------------------------------


def _read_header(f):
    """Return (payload_bytes, fmt_char). Raises on corruption."""
    head = f.read(16)
    if len(head) < 16:
        raise IOError("Header block head truncated")
    e1, _, e2, psz = struct.unpack("<IIII", head)
    if e1 == 0x00000004 and e2 == 0x00000004:
        fmt = "<"
    elif e1 == 0x04000000 and e2 == 0x04000000:
        fmt = ">"
        e1, _, e2, psz = struct.unpack(">IIII", head)
    else:
        raise IOError(f"Bad endian markers 0x{e1:08x}/0x{e2:08x}")
    payload = f.read(psz)
    if len(payload) < psz:
        raise IOError("Header payload truncated")
    tail = struct.unpack(fmt + "I", f.read(4))[0]
    if tail != psz:
        raise IOError(f"Header head/tail mismatch ({psz} vs {tail})")
    return payload, fmt


def _parse_header_meta(payload):
    """Return metadata dict parsed from header payload bytes."""
    c = payload.decode("ascii", errors="ignore")
    v16 = c[16:20].strip()
    v20 = c[20:24].strip()
    if v16 in ("9007", "9601"):
        version = v16
    elif v20 in ("2001", "2013"):
        version = v20
    else:
        raise ValueError(f"Unknown version at [16:20]={repr(v16)} [20:24]={repr(v20)}")
    num_vars = int(c[0:4])
    num_probes = int(c[4:8])
    num_sweeps = int(c[8:12])
    sweep_size = 1
    if num_sweeps > 0:
        tok = c[187:203].strip().split()
        sweep_size = int(tok[0]) if tok else 1
    title = c[24:88].strip()
    num_vectors = num_vars + num_probes
    # Parse variable types and names from offset 256
    tokens = c[256:].split()
    if tokens and tokens[-1] == "$&%#":
        tokens = tokens[:-1]
    var_types = [0] * num_vectors
    var_names = [""] * num_vectors
    if len(tokens) >= 2 * num_vectors:
        # Token layout (natural order):
        #   tokens[0..N-1]   = type codes: type_0 (scale), type_1, ..., type_{N-1}
        #   tokens[N]        = name_0 (scale variable name)
        #   tokens[N+1..2N-1]  = names of circuit vars 1..N-1
        for i in range(num_vectors):
            var_types[i] = int(tokens[i])
        var_names[0] = tokens[num_vectors]
        for i in range(num_vectors - 1):
            var_names[i + 1] = tokens[num_vectors + 1 + i]
    return {
        "version": version,
        "num_vars": num_vars,
        "num_probes": num_probes,
        "num_sweeps": num_sweeps,
        "sweep_size": sweep_size,
        "num_vectors": num_vectors,
        "title": title,
        "var_types": var_types,
        "var_names": var_names,
    }


def _is_term_block(payload, version, fmt):
    sz = len(payload)
    if version in ("2013", "2001"):
        if sz == 8:
            return struct.unpack(fmt + "d", payload)[0] > 9e29
        if sz >= 8:
            return struct.unpack(fmt + "d", payload[-8:])[0] > 9e29
    else:
        if sz == 4:
            return struct.unpack(fmt + "f", payload)[0] > 9e29
        if sz >= 4:
            return struct.unpack(fmt + "f", payload[-4:])[0] > 9e29
    return False


def _var_sizes(meta):
    version = meta["version"]
    is_complex = meta["num_vectors"] > 1 and meta["var_types"][0] == 2
    sizes = []
    for i in range(meta["num_vectors"]):
        if version == "2013":
            s = 8 if i == 0 else (8 if is_complex else 4)
        elif version == "2001":
            s = 8 if (i == 0 or not is_complex) else 16
        else:
            s = 4 if (i == 0 or not is_complex) else 8
        sizes.append(s)
    return sizes, is_complex


def parse_spice_value(val_str):
    val_str = val_str.strip().lower()
    if val_str.endswith("f"):
        return float(val_str[:-1]) * 1e-15
    elif val_str.endswith("p"):
        return float(val_str[:-1]) * 1e-12
    elif val_str.endswith("n"):
        return float(val_str[:-1]) * 1e-9
    elif val_str.endswith("u"):
        return float(val_str[:-1]) * 1e-6
    elif val_str.endswith("meg"):
        return float(val_str[:-3]) * 1e6
    elif val_str.endswith("m"):
        return float(val_str[:-1]) * 1e-3
    elif val_str.endswith("k"):
        return float(val_str[:-1]) * 1e3
    elif val_str.endswith("g"):
        return float(val_str[:-1]) * 1e9
    else:
        try:
            return float(val_str)
        except ValueError:
            m = re.match(r"^([\d\.\-+e]+)", val_str)
            if m:
                return float(m.group(1))
            raise


def parse_meas_file(filepath):
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", errors="ignore") as f:
        lines = f.readlines()
    content_lines = [
        l.strip()
        for l in lines
        if l.strip() and not l.startswith("$") and not l.startswith(".")
    ]
    if len(content_lines) < 2:
        return None
    names = [n.lower() for n in content_lines[0].split()]
    is_sweep = len(content_lines) > 2
    measurements = {}
    for val_line in content_lines[1:]:
        vals = val_line.split()
        if len(vals) != len(names):
            continue
        for name, val in zip(names, vals):
            try:
                parsed_val = parse_spice_value(val)
            except Exception:
                parsed_val = val
            if is_sweep:
                if name not in measurements:
                    measurements[name] = []
                measurements[name].append(parsed_val)
            else:
                measurements[name] = parsed_val
    return measurements


def parse_binary(filename):
    """
    Parse a binary HSPICE output file.
    Returns a list of sweep tables; each table is a dict:
      { var_name: list_of_float_or_complex, ... }
    For AC, circuit values are complex.
    Also returns the metadata dict.
    """
    with open(filename, "rb") as f:
        first = f.read(1)
        if not first or first[0] >= 32:
            return None, None  # ASCII file — skip
        f.seek(0)
        header_payload, fmt = _read_header(f)
        meta = _parse_header_meta(header_payload)

        vsizes, is_complex = _var_sizes(meta)
        row_size = sum(vsizes)
        version = meta["version"]

        # Accumulate data blocks into sweep tables
        tables_raw = []
        current = bytearray()
        while True:
            head_raw = f.read(16)
            if not head_raw:
                if current:
                    tables_raw.append(current)
                break
            if len(head_raw) < 16:
                break
            _, _, _, psz = struct.unpack(fmt + "IIII", head_raw)
            payload = f.read(psz)
            f.read(4)  # tail
            if _is_term_block(payload, version, fmt):
                tables_raw.append(current)
                current = bytearray()
            else:
                current.extend(payload)

        # Decode each sweep table into a dict of arrays
        sweep_val_size = 8 if version == "2001" else 4
        sweep_val_fmt = "d" if version == "2001" else "f"
        tables = []
        for t_raw in tables_raw:
            offset = 0
            table = {name: [] for name in meta["var_names"]}
            sweep_val = None
            if meta["num_sweeps"] > 0:
                sweep_val = struct.unpack(fmt + sweep_val_fmt, t_raw[:sweep_val_size])[
                    0
                ]
                offset += sweep_val_size
            data_len = len(t_raw) - offset
            num_rows = data_len // row_size
            for _ in range(num_rows):
                c = offset
                for j, name in enumerate(meta["var_names"]):
                    vsz = vsizes[j]
                    chunk = t_raw[c : c + vsz]
                    if is_complex and j > 0:
                        if vsz == 16:
                            real_val, imag_val = struct.unpack(fmt + "dd", chunk)
                        else:
                            real_val, imag_val = struct.unpack(fmt + "ff", chunk)
                        table[name].append(complex(real_val, imag_val))
                    else:
                        vfmt = "d" if vsz == 8 else "f"
                        table[name].append(struct.unpack(fmt + vfmt, chunk)[0])
                    c += vsz
                offset += row_size
            if sweep_val is not None:
                table["__sweep_val__"] = sweep_val
            tables.append(table)

        tables_res = tables
        meta_res = meta

    # Parse measurements if any exist
    meta_res["measurements"] = {}
    for ext in [".ms0", ".mt0", ".ma0"]:
        meas_path = filename.rsplit(".", 1)[0] + ext
        if os.path.exists(meas_path):
            meas = parse_meas_file(meas_path)
            if meas is not None:
                meta_res["measurements"] = meas

    return tables_res, meta_res


# ---------------------------------------------------------------------------
# ASCII golden reference parser
# ---------------------------------------------------------------------------


def parse_ascii_to_flat_floats(filename):
    """
    Extract every 13-character float from the post-terminator data section.
    """
    if not os.path.exists(filename):
        return None
    with open(filename, "r", errors="ignore") as f:
        lines = f.readlines()

    header_ended = False
    vals = []
    for line in lines:
        if not header_ended:
            if "$&%#" in line:
                header_ended = True
            continue

        stripped = line.replace("\n", "").replace("\r", "")
        if not stripped:
            continue
        # Split this line into 13-character chunks starting at index 0 of the line
        for i in range(0, len(stripped), 13):
            chunk = stripped[i : i + 13].strip()
            if chunk:
                try:
                    vals.append(float(chunk))
                except ValueError:
                    pass
    return vals


def parse_ascii(filename, meta):
    """
    Parse an HSPICE ASCII post=2 output file guided by binary file metadata.
    Returns a list of tables; each table is a dict of name -> list of float/complex.
    """
    flat_vals = parse_ascii_to_flat_floats(filename)
    if flat_vals is None:
        return None

    version = meta["version"]
    num_sweeps = meta["num_sweeps"]
    num_vectors = meta["num_vectors"]
    var_names = meta["var_names"]
    var_types = meta["var_types"]
    is_complex = num_vectors > 1 and var_types[0] == 2

    tables = []
    idx = 0

    while idx < len(flat_vals):
        if idx >= len(flat_vals):
            break
        table = {name: [] for name in var_names}
        sweep_val = None
        if num_sweeps > 0:
            sweep_val = flat_vals[idx]
            idx += 1
        while idx < len(flat_vals):
            if flat_vals[idx] > 9e29:
                idx += 1
                break
            table[var_names[0]].append(flat_vals[idx])
            idx += 1
            for j in range(1, num_vectors):
                if idx + (2 if is_complex else 1) > len(flat_vals):
                    break
                if is_complex:
                    re = flat_vals[idx]
                    im = flat_vals[idx + 1]
                    table[var_names[j]].append(complex(re, im))
                    idx += 2
                else:
                    table[var_names[j]].append(flat_vals[idx])
                    idx += 1
        if sweep_val is not None:
            table["__sweep_val__"] = sweep_val
        tables.append(table)
    return tables


# ---------------------------------------------------------------------------
# Test cases: (binary_file, ascii_file, assertions)
# Each assertion is (description, callable(tables, meta) -> bool)
# ---------------------------------------------------------------------------


def _close(a, b, rtol=1e-3, atol=1e-30):
    """Relative and absolute tolerance comparison for floats."""
    if abs(a - b) < atol:
        return True
    if b == 0:
        return abs(a) < atol
    return abs(a - b) / abs(b) < rtol


def make_tests():
    """
    Return list of (binary_file, ascii_file, case_name, list_of_(desc, fn)).
    """
    tests = []

    # Helper: look up variable by partial name match (handles 'v(node_a' without closing paren)
    def find_var(table, partial):
        for k in table:
            if k.startswith(partial) or k == partial:
                return k
        return None

    # ------------------------------------------------------------------
    # DC BASIC — no sweep, voltage source
    # ------------------------------------------------------------------
    def dc_basic_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        scale = find_var(t, meta["var_names"][0])
        if scale is None:
            errs.append("Scale variable not found")
            return errs
        # v1 sweeps 1.0..5.0 in steps of 1.0 => 5 points
        if len(t[scale]) != 5:
            errs.append(f"Expected 5 scale points, got {len(t[scale])}")
        if not _close(t[scale][0], 1.0):
            errs.append(f"Scale[0] expected 1.0, got {t[scale][0]}")
        if not _close(t[scale][-1], 5.0):
            errs.append(f"Scale[-1] expected 5.0, got {t[scale][-1]}")
        # node_a == v1 voltage (DC wire)
        node_a = find_var(t, "node_a")
        if node_a:
            for i, (vs, va) in enumerate(zip(t[scale], t[node_a])):
                if not _close(vs, va, rtol=1e-4):
                    errs.append(f"Row {i}: v(node_a)={va} != VOLTS={vs}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_basic_{version}.sw0",
                "dc_basic_ascii.sw0",
                f"dc_basic_{version}",
                dc_basic_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC SWEEP TEMP — temperature is the primary .dc axis (DEG_C scale var),
    # NOT a secondary outer sweep. Produces 1 table with 5 rows.
    # ------------------------------------------------------------------
    def dc_sweep_temp_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(
                f"Expected 1 table (temp is primary .dc axis), got {len(tables)}"
            )
            return errs
        t = tables[0]
        scale = meta["var_names"][0]  # should be DEG_C
        if scale not in t:
            errs.append(f"Scale '{scale}' missing from table")
            return errs
        if len(t[scale]) != 5:
            errs.append(
                f"Expected 5 rows (temp -40 to 120 step 40), got {len(t[scale])}"
            )
        if not _close(t[scale][0], -40.0, rtol=1e-3):
            errs.append(f"Scale[0] expected -40.0, got {t[scale][0]}")
        if not _close(t[scale][-1], 120.0, rtol=1e-3):
            errs.append(f"Scale[-1] expected 120.0, got {t[scale][-1]}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_sweep_temp_{version}.sw0",
                "dc_sweep_temp_ascii.sw0",
                f"dc_sweep_temp_{version}",
                dc_sweep_temp_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC SWEEP PARAM — rval is the primary .dc axis (rval scale var),
    # NOT a secondary outer sweep. Produces 1 table with 5 rows.
    # ------------------------------------------------------------------
    def dc_sweep_param_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(
                f"Expected 1 table (rval is primary .dc axis), got {len(tables)}"
            )
            return errs
        t = tables[0]
        scale = meta["var_names"][0]  # should be rval
        if scale not in t:
            errs.append(f"Scale '{scale}' missing from table")
            return errs
        if len(t[scale]) != 5:
            errs.append(f"Expected 5 rows (rval 10 to 50 step 10), got {len(t[scale])}")
        if not _close(t[scale][0], 10.0, rtol=1e-3):
            errs.append(f"Scale[0] expected 10.0, got {t[scale][0]}")
        if not _close(t[scale][-1], 50.0, rtol=1e-3):
            errs.append(f"Scale[-1] expected 50.0, got {t[scale][-1]}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_sweep_param_{version}.sw0",
                "dc_sweep_param_ascii.sw0",
                f"dc_sweep_param_{version}",
                dc_sweep_param_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC MONTE — 5 Monte Carlo runs, 5 data points each (v1 1..5 step 1)
    # ------------------------------------------------------------------
    def dc_monte_assertions(tables, meta):
        errs = []
        if len(tables) != 5:
            errs.append(f"Expected 5 Monte Carlo tables, got {len(tables)}")
            return errs
        scale = meta["var_names"][0]
        for i, t in enumerate(tables):
            if scale not in t:
                errs.append(f"Table {i}: scale var '{scale}' missing")
                continue
            if len(t[scale]) != 5:
                errs.append(f"Table {i}: expected 5 rows, got {len(t[scale])}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_monte_{version}.sw0",
                None,  # Monte Carlo output is non-deterministic; no golden reference check
                f"dc_monte_{version}",
                dc_monte_assertions,
            )
        )

    # ------------------------------------------------------------------
    # AC BASIC — no sweep, 5 frequency points (dec 5 100 10k => 21 pts)
    # ------------------------------------------------------------------
    def ac_basic_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        scale = meta["var_names"][0]
        if scale not in t:
            errs.append(f"Scale '{scale}' not in table")
            return errs
        freqs = t[scale]
        # dec 5 100 10k gives (5*2)+1 = 11 pts per decade, 2 decades => 11 pts
        if len(freqs) < 5:
            errs.append(f"Expected >=5 frequency points, got {len(freqs)}")
        if freqs[0] > freqs[-1]:
            errs.append("Frequency should be increasing")
        if not _close(freqs[0], 100.0, rtol=0.01):
            errs.append(f"First freq expected ~100 Hz, got {freqs[0]}")
        if not _close(freqs[-1], 10000.0, rtol=0.01):
            errs.append(f"Last freq expected ~10000 Hz, got {freqs[-1]}")
        # All circuit vars should be complex
        for name in meta["var_names"][1:]:
            if name not in t:
                errs.append(f"Var '{name}' missing")
                continue
            sample = t[name][0]
            if not isinstance(sample, complex):
                errs.append(f"Var '{name}' should be complex, got {type(sample)}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_basic_{version}.ac0",
                "ac_basic_ascii.ac0",
                f"ac_basic_{version}",
                ac_basic_assertions,
            )
        )

    # ------------------------------------------------------------------
    # AC SWEEP PARAM — 3 sweep points for rval (10, 20, 30)
    # ------------------------------------------------------------------
    def ac_sweep_param_assertions(tables, meta):
        errs = []
        if len(tables) != 3:
            errs.append(f"Expected 3 sweep tables, got {len(tables)}")
            return errs
        expected_rvals = [10.0, 20.0, 30.0]
        for i, (t, er) in enumerate(zip(tables, expected_rvals)):
            sv = t.get("__sweep_val__")
            if sv is None:
                errs.append(f"Table {i}: no sweep value")
            elif not _close(sv, er, rtol=1e-3):
                errs.append(f"Table {i}: sweep_val={sv}, expected {er}")
            scale = meta["var_names"][0]
            if scale in t and len(t[scale]) < 5:
                errs.append(f"Table {i}: too few frequency points ({len(t[scale])})")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_sweep_param_{version}.ac0",
                "ac_sweep_param_ascii.ac0",
                f"ac_sweep_param_{version}",
                ac_sweep_param_assertions,
            )
        )

    # ------------------------------------------------------------------
    # AC SWEEP TEMP — AC sweep with temperature (-40 to 120 step 40 => 5 tables)
    # ------------------------------------------------------------------
    def ac_sweep_temp_assertions(tables, meta):
        errs = []
        if len(tables) != 5:
            errs.append(f"Expected 5 outer sweep tables (temp), got {len(tables)}")
            return errs
        scale = meta["var_names"][0]
        for i, t in enumerate(tables):
            if scale not in t:
                errs.append(f"Table {i}: scale '{scale}' missing")
                continue
            if len(t[scale]) != 11:
                errs.append(
                    f"Table {i}: expected 11 frequency points, got {len(t[scale])}"
                )
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_sweep_temp_{version}.ac0",
                "ac_sweep_temp_ascii.ac0",
                f"ac_sweep_temp_{version}",
                ac_sweep_temp_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN BASIC — no sweep, time 0..20ns step 1ns
    # ------------------------------------------------------------------
    def tran_basic_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        scale = meta["var_names"][0]
        if scale not in t:
            errs.append(f"Scale '{scale}' missing")
            return errs
        times = t[scale]
        if len(times) < 5:
            errs.append(f"Too few time points: {len(times)}")
        if times[0] > 1e-12:
            errs.append(f"Time[0] expected ~0, got {times[0]}")
        if not _close(times[-1], 20e-9, rtol=0.01):
            errs.append(f"Time[-1] expected ~20ns, got {times[-1]:.4e}")
        # All circuit vars should be real (non-complex)
        for name in meta["var_names"][1:]:
            if name not in t:
                errs.append(f"Var '{name}' missing")
                continue
            sample = t[name][0]
            if isinstance(sample, complex):
                errs.append(f"Var '{name}' should be real in transient, got complex")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_basic_{version}.tr0",
                "tran_basic_ascii.tr0",
                f"tran_basic_{version}",
                tran_basic_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN SWEEP TEMP — 5 temperature sweep points
    # ------------------------------------------------------------------
    def tran_sweep_temp_assertions(tables, meta):
        errs = []
        if len(tables) != 5:
            errs.append(f"Expected 5 tables, got {len(tables)}")
            return errs
        expected_temps = [-40.0, 0.0, 40.0, 80.0, 120.0]
        for i, (t, et) in enumerate(zip(tables, expected_temps)):
            sv = t.get("__sweep_val__")
            if sv is None:
                errs.append(f"Table {i}: no sweep value")
            elif not _close(sv, et, rtol=1e-3):
                errs.append(f"Table {i}: sweep_val={sv}, expected {et}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_sweep_temp_{version}.tr0",
                "tran_sweep_temp_ascii.tr0",
                f"tran_sweep_temp_{version}",
                tran_sweep_temp_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN SWEEP PARAM — rval 10..30 step 10, 3 tables
    # ------------------------------------------------------------------
    def tran_sweep_param_assertions(tables, meta):
        errs = []
        if len(tables) != 3:
            errs.append(f"Expected 3 tables, got {len(tables)}")
            return errs
        expected_rvals = [10.0, 20.0, 30.0]
        for i, (t, er) in enumerate(zip(tables, expected_rvals)):
            sv = t.get("__sweep_val__")
            if sv is None:
                errs.append(f"Table {i}: no sweep value")
            elif not _close(sv, er, rtol=1e-3):
                errs.append(f"Table {i}: sweep_val={sv}, expected {er}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_sweep_param_{version}.tr0",
                "tran_sweep_param_ascii.tr0",
                f"tran_sweep_param_{version}",
                tran_sweep_param_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN SWEEP SOURCE — v2 sweep 1..3 step 1, 3 tables
    # ------------------------------------------------------------------
    def tran_sweep_source_assertions(tables, meta):
        errs = []
        if len(tables) != 3:
            errs.append(f"Expected 3 tables, got {len(tables)}")
            return errs
        expected_vals = [1.0, 2.0, 3.0]
        for i, (t, ev) in enumerate(zip(tables, expected_vals)):
            sv = t.get("__sweep_val__")
            if sv is None:
                errs.append(f"Table {i}: no sweep value")
            elif not _close(sv, ev, rtol=1e-3):
                errs.append(f"Table {i}: sweep_val={sv}, expected {ev}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_sweep_source_{version}.tr0",
                "tran_sweep_source_ascii.tr0",
                f"tran_sweep_source_{version}",
                tran_sweep_source_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN PROBES ONLY — transient analysis with probe statements only
    # ------------------------------------------------------------------
    def tran_probes_only_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        scale = meta["var_names"][0]
        if scale not in t:
            errs.append(f"Scale '{scale}' missing")
        if len(meta["var_names"]) < 3:
            errs.append(f"Expected multiple probes, got {meta['var_names']}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_probes_only_{version}.tr0",
                "tran_probes_only_ascii.tr0",
                f"tran_probes_only_{version}",
                tran_probes_only_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN PROBE AND SWEEP — rval 10..30 step 10, explicit probes
    # ------------------------------------------------------------------
    def tran_probe_and_sweep_assertions(tables, meta):
        errs = []
        if len(tables) != 3:
            errs.append(f"Expected 3 tables, got {len(tables)}")
            return errs
        # Probes: v(node_a), v(node_b), i(v1) — verify they are present
        expected_probe_partials = ["node_a", "node_b"]
        for i, t in enumerate(tables):
            for partial in expected_probe_partials:
                found = any(partial in k for k in t if k != "__sweep_val__")
                if not found:
                    errs.append(
                        f"Table {i}: expected probe containing '{partial}' not found. Keys={list(t.keys())}"
                    )
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_probe_and_sweep_{version}.tr0",
                "tran_probe_and_sweep_ascii.tr0",
                f"tran_probe_and_sweep_{version}",
                tran_probe_and_sweep_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN MONTE — 5 runs, all real
    # ------------------------------------------------------------------
    def tran_monte_assertions(tables, meta):
        errs = []
        if len(tables) != 5:
            errs.append(f"Expected 5 Monte Carlo tables, got {len(tables)}")
            return errs
        scale = meta["var_names"][0]
        for i, t in enumerate(tables):
            if scale not in t:
                errs.append(f"Table {i}: scale '{scale}' missing")
                continue
            if len(t[scale]) < 2:
                errs.append(f"Table {i}: too few time points ({len(t[scale])})")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"tran_monte_{version}.tr0",
                None,
                f"tran_monte_{version}",
                tran_monte_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC PROBES ONLY — v(node_a), v(node_b), differential, i(v1), i(r1), p(r1)
    # ------------------------------------------------------------------
    def dc_probes_only_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        if len(meta["var_names"]) < 3:
            errs.append(f"Expected multiple probes, got {meta['var_names']}")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_probes_only_{version}.sw0",
                "dc_probes_only_ascii.sw0",
                f"dc_probes_only_{version}",
                dc_probes_only_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC PROBE AND SWEEP — rval 10..30 step 10, explicit probes (5 points inner)
    # ------------------------------------------------------------------
    def dc_probe_and_sweep_assertions(tables, meta):
        errs = []
        if len(tables) != 3:
            errs.append(f"Expected 3 outer sweep tables (rval), got {len(tables)}")
            return errs
        scale = meta["var_names"][0]
        for i, t in enumerate(tables):
            if scale not in t:
                errs.append(f"Table {i}: scale '{scale}' missing")
                continue
            if len(t[scale]) != 5:
                errs.append(f"Table {i}: expected 5 inner points, got {len(t[scale])}")
            for p in ["node_a", "node_b"]:
                if find_var(t, p) is None:
                    errs.append(f"Table {i}: '{p}' probe missing")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_probe_and_sweep_{version}.sw0",
                "dc_probe_and_sweep_ascii.sw0",
                f"dc_probe_and_sweep_{version}",
                dc_probe_and_sweep_assertions,
            )
        )

    # ------------------------------------------------------------------
    # AC PROBES ONLY — v(node_a), v(node_b), differential, i(v1), i(r1)
    # ------------------------------------------------------------------
    def ac_probes_only_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        scale = meta["var_names"][0]
        if scale not in t:
            errs.append(f"Scale '{scale}' missing")
        # All circuit vars must be complex
        for name in meta["var_names"][1:]:
            if name not in t:
                errs.append(f"Var '{name}' missing")
                continue
            if t[name] and not isinstance(t[name][0], complex):
                errs.append(f"Var '{name}' should be complex in AC")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_probes_only_{version}.ac0",
                "ac_probes_only_ascii.ac0",
                f"ac_probes_only_{version}",
                ac_probes_only_assertions,
            )
        )

    # ------------------------------------------------------------------
    # AC PROBE AND SWEEP — rval 10..30 step 10, explicit probes (11 points inner)
    # ------------------------------------------------------------------
    def ac_probe_and_sweep_assertions(tables, meta):
        errs = []
        if len(tables) != 3:
            errs.append(f"Expected 3 outer sweep tables (rval), got {len(tables)}")
            return errs
        scale = meta["var_names"][0]
        for i, t in enumerate(tables):
            if scale not in t:
                errs.append(f"Table {i}: scale '{scale}' missing")
                continue
            if len(t[scale]) != 11:
                errs.append(
                    f"Table {i}: expected 11 frequency points, got {len(t[scale])}"
                )
            for p in ["node_a", "node_b"]:
                pv = find_var(t, p)
                if pv is None:
                    errs.append(f"Table {i}: '{p}' probe missing")
                elif t[pv] and not isinstance(t[pv][0], complex):
                    errs.append(f"Table {i}: '{pv}' should be complex in AC")
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"ac_probe_and_sweep_{version}.ac0",
                "ac_probe_and_sweep_ascii.ac0",
                f"ac_probe_and_sweep_{version}",
                ac_probe_and_sweep_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC NESTED SWEEP — v1 1..5 sweep rval 10..30, 3 outer sweep tables
    # ------------------------------------------------------------------
    def dc_nested_sweep_assertions(tables, meta):
        errs = []
        if len(tables) != 3:
            errs.append(f"Expected 3 outer sweep tables (rval), got {len(tables)}")
            return errs
        scale = meta["var_names"][0]
        for i, t in enumerate(tables):
            if scale not in t:
                errs.append(f"Table {i}: scale '{scale}' missing")
                continue
            if len(t[scale]) != 5:
                errs.append(
                    f"Table {i}: expected 5 inner points (v1 1..5), got {len(t[scale])}"
                )
        return errs

    for version in ["9601", "2001", "2013"]:
        tests.append(
            (
                f"dc_nested_sweep_{version}.sw0",
                "dc_nested_sweep_ascii.sw0",
                f"dc_nested_sweep_{version}",
                dc_nested_sweep_assertions,
            )
        )

    # ------------------------------------------------------------------
    # OP BASIC
    # ------------------------------------------------------------------
    def op_basic_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        node_a = find_var(t, "node_a")
        node_b = find_var(t, "node_b")
        if not node_a or not _close(t[node_a][0], 1.0):
            errs.append(f"node_a expected 1.0, got {t.get(node_a)}")
        if not node_b or not _close(t[node_b][0], 0.66667):
            errs.append(f"node_b expected 0.66667, got {t.get(node_b)}")
        v1 = find_var(t, "i(v1")
        if not v1 or not _close(t[v1][0], -0.033333):
            errs.append(f"i(v1) expected -0.033333, got {t.get(v1)}")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"op_basic_{version}.ic0",
                "op_basic_ascii.ic0",
                f"op_basic_{version}",
                op_basic_assertions,
            )
        )

    # ------------------------------------------------------------------
    # OP WITH PROBES
    # ------------------------------------------------------------------
    def op_with_probes_assertions(tables, meta):
        errs = []
        if len(tables) != 1:
            errs.append(f"Expected 1 table, got {len(tables)}")
            return errs
        t = tables[0]
        node_a = find_var(t, "node_a")
        node_b = find_var(t, "node_b")
        if not node_a or not _close(t[node_a][0], 2.0):
            errs.append(f"node_a expected 2.0, got {t.get(node_a)}")
        if not node_b or not _close(t[node_b][0], 1.0):
            errs.append(f"node_b expected 1.0, got {t.get(node_b)}")
        v1 = find_var(t, "i(v1")
        v2 = find_var(t, "i(v2")
        if not v1 or not _close(t[v1][0], -0.2):
            errs.append(f"i(v1) expected -0.2, got {t.get(v1)}")
        if not v2 or not _close(t[v2][0], 0.2):
            errs.append(f"i(v2) expected 0.2, got {t.get(v2)}")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"op_with_probes_{version}.ic0",
                "op_with_probes_ascii.ic0",
                f"op_with_probes_{version}",
                op_with_probes_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC MEASUREMENT
    # ------------------------------------------------------------------
    def dc_meas_assertions(tables, meta):
        errs = []
        meas = meta.get("measurements", {})
        if "v_out_max" not in meas:
            errs.append(
                f"Expected measurement 'v_out_max' not found (keys={list(meas.keys())})"
            )
        else:
            val = meas["v_out_max"]
            if not _close(val, 3.3333):
                errs.append(f"v_out_max {val} != expected 3.3333")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"dc_meas_{version}.sw0",
                "dc_meas_ascii.sw0",
                f"dc_meas_{version}",
                dc_meas_assertions,
            )
        )

    # ------------------------------------------------------------------
    # AC MEASUREMENT
    # ------------------------------------------------------------------
    def ac_meas_assertions(tables, meta):
        errs = []
        meas = meta.get("measurements", {})
        if "v_out_at_1k" not in meas:
            errs.append(
                f"Expected measurement 'v_out_at_1k' not found (keys={list(meas.keys())})"
            )
        else:
            val = meas["v_out_at_1k"]
            if not _close(val, 0.998):
                errs.append(f"v_out_at_1k {val} != expected 0.998")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"ac_meas_{version}.ac0",
                "ac_meas_ascii.ac0",
                f"ac_meas_{version}",
                ac_meas_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN MEASUREMENT
    # ------------------------------------------------------------------
    def tran_meas_assertions(tables, meta):
        errs = []
        meas = meta.get("measurements", {})
        if "v_out_max" not in meas:
            errs.append(
                f"Expected measurement 'v_out_max' not found (keys={list(meas.keys())})"
            )
        else:
            val = meas["v_out_max"]
            if not _close(val, 0.66667):
                errs.append(f"v_out_max {val} != expected 0.66667")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"tran_meas_{version}.tr0",
                "tran_meas_ascii.tr0",
                f"tran_meas_{version}",
                tran_meas_assertions,
            )
        )

    # ------------------------------------------------------------------
    # DC MEASUREMENT SWEEP
    # ------------------------------------------------------------------
    def dc_meas_sweep_assertions(tables, meta):
        errs = []
        meas = meta.get("measurements", {})
        if "v_out_max" not in meas:
            errs.append("Expected measurement 'v_out_max' not found")
        else:
            vals = meas["v_out_max"]
            expected = [3.3333, 2.5, 2.0]
            if (
                not isinstance(vals, list)
                or len(vals) != len(expected)
                or not all(_close(x, y) for x, y in zip(vals, expected))
            ):
                errs.append(f"v_out_max {vals} != expected {expected}")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"dc_meas_sweep_{version}.sw0",
                "dc_meas_sweep_ascii.sw0",
                f"dc_meas_sweep_{version}",
                dc_meas_sweep_assertions,
            )
        )

    # ------------------------------------------------------------------
    # AC MEASUREMENT SWEEP
    # ------------------------------------------------------------------
    def ac_meas_sweep_assertions(tables, meta):
        errs = []
        meas = meta.get("measurements", {})
        if "v_out_at_1k" not in meas:
            errs.append("Expected measurement 'v_out_at_1k' not found")
        else:
            vals = meas["v_out_at_1k"]
            expected = [0.998, 0.9922, 0.9827]
            if (
                not isinstance(vals, list)
                or len(vals) != len(expected)
                or not all(_close(x, y) for x, y in zip(vals, expected))
            ):
                errs.append(f"v_out_at_1k {vals} != expected {expected}")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"ac_meas_sweep_{version}.ac0",
                "ac_meas_sweep_ascii.ac0",
                f"ac_meas_sweep_{version}",
                ac_meas_sweep_assertions,
            )
        )

    # ------------------------------------------------------------------
    # TRAN MEASUREMENT SWEEP
    # ------------------------------------------------------------------
    def tran_meas_sweep_assertions(tables, meta):
        errs = []
        meas = meta.get("measurements", {})
        if "v_out_max" not in meas:
            errs.append("Expected measurement 'v_out_max' not found")
        else:
            vals = meas["v_out_max"]
            expected = [0.66667, 0.5, 0.4]
            if (
                not isinstance(vals, list)
                or len(vals) != len(expected)
                or not all(_close(x, y) for x, y in zip(vals, expected))
            ):
                errs.append(f"v_out_max {vals} != expected {expected}")
        return errs

    for version in ["9601", "2001", "2013", "ascii"]:
        tests.append(
            (
                f"tran_meas_sweep_{version}.tr0",
                "tran_meas_sweep_ascii.tr0",
                f"tran_meas_sweep_{version}",
                tran_meas_sweep_assertions,
            )
        )

    return tests


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main():
    logs_dir = "logs"
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "verify_all_types.log")

    with open(log_path, "w") as lf:

        def log(msg):
            print(msg)
            lf.write(msg + "\n")
            lf.flush()

        log("=" * 70)
        log("HSPICE Simulation Type Verification (post-implementation test)")
        log("=" * 70)
        log(f"Work directory: {os.path.abspath(WORK_DIR)}")

        tests = make_tests()
        passed = 0
        failed = 0
        skipped = 0

        for bin_file, ascii_file, case_name, assertion_fn in tests:
            log(f"\n--- {case_name} ---")
            log(f"    Binary:  {bin_file}")

            full_path = os.path.join(WORK_DIR, bin_file)
            if not os.path.exists(full_path):
                log(f"    SKIP: file not found ({full_path})")
                skipped += 1
                continue

            try:
                tables, meta = parse_binary(full_path)
            except Exception:
                log(f"    FAIL: parse_binary raised exception")
                log(traceback.format_exc())
                failed += 1
                continue

            if tables is None:
                log(f"    SKIP: ASCII file detected (not a binary)")
                skipped += 1
                continue

            log(
                f"    Version: {meta['version']}  Vectors: {meta['num_vectors']}  "
                f"SweepSize: {meta['sweep_size']}  Tables: {len(tables)}"
            )
            log(f"    Var names: {meta['var_names']}")
            log(f"    Var types: {meta['var_types']}")

            try:
                errs = assertion_fn(tables, meta)
            except Exception:
                log(f"    FAIL: assertion_fn raised exception")
                log(traceback.format_exc())
                failed += 1
                continue

            # Load and compare against ASCII golden file
            if ascii_file is not None and ascii_file != bin_file:
                ascii_path = os.path.join(WORK_DIR, ascii_file)
                try:
                    ascii_tables = parse_ascii(ascii_path, meta)
                    if ascii_tables is None:
                        errs.append(
                            f"ASCII golden file not found or failed to parse ({ascii_path})"
                        )
                    elif len(tables) != len(ascii_tables):
                        errs.append(
                            f"Number of tables mismatch: binary has {len(tables)}, ASCII has {len(ascii_tables)}"
                        )
                    else:
                        for t_idx, (bin_table, ascii_table) in enumerate(
                            zip(tables, ascii_tables)
                        ):
                            bin_keys = [
                                k for k in bin_table.keys() if k != "__sweep_val__"
                            ]
                            ascii_keys = [
                                k for k in ascii_table.keys() if k != "__sweep_val__"
                            ]
                            if len(bin_keys) != len(ascii_keys):
                                errs.append(
                                    f"Table {t_idx} variable count mismatch: binary has {len(bin_keys)}, ASCII has {len(ascii_keys)}"
                                )
                                continue
                            for key in bin_keys:
                                bin_vals = bin_table[key]
                                ascii_vals = ascii_table[key]
                                if len(bin_vals) != len(ascii_vals):
                                    errs.append(
                                        f"Table {t_idx} variable '{key}' length mismatch: binary has {len(bin_vals)}, ASCII has {len(ascii_vals)}"
                                    )
                                    continue
                                for val_idx, (b_v, a_v) in enumerate(
                                    zip(bin_vals, ascii_vals)
                                ):
                                    if isinstance(b_v, complex):
                                        if not _close(
                                            b_v.real, a_v.real, rtol=1e-3
                                        ) or not _close(b_v.imag, a_v.imag, rtol=1e-3):
                                            errs.append(
                                                f"Table {t_idx} variable '{key}' row {val_idx} value mismatch: binary={b_v}, ASCII={a_v}"
                                            )
                                            break
                                    else:
                                        if not _close(b_v, a_v, rtol=1e-3):
                                            errs.append(
                                                f"Table {t_idx} variable '{key}' row {val_idx} value mismatch: binary={b_v}, ASCII={a_v}"
                                            )
                                            break
                            if "__sweep_val__" in bin_table:
                                if "__sweep_val__" not in ascii_table:
                                    errs.append(
                                        f"Table {t_idx} sweep value missing in ASCII table"
                                    )
                                elif not _close(
                                    bin_table["__sweep_val__"],
                                    ascii_table["__sweep_val__"],
                                    rtol=1e-3,
                                ):
                                    errs.append(
                                        f"Table {t_idx} sweep value mismatch: binary={bin_table['__sweep_val__']}, ASCII={ascii_table['__sweep_val__']}"
                                    )
                except Exception as ae:
                    errs.append(f"ASCII parser/comparison raised exception: {str(ae)}")

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
