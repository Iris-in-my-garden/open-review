# Diff 行数与 reference comment 校验

统计数据基于当前 `collect_local_changes`（三点 diff，pinned base/head）。

`change_line_count` 是 GitHub PR **最终 head** 的 additions+deletions；
manifest 的 `head_commit` 是 reference comment 提交时的 commit（`original_commit_id`）。
因此两者不应逐条相等；diff 正确性应看 reference comment 的 path/line 是否落在 pinned diff 内。

- instances: 40
- reference comments: 234
- path 不在 diff: 0
- 行号不在可见行: 2
- 行号合法: 232/234

| instance | split | change_line_count | pinned 3-dot | delta | files | refs | bad_path | bad_line |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| mudler__LocalAI@e466450 | dev | 185 | 209 | +24 | 20 | 15 | 0 | 0 |
| ollama__ollama@0b52f18 | dev | 132 | 68 | -64 | 4 | 3 | 0 | 0 |
| ollama__ollama@2ed14f2 | dev | 716 | 725 | +9 | 9 | 20 | 0 | 1 |
| ollama__ollama@498792c | dev | 776 | 696 | -80 | 8 | 3 | 0 | 0 |
| ollama__ollama@af112d6 | dev | 132 | 236 | +104 | 2 | 5 | 0 | 0 |
| ollama__ollama@b6002f6 | dev | 64 | 197 | +133 | 23 | 12 | 0 | 0 |
| ollama__ollama@c2d08dd | dev | 24 | 74 | +50 | 5 | 2 | 0 | 0 |
| wavetermdev__waveterm@4b7637f | dev | 792 | 379 | -413 | 13 | 11 | 0 | 0 |
| wavetermdev__waveterm@61b0fb4 | dev | 808 | 810 | +2 | 22 | 18 | 0 | 0 |
| wavetermdev__waveterm@90a57d5 | dev | 522 | 468 | -54 | 13 | 5 | 0 | 0 |
| browser-use__browser-use@0b21e50 | dev | 133 | 135 | +2 | 5 | 2 | 0 | 0 |
| browser-use__browser-use@72f1585 | dev | 324 | 328 | +4 | 1 | 2 | 0 | 0 |
| browser-use__browser-use@aab7b3b | dev | 95 | 97 | +2 | 11 | 1 | 0 | 0 |
| comfyanonymous__ComfyUI@0bb6023 | dev | 30 | 31 | +1 | 9 | 4 | 0 | 0 |
| comfyanonymous__ComfyUI@4dcd698 | dev | 971 | 996 | +25 | 13 | 18 | 0 | 0 |
| comfyanonymous__ComfyUI@b55c224 | dev | 15 | 15 | +0 | 3 | 2 | 0 | 0 |
| langflow-ai__langflow@6239290 | dev | 387 | 395 | +8 | 3 | 6 | 0 | 0 |
| langflow-ai__langflow@9513d15 | dev | 37 | 48 | +11 | 2 | 4 | 0 | 0 |
| langflow-ai__langflow@b1cbdba | dev | 22 | 26 | +4 | 1 | 3 | 0 | 0 |
| langflow-ai__langflow@bcfe6f9 | dev | 882 | 847 | -35 | 20 | 9 | 0 | 0 |
| gofr-dev__gofr@2c0c099 | holdout | 348 | 457 | +109 | 24 | 4 | 0 | 0 |
| gofr-dev__gofr@49ab394 | holdout | 554 | 492 | -62 | 4 | 5 | 0 | 1 |
| gofr-dev__gofr@5e284ae | holdout | 356 | 373 | +17 | 5 | 4 | 0 | 0 |
| gofr-dev__gofr@b9cdcbb | holdout | 215 | 215 | +0 | 5 | 2 | 0 | 0 |
| gofr-dev__gofr@c1a26ad | holdout | 217 | 112 | -105 | 7 | 5 | 0 | 0 |
| gofr-dev__gofr@dc0b3e6 | holdout | 309 | 224 | -85 | 2 | 6 | 0 | 0 |
| microsoft__typescript-go@1a84d8d | holdout | 92 | 84 | -8 | 3 | 3 | 0 | 0 |
| microsoft__typescript-go@45c0de9 | holdout | 47 | 45 | -2 | 1 | 1 | 0 | 0 |
| microsoft__typescript-go@892b4a3 | holdout | 80 | 73 | -7 | 3 | 0 | 0 | 0 |
| microsoft__typescript-go@ae447d3 | holdout | 176 | 143 | -33 | 19 | 2 | 0 | 0 |
| microsoft__typescript-go@b970689 | holdout | 933 | 894 | -39 | 53 | 14 | 0 | 0 |
| infiniflow__ragflow@2d06022 | holdout | 65 | 67 | +2 | 3 | 1 | 0 | 0 |
| infiniflow__ragflow@7051234 | holdout | 37 | 37 | +0 | 4 | 6 | 0 | 0 |
| infiniflow__ragflow@a63caa1 | holdout | 11 | 14 | +3 | 3 | 6 | 0 | 0 |
| infiniflow__ragflow@f189316 | holdout | 207 | 123 | -84 | 2 | 10 | 0 | 0 |
| vllm-project__vllm@075f1b5 | holdout | 194 | 208 | +14 | 3 | 6 | 0 | 0 |
| vllm-project__vllm@134705b | holdout | 154 | 149 | -5 | 13 | 8 | 0 | 0 |
| vllm-project__vllm@3fe1893 | holdout | 51 | 20 | -31 | 3 | 4 | 0 | 0 |
| vllm-project__vllm@6217b0c | holdout | 12 | 12 | +0 | 1 | 1 | 0 | 0 |
| vllm-project__vllm@a800045 | holdout | 55 | 44 | -11 | 2 | 1 | 0 | 0 |
