import os
import sys
import subprocess
import shutil

# Verify that hspice executable is available
HSPICE_BIN = shutil.which("hspice")

# Define the simulation option space templates
# Format: { case_name: { "type": "dc"|"ac"|"tran", "template": SPICE_netlist } }
# {version} will be substituted by the loop.
templates = {
  "dc_basic": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Basic Sweep
.option post=1 post_version={version}
v1 node_a 0 1.0
r1 node_a 0 10.0
.dc v1 1.0 5.0 1.0
.end
"""
  },
  "dc_sweep_temp": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Nested Sweep Temperature
.option post=1 post_version={version}
v1 node_a 0 1.0
r1 node_a 0 10.0
.dc temp -40 120 40
.end
"""
  },
  "dc_sweep_param": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Parameter Sweep
.option post=1 post_version={version}
.param rval=10.0
v1 node_a 0 1.0
r1 node_a 0 rval
.dc rval 10.0 50.0 10.0
.end
"""
  },
  "dc_nested_sweep": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Nested Source and Parameter Sweep
.option post=1 post_version={version}
.param rval=10.0
v1 node_a 0 1.0
r1 node_a 0 rval
.dc v1 1.0 5.0 1.0 sweep rval 10.0 30.0 10.0
.end
"""
  },
  "dc_probes_only": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Probe Only
.option post=1 post_version={version} probe
v1 node_a 0 1.0
r1 node_a node_b 10.0
r2 node_b 0 20.0
.dc v1 1.0 5.0 1.0
.probe dc v(node_a) v(node_b) v(node_a,node_b) i(v1) i(r1) p(r1)
.end
"""
  },
  "dc_probe_and_sweep": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Probe and Parameter Sweep Combination
.option post=1 post_version={version} probe
.param rval=10.0
v1 node_a 0 1.0
r1 node_a node_b rval
r2 node_b 0 20.0
.dc v1 1.0 5.0 1.0 sweep rval 10.0 30.0 10.0
.probe dc v(node_a) v(node_b) i(v1) i(r1)
.end
"""
  },
  "dc_monte": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Monte Carlo Sweep
.option post=1 post_version={version}
.param rval=gauss(10.0, 1.0, 3)
v1 node_a 0 1.0
r1 node_a 0 rval
.dc v1 1.0 5.0 1.0 sweep monte=5
.end
"""
  },
  "ac_basic": {
    "type": "ac",
    "suffix": "ac0",
    "netlist": """* AC Basic Sweep
.option post=1 post_version={version}
v1 node_a 0 ac 1.0
r1 node_a node_b 10.0
c1 node_b 0 1u
.ac dec 5 100 10k
.end
"""
  },
  "ac_sweep_temp": {
    "type": "ac",
    "suffix": "ac0",
    "netlist": """* AC Sweep with Temperature
.option post=1 post_version={version}
v1 node_a 0 ac 1.0
r1 node_a node_b 10.0
c1 node_b 0 1u
.ac dec 5 100 10k sweep temp -40 120 40
.end
"""
  },
  "ac_sweep_param": {
    "type": "ac",
    "suffix": "ac0",
    "netlist": """* AC Sweep with Parameter
.option post=1 post_version={version}
.param rval=10.0
v1 node_a 0 ac 1.0
r1 node_a node_b rval
c1 node_b 0 1u
.ac dec 5 100 10k sweep rval 10.0 30.0 10.0
.end
"""
  },
  "ac_probes_only": {
    "type": "ac",
    "suffix": "ac0",
    "netlist": """* AC Probe Only
.option post=1 post_version={version} probe
v1 node_a 0 ac 1.0
r1 node_a node_b 10.0
c1 node_b 0 1u
.ac dec 5 100 10k
.probe ac v(node_a) v(node_b) v(node_a,node_b) i(v1) i(r1)
.end
"""
  },
  "ac_probe_and_sweep": {
    "type": "ac",
    "suffix": "ac0",
    "netlist": """* AC Probe and Parameter Sweep Combination
.option post=1 post_version={version} probe
.param rval=10.0
v1 node_a 0 ac 1.0
r1 node_a node_b rval
c1 node_b 0 1u
.ac dec 5 100 10k sweep rval 10.0 30.0 10.0
.probe ac v(node_a) v(node_b) i(v1)
.end
"""
  },
  "tran_basic": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient Basic
.option post=1 post_version={version}
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b 10.0
r2 node_b 0 20.0
.tran 1n 20n
.end
"""
  },
  "tran_sweep_temp": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient with Temperature Sweep
.option post=1 post_version={version}
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b 10.0
r2 node_b 0 20.0
.tran 1n 20n sweep temp -40 120 40
.end
"""
  },
  "tran_sweep_param": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient with Parameter Sweep
.option post=1 post_version={version}
.param rval=10.0
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b rval
r2 node_b 0 20.0
.tran 1n 20n sweep rval 10.0 30.0 10.0
.end
"""
  },
  "tran_sweep_source": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient with Source Sweep
.option post=1 post_version={version}
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b 10.0
r2 node_b 0 20.0
v2 node_c 0 1.0
.tran 1n 20n sweep v2 1.0 3.0 1.0
.end
"""
  },
  "tran_probes_only": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient Probe Only
.option post=1 post_version={version} probe
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b 10.0
r2 node_b 0 20.0
.tran 1n 20n
.probe tran v(node_a) v(node_b) v(node_a,node_b) i(v1) i(r1) p(r1)
.end
"""
  },
  "tran_probe_and_sweep": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient Probe and Parameter Sweep Combination
.option post=1 post_version={version} probe
.param rval=10.0
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b rval
r2 node_b 0 20.0
.tran 1n 20n sweep rval 10.0 30.0 10.0
.probe tran v(node_a) v(node_b) i(v1)
.end
"""
  },
  "tran_monte": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient Monte Carlo Sweep
.option post=1 post_version={version}
.param rval=gauss(10.0, 1.0, 3)
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a 0 rval
.tran 1n 20n sweep monte=5
.end
"""
  },
  "op_basic": {
    "type": "op",
    "suffix": "ic0",
    "netlist": """* Operating Point Basic
.option post=1 post_version={version}
v1 node_a 0 1.0
r1 node_a node_b 10.0
r2 node_b 0 20.0
.op
.end
"""
  },
  "op_with_probes": {
    "type": "op",
    "suffix": "ic0",
    "netlist": """* Operating Point with Probes and Currents
.option post=1 post_version={version}
v1 node_a 0 2.0
r1 node_a node_b 5.0
v2 node_b 0 1.0
.op
.end
"""
  },
  "dc_meas": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Sweep with Measurement
.option post=1 post_version={version}
v1 node_a 0 1.0
r1 node_a node_b 10.0
r2 node_b 0 20.0
.dc v1 1.0 5.0 1.0
.meas dc v_out_max max v(node_b)
.end
"""
  },
  "ac_meas": {
    "type": "ac",
    "suffix": "ac0",
    "netlist": """* AC Sweep with Measurement
.option post=1 post_version={version}
v1 node_a 0 ac 1.0
r1 node_a node_b 10.0
c1 node_b 0 1u
.ac dec 5 100 10k
.meas ac v_out_at_1k find v(node_b) at 1000
.end
"""
  },
  "tran_meas": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient with Measurement
.option post=1 post_version={version}
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b 10.0
r2 node_b 0 20.0
.tran 1n 20n
.meas tran v_out_max max v(node_b)
.end
"""
  },
  "dc_meas_sweep": {
    "type": "dc",
    "suffix": "sw0",
    "netlist": """* DC Sweep with Outer Param Sweep and Measurement
.option post=1 post_version={version}
.param rval=10.0
v1 node_a 0 1.0
r1 node_a node_b rval
r2 node_b 0 20.0
.dc v1 1.0 5.0 1.0 sweep rval 10.0 30.0 10.0
.meas dc v_out_max max v(node_b)
.end
"""
  },
  "ac_meas_sweep": {
    "type": "ac",
    "suffix": "ac0",
    "netlist": """* AC Sweep with Outer Param Sweep and Measurement
.option post=1 post_version={version}
.param rval=10.0
v1 node_a 0 ac 1.0
r1 node_a node_b rval
c1 node_b 0 1u
.ac dec 5 100 10k sweep rval 10.0 30.0 10.0
.meas ac v_out_at_1k find v(node_b) at 1000
.end
"""
  },
  "tran_meas_sweep": {
    "type": "tran",
    "suffix": "tr0",
    "netlist": """* Transient Sweep with Outer Param Sweep and Measurement
.option post=1 post_version={version}
.param rval=10.0
v1 node_a 0 pulse(0 1.0 0 1n 1n 10n 20n)
r1 node_a node_b rval
r2 node_b 0 20.0
.tran 1n 20n sweep rval 10.0 30.0 10.0
.meas tran v_out_max max v(node_b)
.end
"""
  }
}

versions = ["9007", "9601", "2001", "2013", "ascii"]

def main():
  # Ensure the logs directory exists and is clean
  logs_dir = "logs"
  if os.path.exists(logs_dir):
    shutil.rmtree(logs_dir)
  os.makedirs(logs_dir, exist_ok=True)

  # Ensure the work directory exists and is clean
  work_dir = "work"
  if os.path.exists(work_dir):
    shutil.rmtree(work_dir)
  os.makedirs(work_dir, exist_ok=True)

  # Summary log file
  run_log_path = os.path.join(logs_dir, "run_simulations.log")
  run_log = open(run_log_path, "w")

  def log_and_print(msg):
    print(msg)
    run_log.write(msg + "\n")
    run_log.flush()

  log_and_print("==================================================")
  log_and_print("HSPICE Simulation Runner for Option Space Coverage")
  log_and_print("==================================================")
  log_and_print(f"HSPICE Executable: {HSPICE_BIN}")

  passed_count = 0
  failed_count = 0

  for case_name, info in templates.items():
    for version in versions:
      name_prefix = f"{case_name}_{version}"
      sp_filename = os.path.join(work_dir, f"{name_prefix}.sp")
      lis_filename = os.path.join(work_dir, f"{name_prefix}.lis")

      # Write netlist to .sp file
      if version == "ascii":
        netlist_content = info["netlist"].replace(".option post=1 post_version={version}", ".option post=2")
      else:
        netlist_content = info["netlist"].format(version=version)

      with open(sp_filename, "w") as f:
        f.write(netlist_content)

      log_and_print(f"\nRunning case: {name_prefix}...")

      # Command to run HSPICE
      cmd = [HSPICE_BIN, "-i", sp_filename, "-o", os.path.join(work_dir, name_prefix)]

      try:
        # Run with timeout to prevent hanging if license is busy
        result = subprocess.run(
          cmd,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          text=True,
          timeout=30
        )

        # Save command output to logs
        cmd_log_path = os.path.join(logs_dir, f"{name_prefix}_cmd.log")
        with open(cmd_log_path, "w") as lf:
          lf.write(f"COMMAND: {' '.join(cmd)}\n")
          lf.write(f"RETURN CODE: {result.returncode}\n")
          lf.write("--- STDOUT ---\n")
          lf.write(result.stdout)
          lf.write("--- STDERR ---\n")
          lf.write(result.stderr)

        # Check if run was successful and expected binary file was created
        expected_bin = os.path.join(work_dir, f"{name_prefix}.{info['suffix']}")
        if result.returncode != 0:
          log_and_print(f"  [FAILED] HSPICE execution exited with non-zero code {result.returncode}!")
          failed_count += 1
        elif os.path.exists(expected_bin):
          log_and_print(f"  [SUCCESS] Created {expected_bin}")
          passed_count += 1
        else:
          log_and_print(f"  [FAILED] Binary/ASCII file {expected_bin} was not created!")
          failed_count += 1

      except subprocess.TimeoutExpired:
        log_and_print(f"  [TIMEOUT] HSPICE execution timed out for {name_prefix}")
        failed_count += 1
      except Exception as e:
        log_and_print(f"  [ERROR] Failed to execute {name_prefix}: {str(e)}")
        failed_count += 1

  log_and_print("\n==================================================")
  log_and_print(f"Simulation Summary: {passed_count} passed, {failed_count} failed")
  log_and_print("==================================================")
  run_log.close()

  if failed_count > 0:
    sys.exit(1)
  else:
    sys.exit(0)

if __name__ == "__main__":
  main()
