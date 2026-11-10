# ORFS design config. EDAPort mounts the snapshot at $(DESIGN_DIR) (= /work in the ORFS container).
export DESIGN_NAME = gcd
export DESIGN_NICKNAME = gcd
export PLATFORM    = sky130hd

export VERILOG_FILES = $(DESIGN_DIR)/src/gcd.v
export SDC_FILE      = $(DESIGN_DIR)/constraint.sdc

export ADDER_MAP_FILE :=
export CORE_UTILIZATION = 38
export TNS_END_PERCENT = 100
