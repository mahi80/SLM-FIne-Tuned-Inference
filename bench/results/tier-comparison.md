| Target | Source | c | Requests | Req/s | Output tok/s | TTFT p50 | TTFT p95 | Total p50 | Total p95 | Errors |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gpu-direct c=1 | measured | 1 | 20/20 | 1.96 | 125.5 | 20 ms | 22 ms | 509 ms | 512 ms | 0 |
| gpu-direct c=4 | measured | 4 | 20/20 | 7.58 | 484.8 | 27 ms | 41 ms | 524 ms | 539 ms | 0 |
| gpu-direct c=12 | measured | 12 | 20/20 | 17.55 | 1123.2 | 53 ms | 72 ms | 569 ms | 589 ms | 0 |
| adapter-v2 c=1 | measured | 1 | 20/20 | 1.75 | 101.6 | 28 ms | 31 ms | 570 ms | 573 ms | 0 |
| adapter-v2 c=4 | measured | 4 | 20/20 | 6.71 | 389.2 | 49 ms | 59 ms | 590 ms | 606 ms | 0 |
| adapter-v2 c=12 | measured | 12 | 20/20 | 14.67 | 850.9 | 128 ms | 129 ms | 683 ms | 713 ms | 0 |
| cloud-sim c=1 | MODELLED | 1 | 20/20 | 1.27 | 81.4 | 25 ms | 27 ms | 785 ms | 794 ms | 0 |
| cloud-sim c=4 | MODELLED | 4 | 20/20 | 4.92 | 314.7 | 26 ms | 27 ms | 819 ms | 827 ms | 0 |
| cloud-sim c=12 | MODELLED | 12 | 20/20 | 12.32 | 788.4 | 30 ms | 36 ms | 810 ms | 817 ms | 0 |
| cpu-sim c=1 | MODELLED | 1 | 8/8 | 0.24 | 15.4 | 116 ms | 118 ms | 4141 ms | 4184 ms | 0 |
| cpu-sim c=4 | MODELLED | 4 | 8/8 | 0.96 | 61.7 | 117 ms | 120 ms | 4145 ms | 4154 ms | 0 |
| cpu-sim c=12 | MODELLED | 12 | 8/8 | 1.91 | 122.2 | 121 ms | 121 ms | 4185 ms | 4186 ms | 0 |
