# Canonical Correctness Model 字段参考

这份文件是 `correctness.yaml` 的人类可读 schema reference。它解释每个受约束字段的类型、可选值、必需性和语义；当前 DAG 实例仍以 `correctness.yaml` 为准。

Harness 会校验文件末尾的 machine-checkable schema index。该索引来自 canonical schema 的 field path + presence + structural constraint，用来检测新增/删除字段、enum/const、required/optional 条件等结构漂移。中文说明本身保持人工维护，不做代码级 anchor。

这个文件只描述 **canonical correctness model** 的字段、类型、固定取值和条件约束。它不是当前 DAG 的内容快照，也不覆盖 mutation-plan DSL；后者属于 Harness control plane，可通过 `correctness.py mutation-schema` 查询。

## 全局规则与 ID 类型

除动态 key map 外，所有 object 都是 **closed object**：未在 schema 中声明的字段会被拒绝。

| 类型 | 约束 | 用途 |
| --- | --- | --- |
| `lower_snake` | `^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$` | catalog entry、verifier kind 等稳定语义 ID |
| `source_id` | 同 `lower_snake`，但禁止 `section_<n>` | architecture source section 的稳定语义 ID |
| `assumption_id` | `^A[0-9]+_<lower_snake>$` | assumption |
| `root_id` | `^G[0-9]+_<lower_snake>$` | top-level root reference |
| `claim_id` | `^[GCL][0-9]+_<lower_snake>$` | root / derived / leaf claim |
| `decision_id` | `^D[0-9]+_<lower_snake>$` | human semantic decision |
| `tooling_blocker_id` | `^T[0-9]+_<lower_snake>$` | tooling blocker |
| `signature` | 64 位 lowercase hex SHA-256 | semantic/composition/coverage/evidence signature |

常见 collection 规则：除特别说明外，reference list 都要求元素唯一；声明 `minItems=1` 的 list 不允许为空。

## 顶层结构

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `schema_version` | const `0.16` | required | canonical document schema 版本 |
| `system` | closed object | required | 所有局部 proof audit 共享的全局系统语义 |
| `source` | closed object | required | architecture narrative 的 traceability pointer |
| `scope` | closed object | required | 明确哪些 product/protocol concern 被建模或排除 |
| `catalog` | closed object | required | typed vocabulary 与 reusable semantic registry |
| `id_allocator` | closed object | required | single-writer mutation engine 的持久 ID allocator state |
| `assumptions` | dynamic map keyed by `assumption_id` | required | DAG 外部的 terminal proof boundary |
| `roots` | unique `root_id[]`, min 1 | required | 当前 correctness model 的 top-level guarantee 集合 |
| `claims` | dynamic map keyed by `claim_id` | required | correctness propositions |
| `refinement` | closed object | required | proposition/decomposition maturity 与 blocker state |
| `assurance` | closed object | required | coverage / composition / implementation evidence |

## `system` / `source` / `scope`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `system.id` | non-empty `lower_snake` string | required | 当前 system model 的稳定 ID |
| `system.summary` | non-empty string | required | 注入所有 local audit，并进入 semantic signature 的中性系统摘要 |
| `system.liveness_boundary` | non-empty string | required | 全局 liveness interpretation boundary；未明确声明的 liveness 不可自行推断 |
| `source.article` | non-empty string | required | repository-relative architecture article path；pointer 本身不是 proof premise |
| `scope.includes` | unique non-empty `string[]`, min 1 | required | DAG 明确覆盖的 system/protocol concern |
| `scope.excludes` | unique non-empty `string[]`, min 1 | required | DAG 明确不覆盖的 concern；failure assumption 不放这里 |

## `assumptions`

`assumptions.<A...>` 的 key 必须符合 `assumption_id`。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.statement` | non-empty string | required | 被接受为 terminal proof boundary 的 authoritative proposition |
| `.status` | `external_assumption` / `protocol_assumption` / `environment_assumption` | required | 分别表示责任委托给外部组件、protocol participant、environment/liveness condition |
| `.note` | non-empty string | optional | assumption boundary 的补充说明；存在时进入 assumption semantics |
| `.source_refs` | unique `source_id[]`, min 1 | required | 必须解析到 `catalog.sources` 的 architecture traceability |

## `claims`

`claims.<G/C/L...>` 的 key 必须符合 `claim_id`；Harness 还会做 ID role consistency、foreign-key、cycle、reachability 等 semantic validation。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.kind` | `root` / `derived` / `leaf` | required | proposition 在 proof graph 中的角色 |
| `.statement` | non-empty string | required | 该 node 的 authoritative natural-language proposition |
| `.source_refs` | unique `source_id[]`, min 1 | required | traceability 到 `catalog.sources`；不属于 proposition semantics |
| `.formal_intent` | non-empty string | optional | proposition 的紧凑/形式化重述；进入 semantic signature |
| `.semantic_contracts` | unique `lower_snake[]`, min 1 | optional | 必须解析到 `catalog.semantic_contracts`；定义 interpretation boundary，不增加 proof premise |
| `.depends_on` | unique dependency ID array, min 1；元素可为 `A/G/C/L...` | `root` / `derived` required；`leaf` forbidden | logical proof dependencies；不是 runtime ordering |
| `.mechanisms` | unique `lower_snake[]`, min 1 | `leaf` required | 必须解析到 `catalog.mechanisms`；表示 leaf 所依赖的已批准 architecture mechanism |
| `.verification` | closed object | `leaf` required | 计划如何获得 mechanical/executable evidence；不记录执行状态 |
| `.verification.verifiers` | object array, min 1 | required when `.verification` exists | 一个或多个 verifier strategy |
| `.verification.verifiers[].kind` | `lower_snake` string | required | 必须解析到 `catalog.verifier_kinds` |
| `.verification.verifiers[].intent` | non-empty string | required | 该 verifier 要观察/检查的具体 scenario |

`kind` 的关键条件约束：

| `kind` | 必须存在 | 明确禁止 |
| --- | --- | --- |
| `root` | `depends_on` | — |
| `derived` | `depends_on` | — |
| `leaf` | `mechanisms`, `verification` | `depends_on` |

## `catalog`

`catalog` 的八个 core namespace required；`scope_dimensions` 是可选的 typed namespace，只有系统显式建模 semantic scope / resource namespace 时才需要出现。除 `sources` 外，其余 dynamic key 使用 `lower_snake`；任何已出现的 namespace 至少包含一个 entry。

### `catalog.terms`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `terms.<id>.description` | non-empty string | required | protocol/domain vocabulary 定义；只是词义，不是 correctness premise |
| `terms.<id>.scope_dimensions` | unique `lower_snake[]`, min 1 | optional | 该 term 所属的 semantic namespace/resource coordinates；每项必须解析到 `catalog.scope_dimensions` |

### `catalog.state`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `state.<id>.class` | `authoritative` / `derived` / `speculative` / `control` | required | state 的 semantic role |
| `state.<id>.description` | non-empty string | required | state 的定义及其 correctness role |
| `state.<id>.symbolic_dimensions` | unique `lower_snake[]`, min 1 | optional | symbolic mapping 读取该 state 时必须显式保留的有序语义坐标；每项必须解析到 `catalog.symbolic_dimensions` |
| `state.<id>.scope_dimensions` | unique `lower_snake[]`, min 1 | optional | 该 state 所属的 semantic namespace/resource coordinates；matching provenance relation 必须保留这些 scope |

### `catalog.symbolic_dimensions`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `symbolic_dimensions.<id>.description` | non-empty string | required | symbolic lowering 使用的稳定语义坐标定义；用于给 `state.*.symbolic_dimensions` 提供 closed vocabulary |

### `catalog.scope_dimensions`

这个 namespace 本身 optional；一旦任何 term/state 使用 `scope_dimensions`，对应 ID 必须在这里定义。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `scope_dimensions.<id>.description` | non-empty string | required | semantic namespace/resource coordinate，例如 `document` / `tenant`；用于 provenance scope closure，不是 proof premise |

### `catalog.mechanisms`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `mechanisms.<id>.description` | non-empty string | required | 已批准 architecture mechanism 的定义 |
| `mechanisms.<id>.automation_reusable` | boolean | required | automation 是否可以在新/修改后的 proof-structure claim 中复用该 mechanism |

### `catalog.semantic_contracts`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `semantic_contracts.<id>.class` | `safety_contract` / `equivalence_relation` / `state_invariant` / `provenance_binding` | required | reusable semantic relation 的类别 |
| `.definition` | non-empty string | required | contract 的 normative meaning；引用它不等于证明它成立 |
| `.excludes` | unique `string[]`; 可为空 | required | 明确禁止从该 contract 推出的 observation / horizon / stronger guarantee |
| `.automation_reusable` | boolean | required | automation 是否可以在新 proof-structure claim 中引用这个已获 human approval 的 contract |
| `.state_symbols` | unique `lower_snake[]`, min 1 | `state_invariant` required | 构成 invariant predicate 的 typed term/state symbols |
| `.observation_scope` | `all_reachable_states` / `authoritative_states` / `user_visible_states` / `protocol_internal_states` | `state_invariant` required | invariant 必须 inductively 成立的 state horizon |
| `.source_symbols` | unique `lower_snake[]`, min 1 | `provenance_binding` required | provenance relation 的 semantic source symbols |
| `.bound_symbols` | unique `lower_snake[]`, min 1 | `provenance_binding` required | 被绑定到 source 的 downstream token/state symbols |
| `.scope_bindings` | object array, min 1 | conditional optional | 当 provenance source/bound 两侧共享已声明的 scope dimension 时必须存在；每个 entry 完整枚举该 dimension 上所有 source/bound symbols，validator fail closed 检查遗漏 |
| `.scope_bindings[].dimension` | `lower_snake` | required in entry | 必须解析到 `catalog.scope_dimensions` 的 scope coordinate |
| `.scope_bindings[].source_symbols` | unique `lower_snake[]`, min 1 | required in entry | contract source side 上属于该 scope 的完整 symbol 集 |
| `.scope_bindings[].bound_symbols` | unique `lower_snake[]`, min 1 | required in entry | contract bound side 上属于该 scope 的完整 symbol 集 |

Contract class 的排他约束：

| `class` | 必须存在 | 明确禁止 |
| --- | --- | --- |
| `state_invariant` | `state_symbols`, `observation_scope` | `source_symbols`, `bound_symbols` |
| `provenance_binding` | `source_symbols`, `bound_symbols`；若两侧共享 scope dimension，则还需对应 `scope_bindings` | `state_symbols`, `observation_scope` |
| `safety_contract` | — | 上述 state/provenance typed relation field（含 `scope_bindings`） |
| `equivalence_relation` | — | 上述 state/provenance typed relation field（含 `scope_bindings`） |

### `catalog.failure_events`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `failure_events.<id>.class` | `allowed` / `excluded` | required | 该 event 必须被 proof 容忍，还是明确在 failure envelope 之外 |
| `failure_events.<id>.description` | non-empty string | required | failure/concurrency event 的具体定义 |

### `catalog.verifier_kinds`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `verifier_kinds.<id>.description` | non-empty string | required | reusable verifier strategy category；scenario-specific detail 放在 verifier `intent` |

### `catalog.sources`

`catalog.sources` 至少一个 entry；key 必须是 `source_id`，因此禁止 `section_6` 这类依赖文章位置的 ID。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `sources.<source_id>.heading` | non-empty string | required | `source.article` 中精确的 semantic H2 heading；不包含展示用数字前缀，如 `6.` |

## `id_allocator`

这些字段只是 mutation engine 的持久 allocator state，**不属于 proof semantics**。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `id_allocator.next_sequence` | closed object | required | 每种 ID prefix 的下一个未使用 sequence |
| `.A` | integer >= 1 | required | assumption ID sequence |
| `.C` | integer >= 1 | required | derived-claim ID sequence |
| `.D` | integer >= 1 | required | decision ID sequence |
| `.G` | integer >= 1 | required | root-claim ID sequence |
| `.L` | integer >= 1 | required | leaf-claim ID sequence |
| `.T` | integer >= 1 | required | tooling-blocker ID sequence |

## `refinement`

`refinement` 包含三类独立 state：human semantic decision、tooling blocker、per-claim refinement maturity。

### `refinement.decisions`

Dynamic key 必须是 `decision_id`。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.question` | non-empty string | required | 当前 specification 无法自动决定的明确 system-semantic choice |
| `.reason` | non-empty string | required | 为什么这里需要 human architecture/product judgment，而不是继续 proof decomposition |
| `.options` | unique non-empty `string[]`, min 1 | required | 可供 human 选择的具体 semantic alternatives |
| `.status` | `open` / `resolved` | required | decision 是否仍未决 |
| `.resolution` | non-empty string | required when `status=resolved` | 最终选择及其 operative meaning |

### `refinement.tooling_blockers`

Dynamic key 必须是 `tooling_blocker_id`。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.status` | `open` / `resolved` | required | control-plane defect 是否仍阻塞可信的 automated refinement |
| `.issue` | non-empty string | required | tooling/control-plane 的具体缺陷 |
| `.reason` | non-empty string | required | 为什么带着该缺陷继续 refinement 会不可信或不可表达 |
| `.suggested_change` | non-empty string | optional | 建议的 tooling repair；只是 advisory |
| `.resolution` | non-empty string | required when `status=resolved` | 已完成 repair 的说明 |

### `refinement.nodes`

Dynamic key 必须是现有 `claim_id`。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.status` | `pending` / `stable` / `waived` | required | 当前 refinement maturity |
| `.blocked_by` | unique `decision_id[]`; 可为空 | required | 直接阻塞该 pending claim 的 open human decisions |
| `.signature` | 64-hex SHA-256 | required when `status=stable` | 当前 recursive proof semantics + relevant global/catalog semantics 的签名 |
| `.rationale` | non-empty string | required when `status=waived`；`stable` 必须省略；`pending` 可选 | 仅记录当前未决/豁免边界；stable audit history 不进入 canonical YAML |

注意：`stale` **不是可写 enum**。当已记录 signature 与当前 semantics 不匹配时，由 Harness 派生 stale/runnable 状态。

## `assurance`

`assurance` 的三个维度相互正交，且都不能作为 proof premise 或 refinement scheduler gate。

### `assurance.specification_coverage`

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.status` | `unaudited` / `gap_found` / `closed` | required | 当前 root universe + canonical design 的 architecture-level coverage judgment |
| `.signature` | 64-hex SHA-256 | `gap_found` / `closed` required | audited roots、global semantics、assumptions、canonical design content 的签名 |
| `.auditor_count` | integer >= 1 | `gap_found` / `closed` required | 纳入该 judgment 的 independent adversarial auditor 数量 |
| `.rationale` | non-empty string | `gap_found` / `closed` required | 最新 coverage judgment 的简短理由 |

`stale` 同样是 signature mismatch 的派生状态，不直接写入 YAML。

### `assurance.composition.<claim_id>`

用于 non-leaf claim 的 `direct dependencies => target` assurance。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.status` | `unaudited` / `single_agent_audited` / `multi_agent_audited` / `machine_checked` | required | composition assurance level |
| `.signature` | 64-hex SHA-256 | audited / machine-checked 状态 required | target proposition + direct dependency propositions + interpretation context 的签名；不包含更深 decomposition |
| `.auditor_count` | integer >= 1 | agent-audited 状态 required | independent semantic auditor 数量 |
| `.artifact_refs` | unique non-empty `string[]`, min 1 | `machine_checked` required | machine-checkable proof/model artifact references |
| `.rationale` | non-empty string | audited / machine-checked 状态 required | 为什么当前 composition 被认为满足该 assurance level |

额外约束：

| `status` | 特殊要求 |
| --- | --- |
| `single_agent_audited` | `auditor_count == 1` |
| `multi_agent_audited` | `auditor_count >= 2` |
| `machine_checked` | 必须有 `artifact_refs`；不要求 `auditor_count` |

`stale` 仍由 composition signature mismatch 派生，不是可写 status。

### `assurance.implementation_evidence.<claim_id>`

用于 leaf 的真实 executable evidence execution state；leaf 上的 `verification` 只描述计划，不描述执行结果。

| 字段 | 类型 / 可选值 | 必需性 | 含义 |
| --- | --- | --- | --- |
| `.status` | `planned` / `implemented` / `passing` / `failing` | required | implementation evidence lifecycle |
| `.signature` | 64-hex SHA-256 | `implemented` / `passing` / `failing` required | evidence 所针对的 leaf semantic signature |
| `.implementation_revision` | non-empty string | `implemented` / `passing` / `failing` required | evidence 对应的 implementation revision/build |
| `.artifact_refs` | unique non-empty `string[]`, min 1 | `implemented` / `passing` / `failing` required | test、CI、model checker 或其他 evidence artifact references |
| `.rationale` | non-empty string | `failing` required；其他状态 optional | evidence state 的解释，失败时必须说明 |

这里的 `stale` 也不是 status enum；当 leaf semantics 改变导致 signature mismatch 时由 Harness 派生。

## 如何使用这个字段参考

这部分的目标是回答两类非常具体的问题：

1. **我看到一个 YAML 字段，它在模型里到底扮演什么角色、允许什么值？**
2. **我准备增加/修改一种表达能力，这究竟是在扩展 correctness model 的 type system，还是只是在修改 Harness implementation？**

如果问题只是“当前有哪些 roots / contracts / verifier kinds”，直接查询 canonical YAML / `correctness.py catalog ...`；不要把当前实例列表复制进这个字段参考。

如果 schema 本身发生较大的概念变化，则同步更新对应表格即可。这里不要求 field-to-code anchor，也不要求 mechanically generated documentation；它首先服务于人工快速审阅和 mental-model recovery。

## Formalization routing

`formalization` 与 `refinement` / `assurance` 同级，是 verifier control-plane metadata，不是 proof premise，也不进入 proposition semantic signature。它必须穷举当前全部 `claims`、`assumptions` 与 `catalog.semantic_contracts`。

- `mode: symbolic`：该 subject 必须走 symbolic-first 路径；缺 mapping 或 translation assurance 时形成显式 blocker，禁止静默降级为 LLM proof。
- `mode: non_symbolic`：必须同时记录结构化 `reason` 与 `rationale`；symbolic bridge 不得再为该 subject 保留 mapping。

新增 claim/assumption 时 mutation plan 必须同时声明 formalization mode。

## Machine-checkable schema index

下面这段索引由 Harness 用于检测 schema/documentation drift；人工解释仍以上面的中文表格为主。

<!-- canonical-schema-index:start -->
- `assumptions` => required; closed object; dynamic keys match ^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `assumptions.<key:^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.note` => optional; string, minLength=1
- `assumptions.<key:^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.source_refs` => required; array, minItems=1, uniqueItems=true, items=string, itemPattern=^(?!section_[0-9]+$)[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `assumptions.<key:^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.statement` => required; string, minLength=1
- `assumptions.<key:^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.status` => required; enum=external_assumption|protocol_assumption|environment_assumption
- `assurance` => required; closed object
- `assurance.composition` => required; closed object; dynamic keys match ^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `assurance.composition.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.artifact_refs` => required when status=machine_checked; forbidden when status=single_agent_audited or status=multi_agent_audited; array, minItems=1, uniqueItems=true, items=string
- `assurance.composition.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.auditor_count` => required when status=single_agent_audited or status=multi_agent_audited; forbidden when status=machine_checked; integer, minimum=1
- `assurance.composition.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.rationale` => required when status=single_agent_audited or status=multi_agent_audited or status=machine_checked; string, minLength=1
- `assurance.composition.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.signature` => required when status=single_agent_audited or status=multi_agent_audited or status=machine_checked; string, pattern=^[0-9a-f]{64}$
- `assurance.composition.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.status` => required; enum=unaudited|single_agent_audited|multi_agent_audited|machine_checked
- `assurance.implementation_evidence` => required; closed object; dynamic keys match ^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `assurance.implementation_evidence.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.artifact_refs` => required when status in implemented|passing|failing; array, minItems=1, uniqueItems=true, items=string
- `assurance.implementation_evidence.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.implementation_revision` => required when status in implemented|passing|failing; string, minLength=1
- `assurance.implementation_evidence.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.rationale` => required when status=failing; string, minLength=1
- `assurance.implementation_evidence.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.signature` => required when status in implemented|passing|failing; string, pattern=^[0-9a-f]{64}$
- `assurance.implementation_evidence.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.status` => required; enum=planned|implemented|passing|failing
- `assurance.specification_coverage` => required; closed object
- `assurance.specification_coverage.auditor_count` => required when status in gap_found|closed; integer, minimum=1
- `assurance.specification_coverage.rationale` => required when status in gap_found|closed; string, minLength=1
- `assurance.specification_coverage.signature` => required when status in gap_found|closed; string, pattern=^[0-9a-f]{64}$
- `assurance.specification_coverage.status` => required; enum=unaudited|gap_found|closed
- `catalog` => required; closed object
- `catalog.failure_events` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.failure_events.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.class` => required; enum=allowed|excluded
- `catalog.failure_events.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.description` => required; string, minLength=1
- `catalog.mechanisms` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.mechanisms.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.automation_reusable` => required; boolean
- `catalog.mechanisms.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.description` => required; string, minLength=1
- `catalog.scope_dimensions` => optional; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.scope_dimensions.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.description` => required; string, minLength=1
- `catalog.semantic_contracts` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.automation_reusable` => required; boolean
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.bound_symbols` => required when class=provenance_binding; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.class` => required; enum=safety_contract|equivalence_relation|state_invariant|provenance_binding
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.definition` => required; string, minLength=1
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.excludes` => required; array, uniqueItems=true, items=string
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.observation_scope` => required when class=state_invariant; enum=all_reachable_states|authoritative_states|user_visible_states|protocol_internal_states
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.scope_bindings` => optional; array, minItems=1, uniqueItems=true, items=object
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.scope_bindings.[].bound_symbols` => required; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.scope_bindings.[].dimension` => required; string, pattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.scope_bindings.[].source_symbols` => required; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.source_symbols` => required when class=provenance_binding; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.state_symbols` => required when class=state_invariant; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.sources` => required; closed object; dynamic keys match ^(?!section_[0-9]+$)[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.sources.<key:^(?!section_[0-9]+$)[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.heading` => required; string, minLength=1
- `catalog.state` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.state.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.class` => required; enum=authoritative|derived|speculative|control
- `catalog.state.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.description` => required; string, minLength=1
- `catalog.state.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.scope_dimensions` => optional; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.state.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.symbolic_dimensions` => optional; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.symbolic_dimensions` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.symbolic_dimensions.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.description` => required; string, minLength=1
- `catalog.terms` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.terms.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.description` => required; string, minLength=1
- `catalog.terms.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.scope_dimensions` => optional; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.verifier_kinds` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `catalog.verifier_kinds.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.description` => required; string, minLength=1
- `claims` => required; closed object; dynamic keys match ^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.depends_on` => required when kind in root|derived; forbidden when kind=leaf; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[AGCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.formal_intent` => optional; string, minLength=1
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.kind` => required; enum=root|derived|leaf
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.mechanisms` => required when kind=leaf; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.semantic_contracts` => optional; array, minItems=1, uniqueItems=true, items=string, itemPattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.source_refs` => required; array, minItems=1, uniqueItems=true, items=string, itemPattern=^(?!section_[0-9]+$)[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.statement` => required; string, minLength=1
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.verification` => required when kind=leaf; closed object
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.verification.verifiers` => required; array, minItems=1, items=object
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.verification.verifiers.[].intent` => required; string, minLength=1
- `claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.verification.verifiers.[].kind` => required; string, pattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `formalization` => required; closed object
- `formalization.assumptions` => required; closed object; dynamic keys match ^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `formalization.assumptions.<key:^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.mode` => required; enum=symbolic|non_symbolic
- `formalization.assumptions.<key:^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.rationale` => required when mode=non_symbolic; string, minLength=1
- `formalization.assumptions.<key:^A[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.reason` => required when mode=non_symbolic; enum=unsupported_logic|external_semantics_only|human_semantic_judgment|implementation_correspondence_only|other
- `formalization.claims` => required; closed object; dynamic keys match ^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `formalization.claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.mode` => required; enum=symbolic|non_symbolic
- `formalization.claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.rationale` => required when mode=non_symbolic; string, minLength=1
- `formalization.claims.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.reason` => required when mode=non_symbolic; enum=unsupported_logic|external_semantics_only|human_semantic_judgment|implementation_correspondence_only|other
- `formalization.semantic_contracts` => required; closed object; dynamic keys match ^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `formalization.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.mode` => required; enum=symbolic|non_symbolic
- `formalization.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.rationale` => required when mode=non_symbolic; string, minLength=1
- `formalization.semantic_contracts.<key:^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.reason` => required when mode=non_symbolic; enum=unsupported_logic|external_semantics_only|human_semantic_judgment|implementation_correspondence_only|other
- `id_allocator` => required; closed object
- `id_allocator.next_sequence` => required; closed object
- `id_allocator.next_sequence.A` => required; integer, minimum=1
- `id_allocator.next_sequence.C` => required; integer, minimum=1
- `id_allocator.next_sequence.D` => required; integer, minimum=1
- `id_allocator.next_sequence.G` => required; integer, minimum=1
- `id_allocator.next_sequence.L` => required; integer, minimum=1
- `id_allocator.next_sequence.T` => required; integer, minimum=1
- `refinement` => required; closed object
- `refinement.decisions` => required; closed object; dynamic keys match ^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `refinement.decisions.<key:^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.options` => required; array, minItems=1, uniqueItems=true, items=string
- `refinement.decisions.<key:^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.question` => required; string, minLength=1
- `refinement.decisions.<key:^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.reason` => required; string, minLength=1
- `refinement.decisions.<key:^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.resolution` => required when status=resolved; forbidden when status=open; string, minLength=1
- `refinement.decisions.<key:^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.status` => required; enum=open|resolved
- `refinement.nodes` => required; closed object; dynamic keys match ^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `refinement.nodes.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.blocked_by` => required; array, uniqueItems=true, items=string, itemPattern=^D[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `refinement.nodes.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.rationale` => required when status=waived; forbidden when status=stable; string, minLength=1
- `refinement.nodes.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.signature` => required when status=stable; forbidden when status=waived or status=pending; string, pattern=^[0-9a-f]{64}$
- `refinement.nodes.<key:^[GCL][0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.status` => required; enum=pending|stable|waived
- `refinement.tooling_blockers` => required; closed object; dynamic keys match ^T[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `refinement.tooling_blockers.<key:^T[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.issue` => required; string, minLength=1
- `refinement.tooling_blockers.<key:^T[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.reason` => required; string, minLength=1
- `refinement.tooling_blockers.<key:^T[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.resolution` => required when status=resolved; forbidden when status=open; string, minLength=1
- `refinement.tooling_blockers.<key:^T[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.status` => required; enum=open|resolved
- `refinement.tooling_blockers.<key:^T[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$>.suggested_change` => optional; string, minLength=1
- `roots` => required; array, minItems=1, uniqueItems=true, items=string, itemPattern=^G[0-9]+_[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `schema_version` => required; const='0.16'
- `scope` => required; closed object
- `scope.excludes` => required; array, minItems=1, uniqueItems=true, items=string
- `scope.includes` => required; array, minItems=1, uniqueItems=true, items=string
- `source` => required; closed object
- `source.article` => required; string, minLength=1
- `system` => required; closed object
- `system.id` => required; string, pattern=^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$
- `system.liveness_boundary` => required; string, minLength=1
- `system.summary` => required; string, minLength=1
<!-- canonical-schema-index:end -->
