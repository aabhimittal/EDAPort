`timescale 1ns/1ps
module tb;
    reg clk = 0, rst = 1, en = 0;
    wire [7:0] count;
    wire wrap;
    integer seed = 1, i, cycles, fails = 0;
    counter #(.WIDTH(8), .MAX(9)) dut (.clk(clk), .rst(rst), .en(en), .count(count), .wrap(wrap));
    always #5 clk = ~clk;

    initial begin
        if (!$value$plusargs("seed=%d", seed)) seed = 1;
        $dumpfile("sim.vcd");
        $dumpvars(0, tb);
        repeat (2) @(posedge clk);
        #1 rst = 0;
        en = 1;
        // test 1: count stays within [0, MAX] for a randomized number of cycles
        cycles = 20 + ($urandom(seed) % 20);
        for (i = 0; i < cycles; i = i + 1) begin
            @(posedge clk); #1;
            if (count > 9 && fails == 0) begin
                $display("TEST range FAIL @%0d count=%0d exceeds MAX=9", $time, count);
                fails = fails + 1;
            end
        end
        if (fails == 0) $display("TEST range PASS @%0d", $time);
        // test 2: reset clears the counter
        rst = 1; @(posedge clk); #1; rst = 0;
        if (count == 0) $display("TEST reset PASS @%0d", $time);
        else $display("TEST reset FAIL @%0d count=%0d after reset", $time, count);
        $finish;
    end
endmodule
