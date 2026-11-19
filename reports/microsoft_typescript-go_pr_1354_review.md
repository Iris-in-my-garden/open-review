# PR Review Report

## 1. PR Summary
[SINGLE_SHOT] [MOCK] 本次审查完成，发现了一些需要注意的潜在问题。

[SINGLE_SHOT] [MOCK] 本次审查完成，发现了一些需要注意的潜在问题。

[SINGLE_SHOT] [MOCK] 本次审查完成，发现了一些需要注意的潜在问题。

## 2. Risk Level
- **Risk Level**: HIGH
- **Reason**: 共保留 6 个高置信度问题（Critical 0 / High 3 / Medium 3 / Low 0）。

## 3. Key Changes
- 代码评审已完成

## 4. Potential Issues
### Issue 1
- **file**: `cmd/tsgo/api.go:26`
- **severity**: high
- **category**: security
- **confidence**: 1.0
- **reasoning**: 发现硬编码的 JWT 密钥或敏感凭证，存在严重的安全隐患。
- **suggestion**: 请将密钥移除，并改用环境变量（如 os.environ.get('JWT_SECRET')）进行加载。
- **evidence**: `Out:                os.Stdout,`

### Issue 2
- **file**: `cmd/tsgo/sys.go:4`
- **severity**: medium
- **category**: clean_code
- **confidence**: 0.8
- **reasoning**: 函数体过长且嵌套层级较深，可读性较差。
- **suggestion**: 建议将核心逻辑拆分为独立的小函数，遵循单一职责原则（SRP）。
- **evidence**: `"fmt"`

### Issue 3
- **file**: `internal/execute/outputs.go:98`
- **severity**: high
- **category**: security
- **confidence**: 0.92
- **reasoning**: 发现硬编码的 JWT 密钥或敏感凭证，存在严重的安全隐患。
- **suggestion**: 请将密钥移除，并改用环境变量（如 os.environ.get('JWT_SECRET')）进行加载。
- **evidence**: `}`

### Issue 4
- **file**: `internal/execute/system.go:13`
- **severity**: medium
- **category**: clean_code
- **confidence**: 0.9
- **reasoning**: 函数体过长且嵌套层级较深，可读性较差。
- **suggestion**: 建议将核心逻辑拆分为独立的小函数，遵循单一职责原则（SRP）。
- **evidence**: `FS() vfs.FS`

### Issue 5
- **file**: `internal/lsp/server.go:29`
- **severity**: high
- **category**: security
- **confidence**: 1.0
- **reasoning**: 发现硬编码的 JWT 密钥或敏感凭证，存在严重的安全隐患。
- **suggestion**: 请将密钥移除，并改用环境变量（如 os.environ.get('JWT_SECRET')）进行加载。
- **evidence**: `Err io.Writer`

### Issue 6
- **file**: `internal/project/host.go:20`
- **severity**: medium
- **category**: clean_code
- **confidence**: 0.9
- **reasoning**: 函数体过长且嵌套层级较深，可读性较差。
- **suggestion**: 建议将核心逻辑拆分为独立的小函数，遵循单一职责原则（SRP）。
- **evidence**: `DefaultLibraryPath() string`


## 5. Missing Test Suggestions
- 未发现与 `cmd/tsgo/api.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `cmd/tsgo/sys.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/api/api.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/api/host.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/api/server.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/compiler/host.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/execute/outputs.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/execute/system.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/execute/tsc.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/execute/watch.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/execute/watcher.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/fourslash/fourslash.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/lsp/server.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/project/host.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/project/project.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/project/service.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/testutil/harnessutil/harnessutil.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。
- 未发现与 `internal/testutil/projecttestutil/projecttestutil.go` 关联的测试文件，建议补充覆盖本次变更逻辑的单元测试或集成测试。

## 6. Evidence
- cmd/tsgo/api.go
- cmd/tsgo/sys.go
- internal/execute/outputs.go
- internal/execute/system.go
- internal/lsp/server.go
- internal/project/host.go

## 7. Final Recommendation
- **Decision**: REQUEST_CHANGES
- **Summary**: Verifier 后保留 6 个问题。最终决策: REQUEST_CHANGES (风险等级: HIGH)

## 8. Review Notes
- PR 较大，已按 token 预算拆分为 3 个批次审查。
- 有 8 段检索上下文因 prompt 预算被省略（约 11079 tokens），关键证据可能不在本次输入中。
- Verifier: 校验 3 个候选问题，verified=3, rejected=0, needs_more_context=0, downgraded_low_confidence=0, dropped_low_confidence=0, missing_counter_arguments=0, symbol_searches=0, llm_calls=1。

### Run Metrics
- **Stages**: parse_url=0.00s, fetch_pr=1.66s, prepare_repo=35.88s, parse_diff=0.01s, impact_analysis=8.43s, retrieve_and_review=1.16s, verify=0.00s, write_report=0.00s
- **Files**: 21
- **Batches**: 3
- **Ablation**: production
- **Retrieval Mode**: graph
- **Focus Aspects**: 4
- **LLM Calls**: 4
- **Estimated Patch Tokens**: 7512
- **Estimated Context Tokens**: 14534
- **Total Seconds**: 47.14
- **Triage**: enabled=True, skipped_llm=False, reason=常规代码变更，运行完整审查, aspects=['clean_code', 'bugs', 'performance', 'security']
- **Impact Analysis**: enabled=True, findings=30
- **Static Analysis**: enabled=False, findings=0
- **Verifier**: candidates=3, symbol_searches=0, symbol_contexts=0, missing_counter_arguments=0, verified=3, rejected=0, needs_more_context=0, downgraded_low_confidence=0, dropped_low_confidence=0, llm_calls=1, skipped_by_budget=0
- **Cache**: review_hit=False, key=, llm_hits=0, llm_misses=0
- **Budget**: max_llm_calls=0, used_llm_calls=4, max_input_tokens=0, used_input_tokens=38556, max_output_tokens=0, used_output_tokens=0, max_seconds=0.0, elapsed_seconds=71.446
- **LLM Usage**: calls=4, completion_tokens=0, prompt_tokens=0, total_tokens=0
