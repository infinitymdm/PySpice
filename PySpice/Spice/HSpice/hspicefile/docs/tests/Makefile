# Default target (running 'make' with no arguments will trigger this)
.PHONY: all test clean
all: test

# 1. This runs only if run_simulations.py changes
simulate: run_simulations.py
	python3 ./run_simulations.py
	touch simulate

# 2. This runs if the 'simulate' file was just updated, or if verify_spec.py changes
verify: simulate verify_spec.py
	python3 ./verify_spec.py
	python3 ./verify_all_types.py
	touch verify

# 3. This runs if the 'verify' file was just updated, or if test_hspice_read.py changes
test: verify test_hspice_read.py
	python3 ./test_hspice_read.py

# Clean up build artifacts and sentinel files
clean:
	rm -rf work/ logs/ simulate verify
