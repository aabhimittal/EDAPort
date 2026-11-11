"""cocotb version of tb_counter.sv: sim_run(tb_ref="tb/test_counter.py", top="counter")."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer


async def reset(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.en.value = 0
    for _ in range(2):
        await RisingEdge(dut.clk)
    await Timer(1, unit="ns")
    dut.rst.value = 0
    dut.en.value = 1


@cocotb.test()
async def count_stays_in_range(dut):
    await reset(dut)
    for _ in range(30):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        assert int(dut.count.value) <= 9, f"count={int(dut.count.value)} exceeds MAX=9"


@cocotb.test()
async def reset_clears(dut):
    await reset(dut)
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await Timer(1, unit="ns")
    assert int(dut.count.value) == 0
