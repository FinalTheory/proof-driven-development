# 系统设计迷思： 设计能打的 Google Docs 要靠手搓过 Raft 的脑子

先说结论：目前市面上所有 Google Docs 系统设计题解都经不起深度追问。这些题解一般会很快画出一个 `Document Service`：客户端通过 WebSocket 连接它，用户的编辑操作发给它，它用 Operational Transformation 解决冲突，然后把更新广播给其他客户端，并把文档存进数据库。

这个设计看起来合理，但真正的问题也正好藏在这个 `Document Service` 里。它什么时候认为一个 edit 已经成功？它的内存状态是不是系统事实？如果它已经算出了一个 transform 结果，但还没写数据库就崩溃，客户端应该重试还是等待？如果数据库写成功但 ACK 丢了，同一个 edit 会不会被应用两次？如果客户端断线重连，它怎么证明自己没有漏掉某个 change set？

如果这些问题没有答案，`Document Service` 就只是一个“会魔法的黑盒子”。它能让架构图看起来完整，却经不起真正的系统设计追问。

要把这个黑盒子拆开，第一步不是急着选数据库、Kafka、WebSocket 或缓存，而是先定义 OT 本身到底承诺什么。OT 在这里不该被当成一个负责持久化、广播、容灾和重连恢复的万能服务。在 server-authoritative OT 架构里，OT control layer 更像一个 conflict-resolution state machine：给定客户端基于某个 revision 的 raw edit，以及当前已经 accepted 的文档历史，它输出 canonical transformed change set，或者拒绝这个 edit 并要求客户端 rebase。

一旦这样定义，后面的架构就不再依赖一个“Document Service 负责一切”的黑盒。它会自然分解成几件事：OT 负责 transform；accepted-change-set log 负责定义事实；snapshot 负责加速恢复；WebSocket 负责 delivery；客户端通过 revision frontier 证明自己是否连续。

这个分解背后的 mental model，和 Raft 非常接近。

先划清边界：本文不是说 Google Docs 要使用 Raft，也不是说 OT 和 Raft 是同一种算法。Raft 解决的是 consensus / replicated log / state machine replication；OT 解决的是协作编辑里的 change-set transformation / concurrency control。它们的理论目标不同。

但 Raft 训练出来的思维方式非常适合用来拆解 Google Docs 这类协作系统：什么是 committed history？什么状态可以重放？什么时候可以 ACK？snapshot 必须绑定哪个 log frontier？owner 崩溃后如何从 durable truth 恢复？旧 owner 恢复后如何避免污染历史？

把这些问题迁移到 Google Docs，`Document Service` 才会从一个黑盒变成一个机制清楚、边界明确、经得起追问的系统。

## 1. OT 首先要有明确的输入输出

Google Docs 的核心体验是多人实时协作。用户 A 输入一个字符，用户 B 很快能看到；两个用户同时编辑同一个文档，最终应该收敛到一致状态。这里最常见的答案是“用 OT”。

但“用 OT”这句话本身没有工程语义。真正需要定义的是 OT 在这个系统里接收什么、输出什么、承诺什么、拒绝承诺什么。

在一个 server-authoritative OT 架构里，它可以被抽象成：

```text
Input:
  document_id
  client_change_id
  client base_revision
  raw edit change set
  current committed document context / accepted history

Output:
  canonical accepted change set at next revision
  or reject / rebase-required
```

这里的 `base_revision` 不是一个可以独立填写的普通 metadata。对于任何可能进入服务端 OT 并最终成为 canonical acceptance 的请求，客户端必须把 **请求的 `document_id`、server-visible `raw edit`，以及生成它时所依据的 canonical client base 绑定成同一个 authoring tuple**：如果该请求针对文档 `D`，而该文档的 canonical client base 表示 through revision `F` 的状态，那么 `raw edit` 必须是基于这个 `D@F` authoring state 产生或编码的，请求携带的 `document_id` 必须仍是 `D`，`base_revision` 必须就是 `F`。请求构造、序列化和重试都不能把同一个 raw edit 换绑到另一个 document 或 revision。correctness model 把这条关系命名为 `request_authoring_frontier_binding`。

但 authoring frontier 绑定正确，还不能证明 request payload 属于**同一个 logical edit**。如果用户实际创建的是 logical edit `X`，并为它分配 `(document_id=D, client_change_id=k)`，request construction 不能在保留 `(D,k)` 和 `base_revision=F` 的同时，把另一个独立、同样在 `D@F` 上合法的操作 `Y` 编码成 server-visible `raw_edit`。对于每个提交给 server OT 或可能形成 authoritative terminal decision 的 `submitted_request`，`document_id`、`client_change_id`、`raw_edit` 和 `base_revision` 必须共同来自同一个 `logical_client_edit` 与它的 canonical authoring state；序列化可以改变物理表示，但不能改变它所表达的 logical operation。correctness model 把这条关系命名为 `logical_edit_request_payload_binding`。

客户端 UI 可以存在 speculative state；本文并不因此承诺完整的 client-side OT。这里要求的是更窄的 server protocol boundary：真正提交给 server OT 的 raw edit 必须已经具有相对于其声明 canonical base 的明确语义。如果客户端仍有无法安全映射到新 canonical base 的 unresolved speculative edit，就不能靠随手改一个 `base_revision` 继续发送正常编辑请求；后文的 reconnect/conflict boundary 会明确处理这一点。

举个简单例子。客户端看到的是 revision 100，它发来一个 edit：“在 position 10 插入字符 X”。但服务端此时可能已经接受了 revision 101 到 105。position 10 在最新文档里可能已经不再对应客户端当时看到的位置。OT control layer 的职责，就是根据 revision 101 到 105 的 accepted history，把这个 raw edit transform 成当前 canonical history 下的下一个合法 edit。

所以 OT 的能力边界应该说清楚：

```text
OT 负责：
  - transform/rebase raw edit
  - resolve concurrent edit conflict
  - preserve convergence under its algorithm assumptions

OT 不直接负责：
  - durable commit
  - client ACK semantics
  - duplicate retry dedupe
  - WebSocket delivery reliability
  - server crash recovery
  - snapshot/log consistency
```

关键问题不在于不知道 OT，而在于把 OT 当成全能黑盒。只要 OT 还是黑盒，`Document Service` 的边界就定义不清。

本文也不试图实现 OT 的 transformation function。rich text、range formatting、undo/redo、复杂 selection semantics 都有大量细节。这里讨论的是系统架构层面的边界：假设我们有一个正确的 OT engine，系统如何把它放进一个可持久化、可恢复、可重试、可重连的生产架构里。

## 2. 从 OT 输出自然推导出 accepted-change-set log

如果 OT 的输出是 canonical transformed change set，下一个问题就是：这个 output 写到哪里？什么时候算系统接受了它？

先定一个核心概念：

```text
accepted-change-set log = authoritative change-set history
```

一次 edit 只有在 canonical transformed change set 被 durable append 到 accepted-change-set log，并获得一个单调递增 revision 后，才算被系统接受。

对应关系是：

```text
accepted = durably appended to canonical history
acked = accepted
unacked = retryable
```

这就是整个设计的 acceptance boundary。

写入路径可以这样定义：

```text
1. client submits raw edit with client_change_id and base_revision
2. active OT owner transforms it against committed document state
3. server produces candidate_canonical_change
4. server assigns revision N+1 to the candidate
5. acceptance transaction atomically appends candidate_canonical_change@N+1 to accepted_change_log only if:
     - document.current_epoch == owner_epoch
     - document.owner_generation_id == append_owner_generation_id
     - document.latest_revision == N
     - (document_id, client_change_id) has no authoritative terminal result
   then advances document frontier and records terminal result ACCEPTED(canonical_result)
6. transaction commits; candidate_canonical_change@N+1 becomes canonical_accepted_change@N+1 and the same-key terminal result becomes authoritative
7. server ACKs client and broadcasts canonical_accepted_change@N+1
```

`client_change_id` 不是细枝末节，而是协议的一部分。客户端为每个 logical edit 生成一个新的 UUID；同一个 edit 的所有 retry 复用这个 UUID，不同 logical edit 使用不同 UUID。服务端可能在写入 accepted-change-set log 成功之后、返回 ACK 之前崩溃。客户端没有收到 ACK，会重试同一个 edit。如果系统没有稳定的 idempotency identity，它可能把同一个用户操作应用两次。

这里的幂等身份直接定义为 `(document_id, client_change_id)`。`client_id` 可以继续作为审计、调试或来源 metadata 存储，但不参与“这是不是同一个 logical operation”的判断。这个身份必须跨 acceptance boundary 原样保留：如果携带 `(document_id=D, client_change_id=k)` 的请求产生 canonical acceptance，那么 authoritative accepted record 以及与该 acceptance 关联的 durable idempotency evidence 都必须继续以同一个 `(D,k)` 标识它；服务端不能在 request admission、OT transform、idempotency arbitration 或 commit 之间重新生成、替换或错误绑定这个 protocol identity。否则 retry / reconnect 虽然仍然使用 `k`，却无法定位已经以其他 key 写入的那次 acceptance。

另一个容易漏掉的边界是时间。这里把一个 logical edit 的正常 retry / reconnect identity eligibility 明确定义为 **30 days**：只有当 `(document_id, client_change_id)` 的 pending/retry age 不超过 30 天时，它才属于协议正常支持的 retry 和 authoritative reconnect reconciliation 范围。在这个窗口内，系统必须保留足以证明这个 key 是否已经 accepted 的 authoritative evidence；即使 accepted-change history 被 snapshot / compact，也不能因为原 row 消失就让同一个 key 再次产生新的 acceptance。实现上可以保留原记录，也可以把 dedupe identity 压缩进独立的 durable idempotency index / tombstone，或者采用其他等价表示。

30 天是 correctness guarantee 的协议适用边界，并不是某张数据库记录必须精确执行的 TTL。实现可以比 30 天保留更久，也不要求为了执行这个边界专门增加服务端 timestamp、generation registry 或永久 tombstone。正常参与协议的客户端在 pending/retry age 超过 30 天后，不再把旧 `(document_id, client_change_id)` 当作普通 retry 或 authoritative reconnect reconciliation 请求重新发送；它应丢弃该 pending operation，或者进入显式 expired / conflict / manual-resolution 路径。对于违反这个协议边界、在 30 天以后重新提交旧 pending identity 的客户端，这套 correctness model 不再承诺 exactly-once 或 authoritative reconciliation 语义。

但这份 evidence 还必须满足反方向的 soundness：**authoritative state 只有在同一个 `(document_id, client_change_id)` 已经存在 committed canonical acceptance 时，才能把这个 identity 分类为 `accepted`。** 幂等 gate 内部可以有 reservation、in-progress arbitration 或 transient ownership state，但这些状态必须与 `accepted` 明确区分，不能提前充当“已经接受”的事实。实现上，accepted evidence 可以直接来自 canonical accepted row / unique index，也可以是与 accepted append 在同一提交边界内原子产生的 durable index；不能先永久写下 `accepted(k)`，再尝试另一个可能失败的 canonical append。否则如果 accepted marker 已持久化而 append 没发生，客户端 retry 会被一个不存在的成功结果永久挡住：系统既没有接受这次 edit，也不再允许它被接受。

因此，对于仍处在 30-day normal retry/reconnect eligibility horizon 内、协议仍可能合法查询的 idempotency identity，服务端维护的是同一 `(document_id, client_change_id)` 的 **authoritative terminal result**，而不只是“是否 accepted”的单向 marker。terminal result 只有两类：`ACCEPTED(canonical_result)` 或 `REJECTED(definitive_reason)`。timeout、cancellation、crash-before-decision、网络中断等只表示本次 attempt 没有观察到 authoritative terminal decision，不会占用这个 identity 的终态。对同一个 `(D,k)`，首次在线性化边界上成功写入的 terminal result 获胜并成为不可变事实；此后所有 normal retry 都直接返回同一个 terminal result，不重新计算一个可能不同的成功或失败结论。

`ACCEPTED` 与 canonical acceptance 必须原子对应：正常 hot path 上，acceptance transaction 在同一个提交边界内 append canonical accepted change 并把 `(D,k)` 的 terminal result 设为 `ACCEPTED(canonical_result)`；事务 abort 时两者都不存在。`REJECTED(definitive_reason)` 也必须经过同一个 per-key arbitration boundary 持久化，只有当 `(D,k)` 尚无 terminal result 时才能获胜；一旦 `REJECTED` 成为 authoritative terminal result，任何重叠或后续 attempt 都不能再为同一个 `(D,k)` commit canonical acceptance。反过来，一旦 `ACCEPTED` 获胜，任何 stale-base、OT-invalid、rebase-required 或其他 definitive rejection 都不能再成为该 identity 的 externally terminal result。实现可以用一张带 `UNIQUE(document_id, client_change_id)` 的 terminal-outcome 表、一个等价的 transactional idempotency record，或其他能让 ACCEPTED/REJECTED 竞争同一线性化点的表示；关键是两类终态不能由彼此独立的 arbitration path 决定。

这份 per-key terminal-outcome state 本身必须是 **durable authoritative state**，而不是 active OT owner 的易失内存，也不是只能从 accepted-change history 推导出来的缓存。最直接的实现就是把它放在与文档 authoritative metadata 同一 PostgreSQL shard 的 durable terminal-outcome / idempotency table 中。owner crash、restart 或 takeover 不需要从 snapshot + accepted tail “重建”这些终态；新 owner 在重新开放 retry / acceptance arbitration 之前，直接继续读取并使用同一份 authoritative per-key terminal-outcome state。这样 definitive `REJECTED` 即使没有对应 accepted history row，也不会因 owner recovery 消失；`ACCEPTED` 则仍与 matching canonical acceptance 保持事务性对应。correctness model 把这个持久化边界作为 `durable_terminal_outcome_store` 机制。

对这份 durable state 的 **protocol-visible authoritative read** 也只有一个语义出口：`terminal_outcome_publication_gate`。任何普通请求、retry、reconnect 或其他会把 `ACCEPTED` / `REJECTED` terminal result 暴露给协议消费者的路径，都必须先从 `durable_terminal_outcome_store` 读取 exact full identity 的 authoritative result，并通过这个 publication gate；不能有旁路直接读取 store 后自行拼装或发布 terminal outcome。这个 gate 不是新的持久化 source of truth，它只是把所有 terminal-result reader / response sink 收敛成一个可穷举的 correctness boundary；具体代码可以有多个 handler，但它们在静态 control-flow 上都必须汇入同一个 publication abstraction。



这条规则并不要求所有失败都永久化。只有产品/协议愿意对外承诺“这次 logical operation 已被确定拒绝”的结果，才写入 `REJECTED`；timeout、cancellation、crash-before-terminal-commit 等仍然是 non-terminal uncertainty，同一 logical edit 可以继续以原 `client_change_id` 重试。超过 30-day eligibility horizon 后，客户端不再走普通 retry 路径，因此这里也不要求服务端继续为 expired identity 保持 normal retry-resolution guarantee。

correctness model 把“同一 `(D,k)` 的 authoritative terminal result 单值、不可翻转，且 ACCEPTED/REJECTED 与 canonical acceptance 互斥一致”的状态性质命名为 `idempotency_terminal_outcome_coherence`；把“任何 externally definitive response 必须来自该 key 已提交的 authoritative terminal result，而不是 attempt-local 临时判断”的来源关系命名为 `terminal_response_outcome_binding`。

ACCEPTED 侧还需要一条独立于“同 key + 同事务”的 producer provenance。对任意 authoritative `ACCEPTED(canonical_result)` terminal result `(D,k)`，其中的 `canonical_result` 必须来自**同一次 acceptance execution 为同一完整 identity `(D,k)` 提交的 `canonical_accepted_change`**；不能在 acceptance transaction assembly 时把另一 request、另一 document 或另一次 acceptance execution 产生的 result 重新绑定进当前 terminal outcome，即使两份 payload、revision 或当前可观察 scalar 恰好 extensionally equal。这个 binding 在 durable persistence、recovery/restoration、representation migration/compaction 中也必须继续代表原来的 acceptance producer，不能通过“值相等”重新选一个别的 source；随后 protocol publication 再由既有 `terminal_response_outcome_binding` / `terminal_outcome_publication_gate` 原样向外传播这个 authoritative result。correctness model 把这条 producer relation 命名为 `accepted_terminal_result_binding`。

这里还要补上 REJECTED 的 producer-side provenance。`terminal_response_outcome_binding` 只保证 authoritative terminal result 形成以后，对外 response 不会再换绑；它并不能证明被写进 `(D,k)` 的 `REJECTED(definitive_reason)` 本身就是为这个 request 计算出来的。每一个能够被 terminalize 的 `definitive_rejection_decision` 都必须来自同一个 `submitted_request`，并在 validation / OT / asynchronous routing / per-key arbitration / terminal commit 整条路径上保持该 request 的 `document_id`、`client_change_id` 以及 rejection 所依赖的 `raw_edit` / `base_revision` 语义绑定。另一个 request `B` 的 legitimate rejection 不能因为路由或状态关联错误而被写成 request `A` 的 authoritative REJECTED。correctness model 把这条关系命名为 `definitive_rejection_request_binding`。

history compaction 是这个简单关系唯一会发生物理表示变化的地方：原始 accepted row 可以被压缩，但替代它的 tombstone / idempotency index 只能从已经 committed 的 acceptance 派生，并继续代表同一个 `(document_id, client_change_id)` 的既有事实，而不能成为新的 acceptance source of truth。

这和 Raft 里 client retry 的问题非常相似。client command 被 leader 收到，不代表 committed；只有进入 committed log 后，状态机才应该把它当作事实。ACK 丢失不应该导致 command 重复生效。

Google Docs 里也是一样。客户端 raw edit 被 OT owner 收到，不代表 accepted。只有 accepted-change-set log commit 成功，才是系统事实。

## 3. 谁是 truth，谁只是 materialized state

这条边界决定架构能不能扛追问。

在这个设计里，系统事实不是 WebSocket，不是客户端本地状态，也不是 OT owner 的内存状态。系统事实应该落在：

```text
snapshot(frontier) + accepted-change-set log after frontier
```

更具体地说，accepted-change-set log 是 canonical change-set history；snapshot 是带 frontier 的 checkpoint；当前文档状态由 snapshot + log tail 重建。

套到 Google Docs 里，边界应该是：

```text
Source of truth:
  - accepted-change-set log
  - document metadata / latest revision frontier
  - snapshot with snapshot_revision

Derived / materialized state:
  - OT owner in-memory state
  - WebSocket live stream
  - client local document state
  - search index
  - cache
```

这个划分会让 `Document Service` 立刻变得清晰。它可以被拆成几个更明确的角色：

```text
Document Router / Coordinator:
  - maps document_id to active OT owner
  - handles ownership / fencing / failover

OT Owner / Document Actor:
  - maintains committed in-memory OT state
  - transforms raw edits into canonical accepted change sets
  - does not define truth by itself

Accepted-change-set Store:
  - durable source of truth (typically PostgreSQL shard)
  - stores document_id + revision + accepted_change
  - supports idempotency by client_change_id
  - validates fencing epoch + latest_revision conditional append
  - uses synchronous replication per shard for HA

Snapshot Store:
  - stores document checkpoint with snapshot_revision
  - speeds up load/reconnect/failover

WebSocket Gateway:
  - pushes accepted change sets to online clients
  - delivery only, not truth
  - every live stream is document-scoped: a change delivered on stream D must come from accepted-change-set history for the same document D; equal revision/payload values from another document are not interchangeable provenance
```

这样拆开后，架构图里的方框不再是“Document Service handles everything”。每个组件都有明确职责、输入输出和故障语义。

## 4. Snapshot 必须绑定 frontier

文档不可能每次都从第一条 edit replay 到最新 revision。系统一定需要 snapshot。但 snapshot 也不能只是“保存一份当前文档内容”。

正确的 snapshot 必须声明：

```text
snapshot_content includes all accepted change sets up to snapshot_revision
```

公式写成：

```text
DocumentState = Snapshot(snapshot_revision=S) + Replay accepted change sets where revision > S
```

如果 snapshot 内容和 frontier 不一致，会出现两类严重错误。

第一种是 double apply：

```text
snapshot 实际包含到 rev=105
但 snapshot_revision 标成 100
恢复时 replay 101..105
=> 这些 change set 被应用两次
```

第二种是永久漏 apply：

```text
snapshot 实际只包含到 rev=100
但 snapshot_revision 标成 105
恢复时从 106 开始 replay
=> 101..105 永久丢失
```

所以 snapshot 和 frontier 必须原子一致。实现上可以有很多方式：单条记录、事务、copy-on-write snapshot、manifest pointer、后台 compaction 后原子切换指针。具体选型不影响核心抽象：

```text
snapshot is not a text cache;
snapshot is a state-machine checkpoint with a revision frontier.
```

这正是 Raft snapshot 带来的直觉。Raft snapshot 不只是“当前状态”，它隐含了“这个状态覆盖到哪个 log index”。Google Docs 的 document snapshot 也必须如此。

这里所谓 snapshot / compaction / recovery 与“从 canonical history 开始 replay”保持等价，也要把 observer boundary 说清楚。correctness model 把这条语义边界命名为 `logical_state_equivalence_at_frontier`。比较必须发生在同一个 authoritative frontier H：优化后的 authoritative representation 或完成的 recovery 应该得到与 replay accepted revisions through H 相同的 logical document state，并保持 `latest_revision = H`；如果它要继续作为 active owner，还必须恢复从 H 继续正确接受下一条 canonical change 所需的 acceptance-relevant state。

不要求等价的是 physical storage layout、某条旧 revision row 是否仍然存在、checkpoint identity、cache 内容、audit API 能否查询被 compact 的单条历史记录，以及 replay latency / CPU / I/O / scheduling timing。snapshot 本来就是为了让这些实现层观察与 full replay 不同；correctness 只要求它们在定义好的 logical state-machine boundary 上等价。

## 5. 客户端重连：last revision 是协议边界

Google Docs 这样的实时协作系统里，断线和重连是常态。浏览器刷新、网络抖动、移动端切后台、WebSocket reconnect，都不能被当作异常路径。

所以客户端必须维护一个简单但关键的状态：

```text
last_applied_revision
```

重连时，客户端带上这个 revision。服务端根据它返回缺失历史：

```text
if deltas after last_applied_revision still retained:
    return accepted change sets where revision > last_applied_revision
else:
    return latest snapshot(frontier=S)
    return accepted change sets where revision > S
```

客户端应用完历史 delta 后，再进入 live stream。

这里还有一个容易被忽略的细节：即使 TCP/WebSocket 保证单连接内消息有序，应用层仍然可能出现 catch-up 和 live stream 交错。

例如：

```text
client disconnects at rev=100
client reconnects and asks for 101..110
meanwhile live stream sends rev=111
client receives 111 before 101..110
```

如果客户端直接应用 111，就破坏了文档状态的连续性。因此客户端应该只应用连续 revision：

```text
if change set.revision == last_applied_revision + 1:
    apply

if change set.revision > last_applied_revision + 1:
    buffer or trigger gap recovery

if change set.revision <= last_applied_revision:
    ignore duplicate
```

这个规则很便宜，但它把客户端从“相信推送顺序”提升为“验证自己的历史是否连续”。

WebSocket 是 delivery。Revision log 才是 ordering proof。

`last_applied_revision` 还必须和当前 user-visible document identity、客户端保存的 canonical materialization 构成一个持续状态不变量，而不能只在 catch-up 或 revision 前进的瞬间正确。correctness model 把它命名为 `client_canonical_frontier_coherence`：**在每一个代表文档 `D` 的 user-visible client state 中，`client_document_id=D`，并且 `client_local_document` 必须等于文档 `D` 的 canonical accepted history replay through `last_applied_revision` 的结果。** `document_id`、canonical document 和 frontier 是一个不可 cross-bind 的 visible canonical tuple；任何初始化、tab/view 切换、local restore、live delivery、catch-up publication 或其他能够修改、恢复、替换、发布其中任一坐标的 transition，都必须保持这组三元关系，即使该 transition 根本没有推进 revision。resync 内部尚未 publication 的 private candidate state 不属于这个 user-visible observer boundary。

Speculative state 也必须遵守同一个 document observer boundary，但需要区分 **locally retained** 与 **currently exposed**。客户端可以按文档保留尚未解决的 speculative state；切换到另一个文档并不要求删除这些 retained overlays。这里采用 `client_document_scoped_speculative_state` 作为实现边界：retained overlays 按 document identity 分区，而当前 exposed overlay set 只能从当前 visible document 的分区投影出来。

这里的 provenance 不能只绑定 document coordinate。一个 logical edit 在创建时已经获得完整 creation identity `(document_id=D, client_change_id=k)`；只要后续 retained / restored / exposed representation 仍代表**同一个 logical edit**，restore、switch、reactivation、ACK bookkeeping 或 publication 都不能重新生成、替换、丢失或 cross-bind 其中任一坐标。也就是说，同一个 logical edit 不能从 `(D,k1)` 被重新标记成 `(D,k2)`，即使所有真实 submission / retry 仍然沿用原来的 `k1`。correctness model 把这条完整 identity provenance 命名为 `speculative_overlay_identity_provenance`。

同时，exposed speculative state 不是一个可以脱离协议 ownership 独立存在的 UI cache。每一个当前 user-visible speculative representation `(D,k)` 都必须对应 `client_speculative_overlays` 中 retained 的同一个 logical edit `(D,k)`；如果 abandonment、expiry、manual/conflict resolution、authoritative reconciliation 或其他 terminal lifecycle transition 删除了 retained representation，那么 exposed representation 必须在 resulting user-visible state 之前或与之原子地一起消失。单纯留下一个仍可见、但已经没有 retained protocol state 的 orphan overlay 是不允许的。correctness model 把这条持续 invariant 命名为 `visible_speculative_retention_coherence`。

真正的 document-level user-visible invariant 仍然是：在每一个代表文档 `D` 的 visible client state 中，所有当前 exposed speculative overlays 都必须同样属于 `D`。因此 tab/view switch、restore、overlay activation/reactivation、canonical publication、resync publication，以及任何能够改变 visible document identity 或 exposed overlay set 的 transition，都不能把 `A` 的 retained overlay 暴露在当前代表 `B` 的界面上。correctness model 把这条关系命名为 `visible_speculative_overlay_document_binding`。它不禁止后台保留其他文档的 unresolved overlay，只禁止 cross-document exposure。

这里还需要一条与 revision continuity 正交的客户端 observer invariant。真实用户看到的不是单独的 canonical base，而是 **canonical base + speculative overlays** 的组合。即使 canonical base 本身已经严格等于 replay through frontier F，如果同一个 logical edit 又以 optimistic overlay 的形式继续可见，用户仍然会看到重复结果。

因此，任何把新的 canonical frontier `F` 暴露给用户的路径——普通 live delivery、catch-up 后 buffered live delivery、或 reconnect/resync publication——都必须遵守同一个规则：如果新 canonical prefix through `F` 已经包含 identity `(document_id=D, client_change_id=k)` 的 acceptance，而客户端此刻仍有同一个 `(D,k)` 的 speculative overlay，那么该 overlay 必须在 `state@F` 对用户可见之前被移除或 suppress，或者与 frontier publication 在同一个原子可见切换里一起消失。客户端不能先展示 canonical `k`，再异步清理 speculative `k`；是否是同一个 logical edit 依赖稳定的 `client_change_id` identity，而不是比较 raw/canonical payload 是否相同。

```text
canonical_frontier F newly includes acceptance(D,k)
AND speculative_overlay(D,k) exists
    =>
visible publication of state@F must not contain speculative_overlay(D,k)
```

这条规则不是 reconnect 特例，而且它也不能只在“canonical acceptance 第一次 publication”的瞬间成立。**只要当前 user-visible canonical prefix 仍然包含 acceptance `(D,k)`，任何后续 user-visible client transition 都必须继续保持 matching speculative overlay `(D,k)` 不可见。** ACK uncertainty、retry/pending bookkeeping、UI state restore 或其他不推进 canonical frontier 的本地状态转换，都不能把已经被 canonical representation 吸收的同一 logical edit 重新渲染出来。只有用户真正产生一个新的 logical edit，并因此获得新的 `client_change_id`，才可以创建新的 speculative representation。

```text
visible canonical prefix contains acceptance(D,k)
    =>
for every subsequent user-visible client state while that acceptance remains visible:
    speculative_overlay(D,k) is absent
```

correctness model 把这条持续 observer invariant 命名为 `visible_canonical_speculative_exclusion`。它约束的是整个 user-visible state space，而不是某一次 publication event。

下面的 reconnect reconciliation 只是这个持续 observer invariant 在 snapshot/catch-up 场景下更复杂的一种实现：除了不能 double-render，还必须处理 response head、unresolved pending identity、stale response 和原子 publication。

### 5.1 Pending optimistic edit：重连只保证 identity reconciliation，不保证自动 local rebase

本文中的 **normal reconnect / catch-up 是 foreground、visible-document-bound 的协议路径**。如果一次正常重连针对文档 `D`，那么它只能在当前 user-visible canonical tuple 也代表 `D` 时启动，并且该 attempt 的 reconnect-start canonical base 与 `last_applied_revision` 都从这一个 visible tuple 原子捕获。这个 foreground 资格必须持续到 user-visible publication：如果 attempt 尚未 publication 时当前 visible document 已经从 `D` 切走，那么该 attempt 立即失去 foreground publication eligibility；其延迟 response 可以被丢弃，或在未来另行定义的 background/non-visible synchronization 边界内继续处理，但不能再通过本文这条 foreground reconnect path 修改当前 user-visible state。也就是说，当前屏幕正在展示 `A@F` 时，不能把这组 state/frontier 当作后台 `reconnect(B)` 的起点，也不能让此前针对 A 启动但已经失去 visible-document binding 的 delayed response 重新获得 foreground publication 权限。若未来产品需要在文档 A 可见时同时后台同步文档 B，则必须另外维护 B 自己的 per-document canonical state/frontier，并为 background publication 与后续 view-switch 定义独立的 correctness boundary；这种 background/non-visible synchronization 不属于本文当前 reconnect protocol 的范围。

`last_applied_revision` 只能描述客户端已经吸收的 canonical history。真实客户端还会有另一类状态：已经在本地 optimistic 展示、但尚未得到服务端确定结果的 logical edits。它们必须继续保留原始 `client_change_id`，因为 reconnect 后客户端不能仅凭 payload 判断某个本地 edit 是否已经被 canonical history 吸收：raw edit 和服务端 OT 后的 canonical change 本来就可能不同。

因此，对文档 `D` 的重连请求除了 `last_applied_revision=L`，还携带当前仍处在 **30-day normal reconnect eligibility horizon** 内的 pending edit `client_change_id` 集合；这些 key 的完整语义身份始终是 `(document_id=D, client_change_id=k)`。这里的 pending bookkeeping 本身也是 logical edit identity 的语义状态，而不是可以在 restore、tab/view switch、ACK/retry bookkeeping 或 reconnect capture 时重新贴标签的临时记录：对为文档 `D` 创建并分配 `client_change_id=k` 的 logical edit，任何 pending representation 都必须在整个 normal speculative lifecycle 中继续绑定同一个 `(D,k)`，直到 authoritative reconciliation 或显式 terminal disposition 结束该 lifecycle。correctness model 把这条 provenance relation 命名为 `pending_edit_identity_provenance`。

pending identity 保持正确还不够，**用来退休它的 authoritative acceptance 也必须绑定同一个完整 identity**：只有 canonical acceptance `(D,k)` 或等价的 same-identity authoritative evidence 才能以“already accepted / reconciled”的理由结束 pending `(D,k)`；另一个文档上的 `(D2,k)` acceptance、另一个 change id `(D,k2)`，都不能成为该 pending edit 的 acceptance-retirement source。显式 expiry、conflict/manual resolution 或 abandonment 仍可作为非-acceptance terminal disposition。correctness model 把这条关系命名为 `pending_retirement_acceptance_binding`。

同时，pending set 不能只是一个可丢失的辅助索引。只要一个 logical edit 仍然 unresolved、acceptance-eligible、处在 normal eligibility horizon 内，并且仍作为 retained speculative state 参与正常协议，它就必须有 matching pending entry，直到 authoritative reconciliation 或显式 expiry / conflict / manual resolution / abandonment 结束这次 normal speculative lifecycle。单纯删除 pending bookkeeping、但仍保留这个 unresolved acceptance-eligible speculative edit，不能把它变成“已解决”。correctness model 把这个 inductive relation 命名为 `pending_speculative_lifecycle_coverage`。

正常参与协议的客户端不得把 pending/retry age 已超过 30 天的旧 identity 继续作为普通 authoritative reconciliation 输入：这种 expired pending operation 应被丢弃，或进入显式 expired / conflict / manual-resolution 路径。超出这个 eligibility boundary 后，本文不再要求服务端对重新提交的旧 identity 提供 authoritative reconciliation 或 exactly-once retry 语义，也不把缺少 accepted evidence 解释为“证明从未 accepted”。客户端进入 resync 状态后可以暂时禁止新的编辑；这是一种有意选择的简单 UX，用来避免在 canonical base 正在切换时继续制造新的 speculative state。

服务端为一次 catch-up 在一个明确的 authoritative-store read / linearization point 读取 `document_latest_revision`，并把当时读到的值固定为这次响应的 authoritative response head `H`。后续 canonical range / snapshot mode selection、pending-key reconciliation、completion 与 publication 都必须使用这个同一个 captured `H`；`H` 捕获之后系统当然可以继续接受新的 revision，这些更晚的 acceptance 不属于本次 response，也不要求本次 catch-up 追上一个持续移动的 head。

response head 绑定正确并不自动证明 response payload 的 provenance 正确。对 document `D` 的 catch-up，delta mode 返回的每一条 canonical accepted change 都必须来自 `accepted_change_log(D)`；snapshot mode 选中的 checkpoint 必须是 `D` 的 authoritative checkpoint，随后 replay 的 tail 也必须来自 `accepted_change_log(D)`。这些 source-object 的 document binding 要一直保持到 private `client_resync_candidate` 的构造与 publication；另一个 document `D2` 的 checkpoint/history 即使 revision、payload、frontier 和最终 replay 结果都与 `D` 完全相同，也不能仅凭这种 extensional equality 充当 `D` 的 authoritative catch-up source。底层 immutable bytes 可以 content-addressed / deduplicated 共享，但 semantic authoritative reference 仍必须是 `D -> D-history/checkpoint`。correctness model 把这条关系命名为 `catchup_canonical_source_provenance`。

然后服务端在同一个逻辑响应中返回两类信息：

```text
canonical catch-up through H:
  retained deltas L+1..H
  or snapshot(frontier=S) + tail S+1..H

pending resolution through H for catch-up document D:
  for each pending identity (D, k):
    accepted at revision r <= H only for canonical acceptance(D,k)
    or unresolved through H when no acceptance(D,k) exists through H
```

这里 `unresolved through H` 只对仍处在 30-day normal reconnect eligibility horizon 内的 `(D,k)` 定义：它表示 authoritative history through `H` 尚不能证明同一完整 identity 已被接受，并不是“永远不会被接受”的终态承诺。另一个文档 `D2` 上即使存在相同 `client_change_id=k` 的 acceptance，也与 `D` 的这次 reconciliation 无关。为了让这个判断在 snapshot / compaction 之后仍然可靠，accepted-key lifecycle 不能只保留“这个 key 以后不可复用”的 tombstone；在 eligibility horizon 内，它还必须保留同一 `(D,k)` 的 accepted revision，或等价的 frontier evidence，使服务端能够判断该 pending identity 是否已经包含在指定 response head `H` 中。原始 accepted row 可以被压缩，但只要 `(D,k)` 仍可能合法参与普通 retry/reconnect，这项 identity-to-frontier 证据就不能丢失；超过 30 天以后，协议不再要求继续为该旧 identity 提供这项 classification guarantee。

客户端的 safety boundary 是：**整个 resync 先在不可见的 candidate state 中完成 canonical base adoption 与 pending reconciliation，随后只能以一次原子、单调的 publication 切换对用户可见状态。** 如果 response 证明同一完整 identity `(D,k)` 已经在 revision `r <= H` 被接受，那么 candidate canonical base through `H` 已经包含这个 logical edit；客户端不得在任何 user-visible resync 中间态里再把同一个 `(D,k)` 的 speculative overlay 叠加到已经包含它的 canonical prefix 上。

```text
accepted_revision(D,k) <= candidate_frontier(H_D)
    =>
speculative_overlay(D,k) must be absent before publication
```

多个 reconnect/resync attempt 还可能因为网络延迟或 retry 重叠。设当前已经对用户可见的 canonical frontier 为 `F`。一个 response through `H` 到达时，如果 `H < F`，它已经是 stale response：**整个 response 必须被丢弃，不能回退 visible canonical base / last_applied_revision，也不能恢复已经被较新 reconciliation 删除的 speculative overlay 或 pending state。** 正确性真正依赖的是 canonical frontier 的单调 publication，而不是 request 发起顺序；因此不要求为 correctness 引入独立的 attempt-generation ordering。

客户端也不能一边增量应用 response、一边把未完成 reconciliation 的 prefix 暴露给用户。例如 `(D,k)` 在 revision 105 已被接受、response head 为 110 时，不能先暴露 canonical@105 且仍保留 overlay `(D,k)`，然后到 110 才清理。更简单的状态转换是：

```text
freeze editing
→ receive canonical state through H + pending resolutions through H
→ build reconciled candidate state privately
→ if H < currently visible frontier F: discard the entire candidate
→ otherwise remove every speculative overlay whose key is already accepted within H
→ atomically expose exactly one reconciled state through H
```

因此 user-visible canonical frontier 在 resync publication 上永不倒退，也不存在 user-visible 的 checkpoint/tail 中间 prefix。不同 attempt 可以并行计算 candidate，但只有满足上述 publication guard 的完整 reconciled candidate 才能影响可见状态。

对于仍然 `unresolved through H` 的 local edits，本文**不承诺自动把它们 rebase 到新的 canonical base 上，也不承诺它们最终一定能够自动解决**。但这里有一个必须持续成立的 safety boundary：只要仍存在没有被安全 reconciliation 掉的 pending edit，客户端就不能回到 normal editable mode，也不能继续创建新的 acceptance-eligible speculative edit。它必须保持 read-only / conflict / reconciliation 一类非正常编辑状态，直到 unresolved set 被显式 reconciliation 清空。correctness model 把这条 control-state invariant 命名为 `unresolved_pending_blocks_normal_editing`。

这个约束没有引入新的 progress guarantee。系统完全可以长时间停在 conflict 状态；最终 UI 也可以像版本冲突一样保留用户原始内容并要求人工处理，而具体冲突解决语义仍属于本文 scope 之外。

这个限制是有意的。特别是在 snapshot catch-up 中，如果客户端从旧 frontier `L` 直接跳到 `snapshot@S` 且 `S > L`，`L+1..S` 的 operation-level history 可能已经被压缩进 checkpoint。仅凭新的 document state，客户端通常没有足够信息证明一个基于旧 speculative state 产生的 unresolved edit 应如何 transform 到新 base。完整的 client-side OT / CRDT、多个 pending edit 之间的 local rebase、以及无缝保持可编辑 UX 都是独立问题，不由这套 server-authoritative correctness model 解决。

## 6. 服务端崩溃：OT owner 可丢，accepted history 不可丢

再看 failover。

如果 active OT owner 崩溃，系统不应该依赖它内存里的状态继续存在。OT owner 是 materialized state。只要 accepted-change-set log 和 snapshot frontier 是 truth，新 owner 就可以重建。

恢复路径是：

```text
1. coordinator detects OT owner failure
2. assigns document_id to a new owner with a new fenced epoch
3. new owner loads latest snapshot(frontier=S)
4. replays accepted change sets where revision > S
5. rebuilds OT metadata
6. starts accepting new writes from latest_revision + 1
7. clients reconnect with last_applied_revision
8. unacked edits retry with client_change_id
```

这里的 **OT metadata** 在 correctness model 中由 `ot_owner_memory` 表示，并且它是 recovery 安装的完整 document-history-dependent OT metadata carrier：除 recovered logical document state 与 local head / next-revision 之外，任何仍能影响后续 server-side OT transform 或 canonical acceptance 的历史依赖 metadata 都必须包含在 `ot_owner_memory` 中；不存在另一个未建模但同样能影响 canonical semantics 的 recovered OT metadata bucket。

这里还缺一个决定 failover 是否安全的边界：fencing 必须下沉到 accepted-change-set append 的条件写里，而不是只停留在 coordinator 的内存认知里。

可以把 ownership 建模成：

```text
document_owner_epoch:
  document_id -> current_epoch, owner_id, owner_generation_id, lease_expire_time
```

`owner_generation_id` 标识一次具体的 owner generation / ownership tenure；同一个进程重启、重新 acquire ownership，或者 ownership 从 A 切到 B，都必须得到新的 generation identity。它是 server-side ownership context 的一部分，不是 client request 可以自由填写或替换的业务字段。一个旧 owner generation 即使后来读到了新的 numeric epoch，也不能把自己的 generation identity 重新绑定成新 owner 的 generation。

这里的 authority identity 不是裸 `(epoch, owner_generation_id)`，而是 document-scoped tuple `(document_id, epoch, owner_generation_id)`。两个文档即使恰好出现相同的 epoch/generation scalar，也不能互相授权；执行 append 的 generation 必须就是目标 document 的 authoritative current generation。换句话说，document_id 是 fencing provenance 的 namespace coordinate，而不是 routing 层可以事后替换的旁路字段。

correctness model 把这条“append 所使用的 document-scoped fencing tuple 必须来自执行该 append 的 immutable owner generation，并与同一 document 的 authoritative current owner generation 对齐；旧 generation 不能仅通过复制新 epoch/owner metadata 重新获得 authority”的 provenance relation 命名为 `owner_generation_authorization_binding`。

新 owner 接管时：

```text
1. coordinator uses conditional write / CAS to install a new ownership generation
2. the handoff atomically/persistently installs a strictly larger current_epoch
   and a fresh owner_generation_id before the new generation becomes acceptance-eligible
3. every accepted-change-set append carries the owner_epoch plus the immutable
   server-side owner_generation_id of the generation performing that append
4. accepted-change-set store accepts the write only if both
      supplied owner_epoch == current_epoch
   and supplied owner_generation_id == authoritative owner_generation_id
```

因此写入不应该只是：

```text
append document_id, revision=N+1
```

而应该是条件写：

```text
append accepted_change@revision=N+1
only if document.current_epoch == my_epoch
and     document.owner_generation_id == my_owner_generation_id
and     document.latest_revision == N
and     idempotency terminal result for client_change_id is still absent
```

这四个条件分别解决四件事：

```text
epoch / fencing token:
  防旧 generation 继续使用自己已经 superseded 的 epoch

owner_generation provenance:
  防旧 owner 仅通过复制/替换新的 numeric epoch 重新取得写权限

latest_revision conditional check:
  防并发处理导致 revision 冲突

idempotency terminal-outcome arbitration:
  防 ACK 丢失、重叠 retry 或并发 rejection/acceptance 为同一 logical edit 产生两个互相矛盾的终态
```

这里还要把两种完全不同的 serialization boundary 分开。正常 edit acceptance 是高频 hot path，它不需要拿一个覆盖整个文档生命周期的长锁；它只需要在短数据库事务里原子检查 `current_epoch`、`owner_generation_id`、`latest_revision` 和 `(document_id, client_change_id)` 的 terminal-outcome arbitration 状态，然后 append accepted change、写入 `ACCEPTED` terminal result 并推进 frontier。

但 owner recovery、snapshot publication、snapshot cleanup/compaction 属于低频 lifecycle 操作。它们会跨越 snapshot I/O、replay、metadata rebuild 等多步过程；如果让这些过程互相穿插，正确性证明会迅速变复杂。这里更便宜的做法是为每个 document 使用同一个 **session-scoped PostgreSQL advisory lock**：

```text
lifecycle_lock(document_id)
  serializes:
    - new-owner recovery
    - checkpoint publication
    - checkpoint cleanup / compaction

normal accepted-edit transaction:
  does NOT acquire lifecycle_lock
```

new-owner recovery 可以具体写成：

```text
1. acquire lifecycle_lock(document_id)
2. in a short authoritative transaction:
     advance document.current_epoch to the new strictly-greater epoch
3. capture H = authoritative latest_revision
4. select one authoritative snapshot P=(content,S), where S <= H
5. install exactly P
6. replay canonical accepted changes S+1..H in order exactly once
7. rebuild OT metadata from snapshot + canonical tail
8. initialize local head = H and next revision = H+1
9. only now expose transform / acceptance entrypoints
10. release lifecycle_lock
```

Recovery 的 authoritative truth 还必须和 recovering document 本身做 provenance closure。对 recovery target `D`，ownership handoff、captured `recovery_head`、selected checkpoint、replayed canonical tail、重建出的 logical/OT runtime state，以及最终恢复服务的 owner context 必须全部属于同一个 `D`；即使另一个 document `D2` 的 checkpoint、tail 或重建结果在数值上与 `D` 完全相同，也不能替代 `D` 的 authoritative truth。`lifecycle_lock(D)` 只能序列化 `D` 的 checkpoint lifecycle，因此跨 document 取 checkpoint/tail 同样会破坏 lock boundary。correctness model 将这一关系命名为 `recovery_document_scope_binding`。

snapshot publication 和 cleanup/compaction 也必须先拿同一把 `lifecycle_lock(document_id)`，并一直持有到各自 lifecycle procedure 正常结束或 abort。这样 recovery 选中并加载某个 checkpoint 的过程中，checkpoint lifecycle 不会从旁边切换或删除它。

这些 non-append lifecycle transition 还必须保持 target document 的 authoritative provenance，而不只是保持 replay 后的 extensional value。对 target document `D`，语义上为 snapshot publication、compaction、checkpoint replacement 或 cleanup 结果提供依据的 accepted history / checkpoint source，以及最终安装为 `D` authoritative representation 的 lifecycle output，都必须继续绑定 `D`。correctness model 用 `authoritative_history_lifecycle_output` 表示这类 lifecycle procedure 最终安装/发布的 durable authoritative representation。另一个 document `D2` 的 authoritative history 即使与 `D` 在 revision、payload、frontier 和 replay result 上完全相同，也不能被重新绑定成 `D` 的 authoritative source。这个约束不禁止底层 content-addressed blob、immutable page 或其他 physical storage deduplication；允许共享的是 physical bytes，不允许偷换的是 document-scoped authoritative ownership/reference。correctness model 把这条关系命名为 `authoritative_history_lifecycle_provenance`。

这里还有一个必须显式固定的 observer boundary：snapshot publication / compaction 只改变 durable authoritative representation，不直接替换当前 active owner 已安装的 acceptance-relevant runtime state，也不能绕过 recovery barrier 直接开放新的 transform / acceptance entrypoint。某个 snapshot / compacted representation 如果后来被用来初始化或恢复 active owner，必须走上面的 recovery 路径：绑定 authoritative head、恢复 logical state 与 OT metadata，并在 reconstruction 完成后才开放 acceptance。这样 optimized representation 自身只需要保持可恢复的 authoritative semantics，而“继续从 H 正确接受下一条 canonical change”的运行时等价性由 completed recovery 建立。

这里刻意不使用一个跨越 snapshot I/O/replay 的长 PostgreSQL transaction 或 row lock。那会把外部 I/O、连接占用、MVCC snapshot、vacuum/bloat 等成本压到数据库上；session advisory lock 更适合这种低频、跨多步的互斥，而且 session/process/connection 失败时会自动释放。

也不需要持久化 `RECOVERING/READY` 状态或所谓 active/staged generation。epoch bump 先把旧 owner fence 掉，新 owner 在本地 reconstruction 完成前不开放 acceptance；如果它在中途 crash，session lock 自动释放，下一次 takeover 以更高 epoch 再从 durable truth 重建即可。这里牺牲的是低频 lifecycle operation 之间本来就没有价值的并发，换来的是更小的 correctness state machine 和更直接的恢复路径。

这和 job scheduler 里的 lease/fencing 结论一致：lease 只能说明“我以为自己还活着”，不能阻止旧 owner 在 lease 过期后继续对外部资源产生副作用。真正防旧 owner，要由下游 authoritative store 校验 fencing token。

这个设计的关键是定义清楚系统承诺：

```text
acked change set:
  must already be durable accepted
  must survive failover

unacked change set:
  may or may not have been accepted
  client retries with idempotency key
```

如果服务端 append accepted-change-set 成功，但 ACK 之前崩溃，客户端 retry 时服务端通过 `client_change_id` 返回已有结果。如果服务端收到 raw edit，但还没 append accepted-change-set 就崩溃，这个 edit 没有被系统接受，客户端重试即可。

这是一种非常干净的 failover 语义。它不承诺保住崩溃瞬间所有未提交的 in-flight edit，但它能清楚保证：已经 ACK 的 edit 不丢，未 ACK 的 edit 可重试。

这里“已经 ACK 的 edit 不丢”是一个 safety guarantee，不是 availability/liveness guarantee。correctness model 把这条语义边界命名为 `acked_edit_failover_survival`。更精确地说：如果某个 ACKed edit 位于 revision r，而之后一次 owner recovery 完成并从 H >= r 恢复服务，那么 recovered logical state 必须与 canonical history replay through H 相同；failover 不能额外漏掉这个 edit。这个表述不承诺 recovery 一定会被调度或成功完成，也不承诺某个 client 最终一定重连看到它。后续 canonical edit 当然可以合法覆盖或反转它的可见效果，因此“survive”也不等于“这个 edit 的效果永远在当前文档里可见”。

同样，history compaction 之后也不要求这个 edit 对应的 physical accepted-change row 永远可以被单独查询。系统要保存的是足以维持 canonical semantics、恢复和幂等协议的 authoritative representation，而不是永久保存每一种历史物理表示。

对于非常热门或高价值文档，可以维护 warm standby。这个 standby 也不需要复制一个魔法黑盒。它只需要 replay truth：

```text
warm standby tails accepted-change-set log
rebuilds committed OT state
on failover:
    receives new fenced epoch
    catches up to committed frontier
    starts from next revision
```

这样带来的自由度很大：普通文档可以 cold recovery；热门文档可以 warm standby；严格 RTO 的文档可以预热 OT instance。所有这些方案都依赖同一个事实源：accepted-change-set log + snapshot frontier。

一句话：

```text
Hot backup should replay truth, not speculate ahead of truth.
```

## 7. 为什么这和 Raft 如此相似

现在回到标题：为什么理解 Raft，才能更容易设计出一个靠谱的 Google Docs 架构？

两者解决的问题不同，但它们共享一组重要的工程抽象：

```text
并发输入
→ 仲裁/排序/接受
→ 形成一条可重放的 committed history
→ 由 history 推导当前状态
→ 通过 snapshot 加速恢复
→ 在 owner/leader 失败后从 durable truth 重建
```

Raft 里是：

```text
client command
→ leader / consensus
→ committed log entry
→ state machine apply
→ snapshot checkpoint
→ new leader / follower replay committed history
```

Google Docs 里是：

```text
raw client edit
→ OT owner transform/rebase
→ accepted change set at revision N
→ document state apply
→ document snapshot(frontier)
→ new OT owner replay accepted history
```

理解 Raft 的人，会本能地问：

```text
什么是 committed？
log 在哪里？
snapshot 覆盖到哪个 index？
leader 崩了谁能继续写？
client retry 会不会重复执行？
旧 leader 死后复活怎么防止继续写？
```

这些问题迁移到 Google Docs 就是：

```text
什么是 accepted change set？
canonical change-set history 在哪里？
snapshot 覆盖到哪个 revision？
OT owner 崩了怎么恢复？
client edit retry 怎么 dedupe？
旧 owner 恢复后怎么 fencing？
```

这就是 Raft mental model 的价值。它让我们不再满足于“Document Service 用 OT 解决一致性”，而是追问这个服务到底定义了什么历史、承诺了什么 durability、如何恢复、如何处理重复和乱序。

但边界也要说死：OT 不等于 Raft，也不提供 Raft 那种多副本共识。类比只在 mental model 上成立——linear history、state machine、committed log、replay。对应到实现层，就是 OT owner 决定 accepted change-set order，存储提交边界把这个 order 变成 durable truth。

Google Docs 不需要 Raft，但它需要一种类似的严肃性：把输入变成 accepted history，把 accepted history 变成状态，把状态 checkpoint 成 snapshot，把 snapshot 和 log frontier 绑定起来，再让新的 owner 能从 durable truth 恢复。

## 8. CRDT 是另一种边界选择

讲 Google Docs 时，总会有人提出 CRDT。CRDT 的吸引力很直接：如果每个客户端或多个 server 都可以独立生成操作，最终通过 merge 收敛，那似乎就能摆脱单个 OT owner 的写入瓶颈。

但这个 tradeoff 必须说清楚。

OT 的模型更像：

```text
server-authoritative
per-document transform authority
accepted change-set history has total order
```

CRDT 的模型更像：

```text
multi-origin change sets
change identity
causal metadata
commutative merge
eventual convergence
```

CRDT 不需要为所有并发操作建立一个中心化全序，但这不代表它可以完全无视顺序。插入和删除之间存在因果关系；格式变更、range change、undo/redo 也通常需要 causal metadata。它省掉了中心 sequencer，却引入 change identity、causal/vector metadata、tombstone、identifier growth、compaction、客户端协议复杂度。

如果产品目标是 local-first、offline-first、P2P、弱中心化同步，CRDT 很有价值。但在 Google Docs 这种 server-authoritative collaboration system 里，OT + accepted-change-set log 更容易定义 truth boundary，也更容易解释 ack/retry/failover/reconnect。

这个判断可以压成一句：

```text
CRDT buys topology freedom, not free simplicity.
```

这比“用 CRDT 更高级”更经得起追问。

## 9. 存储选型叙事：先从 DynamoDB 起步，再收敛到 PostgreSQL

一个实用起点是 DynamoDB。它对应用层水平扩展友好，也符合“按 `document_id` 分区、按 key 路由”的直觉。

先把默认方案摆出来是合理的：

- 文档天然按 `document_id` 水平切分
- 跨文档吞吐靠分片扩展
- 接入层和 owner 路由模型都容易和 key-value 存储对齐

但当你把 acceptance boundary 写实，就会发现这条 critical path 需要在一次提交里同时满足：

- `(document_id, revision)` 唯一
- `(document_id, client_change_id)` 幂等
- `owner_epoch` fencing 校验
- accepted change durable

在 DynamoDB 里，这通常会走到 `TransactWriteItems`（尤其这些事实分散在多个 item/表时）。它不是做不到，而是条件检查 + 多 item 写入 + 事务协调会让写路径变重，成本和延迟模型更“显眼”。

把约束写全后，PostgreSQL 往往更合适：不是因为“SQL 更高级”，而是因为这里的 acceptance decision 本来就是单 document、单分片内的原子决策，本地事务语义更贴边界。

先拆清职责，避免一个常见误解：数据库事务本身不负责把并发 change set 串行化。

```text
client concurrency:
  - 多客户端可基于不同 base_revision 并发产出 raw edits

OT owner / control layer:
  - 决定 canonical acceptance order
  - 对旧上下文 change set 做 transform/rebase
  - 产出 rev=N+1, N+2... 的 accepted change sets

DB transaction:
  - 原子提交单步 accepted change
  - 保证 (document_id, revision) 唯一
  - 保证 (document_id, client_change_id) 幂等
  - 校验 owner_epoch fencing
  - 不负责 transform，也不负责业务语义上的排序
```

所以真正的串行点是：`one active OT owner per document -> canonical accepted sequence`。
数据库承担的是提交边界：durable commit + uniqueness + idempotency + fencing。

在 PostgreSQL 里，这条路径通常就是一次本地事务：

```sql
BEGIN;
INSERT INTO accepted_changes (
  document_id,
  revision,
  client_change_id,
  owner_epoch,
  transformed_change
)
VALUES (...)
ON CONFLICT DO NOTHING
RETURNING revision;

-- only the transaction that actually inserted this acceptance may advance the frontier
-- if INSERT returned 0 rows because (document_id, client_change_id) already exists:
--     ROLLBACK and return the existing canonical acceptance
-- if INSERT returned 0 rows because the revision lost arbitration:
--     ROLLBACK and retry / retransform as appropriate

UPDATE documents
SET latest_revision = $revision
WHERE document_id = $doc_id
  AND owner_epoch = $epoch
  AND latest_revision = $revision - 1;

-- require UPDATE rowcount = 1
-- otherwise ROLLBACK and return fenced/conflict
COMMIT;
```

这段 SQL 在文中仍是简化表达，但事务成功必须同时满足两个局部事实：`INSERT ... RETURNING` 确认当前 attempt 确实创建了这一条 canonical acceptance，随后 `UPDATE` 也必须恰好命中 1 行。任一条件失败都不能提交。这样 `(document_id, client_change_id)` 的 committed accepted row 本身就可以作为 hot-path idempotency success evidence；如果另有 accepted-key index，它也必须放进同一个事务，使 canonical append、accepted evidence 和 frontier advance 一起 commit 或一起 rollback。不能出现 `INSERT` 因 conflict 实际没有创建 acceptance，却仍然推进 `latest_revision` 的半成功状态。

更干净的 schema 是把顺序和幂等约束放进同一张表：

```sql
CREATE TABLE accepted_changes (
  document_id        BIGINT NOT NULL,
  revision           BIGINT NOT NULL,
  client_id          BIGINT NOT NULL,
  client_change_id   UUID   NOT NULL,
  owner_epoch        BIGINT NOT NULL,
  transformed_change BYTEA  NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, revision),
  UNIQUE (document_id, client_change_id)
);
```

这两个约束分别兜住：

- 同一 document 不会出现两个不同的 `rev=N`
- 同一个 client change 不会被 accepted 两次

代价也要讲清：

1. 单 document 写入天然串行，这是 OT/canonical history 的语义，不是 PostgreSQL 引入的限制。
2. 跨 document 吞吐仍然要靠 application-level sharding（按 `document_id` 路由到不同 PostgreSQL shard）。
3. `documents.latest_revision` 会成为 per-document 的高频更新行，但这不是额外引入的全局瓶颈：server-authoritative OT 本来就要求单个文档的 canonical acceptance 顺序串行化。在本设计中，`latest_revision` 是 authoritative frontier，必须与 accepted-change append 在同一事务中原子推进。跨文档吞吐通过按 `document_id` 分片扩展；若单个文档的写入吞吐最终成为瓶颈，则需要重新设计 canonical sequencing mechanism，而不能简单把 frontier 降级为异步 cache。

补一句边界条件：如果你真的让多个 OT instance 并发 transform 同一个 document，再交给数据库用唯一约束做“最后仲裁”，correctness 可以成立，但工程上通常更差——会出现更多冲突重试、retransform 和 tail latency。除非你进一步做 segment-level ownership，否则同一条 canonical history 的 head 仍是串行瓶颈。

对这条瓶颈也要实话实说：这是有意约束，不是隐藏 bug。它牺牲的是单文档极端写吞吐，换来的是清楚的 truth boundary、replay 语义和 failover 路径。

因此可以把最终立场讲成：默认从 DynamoDB 起步是合理的；但当你把 acceptance boundary 的原子条件写全，PostgreSQL 往往是更自然、更便宜、更可解释的落点。

```text
PostgreSQL does not remove the atomic acceptance boundary;
it makes that boundary local, cheaper, and easier to reason about.
```

## 10. 最终架构

把最终设计收敛成一张图就是：

```text
Client:
  - local optimistic edit
  - sends edit with client_change_id and base_revision
  - binds every server-visible raw edit to the canonical client frontier declared by base_revision
  - tracks last_applied_revision
  - keeps (client_document_id, client_local_document, last_applied_revision) bound to one document's canonical history in every user-visible state
  - tracks unresolved optimistic edits by client_change_id
  - applies only continuous revisions
  - freezes editing during reconnect reconciliation
  - while unresolved pending edits remain, keeps normal editing disabled until explicit reconciliation clears them
  - while the visible canonical prefix contains acceptance (D,k), keeps speculative overlay (D,k) absent across every subsequent user-visible client transition; canonical publication cannot retire it only temporarily
  - unresolved edits may enter conflict handling; automatic local rebase is out of scope

WebSocket Gateway:
  - maintains live connections
  - pushes accepted change sets
  - delivery only, not truth

Document Router / Coordinator:
  - maps document_id to active OT owner
  - manages ownership, failover, fencing epoch

OT Owner / Document Actor:
  - one active owner per document or document shard
  - maintains committed in-memory OT state
  - transforms raw edits into canonical accepted change sets
  - appends accepted change set before ACK/broadcast

Accepted-change-set Store:
  - source of truth (PostgreSQL per document_id shard)
  - key: document_id + revision
  - stores transformed_change and OT metadata needed for replay
  - stores client_change_id for idempotency
  - optionally uses synchronous replication for HA
  - advances latest revision frontier

Snapshot Store:
  - stores snapshot_content + snapshot_revision
  - used for load, reconnect, failover, compaction

Catch-up API:
  - input: document_id, last_applied_revision, pending client_change_ids
  - captures one authoritative response head H
  - output: missing deltas or snapshot + tail deltas through H
  - output: for each pending key, accepted revision <= H or unresolved through H
```

写入路径：

```text
client edit
→ active OT owner
→ transform/rebase against committed state
→ conditional append accepted_change@N+1
   (epoch == current_epoch, latest_revision == N, client_change_id not seen)
→ advance frontier
→ ACK client
→ broadcast accepted_change@N+1
```

恢复路径：

```text
new owner
→ acquire lifecycle_lock(document_id)
→ advance authoritative epoch
→ capture H = latest_revision
→ load one authoritative snapshot(frontier=S <= H)
→ replay accepted change sets S+1..H
→ rebuild OT metadata
→ initialize local head H / next revision H+1
→ expose acceptance
→ release lifecycle_lock
```

客户端重连：

```text
client freezes editing
→ sends last_applied_revision + pending client_change_ids
→ server captures response head H
→ server returns canonical catch-up through H
   (missing accepted changes, or snapshot + tail)
→ server returns pending-key acceptance resolution through the same H
→ client adopts canonical state through H and removes every pending overlay already accepted within H
→ client atomically exposes the reconciled state
→ if unresolved pending edits remain, enter conflict / reconciliation flow
→ otherwise resume live stream and editing
```

为了让 correctness DAG 能系统检查 state closure 与 provenance closure，而不是继续依赖 path-by-path 的文字枚举，当前模型给关键跨边界关系使用以下 typed semantic contracts。它们定义 proposition 的语义边界，本身不作为 proof premise：

```text
state invariants:
  accepted_history_frontier_coherence
    accepted_change_log 与 document_latest_revision 始终表示同一个 gap-free canonical prefix

  snapshot_frontier_coherence
    每个 authoritative published snapshot 的 content 与 snapshot_revision 表示同一个 canonical prefix

  client_canonical_frontier_coherence
    每个代表文档 D 的 user-visible client state 中，(client_document_id=D, client_local_document, last_applied_revision=F) 满足 client_local_document = Replay_D(F)

  visible_canonical_speculative_exclusion
    visible canonical prefix 已包含 acceptance(D,k) 时，不得同时存在 matching speculative overlay(D,k)

  unresolved_pending_blocks_normal_editing
    unresolved pending edits 非空时，client 不能进入 normal editable mode

  accepted_evidence_correspondence
    authoritative already-accepted evidence 与 committed canonical acceptance 保持事实对应

provenance bindings:
  request_authoring_frontier_binding
    submitted document_id / raw_edit / base_revision 与同一文档的真实 canonical authoring state 绑定

  logical_edit_request_payload_binding
    submitted (document_id, client_change_id, raw_edit, base_revision) 共同编码同一个 logical_client_edit；不能保留 identity/frontier 却替换 logical operation

  catchup_head_binding
    catch-up response head 来自一次 authoritative document_latest_revision capture，并贯穿该 response

  recovery_head_binding
    recovery head 来自 post-handoff authoritative document_latest_revision capture

  acceptance_request_identity_binding
    submitted (document_id, client_change_id) 原样成为 canonical acceptance / idempotency identity

  definitive_rejection_request_binding
    authoritative REJECTED 只能来自同一个 submitted_request 的 definitive rejection decision，不能把另一个 request 的 rejection terminalize 到当前 identity

  accepted_evidence_provenance
    already-accepted evidence 只能来自 matching committed canonical acceptance

  recovered_ot_state_provenance
    recovered OT state 只能来自 selected authoritative checkpoint + exact canonical tail

  ot_success_acceptance_binding
    canonical accepted payload 必须来自对应 submitted raw edit 的 successful OT result

  ot_result_frontier_binding
    OT result 必须继续绑定它计算时的 canonical frontier，并在同一 frontier 上才能 commit

  live_delivery_acceptance_binding
    live delivery 只能派生自 committed canonical accepted history
```

这套设计的核心不在组件数量，而在不变量清楚：

```text
accepted = durable appended to canonical history
append must pass epoch + latest_revision + client_change_id checks
acked change set must survive failover
unacked change set may retry
snapshot must carry frontier
client applies only continuous revision
OT owner is materialized state, not truth
WebSocket is delivery, not truth
```

所以这套架构经得起追问。

## 结语：系统设计不是画方框，而是定义承诺

Google Docs 这道题最有价值的地方，不是让我们背会 OT、CRDT、WebSocket 或数据库选型，而是逼我们回答系统设计里最核心的问题：

> 这个系统的事实在哪里？谁可以修改事实？什么时候修改算成功？失败后如何从事实恢复？

如果回答不了这些问题，`Document Service` 就只是一个黑盒。它看起来像架构，实际上只是把困难藏了起来。

理解 Raft 的意义就在这里。Raft 不只是一个共识算法，它也是一套训练工程师思考有序历史、持久化真相、状态机恢复和故障边界的 mental model。它让你本能地区分 raw input 和 committed entry，区分 leader memory 和 durable log，区分 snapshot 和 log frontier，区分 ACK 成功和请求到达。

把这套模型迁移到 Google Docs，OT 就不再是魔法，Document Service 也不再是黑盒。我们得到的是一个机制清楚的协作编辑系统：OT 负责 transform，accepted-change-set log 定义 truth，snapshot 加速恢复，WebSocket 负责 delivery，客户端用 revision frontier 验证连续性。

这就是“WebSocket + OT + DB”的题解和真正可 defend 架构之间的差别。
