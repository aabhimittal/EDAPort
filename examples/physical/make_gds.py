# Regenerate demo.gds: klayout -b -r make_gds.py -rd out=demo.gds
import pya
ly = pya.Layout(); ly.dbu = 0.001
top = ly.create_cell("TOP"); m1 = ly.layer(68, 20)
top.shapes(m1).insert(pya.Box(0, 0, 1000, 200))
top.shapes(m1).insert(pya.Box(0, 250, 1000, 450))   # 0.05um gap: spacing violation
top.shapes(m1).insert(pya.Box(2000, 0, 2100, 1000)) # 0.1um wide: width violation
ly.write(out)
