PY ?= python3

.PHONY: install test test-fast demo demo-orfs eval-ab orfs-image serve

install:
	$(PY) -m pip install -e ".[test]"

test:            ## all tests (ORFS tests auto-skip without docker + the pinned image)
	$(PY) -m pytest -q

test-fast:       ## skip the ORFS/OpenSTA tests
	$(PY) -m pytest -q -m "not orfs"

demo:            ## counter design: lint -> sim -> formal -> fix on a branch -> synth -> equiv -> replay
	$(PY) examples/demo.py

demo-orfs:       ## + gcd/sky130hd through ORFS stages and OpenSTA
	$(PY) examples/demo.py --orfs

eval-ab:         ## A/B observation-cost eval + determinism rate -> evals/ab_interface/results.md
	$(PY) evals/ab_interface/run.py --orfs

orfs-image:      ## pull the digest-pinned ORFS image
	docker pull $$($(PY) -c "import json;print(json.load(open('images/images.lock.json'))['orfs']['image'])")

serve:
	edaport serve
