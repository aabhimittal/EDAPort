# Physical-verification demo inputs

- `demo.gds` (from `make_gds.py`): one met1 spacing violation, one met1 width violation.
- `demo.drc`: two-rule KLayout DRC deck (`m1.1` width, `m1.2` spacing).
- `inv_layout.spice` vs `inv_schematic.spice`: the layout's NMOS gate is tied to VDD instead of A, so LVS fails.
