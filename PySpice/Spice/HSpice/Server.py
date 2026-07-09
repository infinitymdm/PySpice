import fcntl
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from .hspicefile import hspice_read
from .RawFile import HSpiceRawFile

_module_logger = logging.getLogger(__name__)


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


def acquire_hspice_lock(limit, timeout=None):
    """
    Acquire one of the slot locks (0 to limit-1) using fcntl.flock.
    Returns (lock_file_descriptor, slot_index).
    Blocks until a slot becomes available.
    """
    lock_dir = os.path.join(
        tempfile.gettempdir(), f"pyspice_hspice_locks_{os.getuid()}"
    )
    os.makedirs(lock_dir, mode=0o700, exist_ok=True)

    # Ensure lock files exist with correct permissions
    lock_files = []
    for i in range(limit):
        path = os.path.join(lock_dir, f"lock_{i}")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        lock_files.append((fd, i))

    start = time.monotonic()
    while True:
        for fd, idx in lock_files:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for other_fd, other_idx in lock_files:
                    if other_idx != idx:
                        os.close(other_fd)
                return fd, idx
            except BlockingIOError:
                continue
        if timeout is not None and time.monotonic() - start > timeout:
            for fd, _ in lock_files:
                os.close(fd)
            raise TimeoutError("Timed out waiting for an HSPICE slot.")
        time.sleep(0.05)


class HSpiceServer:
    SPICE_COMMAND = "hspice"

    def __init__(self, **kwargs):
        self._spice_command = kwargs.get("spice_command") or self.SPICE_COMMAND
        concurrency_limit = kwargs.get("concurrency_limit")
        if concurrency_limit is None:
            concurrency_limit = os.environ.get("PYSPICE_HSPICE_CONCURRENCY_LIMIT")
        if concurrency_limit is None:
            concurrency_limit = 4
        self._concurrency_limit = int(concurrency_limit)
        if self._concurrency_limit < 1:
            raise ValueError("concurrency_limit must be at least 1")

        timeout = kwargs.get("timeout")
        if timeout is None:
            timeout = os.environ.get("PYSPICE_HSPICE_TIMEOUT")
        if timeout is None:
            timeout = 300
        self._timeout = float(timeout)

    def __call__(self, spice_input):
        logger = _module_logger.getChild("HSpiceServer")
        logger.info("Running HSPICE simulation")

        # Create temporary directory
        tmp_dir = tempfile.mkdtemp(prefix="pyspice_hspice_")
        try:
            input_file = os.path.join(tmp_dir, "input.sp")
            output_base = os.path.join(tmp_dir, "output")

            netlist_str = str(spice_input)
            if ".options post" not in netlist_str.lower():
                netlist_str += "\n.options post=1\n"

            with open(input_file, "w") as f:
                f.write(netlist_str)

            # Acquire the lock to limit the concurrency
            lock_fd, slot_idx = acquire_hspice_lock(self._concurrency_limit)

            try:
                # Submit the job
                # We run in the temporary directory to avoid cluttering current directory
                cmd = [self._spice_command, "-i", "input.sp", "-o", "output"]
                logger.info(f"Executing: {' '.join(cmd)}")
                try:
                    res = subprocess.run(
                        cmd,
                        cwd=tmp_dir,
                        capture_output=True,
                        text=True,
                        timeout=self._timeout,
                    )
                except subprocess.TimeoutExpired as e:
                    logger.error(
                        f"HSPICE simulation timed out after {self._timeout} seconds."
                    )
                    raise TimeoutError(
                        f"HSPICE simulation timed out after {self._timeout} seconds."
                    ) from e
            finally:
                os.close(lock_fd)

            lis_file = os.path.join(tmp_dir, "output.lis")
            lis_content = ""
            if os.path.exists(lis_file):
                with open(lis_file, "r", errors="ignore") as f:
                    lis_content = f.read()
                # Check for errors in lis file
                if (
                    "**error**" in lis_content.lower()
                    or "*error*" in lis_content.lower()
                    or "aborted" in lis_content.lower()
                ):
                    raise NameError(
                        f"HSPICE simulation failed. Log output:\n{lis_content}"
                    )

            if res.returncode != 0:
                raise NameError(
                    f"HSPICE process exited with code {res.returncode}.\n"
                    f"Stderr: {res.stderr}\n"
                    f"Stdout: {res.stdout}\n"
                    f"Log: {lis_content}"
                )

            # Find generated binary raw output files
            raw_file_path = None
            analysis_type = None
            # Prioritize standard waveform files: transient (.tr), ac (.ac), dc sweep (.sw)
            for ext in (".tr", ".ac", ".sw"):
                for f in os.listdir(tmp_dir):
                    if f.startswith("output.") and ext in f:
                        if (
                            f[-1].isdigit()
                            and not f.endswith(".lis")
                            and not f.endswith(".st0")
                            and not f.endswith(".ic0")
                            and not f.endswith(".mc0")
                        ):
                            raw_file_path = os.path.join(tmp_dir, f)

                            # Get "a,s or t" for analysis type
                            detected_type = ext[1]
                            if detected_type in "ast":
                                analysis_type = detected_type
                            break
                if raw_file_path:
                    break

            if raw_file_path is None:
                # Fallback to any file starting with output. and ending with a digit
                for f in os.listdir(tmp_dir):
                    if f.startswith("output.") and f[-1].isdigit():
                        if not (
                            f.endswith(".lis")
                            or f.endswith(".st0")
                            or f.endswith(".ic0")
                            or f.endswith(".mc0")
                            or f.endswith(".pa0")
                        ):
                            raw_file_path = os.path.join(tmp_dir, f)
                            break

            if raw_file_path is None or not os.path.exists(raw_file_path):
                # Let's check if it was an operating point simulation (OP)
                ic0_file = os.path.join(tmp_dir, "output.ic0")
                if os.path.exists(ic0_file) and os.path.exists(lis_file):
                    logger.info(
                        "Operating point simulation detected. Parsing .ic0 and .lis files..."
                    )

                    # Parse node voltages from .ic0
                    nodes = {}
                    with open(ic0_file, "r") as f:
                        lines = f.readlines()
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
                                    if node_name.startswith(
                                        "v("
                                    ) and node_name.endswith(")"):
                                        node_name = node_name[2:-1]
                                    try:
                                        node_val = parse_spice_value(parts[1].strip())
                                        nodes[node_name] = node_val
                                    except Exception:
                                        pass

                    # Parse branch currents from .lis
                    branches = {}
                    with open(lis_file, "r", errors="ignore") as f:
                        lis_content = f.read()

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
                                clean_el = re.sub(r"^\d+:", "", el).lower()
                                try:
                                    current_val = parse_spice_value(curr)
                                    branches[clean_el] = current_val
                                except Exception:
                                    pass

                    return HSpiceRawFile(
                        data=None,
                        op_nodes=nodes,
                        op_branches=branches,
                        analysis_type="o",
                    )
                else:
                    raise NameError(
                        f"No HSPICE raw output files (.tr0, .ac0, .sw0) were found in {tmp_dir}"
                    )

            logger.info(f"Reading output file: {raw_file_path}")
            data = hspice_read(raw_file_path, debug=0)
            if data is None:
                raise NameError(
                    f"Failed to parse HSPICE output file {raw_file_path} using hspice_read"
                )

            # Find and parse measurement files (e.g. output.mt0, output.ms0, output.ma0)
            measurements = {}
            for f in os.listdir(tmp_dir):
                if f.startswith("output.m") and f[-1].isdigit():
                    detected_type = f[-2]
                    if detected_type in "ast":
                        analysis_type = detected_type
                    meas_file_path = os.path.join(tmp_dir, f)
                    logger.info(f"Parsing measurement file: {meas_file_path}")
                    try:
                        with open(meas_file_path, "r") as mf:
                            lines = mf.readlines()
                        content_lines = [
                            l.strip()
                            for l in lines
                            if l.strip()
                            and not l.startswith("$")
                            and not l.startswith(".")
                        ]
                        all_tokens = []
                        for line in content_lines:
                            all_tokens.extend(line.split())

                        def is_numeric_value(val):
                            try:
                                float(val)
                                return True
                            except ValueError:
                                return val.lower() == "failed"

                        header_len = 0
                        for token in all_tokens:
                            if is_numeric_value(token):
                                break
                            header_len += 1

                        names = all_tokens[:header_len]
                        data_tokens = all_tokens[header_len:]

                        if names and data_tokens:
                            is_sweep = len(data_tokens) > header_len
                            for i, name in enumerate(names):
                                name_lower = name.lower()
                                if name_lower not in (
                                    "temper",
                                    "alter#",
                                    "temper#",
                                    "alter",
                                ):
                                    vals = []
                                    for j in range(i, len(data_tokens), header_len):
                                        val = data_tokens[j]
                                        try:
                                            vals.append(float(val))
                                        except ValueError:
                                            pass
                                    if vals:
                                        if is_sweep:
                                            measurements[name_lower] = vals
                                        else:
                                            measurements[name_lower] = vals[0]
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse measurement file {meas_file_path}: {e}"
                        )

            return HSpiceRawFile(
                data, measurements=measurements, analysis_type=analysis_type
            )

        finally:
            shutil.rmtree(tmp_dir)
