# gcd on sky130hd (ORFS)

`src/gcd.v` and `constraint.sdc` are copied verbatim from OpenROAD-flow-scripts
(`flow/designs/src/gcd`, `flow/designs/sky130hd/gcd`) (see that repository for licensing).
`config.mk` is adapted so the design lives in the EDAPort snapshot (`$(DESIGN_DIR)`).

The 1.1 ns clock does not close (WNS ≈ -1 ns): a ready-made "close timing" task.
