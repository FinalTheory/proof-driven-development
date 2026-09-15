# Correctness proof obligation

Reason only from the proof slice below. Treat explicit assumptions as accepted boundaries, 
but do not invent unstated premises. Try to refute the target before accepting it.

## Minimal system context

System model: `google_docs_mini`

This model describes the write, retry, ownership-failover, recovery, and reconnect paths of a server-authoritative collaborative document system. A client submits a raw logical edit. The server-side OT control layer may transform that input into a canonical accepted change. Accepted changes are represented in authoritative history, while clients may retry when request outcome is uncertain. Correctness claims in the DAG specify which stronger properties this design is intended to guarantee.

### In scope

- server-authoritative OT control plane
- canonical accepted-change-set history
- retry/idempotency semantics
- owner failover and fencing
- snapshot/replay recovery
- reconnect/catch-up semantics
- client revision continuity

### Out of scope

- implementation of the OT transformation algorithm itself
- rich-text semantics, range formatting, undo/redo, and selection semantics
- cross-document transactions

### Vocabulary

- **acceptance**: The system decision that creates a canonical accepted-change record in authoritative history. The exact atomicity conditions for that decision are separate claims.
- **acceptance_events**: Abstract set of canonical acceptance decisions used in formal idempotency notation.
- **ack**: A server response informing a client that its submitted edit was accepted. The relationship between ACK emission and durable commit is a separate correctness claim.
- **client_change_id**: A protocol identifier supplied by the client for a logical client edit. Stability across retries and uniqueness across distinct logical edits are explicit protocol assumptions, not part of this vocabulary definition.
- **document_id**: Stable identifier of one collaborative document.
- **idempotency_key**: Authoritative deduplication identity for a logical edit, concretely document_id plus client_change_id.
- **retry**: Resubmission caused by uncertainty such as timeout, lost ACK, reconnect, or process failure. A retry may race with the original request or another retry.

### Architecture mechanisms

- **acceptance_transaction**: Accepted-change creation and authoritative metadata updates commit as one database transaction.
- **accepted_history_log**: Canonical accepted-change log is the durable source of truth for edit history.
- **idempotency_gate**: Authoritative acceptance is keyed by document_id and client_change_id so one logical edit cannot create multiple acceptances.
- **recovery_replay**: A new owner reconstructs state from one authoritative checkpoint plus the exact canonical tail through the captured head.
- **retry_resolution**: Retries resolve through the authoritative idempotency/acceptance outcome rather than producing an independent success path.
- **server_authoritative_ot**: Server OT owner transforms client edits against canonical accepted history before acceptance.
- **snapshot_checkpoint**: Snapshots are authoritative checkpoints whose content is bound atomically to a canonical revision frontier.

### Failure model

Allowed events:
- **request_delivery_uncertainty**: Request delivery may be delayed, duplicated by retry, or have an uncertain outcome.
- **ack_loss_after_processing**: ACK delivery may be lost after server-side processing.
- **overlapping_requests**: Multiple client requests or retries may overlap in time.
- **owner_crash_restart**: An OT owner process may crash and later restart.
- **ownership_move_with_delayed_old_owner**: Document ownership may move to another owner while an old owner is delayed.
- **catchup_live_interleaving**: Catch-up and live delivery may interleave around reconnect.
- **history_lifecycle_change**: Accepted history may undergo snapshotting, compaction, or lifecycle cleanup.

Excluded events:
- **byzantine_behavior**: Byzantine behavior by clients, servers, or storage is excluded.
- **permanent_authoritative_quorum_loss**: Permanent loss of the authoritative storage quorum is excluded.

Liveness boundary: Do not infer eventual retry, reconnect, scheduling fairness, or eventual success from this failure model. Any liveness requirement must appear as an explicit assumption or claim in the proof slice.

### Interpretation rules

- Node IDs are navigation labels; the node statement is the authoritative proposition and must not be strengthened from the ID wording.
- Catalog term/state/mechanism entries define vocabulary and architecture primitives, not premises that a correctness property already holds.
- Every snake_case protocol/state shorthand used in a proposition must resolve to the typed catalog; undefined identifiers have no inferred semantics.
- Automation may reuse only approved catalog mechanisms. If a proof hole requires a new architecture mechanism or system-semantic choice, escalate to a human decision.
- Verifier descriptions are planned future evidence, not evidence that the proposition has already been established; acceptable evidence must observe the claim independently rather than merely restate the mechanism under test.

## Target

**G1_retry_is_logically_exactly_once** — ACK loss, reconnect, or retry cannot cause one logical client edit to create more than one accepted canonical change.

Direct dependencies: `C13_idempotency_key_is_temporally_unique`, `L34_retries_of_logical_edit_reuse_identity`

## Proof obligations

### `C13_idempotency_key_is_temporally_unique` (derived, depth 1)

Across overlapping and sequential processing of the same (document_id, client_change_id), including retries after owner recovery or accepted-history lifecycle changes, at most one acceptance event may create a canonical accepted change for that key.

Depends on: `L14_accepted_idempotency_identity_survives_lifecycle`, `C14_all_acceptance_paths_honor_idempotency_identity`, `L27_concurrent_first_acceptance_is_atomically_arbitrated`

Formal intent: `count(acceptance_events where idempotency_key = k) <= 1`

### `L34_retries_of_logical_edit_reuse_identity` (leaf, depth 1)

Within a document, every submission and retry of one logical client edit carries the same client_change_id.

Planned evidence:
- `property_test`: Generate ACK loss, reconnect, timeout, and process-retry traces for one logical edit and assert that every emitted attempt retains exactly the original client_change_id.

### `C14_all_acceptance_paths_honor_idempotency_identity` (derived, depth 2)

Every transition capable of creating a canonical accepted change for an idempotency key is constrained by authoritative accepted-key identity: if that key is already recorded as accepted, the transition cannot produce another acceptance event for it.

Depends on: `L20_acceptance_paths_are_authoritatively_mediated`, `L21_accepted_key_gate_rejects_new_acceptance`

Planned evidence:
- `control_flow`: Enumerate every acceptance-producing transition and prove each path consults the authoritative accepted-key identity before creating a new accepted change.
- `state_machine`: Explore normal submit, retry, delayed execution, failover, and resumed-owner paths; no transition can accept a key whose accepted identity is already authoritative.

### `L14_accepted_idempotency_identity_survives_lifecycle` (leaf, depth 2)

Once an acceptance event has occurred for an idempotency key, authoritative state continues to preserve sufficient non-reusable evidence that the key has already been accepted for every later state in which an execution carrying that key may still reach acceptance, including across owner recovery, snapshotting, compaction, and accepted-history lifecycle cleanup.

Planned evidence:
- `state_machine`: After accept(k), exercise recovery, snapshot, compaction, and cleanup transitions; while k may still reappear at acceptance, authoritative state must continue to distinguish k as already accepted.
- `fault_injection`: Crash and recover around idempotency-evidence publication and lifecycle transitions; no completed acceptance may become semantically reusable.

### `L27_concurrent_first_acceptance_is_atomically_arbitrated` (leaf, depth 2)

For overlapping transitions mediated by the authoritative accepted-key gate for the same initially absent key, the absent-key decision and create outcome are atomically arbitrated so that at most one transition can produce an acceptance event; every other contender cannot create an acceptance event for that key.

Planned evidence:
- `state_machine`: Explore lookup, reservation, insert, commit, conflict, and retry interleavings from an absent key; at most one create outcome may occur.
- `integration_test`: Race submissions with one key and observe a single acceptance-event identity even for conflict and upsert implementations.

### `L20_acceptance_paths_are_authoritatively_mediated` (leaf, depth 3)

Every transition capable of creating a canonical accepted change routes its acceptance decision through the authoritative accepted-key identity gate; no normal submission, retry, delayed execution, owner-recovery, or resumed-owner path can bypass that gate.

Planned evidence:
- `control_flow`: Enumerate every acceptance-producing transition and prove that each reaches the authoritative accepted-key decision before its acceptance sink.

### `L21_accepted_key_gate_rejects_new_acceptance` (leaf, depth 3)

For a transition mediated by the authoritative accepted-key identity gate, if authoritative state records the key as already accepted at the acceptance decision, that transition cannot create another acceptance event for the key.

Planned evidence:
- `state_machine`: Explore the gate's already-accepted and create branches under overlapping requests; an already-accepted decision can never yield a create outcome.

## Verification task

Return one verdict: `SUPPORTED`, `REFUTED`, or `INCOMPLETE`.

- `SUPPORTED`: explain why the listed dependencies are sufficient for the target.
- `REFUTED`: give a concrete execution/counterexample consistent with the slice.
- `INCOMPLETE`: identify the smallest missing premise, edge, or proof obligation.
- Distinguish a false dependency from a missing dependency.
- Do not treat planned verifier descriptions as evidence that has already passed.
