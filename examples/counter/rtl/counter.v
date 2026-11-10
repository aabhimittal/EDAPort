// Wrapping up-counter. Demo bug: wraps one cycle late (count reaches MAX+1).
module counter #(parameter WIDTH = 8, parameter MAX = 9) (
    input  wire             clk,
    input  wire             rst,
    input  wire             en,
    output reg  [WIDTH-1:0] count,
    output wire             wrap
);
    wire [WIDTH-1:0] dbg = count ^ 8'h00;
    assign wrap = en && (count == MAX);
    always @(posedge clk) begin
        if (rst)
            count <= 0;
        else if (en)
            count <= (count > MAX) ? 0 : count + 1;
    end

`ifdef FORMAL
    reg past_valid = 0;
    always @(posedge clk) past_valid <= 1;
    always @(posedge clk) if (past_valid && !$past(rst)) count_in_range: assert (count <= MAX);
    initial assume (rst);
`endif
endmodule
