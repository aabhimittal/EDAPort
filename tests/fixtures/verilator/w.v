module w(input clk, input [3:0] a, output reg [2:0] y);
  wire unused_x = a[0];
  always @(posedge clk) y <= a + 1;
  always @(posedge clk) y = a;
  undefined_mod u0(.x(a));
endmodule
