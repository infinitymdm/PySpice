import glob
import os
import re
import struct
import sys
import traceback

WORK_DIR = "work"


def parse_header_payload(payload):
    clean = payload.decode("ascii", errors="ignore")

    if len(clean) < 24:
        raise ValueError(f"Header payload too short: {len(clean)} bytes")

    # Detect version from fixed positions
    version_at_16 = clean[16:20].strip()
    version_at_20 = clean[20:24].strip()

    if version_at_16 in ("9007", "9601"):
        version = version_at_16
        is_legacy = True
    elif version_at_20 in ("2001", "2013"):
        version = version_at_20
        is_legacy = False
    else:
        raise ValueError(
            f"Unknown format version: pos16={repr(version_at_16)}, pos20={repr(version_at_20)}"
        )

    try:
        num_vars = int(clean[0:4])
        num_probes = int(clean[4:8])
        num_sweeps = int(clean[8:12])
    except ValueError as e:
        raise ValueError(f"Failed to parse format descriptor '{clean[:24]}': {e}")

    # Sweep size field: offset 187 works for all modern formats.
    # For legacy formats the same offset (187) also holds the sweep size value,
    # because the copyright string ends before it.
    # We parse whatever integer is at position 187 if num_sweeps > 0.
    sweep_size = 1
    if num_sweeps > 0:
        sweep_size_str = clean[187:203].strip().split()
        if sweep_size_str:
            try:
                sweep_size = int(sweep_size_str[0])
            except ValueError:
                sweep_size = 1  # fallback

    title = clean[24:88].strip()
    date = clean[88:112].strip()

    # Variable types and names start at offset 256 in the header payload.
    tokens = clean[256:].split()
    if tokens and tokens[-1] == "$&%#":
        tokens = tokens[:-1]

    num_vectors = num_vars + num_probes
    var_types = [0] * num_vectors
    var_names = [""] * num_vectors

    if len(tokens) >= 2 * num_vectors:
        # Token layout (natural order):
        #   tokens[0..N-1]   = type codes in order: type_0 (scale), type_1, ..., type_{N-1}
        #   tokens[N]        = name_0 (scale variable name)
        #   tokens[N+1..2N-1] = names of circuit vars 1..N-1
        for idx in range(num_vectors):
            var_types[idx] = int(tokens[idx])

        var_names[0] = tokens[num_vectors]
        for idx in range(num_vectors - 1):
            var_names[idx + 1] = tokens[num_vectors + 1 + idx]

    return {
        "version": version,
        "is_legacy": is_legacy,
        "num_vars": num_vars,
        "num_probes": num_probes,
        "num_sweeps": num_sweeps,
        "sweep_size": sweep_size,
        "num_vectors": num_vectors,
        "title": title,
        "date": date,
        "var_types": var_types,
        "var_names": var_names,
    }


def read_block(f, fmt):
    """Read one block (head + payload + tail). Returns (payload_bytes, is_term_block) or None on EOF."""
    head_raw = f.read(16)
    if len(head_raw) == 0:
        return None, False  # clean EOF
    if len(head_raw) < 16:
        raise IOError(f"Truncated block head: only {len(head_raw)} bytes")

    e1, rc, e2, payload_size = struct.unpack(fmt + "IIII", head_raw)
    if e1 != 4 or e2 != 4:
        raise IOError(f"Bad endian markers: 0x{e1:08x}/0x{e2:08x}")

    payload = f.read(payload_size)
    if len(payload) < payload_size:
        raise IOError(f"Truncated payload: got {len(payload)} of {payload_size}")

    tail_raw = f.read(4)
    if len(tail_raw) < 4:
        raise IOError("Truncated block tail")
    tail = struct.unpack(fmt + "I", tail_raw)[0]
    if tail != payload_size:
        raise IOError(f"Block head/tail mismatch: {payload_size} vs {tail}")

    return payload, payload_size


def is_term_block(payload, version, fmt):
    """
    Termination detection rule (from C source and observed binary layout):
    - 2013: a dedicated block whose entire payload is an 8-byte double == 1e30.
             Also accepted if the last 8 bytes of a larger payload are > 9e29.
    - 2001: a dedicated block whose entire payload is an 8-byte double == 1e30.
    - 9601/9007 (legacy): a dedicated block whose entire payload is a 4-byte float == 1e30.
    """
    size = len(payload)
    if version in ("2013", "2001"):
        if size == 8:
            val = struct.unpack(fmt + "d", payload)[0]
            return val > 9e29
        if size >= 8:
            val = struct.unpack(fmt + "d", payload[-8:])[0]
            return val > 9e29
    else:  # legacy 9601/9007
        if size == 4:
            val = struct.unpack(fmt + "f", payload)[0]
            return val > 9e29
        if size >= 4:
            val = struct.unpack(fmt + "f", payload[-4:])[0]
            return val > 9e29
    return False


def row_sizes_for(meta):
    version = meta["version"]
    is_complex = meta["num_vectors"] > 1 and meta["var_types"][0] == 2

    sizes = []
    for idx in range(meta["num_vectors"]):
        if version == "2013":
            if idx == 0:
                s = 8  # scale: double
            elif is_complex:
                s = 8  # complex float: 4B real + 4B imag
            else:
                s = 4  # float
        elif version == "2001":
            if is_complex and idx > 0:
                s = 16  # complex double: 8B real + 8B imag
            else:
                s = 8  # double
        else:  # 9601/9007 legacy
            if is_complex and idx > 0:
                s = 8  # complex float: 4B real + 4B imag
            else:
                s = 4  # float
        sizes.append(s)
    return sizes, sum(sizes), is_complex


def parse_row(raw, offset, meta, var_sizes, is_complex, fmt):
    """Parse one data row starting at `offset`, return (scale_val, circuit_vals)."""
    version = meta["version"]
    row = raw[offset:]
    c_offset = 0

    scale_sz = var_sizes[0]
    scale_fmt = "d" if scale_sz == 8 else "f"
    scale_val = struct.unpack(fmt + scale_fmt, row[c_offset : c_offset + scale_sz])[0]
    c_offset += scale_sz

    circuit_vals = []
    for idx in range(1, meta["num_vectors"]):
        vsz = var_sizes[idx]
        if is_complex and idx > 0:
            if vsz == 16:
                re, im = struct.unpack(fmt + "dd", row[c_offset : c_offset + 16])
            else:
                re, im = struct.unpack(fmt + "ff", row[c_offset : c_offset + 8])
            circuit_vals.append(complex(re, im))
        else:
            vfmt = "d" if vsz == 8 else "f"
            circuit_vals.append(
                struct.unpack(fmt + vfmt, row[c_offset : c_offset + vsz])[0]
            )
        c_offset += vsz

    return scale_val, circuit_vals


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


def verify_op_file(filename, log):
    log(f"\n{'='*70}")
    log(f"VERIFYING OPERATING POINT FILE: {filename}")
    log(f"{'='*70}")

    # Parse node voltages from .ic0
    nodes = {}
    try:
        with open(filename, "r") as f:
            lines = f.readlines()
    except Exception as e:
        log(f"ERROR: could not read {filename}: {e}")
        return False

    in_nodeset = False
    for line in lines:
        line = line.strip()
        if line.startswith(".nodeset"):
            in_nodeset = True
            continue
        if in_nodeset:
            if line.startswith("***"):
                break
            if line.startswith("+"):
                parts = line[1:].split("=")
                if len(parts) == 2:
                    node_name = parts[0].strip().lower()
                    if node_name.startswith("v(") and node_name.endswith(")"):
                        node_name = node_name[2:-1]
                    try:
                        nodes[node_name] = parse_spice_value(parts[1].strip())
                    except Exception:
                        pass

    log(f"Parsed Node Voltages: {nodes}")

    # Parse branch currents from .lis
    lis_filename = filename.replace(".ic0", ".lis")
    if not os.path.exists(lis_filename):
        log(f"ERROR: corresponding .lis file {lis_filename} not found")
        return False

    branches = {}
    try:
        with open(lis_filename, "r", errors="ignore") as f:
            lis_content = f.read()
    except Exception as e:
        log(f"ERROR: could not read {lis_filename}: {e}")
        return False

    lines = lis_content.splitlines()
    in_section = False
    section_lines = []
    for line in lines:
        if "voltage sources" in line.lower():
            in_section = True
            continue
        if in_section:
            if "total voltage source" in line.lower() or (
                line.strip().startswith("****")
                and "voltage sources" not in line.lower()
            ):
                break
            section_lines.append(line)

    elements = []
    for line in section_lines:
        tokens = line.strip().split()
        if not tokens:
            continue
        if tokens[0].lower() == "element":
            elements = tokens[1:]
        elif tokens[0].lower() == "current":
            currents = tokens[1:]
            for el, curr in zip(elements, currents):
                clean_el = el.split(":")[-1].lower() if ":" in el else el.lower()
                try:
                    branches[clean_el] = parse_spice_value(curr)
                except Exception:
                    pass

    log(f"Parsed Branch Currents: {branches}")

    # Verify values
    def is_close(a, b, tol=1e-3):
        return abs(a - b) < tol

    if "op_basic" in filename:
        if "node_a" not in nodes or not is_close(nodes["node_a"], 1.0):
            log(f"ERROR: node_a voltage {nodes.get('node_a')} != 1.0")
            return False
        if "node_b" not in nodes or not is_close(nodes["node_b"], 0.66667):
            log(f"ERROR: node_b voltage {nodes.get('node_b')} != 0.66667")
            return False
        if "v1" not in branches or not is_close(branches["v1"], -0.033333):
            log(f"ERROR: v1 current {branches.get('v1')} != -0.033333")
            return False

    elif "op_with_probes" in filename:
        if "node_a" not in nodes or not is_close(nodes["node_a"], 2.0):
            log(f"ERROR: node_a voltage {nodes.get('node_a')} != 2.0")
            return False
        if "node_b" not in nodes or not is_close(nodes["node_b"], 1.0):
            log(f"ERROR: node_b voltage {nodes.get('node_b')} != 1.0")
            return False
        if "v1" not in branches or not is_close(branches["v1"], -0.2):
            log(f"ERROR: v1 current {branches.get('v1')} != -0.2")
            return False
        if "v2" not in branches or not is_close(branches["v2"], 0.2):
            log(f"ERROR: v2 current {branches.get('v2')} != 0.2")
            return False

    # Verify measurements
    if not verify_meas_file(filename, log):
        return False

    log("VERIFICATION PASSED")
    return True


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


def verify_meas_file(filename, log):
    def is_close(a, b, tol=1e-3):
        return abs(a - b) < tol

    for ext in [".ms0", ".mt0", ".ma0"]:
        meas_path = filename.rsplit(".", 1)[0] + ext
        if os.path.exists(meas_path):
            meas = parse_meas_file(meas_path)
            if meas is not None:
                log(f"  Parsed measurements from {os.path.basename(meas_path)}: {meas}")
                if "dc_meas" in filename and "sweep" not in filename:
                    if "v_out_max" not in meas or not is_close(
                        meas["v_out_max"], 3.3333
                    ):
                        log(f"ERROR: v_out_max {meas.get('v_out_max')} != 3.3333")
                        return False
                elif "ac_meas" in filename and "sweep" not in filename:
                    if "v_out_at_1k" not in meas or not is_close(
                        meas["v_out_at_1k"], 0.998
                    ):
                        log(f"ERROR: v_out_at_1k {meas.get('v_out_at_1k')} != 0.998")
                        return False
                elif "tran_meas" in filename and "sweep" not in filename:
                    if "v_out_max" not in meas or not is_close(
                        meas["v_out_max"], 0.66667
                    ):
                        log(f"ERROR: v_out_max {meas.get('v_out_max')} != 0.66667")
                        return False
                # Swept measurements:
                elif "dc_meas_sweep" in filename:
                    expected = [3.3333, 2.5, 2.0]
                    vals = meas.get("v_out_max")
                    if (
                        not isinstance(vals, list)
                        or len(vals) != len(expected)
                        or not all(is_close(x, y) for x, y in zip(vals, expected))
                    ):
                        log(f"ERROR: v_out_max {vals} != expected {expected}")
                        return False
                elif "ac_meas_sweep" in filename:
                    expected = [0.998, 0.9922, 0.9827]
                    vals = meas.get("v_out_at_1k")
                    if (
                        not isinstance(vals, list)
                        or len(vals) != len(expected)
                        or not all(is_close(x, y) for x, y in zip(vals, expected))
                    ):
                        log(f"ERROR: v_out_at_1k {vals} != expected {expected}")
                        return False
                elif "tran_meas_sweep" in filename:
                    expected = [0.66667, 0.5, 0.4]
                    vals = meas.get("v_out_max")
                    if (
                        not isinstance(vals, list)
                        or len(vals) != len(expected)
                        or not all(is_close(x, y) for x, y in zip(vals, expected))
                    ):
                        log(f"ERROR: v_out_max {vals} != expected {expected}")
                        return False
    return True


def verify_file(filename, log):
    log(f"\n{'='*70}")
    log(f"VERIFYING: {filename}")
    log(f"{'='*70}")

    file_size = os.path.getsize(filename)
    log(f"File size: {file_size} bytes")

    with open(filename, "rb") as f:

        # -------------------------------------------------------
        # Detect ASCII vs binary
        # -------------------------------------------------------
        first = f.read(1)
        if not first:
            log("ERROR: empty file")
            return False
        if first[0] >= 32:
            if filename.endswith(".ic0") or ".ic" in filename:
                return verify_op_file(filename, log)
            log("Skipping: ASCII format file")
            return True
        f.seek(0)

        # -------------------------------------------------------
        # Header block
        # -------------------------------------------------------
        head_raw = f.read(16)
        if len(head_raw) < 16:
            log("ERROR: truncated header block")
            return False
        e1, rc, e2, payload_size = struct.unpack("<IIII", head_raw)

        if e1 == 0x00000004 and e2 == 0x00000004:
            fmt = "<"
            log("Endianness: Little-Endian")
        elif e1 == 0x04000000 and e2 == 0x04000000:
            fmt = ">"
            e1, rc, e2, payload_size = struct.unpack(">IIII", head_raw)
            log("Endianness: Big-Endian")
        else:
            log(f"ERROR: bad endian markers 0x{e1:08x}/0x{e2:08x}")
            return False

        log(f"Header Block Head: RecordCount={rc}, PayloadSize={payload_size}")
        payload = f.read(payload_size)
        if len(payload) < payload_size:
            log("ERROR: truncated header payload")
            return False

        tail_raw = f.read(4)
        tail = struct.unpack(fmt + "I", tail_raw)[0]
        if tail != payload_size:
            log(f"ERROR: header head/tail mismatch ({payload_size} vs {tail})")
            return False

        term_idx = payload.find(b"$&%#")
        if term_idx < 0:
            log("ERROR: '$&%#' termination marker not found in header payload")
            return False
        log(f"Header '$&%#' terminator at offset {term_idx}")

        try:
            meta = parse_header_payload(payload)
        except Exception as e:
            log(f"ERROR parsing header: {e}")
            return False

        log(f"  Version:       {meta['version']}")
        log(f"  Title:         {meta['title']}")
        log(f"  Date:          {meta['date']}")
        log(f"  Num Variables: {meta['num_vars']}")
        log(f"  Num Probes:    {meta['num_probes']}")
        log(f"  Num Sweeps:    {meta['num_sweeps']}")
        log(f"  Sweep Size:    {meta['sweep_size']}")
        log(f"  Total Vectors: {meta['num_vectors']}")
        log(f"  Var Names:     {meta['var_names']}")
        log(f"  Var Types:     {meta['var_types']}")

        var_sizes, row_size, is_complex = row_sizes_for(meta)
        log(f"Analysis type:   {'COMPLEX (AC)' if is_complex else 'REAL (DC/TRAN)'}")
        log(f"Var byte sizes:  {var_sizes}  =>  row_size={row_size} bytes")

        # -------------------------------------------------------
        # Data blocks: accumulate by sweep table
        # Each sweep table ends with a dedicated termination block.
        # -------------------------------------------------------
        version = meta["version"]
        tables_raw = []  # list of bytearray, one per sweep table
        current = bytearray()
        block_idx = 0

        while True:
            try:
                blk_payload, blk_size = read_block(f, fmt)
            except IOError as e:
                log(f"ERROR reading block {block_idx + 1}: {e}")
                return False

            if blk_payload is None:
                # Clean EOF — if there is accumulated data treat it as final table
                if current:
                    tables_raw.append(current)
                break

            block_idx += 1

            if is_term_block(blk_payload, version, fmt):
                # This block is a pure termination marker — close current table
                tables_raw.append(current)
                current = bytearray()
            else:
                current.extend(blk_payload)

        log(
            f"Read {block_idx} data blocks => {len(tables_raw)} sweep tables (expected {meta['sweep_size']})"
        )

        if len(tables_raw) != meta["sweep_size"]:
            log(
                f"ERROR: got {len(tables_raw)} tables but expected {meta['sweep_size']}"
            )
            return False

        # -------------------------------------------------------
        # Validate each sweep table
        # -------------------------------------------------------
        for t_idx, t_raw in enumerate(tables_raw):
            log(f"  Table {t_idx}: raw_size={len(t_raw)} bytes")
            offset = 0

            # Sweep value width:
            #   2001 format stores sweep value as 8-byte double
            #   all other formats (2013, 9601, 9007) use 4-byte float
            if meta["num_sweeps"] > 0:
                if meta["version"] == "2001":
                    if len(t_raw) < 8:
                        log(f"    ERROR: too short for 8-byte sweep value")
                        return False
                    sweep_val = struct.unpack(fmt + "d", t_raw[:8])[0]
                    log(f"    Sweep value (double): {sweep_val:.4f}")
                    offset += 8
                else:
                    if len(t_raw) < 4:
                        log(f"    ERROR: too short for 4-byte sweep value")
                        return False
                    sweep_val = struct.unpack(fmt + "f", t_raw[:4])[0]
                    log(f"    Sweep value (float): {sweep_val:.4f}")
                    offset += 4

            data_len = len(t_raw) - offset

            if data_len % row_size != 0:
                log(
                    f"    ERROR: data_len={data_len} is not a multiple of row_size={row_size} (rem={data_len % row_size})"
                )
                return False

            num_rows = data_len // row_size
            log(f"    Rows: {num_rows}")

            if num_rows == 0:
                log("    WARNING: table contains no data rows")
                continue

            # First row
            sv, cv = parse_row(t_raw, offset, meta, var_sizes, is_complex, fmt)
            log(f"    First row: {meta['var_names'][0]}={sv:.4e}  circuit={cv}")

            # Last row
            if num_rows > 1:
                last_off = offset + (num_rows - 1) * row_size
                sv_last, cv = parse_row(
                    t_raw, last_off, meta, var_sizes, is_complex, fmt
                )
                log(
                    f"    Last  row: {meta['var_names'][0]}={sv_last:.4e}  circuit={cv}"
                )

    # Verify measurements
    if not verify_meas_file(filename, log):
        return False

    log("VERIFICATION PASSED")
    return True


def main():
    logs_dir = "logs"
    os.makedirs(logs_dir, exist_ok=True)

    log_path = os.path.join(logs_dir, "verify_spec.log")

    with open(log_path, "w") as lf:

        def log(msg):
            print(msg)
            lf.write(msg + "\n")
            lf.flush()

        log("=" * 70)
        log("HSPICE Binary Specification Verification Report")
        log("=" * 70)
        log(f"Work directory: {os.path.abspath(WORK_DIR)}")

        bin_files = sorted(
            glob.glob(os.path.join(WORK_DIR, "*.sw*"))
            + glob.glob(os.path.join(WORK_DIR, "*.ac*"))
            + glob.glob(os.path.join(WORK_DIR, "*.tr*"))
            + glob.glob(os.path.join(WORK_DIR, "*.ic*"))
        )
        bin_files = [
            f
            for f in bin_files
            if not f.endswith((".sp", ".lis", ".py", ".sh", ".log"))
        ]

        if not bin_files:
            log("No binary files found!")
            sys.exit(1)

        passed = 0
        failed = 0

        for f in bin_files:
            try:
                ok = verify_file(f, log)
            except Exception as e:
                log(f"CRITICAL ERROR for {f}: {e}")
                lf.write(traceback.format_exc())
                ok = False

            if ok:
                passed += 1
            else:
                failed += 1

        log("\n" + "=" * 70)
        log(f"Summary: {passed} passed, {failed} failed out of {len(bin_files)} files")
        log("=" * 70)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
