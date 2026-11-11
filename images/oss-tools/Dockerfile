# Lint/synth/sim/formal/equiv/DRC/LVS toolchain. Every layer is pinned: base by digest, apt packages by version,
# SymbiYosys by commit. Rebuild => same tools; record the resulting digest in images/images.lock.json.
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        yosys=0.33-5build2 iverilog=12.0-2build2 verilator=5.020-1 z3=4.8.12-3.1build1 \
        klayout=0.28.16-0ubuntu0.24.04.1 netgen-lvs=1.5.133-1.2 \
        python3 python3-pip make g++ git ca-certificates \
    && pip3 install --no-cache-dir --break-system-packages cocotb==2.1.0 \
    && git clone https://github.com/YosysHQ/sby.git /tmp/sby \
    && git -C /tmp/sby checkout 85dd50b92a3acf170954a06f20f49e2162ce5273 \
    && make -C /tmp/sby install PREFIX=/usr/local \
    && rm -rf /tmp/sby /var/lib/apt/lists/*
WORKDIR /work
