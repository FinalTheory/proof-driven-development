# Correctness Harness 规范

这份文件是 Correctness Harness 的**人类可读 mental model**。

正文只解释三件事：**Harness 在建模什么、为什么需要这些类型、这些状态如何随审计和修改演化**。为了便于人工审阅，文末 Appendix A 另外提供 canonical `correctness.yaml` 的完整字段/约束速查；正文仍不展开当前 DAG 内容、CLI 手册或代码实现细节。

我们要解决的问题很简单：架构 correctness reasoning 不能一直隐含在 reviewer 的脑子里；但当 proof model 本身越来越大时，如果没有清晰的语义边界，人也会失去对模型的整体理解。

## Mental model

```text
architecture semantics
        ↓
correctness propositions ── proof dependencies ── assumptions
        ↓
semantic type system
        ↓
refinement：proof decomposition 是否已经成熟？
        ↓
assurance：coverage / composition / implementation evidence
        ↓
implementation 或 architecture 发生变化
        ↓
semantic signatures + targeted invalidation
        ↺
```

Harness 实际上只管理四个彼此不同的层次：

| 层次 | 核心问题 |
| --- | --- |
| **Correctness model** | 系统必须满足哪些 proposition？它们依赖什么？ |
| **Semantic type system** | 重要 state、mechanism 和可复用语义关系到底是什么意思？ |
| **Refinement** | proposition 的边界和 dependency decomposition 是否合理？ |
| **Assurance** | 我们对 specification 完整性、proof composition、implementation correctness 分别有多大把握？ |

这四层不能混在一起。尤其要记住：

```text
refinement stable
    ≠ proposition 已经被证明
    ≠ composition 已认证
    ≠ implementation evidence passing
    ≠ specification 已完整
```

## Correctness model

Graph node 表示的是一个 **proposition**，不是 service、class、文件或组件。`depends_on` 表示逻辑上的 proof dependency，不表示 runtime call flow。

| 对象 | 含义 |
| --- | --- |
| **Root claim** | 系统最终希望守住的、对外有意义的 correctness guarantee |
| **Derived claim** | 由其他 claim / assumption 推导出的、可复用的中间 proposition |
| **Leaf claim** | 不再继续拆 DAG，而是直接交给 implementation evidence 验证的局部 proof obligation |
| **Assumption** | 当前 proof boundary 之外，被显式接受的外部 proposition |
| **System context** | 所有局部 reasoning 共享的 scope、failure model、liveness boundary |
| **Catalog** | proposition 使用的 typed vocabulary |
| **Semantic contract** | 可复用的 correctness property specification / refinement type；它定义 property 的 truth condition、typed relation 与 minimum semantic scope，但本身不是 theorem，也不声明系统已经满足该 property |

整体逻辑形状可以压成：

```text
Assumption ───────┐
                  ├──> Derived claim ───> Root guarantee
Leaf obligation ──┘
```

Leaf 的默认停止条件是 **single-verifier sufficiency**：如果一个 proposition 已经可以由一个局部、确定性的 verifier boundary 直接判断，就通常不值得继续拆 DAG。

一个 verifier 内部有多个 assertion，并不自动意味着应该拆成多个 leaf。

Canonical model 仍然应该被视为**一个逻辑 graph**。cycle detection、reachability、dependency closure、invalidation、semantic signature、atomic mutation 都是在整张图上定义的。将来即使物理上拆文件，也应该由 loader/writer 重新组装成同一个 logical model。

## Semantic type system

Catalog 的作用，是给 claim 中使用的重要名字一个稳定、可复用的语义。主要包括 state、mechanism、failure event、verifier kind、source section 和 semantic contract。

### State classes

| Class | 含义 |
| --- | --- |
| **authoritative** | 定义 canonical truth 的 source-of-truth state |
| **derived** | 可以由 authoritative truth 重建或 materialize 的 state |
| **speculative** | 尚未 commit、可能被接受、拒绝或 reconcile 的 intent |
| **control** | pending、reconciliation mode、editing mode 等 protocol/runtime 协调状态 |

这个分类的重要性在于：真实 invariant 往往同时跨越 canonical、speculative 和 control state。如果其中一类只藏在 prose 里，就很容易漏掉 lifecycle failure。

### Semantic contracts

Semantic contract 与 claim 的逻辑地位必须分开：**contract 定义 property，claim 才断言 property 在系统中成立。** Catalog 中存在一个 contract 不构成任何 proof premise，也不意味着任何 execution 已满足它。`claim.semantic_contracts: [C]` 的含义是该 claim 自己声明完整 realization：它必须推出 C 的 truth condition，并覆盖至少 C 定义的 minimum semantic scope。若 parent 只是通过 `depends_on` 使用一个已经 realization C 的 child，则不应把 C 沿 DAG 向上复制；只有 parent 自己也独立断言完整 C 时，重复 carrier 才是有意的。

| Contract | 含义 | 它主要用来暴露什么问题 |
| --- | --- | --- |
| **safety_contract** | 可复用的 forbidden-outcome boundary | 某条局部合理的 execution 最终仍进入禁止状态 |
| **equivalence_relation** | 定义两个 state / execution 在什么条件下算 observationally equivalent | 系统把用户可区分的两个状态误当成等价 |
| **state_invariant** | 在某个 observation scope 内持续成立的 typed state relation | 某个 transition 建立了关系，但后续 mutator 又把它破坏 |
| **provenance_binding** | semantic source state 到 downstream token/value 的绑定关系 | downstream processing 全部正确，但值本身来自错误 source |

最重要的两个区别是：

```text
named transition guarantee != inductive state invariant

downstream correctness != provenance correctness
```

因此 `state_invariant` 不能只检查几个被点名的 transition，而必须检查：它如何建立，以及所有能够修改参与 state 的 in-scope mutator 是否都保持它。

`provenance_binding` 也不能因为 downstream 使用正确，就反推 provenance 正确。它必须单独证明 producer、source、capture/binding point 以及最终关联到哪个 execution/state/identity。对于复合 identity / semantic tuple，还必须证明所有用于区分实体的坐标来自同一个 source/execution；分别正确的 field-level provenance 并不能证明 `(document_id, key)`、`(document_id, frontier)` 或 authoring-state/request tuple 指向同一个语义实体。

## Refinement 与 invalidation

Refinement 回答的是：

> 这个 proposition 的边界是否合适？direct dependencies 是否足够且必要？leaf 是否已经拆到一个有意义的 verification boundary？

`stable` 只表示：**在当前 semantic signature 下，这个 proposition 的 decomposition 已经通过 refinement audit。**

如果 claim 引用了 `semantic_contracts`，refinement 还包含一个显式的 **contract realization obligation**：authoritative proposition 必须完整推出所引用 contract 的 guarantee，包括 contract class、observation scope、provenance/identity coordinates 与 exclusions。Claim 可以比复用 contract 更强，但不能只覆盖它的 path-local 子集。这个判断本身仍然是自然语言 semantic judgment，Harness 不假装用 schema 自动证明它；Harness 强制 clean auditor 对每个 contract 输出 `REALIZES / WEAKER / MISMATCH / INCOMPLETE`，并且 `set_refinement(status=stable)` 只有在 orchestrator 显式声明所有当前 contract 都已 `REALIZES` 时才接受。Contract-realization audit semantics 有独立版本，因此规则变化只使 contract-bearing proof branches 及其 dependents stale，而不会无差别 reopen 全图。

一旦相关语义发生变化，之前的 reasoning 就应该失效：

```text
semantic change
    ↓
affected proposition
    ↓
transitive dependents become stale
```

Semantic signature 的作用，就是把这种 invalidation 变成确定性的控制逻辑，而不是依赖人记得“哪些东西应该重新 review”。

Global semantic context 的修改故意代价很高，因为它可能真的改变大量 proposition 的含义，因此可能导致大范围 invalidation。

## Assurance

Assurance 和 refinement 是正交的。

| 维度 | 它问的问题 | 典型 failure |
| --- | --- | --- |
| **Specification coverage** | 即使当前 DAG 里所有 claim 都成立，是否仍然存在重要的 in-scope bad execution？ | 漏掉 guarantee、invariant、provenance relation 或 assumption |
| **Composition** | 一个 root / derived claim 的 direct premises 是否真的 imply target？ | `dependencies=true` 但 `target=false` |
| **Implementation evidence** | 真实 implementation 是否满足某个 leaf？ | schema、control flow、transaction、recovery 等真实行为不满足 proposition |

推荐的 reasoning 顺序是：

```text
refinement
    ↓
specification coverage
    ↓
composition certification
    ↓
implementation evidence
```

Coverage 是全局、开放式的 adversarial search；Composition 只看一个 proposition 与它的 direct premises；Implementation evidence 则把 terminal proof obligation 连接到真实实现。


对于可以机械化的 symbolic 子问题，Harness 把“语义翻译”和“solver 检查”分开管理。Canonical proposition 的 symbolic mapping 仍需 translation assurance；coverage bad-state 则作为独立 declarative case 保存。

对于 canonical state，如果某个值的语义依赖必须保留的坐标（例如 document 与 observation），这些坐标可以通过 `catalog.state.*.symbolic_dimensions` 进入 canonical model。任何直接映射该 state symbol 的 symbolic function 都必须显式暴露完全相同的有序 dimensions；Harness 拒绝把 `[document, observation]` 降维成只依赖 `document` 的 timeless function。这个约束只解决结构性语义丢失；“这些 dimensions 是否是业务上正确的定义”仍属于 translation/refinement 的 semantic judgment。

此外，claim statement / formal intent 中直接出现的 canonical snake_case vocabulary 会自动成为 symbolic mapping 的 coverage requirement。Formalizer 不能把 `document_current_epoch` 这类已命名状态匿名化成 `state_value` 再仅靠 predicate meaning 解释其含义；如果需要跨 claim 共享新的 helper relation，其语义忠实性仍必须由 translation assurance 审查。

Symbolic helper 也不能成为隐藏 premise。任何没有直接 `catalog_symbols` projection 的 opaque predicate 都必须通过 `semantic_anchors` 绑定到明确的 canonical claim / semantic contract，并且只能在被该 subject 授权的 lowering 或 coverage query 中出现。这个 provenance constraint 解决的是“formalizer 为了得到 UNSAT 偷偷加入 relation”的 soundness 风险；predicate 本身是否忠实表达了 canonical prose，仍由 translation assurance 负责。

Persisted formal layer 采用独立的 closed machine-readable schema：

```text
correctness.py formal-schema bridge
correctness.py formal-schema assurance
correctness.py formal-schema coverage
correctness.py formal-schema composition-artifact
correctness.py formal-schema coverage-artifact
```

这些 schema 不把 sidecar 提升为新的 product correctness authority。它们只约束“已经选择 formalize 的内容如何合法持久化”。Repository-level `correctness.py validate` 会同时验证 canonical graph 与 formal layer：先做 closed-shape/schema validation，再做 graph↔bridge correspondence、translation trust、coverage/composition fresh solver check，以及 persisted artifact 与当前 semantic signatures/result 的一致性。任何一层 drift 都会让 preflight fail closed。对于 SAT check，persisted model 只是可供人工审阅的 witness；fresh check 重新确认 SAT 即可，因为同一公式可能存在多个合法模型，Harness 不要求两次 solver run 返回逐字相同的 model 文本。

一个有效的 coverage machine check 必须同时满足：

```text
candidate bad state alone           → SAT
trusted symbolic constraints
AND candidate bad state             → UNSAT
```

第一步防止把本身不可能发生的 candidate 当成 coverage 证据；第二步才说明当前 trusted constraints 排除了这个坏状态。Machine-check artifact 应内联 candidate、所依赖的 trusted formula、相关 vocabulary 语义、solver 结果与 semantic signatures，使证明可以脱离临时脚本独立人工审阅。Composition 的对应检查仍是 `direct premises AND NOT(target)` 是否 UNSAT。

## Harness control plane

`correctness.py` 更适合被理解成围绕 semantic model 的 **control plane**，而不是另一份 product correctness specification。

| 责任 | 作用 |
| --- | --- |
| **Load + type-check** | 拒绝结构上非法的 model state |
| **Semantic validation** | 检查 reference、cycle、node role、catalog consistency 等 graph-level property |
| **Semantic signatures** | 判断之前已经审过的 reasoning 是否因为语义变化而失效 |
| **Slice + prompt compilation** | 给 auditor 只暴露当前 reasoning boundary 所需的 context |
| **Scheduling** | 决定哪些 refinement / composition task 当前 runnable，以及何时被 human/tooling blocker 卡住 |
| **Assurance bookkeeping** | 把 coverage、composition、implementation evidence 与 refinement maturity 分开记录 |
| **Controlled mutation** | 在 schema validation、semantic precondition、lock 和 atomic write 约束下修改 model |

Reasoning 角色也有明确分工：

```text
clean auditor           → adversarial discovery
long-lived orchestrator → semantic judgment
Harness                 → deterministic validation / scheduling / invalidation / mutation
```

这里的 `clean auditor` 必须是实际的新 context，而不是 orchestrator 在同一上下文里切换角色。当前 Writer MCP 的标准执行原语是 `spawn_chatgpt_subagents`（单个 fresh agent 也传单元素列表）；并发 slot 用尽或 child task 失败属于 execution/capacity failure，不能降级成同-context audit 后仍声称获得独立审计。如果所需 fresh context 无法取得，本轮 assurance 应保持未认证。

Automation 可以重新组织已经批准的 proof structure，但不能静默发明新的 architecture semantics，例如新的 protocol mechanism、source-of-truth rule、state variable、guarantee、failure assumption 或 semantic-contract meaning。

## Source-of-truth boundaries

| 问题 | Source of truth |
| --- | --- |
| Harness 的人类 mental model | `SPEC.md` |
| `correctness.yaml` 哪些字段 / shape 在结构上合法 | `graph_schema.py` / `graph-schema` / `schema-fields` |
| Formal sidecar / artifact 哪些字段 / shape 在结构上合法 | `harness/symbolic/formal_schema.py` / `formal-schema` |
| 当前 proposition、catalog 和 campaign state | `correctness.yaml` |
| validation、signature、prompt、scheduler、mutation 如何执行 | `correctness.py` |
| 当前建模的系统 architecture | `../design/google-docs.md` |
| Symbolic proposition mapping 与 declarative coverage case | `symbolic_models/*.yaml` |
| 可独立人工审阅的 symbolic machine-check artifact | `symbolic_artifacts/*.yaml` |
| Agent 操作 repository 时必须遵守什么规则 | `../AGENTS.md` |
| 当前 operational workflow | `correctness.py workflow-help` |

`SPEC.md` 的目标就是**足够小，能让人快速重新建立 mental model**。当一次较大的 tooling 修改改变了这里描述的概念或语义边界时，直接精准修改对应段落或表格即可。

不要为了“文档和代码永远机械同步”而加入 generated anchor、当前 node inventory、schema dump 或 code-level mirror。这个文件的价值来自**短、稳定、可读**。

完整的 canonical YAML 字段、类型、可选值与条件约束见 [`SCHEMA.md`](SCHEMA.md)。该文件由 Harness 自动检查结构覆盖是否与 `graph_schema.py` 漂移。
