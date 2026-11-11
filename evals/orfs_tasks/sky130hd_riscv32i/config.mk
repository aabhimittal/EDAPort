# riscv32i on sky130hd: ORFS reference design from the pinned image; only the constraints live in the snapshot.
include /OpenROAD-flow-scripts/flow/designs/sky130hd/riscv32i/config.mk
export SDC_FILE = $(DESIGN_DIR)/constraint.sdc
