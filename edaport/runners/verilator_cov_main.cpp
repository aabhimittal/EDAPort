// Verilator 5.0x `--binary` mains never write coverage.dat; sim_run(coverage=True) builds with this main instead.
#include <memory>
#include "verilated.h"
#include "verilated_cov.h"
#include TOP_HEADER
int main(int argc, char** argv) {
    const std::unique_ptr<VerilatedContext> ctx{new VerilatedContext};
    ctx->commandArgs(argc, argv);
    ctx->traceEverOn(true);
    const std::unique_ptr<TOP_CLASS> top{new TOP_CLASS{ctx.get()}};
    while (!ctx->gotFinish()) {
        top->eval();
        if (!top->eventsPending()) break;
        ctx->time(top->nextTimeSlot());
    }
    top->final();
    ctx->coveragep()->write("coverage.dat");
    return 0;
}
