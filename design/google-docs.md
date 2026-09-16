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
3. server produces candidate accepted_change
4. server assigns revision N+1
5. server atomically appends accepted_change@N+1 only if:
     - document.current_epoch == owner_epoch
     - document.latest_revision == N
     - client_change_id not already accepted
   then advances document frontier
6. commit succeeds
7. server ACKs client and broadcasts accepted_change@N+1
```

`client_change_id` 不是细枝末节，而是协议的一部分。客户端为每个 logical edit 生成一个新的 UUID；同一个 edit 的所有 retry 复用这个 UUID，不同 logical edit 使用不同 UUID。服务端可能在写入 accepted-change-set log 成功之后、返回 ACK 之前崩溃。客户端没有收到 ACK，会重试同一个 edit。如果系统没有稳定的 idempotency identity，它可能把同一个用户操作应用两次。

这里的幂等身份直接定义为 `(document_id, client_change_id)`。`client_id` 可以继续作为审计、调试或来源 metadata 存储，但不参与“这是不是同一个 logical operation”的判断。这个身份必须跨 acceptance boundary 原样保留：如果携带 `(document_id=D, client_change_id=k)` 的请求产生 canonical acceptance，那么 authoritative accepted record 以及与该 acceptance 关联的 durable idempotency evidence 都必须继续以同一个 `(D,k)` 标识它；服务端不能在 request admission、OT transform、idempotency arbitration 或 commit 之间重新生成、替换或错误绑定这个 protocol identity。否则 retry / reconnect 虽然仍然使用 `k`，却无法定位已经以其他 key 写入的那次 acceptance。

另一个容易漏掉的边界是时间：只要协议仍允许某个旧 edit 被 retry，系统就必须保留足以证明这个 key 已经 accepted 的 authoritative evidence。即使 accepted-change history 被 snapshot / compact，也不能因为原 row 消失就让同一个 key 再次产生新的 acceptance；实现上可以保留原记录，或者把 dedupe identity 压缩进独立的 durable idempotency index / tombstone。

但这份 evidence 还必须满足反方向的 soundness：**authoritative state 只有在同一个 `(document_id, client_change_id)` 已经存在 committed canonical acceptance 时，才能把这个 identity 分类为 `accepted`。** 幂等 gate 内部可以有 reservation、in-progress arbitration 或 transient ownership state，但这些状态必须与 `accepted` 明确区分，不能提前充当“已经接受”的事实。实现上，accepted evidence 可以直接来自 canonical accepted row / unique index，也可以是与 accepted append 在同一提交边界内原子产生的 durable index；不能先永久写下 `accepted(k)`，再尝试另一个可能失败的 canonical append。否则如果 accepted marker 已持久化而 append 没发生，客户端 retry 会被一个不存在的成功结果永久挡住：系统既没有接受这次 edit，也不再允许它被接受。

因此，对于协议仍可能查询的 idempotency identity，`accepted` evidence 与 canonical acceptance 的关系不是单向的“接受以后记住 key”，而是事实对应关系：evidence 不能漏掉真实 acceptance，也不能凭空制造不存在的 acceptance。

在正常 hot path 上，最简单的实现其实不需要第二套“成功状态表”：`accepted_changes` 中带 `UNIQUE (document_id, client_change_id)` 的 committed row 本身就可以同时作为 canonical acceptance 和 idempotency success evidence。若实现为了查询或生命周期管理另外维护 accepted-key index，那么该 index 不能成为独立 truth；它必须与 canonical append 在同一个数据库 acceptance transaction 中一起 commit 或一起 rollback。也就是说，事务提交以后可以观察到“canonical acceptance + accepted evidence”，事务未提交或 abort 后只能观察到“两者都没有”，不能留下一个 phantom `already accepted` marker。

这里也不要求每一次失败 attempt 都持久化 terminal `FAILED`。timeout、crash-before-commit、cancellation 等只说明本次 attempt 没有得到确定的 committed success；只要不存在 committed canonical acceptance / accepted evidence，同一个 logical edit 仍可按原 `client_change_id` 重试。只有未来协议显式引入“可重放的永久 rejection”时，才需要把 terminal rejection 作为另一类 durable outcome 建模。

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

下面的 reconnect reconciliation 只是这个持续 observer invariant 在 snapshot/catch-up 场景下更复杂的一种实现：除了不能 double-render，还必须处理 response head、unresolved pending identity、stale response 和原子 publication。

### 5.1 Pending optimistic edit：重连只保证 identity reconciliation，不保证自动 local rebase

`last_applied_revision` 只能描述客户端已经吸收的 canonical history。真实客户端还会有另一类状态：已经在本地 optimistic 展示、但尚未得到服务端确定结果的 logical edits。它们必须继续保留原始 `client_change_id`，因为 reconnect 后客户端不能仅凭 payload 判断某个本地 edit 是否已经被 canonical history 吸收：raw edit 和服务端 OT 后的 canonical change 本来就可能不同。

因此重连请求除了 `last_applied_revision=L`，还携带当前 pending edit 的 `client_change_id` 集合。客户端进入 resync 状态后可以暂时禁止新的编辑；这是一种有意选择的简单 UX，用来避免在 canonical base 正在切换时继续制造新的 speculative state。

服务端为一次 catch-up 在一个明确的 authoritative-store read / linearization point 读取 `document_latest_revision`，并把当时读到的值固定为这次响应的 authoritative response head `H`。后续 canonical range / snapshot mode selection、pending-key reconciliation、completion 与 publication 都必须使用这个同一个 captured `H`；`H` 捕获之后系统当然可以继续接受新的 revision，这些更晚的 acceptance 不属于本次 response，也不要求本次 catch-up 追上一个持续移动的 head。

然后服务端在同一个逻辑响应中返回两类信息：

```text
canonical catch-up through H:
  retained deltas L+1..H
  or snapshot(frontier=S) + tail S+1..H

pending resolution through H:
  for each pending client_change_id k:
    accepted at revision r <= H
    or unresolved through H
```

这里 `unresolved through H` 只表示 authoritative history through `H` 尚不能证明该 key 已被接受；它不是“永远不会被接受”的终态承诺。为了让这个判断在 snapshot / compaction 之后仍然可靠，accepted-key lifecycle 不能只保留“这个 key 以后不可复用”的 tombstone；它还必须保留 accepted revision，或等价的 frontier evidence，使服务端能够判断某个 pending key 是否已经包含在指定 response head `H` 中。原始 accepted row 可以被压缩，但这项 identity-to-frontier 证据不能在客户端仍可能拿该 key 来 reconciliation 时丢失。

客户端的 safety boundary 是：**整个 resync 先在不可见的 candidate state 中完成 canonical base adoption 与 pending reconciliation，随后只能以一次原子、单调的 publication 切换对用户可见状态。** 如果 response 证明 `client_change_id=k` 已经在 revision `r <= H` 被接受，那么 candidate canonical base through `H` 已经包含这个 logical edit；客户端不得在任何 user-visible resync 中间态里再把同一个 `k` 的 speculative overlay 叠加到已经包含它的 canonical prefix 上。

```text
accepted_revision(k) <= candidate_frontier(H)
    =>
k must be absent from the candidate's visible speculative overlay before publication
```

多个 reconnect/resync attempt 还可能因为网络延迟或 retry 重叠。设当前已经对用户可见的 canonical frontier 为 `F`。一个 response through `H` 到达时，如果 `H < F`，它已经是 stale response：**整个 response 必须被丢弃，不能回退 visible canonical base / last_applied_revision，也不能恢复已经被较新 reconciliation 删除的 speculative overlay 或 pending state。** 正确性真正依赖的是 canonical frontier 的单调 publication，而不是 request 发起顺序；因此不要求为 correctness 引入独立的 attempt-generation ordering。

客户端也不能一边增量应用 response、一边把未完成 reconciliation 的 prefix 暴露给用户。例如 `k` 在 revision 105 已被接受、response head 为 110 时，不能先暴露 canonical@105 且仍保留 overlay `k`，然后到 110 才清理。更简单的状态转换是：

```text
freeze editing
→ receive canonical state through H + pending resolutions through H
→ build reconciled candidate state privately
→ if H < currently visible frontier F: discard the entire candidate
→ otherwise remove every speculative overlay whose key is already accepted within H
→ atomically expose exactly one reconciled state through H
```

因此 user-visible canonical frontier 在 resync publication 上永不倒退，也不存在 user-visible 的 checkpoint/tail 中间 prefix。不同 attempt 可以并行计算 candidate，但只有满足上述 publication guard 的完整 reconciled candidate 才能影响可见状态。

对于仍然 `unresolved through H` 的 local edits，本文**不承诺自动把它们 rebase 到新的 canonical base 上，也不承诺 reconnect 后立即恢复可编辑状态**。客户端可以继续保持文档 locked，并把这些 edits 进入 conflict / reconciliation flow；最终 UI 可以像版本冲突一样保留用户原始内容并要求人工处理，但这属于本文 scope 之外。

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

这里还缺一个决定 failover 是否安全的边界：fencing 必须下沉到 accepted-change-set append 的条件写里，而不是只停留在 coordinator 的内存认知里。

可以把 ownership 建模成：

```text
document_owner_epoch:
  document_id -> current_epoch, owner_id, lease_expire_time
```

新 owner 接管时：

```text
1. coordinator uses conditional write / CAS to bump epoch
2. new owner gets a strictly larger epoch
3. every accepted-change-set append carries owner_epoch
4. accepted-change-set store only accepts writes with epoch == current_epoch
```

因此写入不应该只是：

```text
append document_id, revision=N+1
```

而应该是条件写：

```text
append accepted_change@revision=N+1
only if document.current_epoch == my_epoch
and     document.latest_revision == N
and     client_change_id not already accepted
```

这三个条件分别解决三件事：

```text
epoch / fencing token:
  防旧 owner 在 GC pause、网络隔离、慢恢复后“死后复活”继续写

latest_revision conditional check:
  防并发 owner 或重复处理导致 revision 冲突

client_change_id idempotency:
  防 ACK 丢失后的 retry 触发 double apply
```

这里还要把两种完全不同的 serialization boundary 分开。正常 edit acceptance 是高频 hot path，它不需要拿一个覆盖整个文档生命周期的长锁；它只需要在短数据库事务里原子检查 `current_epoch`、`latest_revision` 和 `client_change_id`，然后 append accepted change 并推进 frontier。

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

snapshot publication 和 cleanup/compaction 也必须先拿同一把 `lifecycle_lock(document_id)`，并一直持有到各自 lifecycle procedure 正常结束或 abort。这样 recovery 选中并加载某个 checkpoint 的过程中，checkpoint lifecycle 不会从旁边切换或删除它。

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
  - tracks last_applied_revision
  - tracks unresolved optimistic edits by client_change_id
  - applies only continuous revisions
  - freezes editing during reconnect reconciliation
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
