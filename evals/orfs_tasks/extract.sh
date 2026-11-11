#!/usr/bin/env bash
# Regenerate the task snapshots from the pinned ORFS image: per design a wrapper config.mk plus the
# constraint file copied into the snapshot (so an agent can edit it). RTL stays in the image, pinned by digest.
set -euo pipefail
IMG=$(python3 -c "import json;print(json.load(open('$(dirname "$0")/../../images/images.lock.json'))['orfs']['image'])")
cd "$(dirname "$0")"
for p in sky130hd asap7; do for d in gcd aes ibex jpeg riscv32i; do
  mkdir -p "${p}_${d}"
  sdc=$(docker run --rm "$IMG" bash -c "source env.sh >/dev/null; make -s -C flow DESIGN_CONFIG=designs/$p/$d/config.mk print-SDC_FILE" | sed 's/^SDC_FILE[: =]*//')
  docker run --rm "$IMG" cat "$sdc" > "${p}_${d}/constraint.sdc"
  printf '# %s on %s: ORFS reference design from the pinned image; only the constraints live in the snapshot.\ninclude /OpenROAD-flow-scripts/flow/designs/%s/%s/config.mk\nexport SDC_FILE = $(DESIGN_DIR)/constraint.sdc\n' "$d" "$p" "$p" "$d" > "${p}_${d}/config.mk"
done; done
sha256sum */config.mk */constraint.sdc > TASKS.sha256
