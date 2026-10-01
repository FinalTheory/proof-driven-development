#!/usr/bin/env python3

import argparse
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import correctness
from harness import contracts as harness_contracts
from harness import signatures as harness_signatures


def _leaf(statement: str):
    return {
        "kind": "leaf",
        "statement": statement,
        "source_refs": ["synthetic_source"],
        "mechanisms": ["synthetic_mechanism"],
        "verification": {
            "verifiers": [{"kind": "integration_test", "intent": "synthetic fixture verifier"}],
        },
    }


def make_fixture():
    """Immutable synthetic DAG used only for correctness.py tool regression tests."""
    return {
        "schema_version": "0.14",
        "system": {
            "id": "synthetic_correctness_tool_fixture",
            "summary": "Synthetic system used only to test correctness.py algorithms.",
            "liveness_boundary": "No liveness is implied.",
        },
        "source": {"article": "design/synthetic.md"},
        "scope": {
            "includes": ["synthetic correctness graph behavior"],
            "excludes": ["production behavior"],
        },
        "catalog": {
            "terms": {
                "synthetic_key": {"description": "A neutral identifier used only in tests."},
                "new_term": {"description": "A second synthetic semantic term used by signature tests."},
            },
            "state": {
                "synthetic_state": {"class": "authoritative", "description": "Synthetic authoritative state."}
            },
            "symbolic_dimensions": {
                "entity": {"description": "Synthetic entity coordinate."},
                "observation": {"description": "Synthetic observation coordinate."},
            },
            "mechanisms": {
                "synthetic_mechanism": {
                    "description": "Synthetic approved mechanism.", "automation_reusable": True
                },
                "provisional_mechanism": {
                    "description": "Synthetic human-only mechanism.", "automation_reusable": False
                },
            },
            "semantic_contracts": {
                "synthetic_safety_contract": {
                    "class": "safety_contract",
                    "definition": "Synthetic reusable semantic boundary used only by tool tests.",
                    "excludes": ["stronger synthetic guarantees"],
                    "automation_reusable": True,
                },
                "human_only_contract": {
                    "class": "equivalence_relation",
                    "definition": "Synthetic semantic contract that automation may not newly attach.",
                    "excludes": [],
                    "automation_reusable": False,
                },
            },
            "failure_events": {
                "synthetic_retry": {"class": "allowed", "description": "Synthetic retry may occur."},
                "byzantine_behavior": {"class": "excluded", "description": "Byzantine behavior is excluded."},
            },
            "verifier_kinds": {
                "integration_test": {"description": "Synthetic integration verifier."},
                "state_machine": {"description": "Synthetic state-machine verifier."},
            },
            "sources": {
                "synthetic_source": {"heading": "Synthetic section"}
            },
        },
        "refinement": {
            "decisions": {},
            "tooling_blockers": {},
            "nodes": {
                "G1_primary_goal": {"status": "pending", "blocked_by": []},
                "G2_secondary_goal": {"status": "pending", "blocked_by": []},
                "C1_composite_claim": {"status": "pending", "blocked_by": []},
                "L1_left_boundary": {"status": "pending", "blocked_by": []},
                "L2_right_boundary": {"status": "pending", "blocked_by": []},
                "L3_independent_boundary": {"status": "pending", "blocked_by": []},
            },
        },
        "assurance": {
            "specification_coverage": {"status": "unaudited"},
            "composition": {
                "G1_primary_goal": {"status": "unaudited"},
                "G2_secondary_goal": {"status": "unaudited"},
                "C1_composite_claim": {"status": "unaudited"},
            },
            "implementation_evidence": {
                "L1_left_boundary": {"status": "planned"},
                "L2_right_boundary": {"status": "planned"},
                "L3_independent_boundary": {"status": "planned"},
            },
        },
        "id_allocator": {
            "next_sequence": {"A": 2, "G": 3, "C": 2, "L": 4, "D": 1, "T": 1},
        },
        "assumptions": {
            "A1_fixture_environment": {
                "statement": "Synthetic environment assumption accepted by this fixture.",
                "source_refs": ["synthetic_source"],
                "status": "external_assumption",
            }
        },
        "roots": ["G1_primary_goal", "G2_secondary_goal"],
        "claims": {
            "G1_primary_goal": {
                "kind": "root",
                "statement": "The primary synthetic guarantee holds.",
                "source_refs": ["synthetic_source"],
                "depends_on": ["C1_composite_claim", "L3_independent_boundary", "A1_fixture_environment"],
            },
            "G2_secondary_goal": {
                "kind": "root",
                "statement": "The secondary synthetic guarantee holds.",
                "source_refs": ["synthetic_source"],
                "depends_on": ["L3_independent_boundary"],
            },
            "C1_composite_claim": {
                "kind": "derived",
                "statement": "Both synthetic component boundaries hold.",
                "source_refs": ["synthetic_source"],
                "depends_on": ["L1_left_boundary", "L2_right_boundary"],
            },
            "L1_left_boundary": _leaf("The left synthetic boundary holds."),
            "L2_right_boundary": _leaf("The right synthetic boundary holds."),
            "L3_independent_boundary": _leaf("The independent synthetic boundary holds."),
        },
    }


class CorrectnessToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = make_fixture()

    def graph(self):
        return correctness.Graph(copy.deepcopy(self.base))

    def _write_fixture(self, directory: str, doc=None):
        root = Path(directory)
        path = root / "correctness.yaml"
        path.write_text(
            correctness.yaml.safe_dump(doc or self.base, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        design_dir = root / "design"
        design_dir.mkdir(exist_ok=True)
        (design_dir / "synthetic.md").write_text(
            "# Synthetic\n\n## 1. Synthetic section\n\nSynthetic source body.\n",
            encoding="utf-8",
        )
        return path

    def test_fixture_is_valid(self):
        errors, warnings = correctness.validate(self.graph())
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_dangling_dependency_is_rejected(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["C1_composite_claim"]["depends_on"] = ["DOES_NOT_EXIST"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("dangling dependency DOES_NOT_EXIST" in e for e in errors))

    def test_cycle_is_rejected(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["depends_on"] = ["G1_primary_goal"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any(e.startswith("dependency cycle:") for e in errors))

    def test_unreachable_claim_is_rejected(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L4_unused_boundary"] = _leaf("Synthetic unreachable boundary.")
        doc["refinement"]["nodes"]["L4_unused_boundary"] = {
            "status": "pending", "blocked_by": []
        }
        doc["id_allocator"]["next_sequence"]["L"] = 5
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertIn("L4_unused_boundary: claim is unreachable from every root", errors)

    def test_invalidation_flows_dependency_to_dependent(self):
        graph = self.graph()
        distance, _ = graph.affected_by(["L1_left_boundary"])
        self.assertEqual(distance["C1_composite_claim"], 1)
        self.assertEqual(distance["G1_primary_goal"], 2)
        self.assertNotIn("G2_secondary_goal", distance)
        distance, _ = graph.affected_by(["L3_independent_boundary"])
        self.assertIn("G1_primary_goal", distance)
        self.assertIn("G2_secondary_goal", distance)

    def test_slice_is_dependency_closure(self):
        closure = self.graph().dependency_closure("G1_primary_goal")
        self.assertEqual(
            closure,
            {"G1_primary_goal", "C1_composite_claim", "L1_left_boundary", "L2_right_boundary", "L3_independent_boundary", "A1_fixture_environment"},
        )

    def test_stop_at_is_semantic_cut_point(self):
        closure = self.graph().dependency_closure("G1_primary_goal", stop_at={"C1_composite_claim"})
        self.assertIn("C1_composite_claim", closure)
        self.assertNotIn("L1_left_boundary", closure)
        self.assertNotIn("L2_right_boundary", closure)
        self.assertIn("L3_independent_boundary", closure)

    def test_machine_slice_carries_system_context(self):
        doc = correctness._slice_document(self.graph(), "G1_primary_goal", set())
        self.assertEqual(doc["system_context"]["system"]["liveness_boundary"], "No liveness is implied.")
        self.assertIn("Node IDs are navigation labels", " ".join(doc["system_context"]["interpretation_rules"]))
        self.assertIn("synthetic_mechanism", doc["catalog_context"]["mechanisms"])

    def test_prompt_emits_only_referenced_catalog_context(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._emit_slice_markdown(self.graph(), "G1_primary_goal", set(), prompt=True)
        text = output.getvalue()
        self.assertIn("## Minimal system context", text)
        self.assertIn("**synthetic_mechanism**", text)
        self.assertNotIn("**synthetic_key**", text)
        self.assertIn("### Failure model", text)

    def test_refinement_frontier_is_bottom_up(self):
        snapshot = correctness._refinement_snapshot(self.graph())
        self.assertEqual(snapshot["state"], "CONTINUE")
        runnable = {x["node"] for x in snapshot["runnable"]}
        waiting = {x["node"] for x in snapshot["waiting"]}
        self.assertEqual(runnable, {"L1_left_boundary", "L2_right_boundary", "L3_independent_boundary"})
        self.assertIn("C1_composite_claim", waiting)
        self.assertIn("G1_primary_goal", waiting)

    def test_human_decision_blocks_only_affected_branch(self):
        doc = copy.deepcopy(self.base)
        decision = "D1_retry_horizon"
        doc["id_allocator"]["next_sequence"]["D"] = 2
        doc["refinement"]["decisions"][decision] = {
            "status": "open",
            "question": "Synthetic semantic decision?",
            "reason": "Synthetic reason requiring a human choice.",
            "options": ["option_a", "option_b"],
        }
        doc["refinement"]["nodes"]["L1_left_boundary"]["blocked_by"] = [decision]
        graph = correctness.Graph(doc)
        errors, _ = correctness.validate(graph)
        self.assertEqual(errors, [])
        snapshot = correctness._refinement_snapshot(graph)
        self.assertEqual(snapshot["state"], "CONTINUE")
        self.assertIn("L1_left_boundary", {x["node"] for x in snapshot["blocked_frontier"]})
        self.assertIn("L2_right_boundary", {x["node"] for x in snapshot["runnable"]})
        self.assertIn("G1_primary_goal", snapshot["decision_impacts"][decision]["affected_roots"])
        self.assertNotIn("G2_secondary_goal", snapshot["decision_impacts"][decision]["affected_roots"])

    def test_global_state_is_human_blocked_only_when_no_runnable_frontier_remains(self):
        doc = copy.deepcopy(self.base)
        decision = "D1_global_blocker"
        doc["id_allocator"]["next_sequence"]["D"] = 2
        doc["refinement"]["decisions"][decision] = {
            "status": "open",
            "question": "Synthetic global decision?",
            "reason": "Synthetic reason requiring a human choice.",
            "options": ["option_a", "option_b"],
        }
        for entry in doc["refinement"]["nodes"].values():
            entry["status"] = "pending"
            entry.pop("signature", None)
            entry["blocked_by"] = [decision]
        graph = correctness.Graph(doc)
        errors, _ = correctness.validate(graph)
        self.assertEqual(errors, [])
        snapshot = correctness._refinement_snapshot(graph)
        self.assertEqual(snapshot["state"], "HUMAN_BLOCKED")
        self.assertEqual(snapshot["runnable"], [])

    def test_recursive_signature_reopens_stale_parent_automatically(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        for node_id, entry in doc["refinement"]["nodes"].items():
            entry["status"] = "stable"
            entry["blocked_by"] = []
            entry["signature"] = correctness._node_semantic_signature(graph, node_id)
        self.assertEqual(correctness._refinement_snapshot(correctness.Graph(doc))["state"], "COMPLETE")
        doc["claims"]["L1_left_boundary"]["statement"] += " Changed."
        snapshot = correctness._refinement_snapshot(correctness.Graph(doc))
        self.assertIn("L1_left_boundary", snapshot["stale"])
        self.assertIn("C1_composite_claim", snapshot["stale"])
        self.assertIn("G1_primary_goal", snapshot["stale"])
        self.assertNotIn("G2_secondary_goal", snapshot["stale"])

    def test_evidence_execution_state_is_not_a_canonical_claim_field(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["verification"]["status"] = "passed"
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("verification" in e and "status" in e and "Additional properties" in e for e in errors))

    def test_audit_prompt_is_generated_only_for_runnable_frontier(self):
        graph = self.graph()
        snapshot = correctness._refinement_snapshot(graph)
        runnable = snapshot["runnable"][0]["node"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(correctness._cmd_audit_prompt(graph, argparse.Namespace(node=runnable)), 0)
        text = output.getvalue()
        self.assertIn("Clean-context correctness refinement audit", text)
        self.assertIn("analysis-only", text)
        self.assertIn("do not invoke `mutate`", text)
        self.assertIn("Reason freely in prose", text)
        self.assertIn("small YAML verdict block", text)
        self.assertIn("dependency_findings:", text)
        self.assertIn("A leaf need not be", text)
        for signal_id in correctness.LEAF_STOPPING_SIGNALS:
            self.assertIn(f"`{signal_id}`", text)
        self.assertIn("reason to STOP decomposition", text)
        self.assertNotIn("proposed_refinement:", text)
        self.assertNotIn("recommended_state:", text)
        waiting = snapshot["waiting"][0]["node"]
        with self.assertRaises(SystemExit) as ctx:
            correctness._cmd_audit_prompt(graph, argparse.Namespace(node=waiting))
        self.assertIn("not runnable", str(ctx.exception))

    def test_proof_audit_treats_stable_direct_dependencies_as_opaque_contracts(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        for node_id in (
            "L1_left_boundary",
            "L2_right_boundary",
            "L3_independent_boundary",
            "C1_composite_claim",
        ):
            entry = doc["refinement"]["nodes"][node_id]
            entry["status"] = "stable"
            entry["signature"] = correctness._node_semantic_signature(graph, node_id)
        graph = correctness.Graph(doc)
        snapshot = correctness._refinement_snapshot(graph)
        self.assertIn("G1_primary_goal", {item["node"] for item in snapshot["runnable"]})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_audit_prompt(graph, argparse.Namespace(node="G1_primary_goal"))
        text = output.getvalue()
        self.assertIn("Both synthetic component boundaries hold.", text)
        self.assertIn("The independent synthetic boundary holds.", text)
        self.assertNotIn("The left synthetic boundary holds.", text)
        self.assertNotIn("The right synthetic boundary holds.", text)
        self.assertIn("Cut point: this node is intentionally treated as opaque", text)
        self.assertNotIn("Only then may you read the full", text)
        self.assertIn("complete specification for the clean audit", text)

    def test_compact_refinement_status_omits_stable_node_lists(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        entry = doc["refinement"]["nodes"]["L1_left_boundary"]
        entry["status"] = "stable"
        entry["signature"] = correctness._node_semantic_signature(graph, "L1_left_boundary")
        graph = correctness.Graph(doc)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_refinement_status(graph, argparse.Namespace(format="compact-yaml"))
        compact = correctness.yaml.safe_load(output.getvalue())
        self.assertIn("state", compact)
        self.assertIn("runnable", compact)
        self.assertIn("waiting", compact)
        self.assertIn("counts", compact)
        self.assertNotIn("stable", compact)
        self.assertNotIn("waived", compact)
        self.assertEqual(compact["counts"]["stable"], 1)

    def test_id_allocator_rejects_prefix_role_mismatch(self):
        doc = copy.deepcopy(self.base)
        body = doc["claims"].pop("L1_left_boundary")
        doc["claims"]["C2_left_boundary"] = body
        doc["id_allocator"]["next_sequence"]["C"] = 3
        doc["claims"]["C1_composite_claim"]["depends_on"] = ["C2_left_boundary", "L2_right_boundary"]
        ref = doc["refinement"]["nodes"].pop("L1_left_boundary")
        doc["refinement"]["nodes"]["C2_left_boundary"] = ref
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("prefix C does not match node role; expected L" in e for e in errors))

    def test_atomic_writer_strips_trailing_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "correctness.yaml"
            old_path = correctness.GRAPH_PATH
            correctness.GRAPH_PATH = path
            try:
                correctness._atomic_write_roundtrip(copy.deepcopy(self.base))
                lines = path.read_text(encoding="utf-8").splitlines()
                self.assertTrue(lines)
                self.assertFalse(any(line.endswith((" ", "\t")) for line in lines))
            finally:
                correctness.GRAPH_PATH = old_path

    def test_plain_data_normalizes_roundtrip_scalar_strings(self):
        rt = correctness._rt_yaml()
        doc = rt.load(
            "single: 'quoted value'\n"
            "folded: >-\n"
            "  folded value\n"
        )
        plain = correctness._plain_data(doc)
        self.assertIs(type(plain["single"]), str)
        self.assertIs(type(plain["folded"]), str)
        # Formal validation uses PyYAML to serialize canonical graph fragments.
        # A plain-data conversion must therefore be safe-dump compatible.
        correctness.yaml.safe_dump(plain, sort_keys=False, allow_unicode=True)

    def test_mutation_allocates_id_and_commits_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            old_path = correctness.GRAPH_PATH
            correctness.GRAPH_PATH = path
            try:
                graph = correctness.Graph.load(path)
                target = "C1_composite_claim"
                next_leaf = graph.doc["id_allocator"]["next_sequence"]["L"]
                plan = {
                    "mutation_version": 1,
                    "authority": "automation",
                    "preconditions": {"semantic_signatures": {target: correctness._node_semantic_signature(graph, target)}},
                    "operations": [
                        {
                            "op": "add_claim", "alias": "child", "kind": "leaf", "slug": "synthetic_mutation_boundary",
                            "body": {
                                key: value
                                for key, value in _leaf("Synthetic mutation boundary.").items()
                                if key != "kind"
                            },
                        },
                        {"op": "add_dependency", "node": target, "dependency": "@child"},
                    ],
                }
                plan_path = Path(tmp) / "plan.yaml"
                plan_path.write_text(correctness.yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    correctness._cmd_mutate(graph, argparse.Namespace(plan=str(plan_path), dry_run=False, reject_warnings=False))
                result = correctness.yaml.safe_load(output.getvalue())
                allocated = result["aliases"]["child"]
                self.assertEqual(allocated, f"L{next_leaf}_synthetic_mutation_boundary")
                committed = correctness.Graph.load(path)
                self.assertIn(allocated, committed.claims[target]["depends_on"])
                self.assertEqual(committed.doc["id_allocator"]["next_sequence"]["L"], next_leaf + 1)
            finally:
                correctness.GRAPH_PATH = old_path

    def test_stale_automation_mutation_is_rejected_without_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            before = path.read_text(encoding="utf-8")
            old_path = correctness.GRAPH_PATH
            correctness.GRAPH_PATH = path
            try:
                plan = {
                    "mutation_version": 1,
                    "authority": "automation",
                    "preconditions": {"semantic_signatures": {"L1_left_boundary": "0" * 64}},
                    "operations": [{"op": "update_claim", "node": "L1_left_boundary", "set": {"formal_intent": "Synthetic formal intent."}}],
                }
                plan_path = Path(tmp) / "stale.yaml"
                plan_path.write_text(correctness.yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
                with self.assertRaises(SystemExit) as ctx:
                    correctness._cmd_mutate(correctness.Graph.load(path), argparse.Namespace(plan=str(plan_path), dry_run=False, reject_warnings=False))
                self.assertIn("STALE_MUTATION", str(ctx.exception))
                self.assertEqual(path.read_text(encoding="utf-8"), before)
            finally:
                correctness.GRAPH_PATH = old_path

    def test_open_tooling_blocker_stops_entire_campaign(self):
        doc = copy.deepcopy(self.base)
        blocker = "T1_synthetic_tool_limit"
        doc["id_allocator"]["next_sequence"]["T"] = 2
        doc["refinement"]["tooling_blockers"][blocker] = {
            "status": "open", "issue": "Synthetic issue.", "reason": "Synthetic tooling reason."
        }
        graph = correctness.Graph(doc)
        errors, _ = correctness.validate(graph)
        self.assertEqual(errors, [])
        snapshot = correctness._refinement_snapshot(graph)
        self.assertEqual(snapshot["state"], "TOOLING_BLOCKED")
        runnable = snapshot["runnable"][0]["node"]
        with self.assertRaises(SystemExit) as ctx:
            correctness._cmd_audit_prompt(graph, argparse.Namespace(node=runnable))
        self.assertIn("TOOLING_BLOCKED", str(ctx.exception))

    def test_tooling_blocked_state_rejects_automation_mutations(self):
        doc = copy.deepcopy(self.base)
        doc["refinement"]["tooling_blockers"]["T1_global_stop"] = {
            "status": "open", "issue": "Synthetic issue.", "reason": "Synthetic reason."
        }
        doc["id_allocator"]["next_sequence"]["T"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp, doc)
            before = path.read_text(encoding="utf-8")
            old_path = correctness.GRAPH_PATH
            correctness.GRAPH_PATH = path
            try:
                graph = correctness.Graph.load(path)
                plan = {
                    "mutation_version": 1,
                    "authority": "automation",
                    "preconditions": {"semantic_signatures": {"L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")}},
                    "operations": [{"op": "update_claim", "node": "L1_left_boundary", "set": {"formal_intent": "Synthetic formal intent."}}],
                }
                plan_path = Path(tmp) / "blocked.yaml"
                plan_path.write_text(correctness.yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
                with self.assertRaises(SystemExit) as ctx:
                    correctness._cmd_mutate(graph, argparse.Namespace(plan=str(plan_path), dry_run=False, reject_warnings=False))
                self.assertIn("TOOLING_BLOCKED", str(ctx.exception))
                self.assertEqual(path.read_text(encoding="utf-8"), before)
            finally:
                correctness.GRAPH_PATH = old_path

    def test_tooling_blocker_mutation_allocates_and_resolves_t_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            old_path = correctness.GRAPH_PATH
            correctness.GRAPH_PATH = path
            try:
                graph = correctness.Graph.load(path)
                next_tool = graph.doc["id_allocator"]["next_sequence"]["T"]
                plan = {
                    "mutation_version": 1,
                    "authority": "automation",
                    "preconditions": {"semantic_signatures": {"L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")}},
                    "operations": [{
                        "op": "add_tooling_blocker", "alias": "tool_issue", "slug": "synthetic_tool_limit",
                        "body": {"issue": "Synthetic tool issue.", "reason": "Synthetic reason.", "suggested_change": "Synthetic repair."},
                    }],
                }
                plan_path = Path(tmp) / "add.yaml"
                plan_path.write_text(correctness.yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    correctness._cmd_mutate(graph, argparse.Namespace(plan=str(plan_path), dry_run=False, reject_warnings=False))
                blocker = correctness.yaml.safe_load(output.getvalue())["aliases"]["tool_issue"]
                self.assertEqual(blocker, f"T{next_tool}_synthetic_tool_limit")
                resolve = {
                    "mutation_version": 1,
                    "authority": "human",
                    "operations": [{"op": "resolve_tooling_blocker", "blocker": blocker, "resolution": "Synthetic repair complete."}],
                }
                resolve_path = Path(tmp) / "resolve.yaml"
                resolve_path.write_text(correctness.yaml.safe_dump(resolve, sort_keys=False), encoding="utf-8")
                correctness._cmd_mutate(correctness.Graph.load(path), argparse.Namespace(plan=str(resolve_path), dry_run=False, reject_warnings=False))
                self.assertEqual(correctness.Graph.load(path).doc["refinement"]["tooling_blockers"][blocker]["status"], "resolved")
            finally:
                correctness.GRAPH_PATH = old_path

    def test_mutation_schema_is_machine_readable_and_enforced(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_mutation_schema(None, argparse.Namespace(format="json"))
        exported = json.loads(output.getvalue())
        self.assertEqual(exported["$id"], correctness.MUTATION_PLAN_SCHEMA["$id"])
        graph = self.graph()
        valid = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {"semantic_signatures": {"L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")}},
            "operations": [{"op": "update_claim", "node": "L1_left_boundary", "set": {"formal_intent": "Synthetic formal intent."}}],
        }
        correctness._validate_mutation_plan_schema(valid)
        invalid = copy.deepcopy(valid)
        invalid.pop("preconditions")
        with self.assertRaises(SystemExit) as ctx:
            correctness._validate_mutation_plan_schema(invalid)
        self.assertIn("MUTATION_SCHEMA_REJECTED", str(ctx.exception))

    def test_catalog_command_exposes_typed_definition(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_catalog(
                self.graph(), argparse.Namespace(namespace="mechanisms", key="synthetic_mechanism", format="text")
            )
        text = output.getvalue()
        self.assertIn("synthetic_mechanism", text)
        self.assertIn("automation_reusable=true", text)

    def test_schema_and_workflow_help_do_not_require_graph_load(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_workflow_help(None, argparse.Namespace())
        self.assertIn("mutation-schema", output.getvalue())
        self.assertIn("graph-schema", output.getvalue())
        self.assertIn("formal-schema", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_graph_schema(None, argparse.Namespace(format="yaml"))
        self.assertIn("Proof-Driven Development correctness graph", output.getvalue())
        output = io.StringIO()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_formal_schema(
                None,
                argparse.Namespace(kind="bridge", format="yaml"),
            )
        self.assertIn("symbolic-bridge:v1", output.getvalue())
        with contextlib.redirect_stdout(output):
            correctness._cmd_schema_fields(None, argparse.Namespace(format="text"))
        inventory = output.getvalue()
        self.assertIn("claims.<key:", inventory)
        self.assertIn("depends_on", inventory)
        self.assertIn("canonical field paths", inventory)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_mutation_schema(None, argparse.Namespace(format="yaml"))
        self.assertIn("mutation_version", output.getvalue())

    def test_schema_reference_index_tracks_canonical_fields_and_constraints(self):
        inventory = correctness._canonical_field_inventory()
        lines = [correctness.SCHEMA_INDEX_START]
        lines.extend(
            f"- `{row['path']}` => {row['presence']}; {row['constraint']}"
            for row in inventory
        )
        lines.append(correctness.SCHEMA_INDEX_END)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SCHEMA.md"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual([], correctness._schema_reference_contract_errors(path))

            broken = path.read_text(encoding="utf-8").replace(
                f"- `{inventory[0]['path']}` => {inventory[0]['presence']}; {inventory[0]['constraint']}\n",
                "",
                1,
            )
            path.write_text(broken, encoding="utf-8")
            errors = correctness._schema_reference_contract_errors(path)
            self.assertTrue(any(f"missing field: {inventory[0]['path']}" in error for error in errors))

            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            first = inventory[0]
            original = f"- `{first['path']}` => {first['presence']}; {first['constraint']}"
            path.write_text(
                path.read_text(encoding="utf-8").replace(original, original + " drift", 1),
                encoding="utf-8",
            )
            errors = correctness._schema_reference_contract_errors(path)
            self.assertTrue(any(f"constraint drift for {first['path']}" in error for error in errors))

    def test_undefined_snake_case_term_is_rejected(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["statement"] = "The unknown_protocol_token must hold."
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("undefined snake_case term 'unknown_protocol_token'" in e for e in errors))

    def test_unknown_verifier_kind_is_rejected(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["verification"]["verifiers"][0]["kind"] = "invented_verifier"
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("unknown kind invented_verifier" in e for e in errors))

    def test_canonical_schema_rejects_unknown_claim_field(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["sevverity"] = "critical"
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("Additional properties are not allowed" in e and "sevverity" in e for e in errors))

    def test_canonical_schema_rejects_removed_claim_metadata(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["severity"] = "critical"
        doc["claims"]["L1_left_boundary"]["assurance_required"] = "strong"
        doc["claims"]["L1_left_boundary"]["surfaces"] = ["synthetic_surface"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("Additional properties are not allowed" in e and "severity" in e for e in errors))
        self.assertTrue(any("Additional properties are not allowed" in e and "assurance_required" in e for e in errors))
        self.assertTrue(any("Additional properties are not allowed" in e and "surfaces" in e for e in errors))

    def test_canonical_schema_rejects_unknown_catalog_entry_field(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["terms"]["synthetic_key"]["random_field"] = "x"
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("catalog.terms.synthetic_key" in e and "random_field" in e for e in errors))

    def test_canonical_schema_rejects_invalid_verification_shape(self):
        doc = copy.deepcopy(self.base)
        verifier = doc["claims"]["L1_left_boundary"]["verification"]
        verifier["status"] = "mystery"
        verifier["verifiers"][0]["unexpected"] = True
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("verification" in e and "status" in e and "Additional properties" in e for e in errors))
        self.assertTrue(any("verification.verifiers[0]" in e and "unexpected" in e for e in errors))

    def test_canonical_schema_rejects_stale_state_metadata(self):
        doc = copy.deepcopy(self.base)
        doc["refinement"]["nodes"]["L1_left_boundary"]["signature"] = "0" * 64
        doc["assurance"]["specification_coverage"].update(
            signature="0" * 64,
            auditor_count=2,
            rationale="stale",
        )
        doc["assurance"]["composition"]["G1_primary_goal"].update(
            signature="0" * 64,
            rationale="stale",
        )
        doc["assurance"]["implementation_evidence"]["L1_left_boundary"].update(
            signature="0" * 64,
            implementation_revision="old",
            artifact_refs=["ci://old"],
        )
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("refinement.nodes.L1_left_boundary" in e and "signature" in e for e in errors))
        self.assertTrue(any("assurance.specification_coverage" in e for e in errors))
        self.assertTrue(any("assurance.composition.G1_primary_goal" in e for e in errors))
        self.assertTrue(any("assurance.implementation_evidence.L1_left_boundary" in e for e in errors))

    def test_symbolic_dimensions_must_resolve_to_catalog(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["state"]["synthetic_state"]["symbolic_dimensions"] = ["missing_dimension"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("unknown catalog.symbolic_dimensions entry 'missing_dimension'" in e for e in errors))

    def test_provenance_binding_source_and_bound_symbols_must_be_disjoint(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_provenance"] = {
            "class": "provenance_binding",
            "definition": "Synthetic provenance relation.",
            "excludes": [],
            "automation_reusable": True,
            "source_symbols": ["synthetic_key"],
            "bound_symbols": ["synthetic_key"],
        }
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("source_symbols and bound_symbols must be disjoint" in e for e in errors))

    def test_every_canonical_field_has_description(self):
        missing: list[str] = []

        def walk(value, path=()):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    for name, child in value.get("properties", {}).items():
                        child_path = path + (name,)
                        if not isinstance(child, dict) or not isinstance(child.get("description"), str) or not child["description"].strip():
                            missing.append(".".join(child_path))
                        walk(child, child_path)
                    for pattern, child in value.get("patternProperties", {}).items():
                        walk(child, path + (f"<key:{pattern}>",))
                else:
                    for child in value.values():
                        walk(child, path)
            elif isinstance(value, list):
                for child in value:
                    walk(child, path)

        walk(correctness.CANONICAL_GRAPH_SCHEMA)
        self.assertEqual(missing, [], msg=f"schema fields missing descriptions: {missing}")

    def test_schema_field_inventory_is_unique_described_and_constrained(self):
        inventory = correctness._canonical_field_inventory()
        paths = [row["path"] for row in inventory]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertTrue(inventory)
        self.assertFalse(any(not row["description"].strip() for row in inventory))
        self.assertFalse(any(row["constraint"] == "composite constraint" for row in inventory))

    def test_canonical_schema_closes_every_object_shape(self):
        def walk(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(
                        value.get("additionalProperties"),
                        False,
                        msg=f"open object schema found: {value}",
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(correctness.CANONICAL_GRAPH_SCHEMA)

    def test_tool_intrinsic_policy_fields_are_not_yaml_configuration(self):
        doc = copy.deepcopy(self.base)
        doc["refinement"]["policy"] = {"scheduling": "top_down"}
        doc["composition_rules"] = {"invalidation": "invented"}
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("refinement" in e and "policy" in e and "Additional properties" in e for e in errors))
        self.assertTrue(any("composition_rules" in e and "Additional properties" in e for e in errors))

    def test_derived_scheduler_fields_are_not_yaml_configuration(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["G1_primary_goal"]["status"] = "specified"
        doc["refinement"]["nodes"]["G1_primary_goal"]["task"] = "proof_audit"
        doc["refinement"]["nodes"]["G1_primary_goal"]["priority"] = 300
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("claims.G1_primary_goal" in e and "status" in e and "Additional properties" in e for e in errors))
        self.assertTrue(any("refinement.nodes.G1_primary_goal" in e and "task" in e and "Additional properties" in e for e in errors))
        self.assertTrue(any("refinement.nodes.G1_primary_goal" in e and "priority" in e and "Additional properties" in e for e in errors))

    def test_tool_interpretation_rules_participate_in_signature(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        entry = doc["refinement"]["nodes"]["L2_right_boundary"]
        entry["status"] = "stable"
        entry["signature"] = correctness._node_semantic_signature(graph, "L2_right_boundary")
        original = harness_signatures.AUDIT_INTERPRETATION_RULES
        try:
            harness_signatures.AUDIT_INTERPRETATION_RULES = original + ("Synthetic changed audit semantics.",)
            snapshot = correctness._refinement_snapshot(correctness.Graph(doc))
            self.assertIn("L2_right_boundary", snapshot["stale"])
        finally:
            harness_signatures.AUDIT_INTERPRETATION_RULES = original

    def test_global_scope_change_reopens_stable_proof(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        entry = doc["refinement"]["nodes"]["L1_left_boundary"]
        entry["status"] = "stable"
        entry["signature"] = correctness._node_semantic_signature(graph, "L1_left_boundary")
        doc["scope"]["excludes"].append("newly excluded semantic domain")
        snapshot = correctness._refinement_snapshot(correctness.Graph(doc))
        self.assertIn("L1_left_boundary", snapshot["stale"])

    def test_leaf_cannot_declare_dependencies(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["depends_on"] = ["A1_fixture_environment"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("leaf claims are terminal proof boundaries" in e for e in errors))

    def test_mutation_schema_rejects_unknown_claim_field(self):
        graph = self.graph()
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {
                "semantic_signatures": {
                    "L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")
                }
            },
            "operations": [
                {
                    "op": "update_claim",
                    "node": "L1_left_boundary",
                    "set": {"sevverity": "critical"},
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._validate_mutation_plan_schema(plan)
        self.assertIn("MUTATION_SCHEMA_REJECTED", str(ctx.exception))

    def test_unreferenced_semantic_contract_change_does_not_reopen_proof(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        entry = doc["refinement"]["nodes"]["L1_left_boundary"]
        entry["status"] = "stable"
        entry["signature"] = correctness._node_semantic_signature(graph, "L1_left_boundary")
        doc["catalog"]["semantic_contracts"]["synthetic_safety_contract"]["definition"] = (
            "Changed but still unreferenced synthetic semantic boundary."
        )
        snapshot = correctness._refinement_snapshot(correctness.Graph(doc))
        self.assertNotIn("L1_left_boundary", snapshot["stale"])

    def test_semantic_contract_schema_is_closed_and_typed(self):
        doc = copy.deepcopy(self.base)
        contract = doc["catalog"]["semantic_contracts"]["synthetic_safety_contract"]
        contract["class"] = "whatever_contract"
        contract["free_form_extra"] = "drift"
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("catalog.semantic_contracts.synthetic_safety_contract.class" in e for e in errors))
        self.assertTrue(any("catalog.semantic_contracts.synthetic_safety_contract" in e and "free_form_extra" in e and "Additional properties" in e for e in errors))

    def test_state_catalog_supports_speculative_and_control_classes(self):
        for state_class in ("speculative", "control"):
            doc = copy.deepcopy(self.base)
            doc["catalog"]["state"][f"synthetic_{state_class}_state"] = {
                "class": state_class,
                "description": f"Synthetic {state_class} state.",
            }
            errors, _ = correctness.validate(correctness.Graph(doc))
            self.assertFalse(any(f"synthetic_{state_class}_state" in e for e in errors))

        doc = copy.deepcopy(self.base)
        doc["catalog"]["state"]["bad_state"] = {
            "class": "mystery",
            "description": "Invalid state class.",
        }
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("catalog.state.bad_state.class" in e for e in errors))

    def test_state_catalog_symbolic_dimensions_are_typed(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["state"]["synthetic_state"]["symbolic_dimensions"] = [
            "entity",
            "observation",
        ]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertFalse(any("symbolic_dimensions" in error for error in errors))

        doc["catalog"]["state"]["synthetic_state"]["symbolic_dimensions"] = [
            "document",
            "Bad Dimension",
        ]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("symbolic_dimensions" in error for error in errors))

    def test_state_invariant_contract_requires_typed_scope_and_known_symbols(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_state_invariant"] = {
            "class": "state_invariant",
            "definition": "Synthetic state relation remains true across its observation scope.",
            "excludes": [],
            "automation_reusable": True,
        }
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("state_symbols" in e for e in errors))
        self.assertTrue(any("observation_scope" in e for e in errors))

        doc["catalog"]["semantic_contracts"]["synthetic_state_invariant"]["state_symbols"] = [
            "synthetic_state",
            "missing_state_symbol",
        ]
        doc["catalog"]["semantic_contracts"]["synthetic_state_invariant"]["observation_scope"] = "user_visible_states"
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("unknown semantic symbol 'missing_state_symbol'" in e for e in errors))

    def test_semantic_contract_specialized_fields_are_class_specific(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_safety_contract"]["state_symbols"] = ["synthetic_state"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("synthetic_safety_contract" in error for error in errors))

        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_state_invariant"] = {
            "class": "state_invariant",
            "definition": "Synthetic state invariant.",
            "state_symbols": ["synthetic_state"],
            "observation_scope": "all_reachable_states",
            "source_symbols": ["synthetic_state"],
            "excludes": [],
            "automation_reusable": True,
        }
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("synthetic_state_invariant" in error for error in errors))

    def test_provenance_binding_contract_requires_typed_source_and_bound_symbols(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_binding"] = {
            "class": "provenance_binding",
            "definition": "A synthetic bound value is produced from one required source.",
            "excludes": [],
            "automation_reusable": True,
        }
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("source_symbols" in e for e in errors))
        self.assertTrue(any("bound_symbols" in e for e in errors))

        binding = doc["catalog"]["semantic_contracts"]["synthetic_binding"]
        binding["source_symbols"] = ["synthetic_state"]
        binding["bound_symbols"] = ["synthetic_key", "missing_bound_symbol"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("unknown semantic symbol 'missing_bound_symbol'" in e for e in errors))

    def test_typed_semantic_contracts_render_specialized_audit_semantics(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_state_invariant"] = {
            "class": "state_invariant",
            "definition": "synthetic_state remains coherent.",
            "state_symbols": ["synthetic_state"],
            "observation_scope": "user_visible_states",
            "excludes": [],
            "automation_reusable": True,
        }
        claim = doc["claims"]["L1_left_boundary"]
        claim["semantic_contracts"] = ["synthetic_state_invariant"]
        claim["formal_intent"] = "synthetic_state_invariant"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            correctness._emit_slice_markdown(
                correctness.Graph(doc),
                "L1_left_boundary",
                set(),
                prompt=True,
                include_verification_task=False,
            )
        text = out.getvalue()
        self.assertIn("Observation scope: user_visible_states", text)
        self.assertIn("treat this as an inductive invariant", text)

        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_binding"] = {
            "class": "provenance_binding",
            "definition": "synthetic_key is bound to synthetic_state.",
            "source_symbols": ["synthetic_state"],
            "bound_symbols": ["synthetic_key"],
            "excludes": [],
            "automation_reusable": True,
        }
        claim = doc["claims"]["L1_left_boundary"]
        claim["semantic_contracts"] = ["synthetic_binding"]
        claim["formal_intent"] = "synthetic_binding"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            correctness._emit_slice_markdown(
                correctness.Graph(doc),
                "L1_left_boundary",
                set(),
                prompt=True,
                include_verification_task=False,
            )
        text = out.getvalue()
        self.assertIn("Source symbols: synthetic_state", text)
        self.assertIn("Bound symbols: synthetic_key", text)
        self.assertIn("Correct downstream processing", text)

    def test_provenance_contract_closes_typed_vocabulary_into_audit_slice(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["semantic_contracts"]["synthetic_binding"] = {
            "class": "provenance_binding",
            "definition": "synthetic_key is derived from synthetic_state through synthetic_mechanism.",
            "source_symbols": ["synthetic_state"],
            "bound_symbols": ["synthetic_key"],
            "excludes": [],
            "automation_reusable": True,
        }
        claim = doc["claims"]["L1_left_boundary"]
        claim["semantic_contracts"] = ["synthetic_binding"]
        claim["formal_intent"] = "synthetic_binding"
        # The claim deliberately does not name the contract's source/bound symbols or mechanism.
        claim["statement"] = "The synthetic_binding semantic contract holds for this boundary."
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            correctness._emit_slice_markdown(
                correctness.Graph(doc),
                "L1_left_boundary",
                set(),
                prompt=True,
                include_verification_task=False,
            )
        text = out.getvalue()
        self.assertIn("**synthetic_state** [authoritative]", text)
        self.assertIn("**synthetic_key**", text)
        self.assertIn("**synthetic_mechanism**", text)
        self.assertNotIn("**new_term**", text)

    def test_unknown_semantic_contract_reference_is_rejected(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["semantic_contracts"] = ["does_not_exist"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("unknown semantic contract does_not_exist" in e for e in errors))

    def test_automation_may_use_reusable_contract_only_on_new_claim(self):
        graph = self.graph()
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {
                "semantic_signatures": {
                    "L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")
                }
            },
            "operations": [
                {
                    "op": "add_claim",
                    "kind": "leaf",
                    "slug": "reusable_contract_boundary",
                    "body": {
                        **_leaf("A reusable synthetic contract boundary holds."),
                        "semantic_contracts": ["synthetic_safety_contract"],
                    },
                }
            ],
        }
        candidate = copy.deepcopy(self.base)
        aliases, stable_later = correctness._apply_mutation_plan(candidate, plan)
        new_ids = set(candidate["claims"]) - set(self.base["claims"])
        self.assertEqual(len(new_ids), 1)
        new_id = next(iter(new_ids))
        self.assertEqual(candidate["claims"][new_id]["semantic_contracts"], ["synthetic_safety_contract"])
        self.assertEqual(aliases, {})
        self.assertEqual(stable_later, [])

    def test_semantic_contract_reference_must_be_explicit_in_claim_text(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["semantic_contracts"] = ["synthetic_safety_contract"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(
            any(
                "L1_left_boundary: semantic contract synthetic_safety_contract must appear as an exact identifier in statement or formal_intent"
                in error
                for error in errors
            )
        )

    def test_semantic_contract_reference_must_appear_in_referenced_source_section(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["statement"] = (
            "The left boundary satisfies synthetic_safety_contract."
        )
        doc["claims"]["L1_left_boundary"]["semantic_contracts"] = ["synthetic_safety_contract"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design").mkdir()
            (root / "design" / "synthetic.md").write_text(
                "# Synthetic\n\n"
                "## 1. Synthetic section\n\n"
                "The referenced section intentionally does not name the contract.\n\n"
                "## 2. Other section\n\n"
                "Unrelated text names `synthetic_safety_contract`.\n",
                encoding="utf-8",
            )
            errors, _ = correctness.validate(correctness.Graph(doc, repository_root=root))
        self.assertTrue(
            any(
                "L1_left_boundary: semantic contract synthetic_safety_contract must appear as an exact identifier in at least one source_ref article section"
                in error
                for error in errors
            )
        )

    def test_semantic_contract_source_reference_requires_exact_identifier(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["statement"] = (
            "The left boundary satisfies synthetic_safety_contract."
        )
        doc["claims"]["L1_left_boundary"]["semantic_contracts"] = ["synthetic_safety_contract"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design").mkdir()
            article = root / "design" / "synthetic.md"
            article.write_text(
                "# Synthetic\n\n"
                "## 1. Synthetic section\n\n"
                "This mentions `synthetic_safety_contract_extra`, not the contract itself.\n",
                encoding="utf-8",
            )
            errors, _ = correctness.validate(correctness.Graph(doc, repository_root=root))
            self.assertTrue(any("at least one source_ref article section" in error for error in errors))
            article.write_text(
                "# Synthetic\n\n"
                "## 1. Synthetic section\n\n"
                "This explicitly references `synthetic_safety_contract`.\n",
                encoding="utf-8",
            )
            errors, _ = correctness.validate(correctness.Graph(doc, repository_root=root))
        self.assertFalse(any("semantic contract synthetic_safety_contract must appear" in error for error in errors))

    def test_catalog_source_heading_must_exist_exactly_once(self):
        doc = copy.deepcopy(self.base)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design").mkdir()
            article = root / "design" / "synthetic.md"
            article.write_text(
                "# Synthetic\n\n## 1. Renamed section\n\nBody.\n",
                encoding="utf-8",
            )
            errors, _ = correctness.validate(correctness.Graph(doc, repository_root=root))
            self.assertTrue(any("catalog.sources.synthetic_source: source article missing H2 heading 'Synthetic section'" in error for error in errors))
            article.write_text(
                "# Synthetic\n\n## 1. Synthetic section\n\nA.\n\n## 2. Synthetic section\n\nB.\n",
                encoding="utf-8",
            )
            errors, _ = correctness.validate(correctness.Graph(doc, repository_root=root))
        self.assertTrue(any("catalog.sources.synthetic_source: source article H2 heading 'Synthetic section' is ambiguous" in error for error in errors))

    def test_catalog_source_heading_ignores_numeric_display_prefix(self):
        doc = copy.deepcopy(self.base)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "design").mkdir()
            (root / "design" / "synthetic.md").write_text(
                "# Synthetic\n\n## 42. Synthetic section\n\nBody.\n",
                encoding="utf-8",
            )
            errors, _ = correctness.validate(correctness.Graph(doc, repository_root=root))
        self.assertFalse(any("catalog.sources.synthetic_source" in error for error in errors))

    def test_positional_catalog_source_id_is_rejected(self):
        doc = copy.deepcopy(self.base)
        doc["catalog"]["sources"]["section_1"] = doc["catalog"]["sources"].pop("synthetic_source")
        for space in ("assumptions", "claims"):
            for node in doc[space].values():
                if node.get("source_refs") == ["synthetic_source"]:
                    node["source_refs"] = ["section_1"]
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("catalog.sources.section_1" in error for error in errors))

    def test_semantic_contract_definition_change_reopens_referencing_proof(self):
        doc = copy.deepcopy(self.base)
        claim = doc["claims"]["L1_left_boundary"]
        claim["semantic_contracts"] = ["synthetic_safety_contract"]
        graph = correctness.Graph(doc)
        entry = doc["refinement"]["nodes"]["L1_left_boundary"]
        entry["status"] = "stable"
        entry["signature"] = correctness._node_semantic_signature(graph, "L1_left_boundary")
        doc["catalog"]["semantic_contracts"]["synthetic_safety_contract"]["definition"] = (
            "Changed synthetic semantic boundary."
        )
        snapshot = correctness._refinement_snapshot(correctness.Graph(doc))
        self.assertIn("L1_left_boundary", snapshot["stale"])

    def test_semantic_contract_is_injected_into_audit_prompt(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["semantic_contracts"] = ["synthetic_safety_contract"]
        graph = correctness.Graph(doc)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._cmd_audit_prompt(graph, argparse.Namespace(node="L1_left_boundary"))
        text = output.getvalue()
        self.assertIn("### Semantic contracts", text)
        self.assertIn("synthetic_safety_contract", text)
        self.assertIn("stronger synthetic guarantees", text)
        self.assertIn("Mandatory semantic-contract realization", text)
        self.assertIn("reusable property specification, not a theorem or proof premise", text)
        self.assertIn("target itself asserts a full realization", text)
        self.assertIn("REALIZES", text)
        self.assertIn("WEAKER", text)
        self.assertIn("contract_realizations:", text)

    def test_audit_prompt_flags_repeated_direct_contract_carrier(self):
        doc = copy.deepcopy(self.base)
        parent = doc["claims"]["G2_secondary_goal"]
        child = doc["claims"]["L3_independent_boundary"]
        parent["semantic_contracts"] = ["synthetic_safety_contract"]
        parent["formal_intent"] = "synthetic_safety_contract"
        child["semantic_contracts"] = ["synthetic_safety_contract"]
        child["formal_intent"] = "synthetic_safety_contract"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            correctness._emit_audit_prompt(correctness.Graph(doc), "G2_secondary_goal")
        text = output.getvalue()
        self.assertIn("Repeated semantic-contract carrier diagnostic", text)
        self.assertIn("synthetic_safety_contract", text)
        self.assertIn("L3_independent_boundary", text)
        self.assertIn("must not be propagated", text)

    def test_contract_realization_semantics_only_reopens_contract_bearing_proofs(self):
        plain = copy.deepcopy(self.base)
        plain_graph = correctness.Graph(plain)
        plain_before = correctness._node_semantic_signature(plain_graph, "L1_left_boundary")

        bound = copy.deepcopy(self.base)
        bound_claim = bound["claims"]["L1_left_boundary"]
        bound_claim["semantic_contracts"] = ["synthetic_safety_contract"]
        bound_claim["formal_intent"] = "synthetic_safety_contract"
        bound_graph = correctness.Graph(bound)
        bound_before = correctness._node_semantic_signature(bound_graph, "L1_left_boundary")

        original = harness_signatures.CONTRACT_REALIZATION_SEMANTICS_VERSION
        try:
            harness_signatures.CONTRACT_REALIZATION_SEMANTICS_VERSION = original + "-changed"
            self.assertEqual(
                plain_before,
                correctness._node_semantic_signature(correctness.Graph(plain), "L1_left_boundary"),
            )
            self.assertNotEqual(
                bound_before,
                correctness._node_semantic_signature(correctness.Graph(bound), "L1_left_boundary"),
            )
        finally:
            harness_signatures.CONTRACT_REALIZATION_SEMANTICS_VERSION = original

    def test_stable_mutation_requires_explicit_contract_realization_set(self):
        doc = copy.deepcopy(self.base)
        claim = doc["claims"]["L1_left_boundary"]
        claim["semantic_contracts"] = ["synthetic_safety_contract"]
        claim["formal_intent"] = "synthetic_safety_contract"

        missing = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [{
                "op": "set_refinement",
                "node": "L1_left_boundary",
                "status": "stable",
            }],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._validate_mutation_plan_schema(missing)
        self.assertIn("MUTATION_SCHEMA_REJECTED", str(ctx.exception))

        incomplete = copy.deepcopy(missing)
        incomplete["operations"][0]["contract_realizations"] = []
        with self.assertRaises(SystemExit) as ctx:
            correctness._apply_mutation_plan(copy.deepcopy(doc), incomplete)
        self.assertIn("CONTRACT_REALIZATION_REQUIRED", str(ctx.exception))

        complete = copy.deepcopy(missing)
        complete["operations"][0]["contract_realizations"] = ["synthetic_safety_contract"]
        candidate = copy.deepcopy(doc)
        correctness._apply_mutation_plan(candidate, complete)
        snapshot = correctness._refinement_snapshot(correctness.Graph(candidate))
        self.assertIn("L1_left_boundary", snapshot["stable"])

    def test_stable_mutation_requires_explicit_empty_realization_set_without_contracts(self):
        missing = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [{
                "op": "set_refinement",
                "node": "L1_left_boundary",
                "status": "stable",
            }],
        }
        with self.assertRaises(SystemExit):
            correctness._validate_mutation_plan_schema(missing)
        complete = copy.deepcopy(missing)
        complete["operations"][0]["contract_realizations"] = []
        candidate = copy.deepcopy(self.base)
        correctness._apply_mutation_plan(candidate, complete)
        self.assertIn("L1_left_boundary", correctness._refinement_snapshot(correctness.Graph(candidate))["stable"])

    def test_automation_cannot_change_existing_claim_semantic_contract(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["semantic_contracts"] = ["synthetic_safety_contract"]
        graph = correctness.Graph(doc)
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {
                "semantic_signatures": {
                    "L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")
                }
            },
            "operations": [
                {
                    "op": "update_claim",
                    "node": "L1_left_boundary",
                    "unset": ["semantic_contracts"],
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._apply_mutation_plan(doc, plan)
        self.assertIn("may not change semantic-contract associations", str(ctx.exception))

    def test_automation_cannot_change_semantic_contract_catalog_definition(self):
        graph = self.graph()
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {
                "semantic_signatures": {
                    "L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")
                }
            },
            "operations": [
                {
                    "op": "set_value",
                    "path": ["catalog", "semantic_contracts", "synthetic_safety_contract", "definition"],
                    "value": "Automation must not rewrite the normative contract definition.",
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._apply_mutation_plan(copy.deepcopy(self.base), plan)
        self.assertIn("set_value is human-only", str(ctx.exception))

    def test_automation_cannot_change_contract_association_during_reclassification(self):
        graph = self.graph()
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {
                "semantic_signatures": {
                    "L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")
                }
            },
            "operations": [
                {
                    "op": "reclassify_claim",
                    "node": "L1_left_boundary",
                    "new_kind": "derived",
                    "slug": "reclassified_contract_boundary",
                    "set": {
                        "semantic_contracts": ["synthetic_safety_contract"],
                        "depends_on": ["L2_right_boundary"],
                    },
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._apply_mutation_plan(copy.deepcopy(self.base), plan)
        self.assertIn("may not change semantic-contract associations while reclassifying", str(ctx.exception))

    def test_automation_cannot_introduce_human_only_semantic_contract(self):
        graph = self.graph()
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {
                "semantic_signatures": {
                    "L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")
                }
            },
            "operations": [
                {
                    "op": "add_claim",
                    "kind": "leaf",
                    "slug": "new_contract_boundary",
                    "body": {
                        **_leaf("A new synthetic contract boundary holds."),
                        "semantic_contracts": ["human_only_contract"],
                    },
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._apply_mutation_plan(copy.deepcopy(self.base), plan)
        self.assertIn("ARCHITECTURE_SYNTHESIS_REQUIRED", str(ctx.exception))

    def test_catalog_definition_change_reopens_referencing_proof(self):
        doc = copy.deepcopy(self.base)
        doc["claims"]["L1_left_boundary"]["statement"] = "The synthetic_key boundary holds."
        graph = correctness.Graph(doc)
        entry = doc["refinement"]["nodes"]["L1_left_boundary"]
        entry["status"] = "stable"
        entry["signature"] = correctness._node_semantic_signature(graph, "L1_left_boundary")
        doc["catalog"]["terms"]["synthetic_key"]["description"] = "Changed semantic meaning."
        snapshot = correctness._refinement_snapshot(correctness.Graph(doc))
        self.assertIn("L1_left_boundary", snapshot["stale"])

    def test_automation_cannot_introduce_provisional_mechanism(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {
                "semantic_signatures": {
                    "L1_left_boundary": correctness._node_semantic_signature(graph, "L1_left_boundary")
                }
            },
            "operations": [
                {
                    "op": "add_claim",
                    "kind": "leaf",
                    "slug": "provisional_boundary",
                    "body": {
                        **_leaf("Synthetic provisional boundary."),
                        "mechanisms": ["provisional_mechanism"],
                    },
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._apply_mutation_plan(doc, plan)
        self.assertIn("ARCHITECTURE_SYNTHESIS_REQUIRED", str(ctx.exception))

    def test_stable_refinement_does_not_persist_rationale(self):
        doc = copy.deepcopy(self.base)
        entry = doc["refinement"]["nodes"]["L1_left_boundary"]
        entry["status"] = "stable"
        entry["signature"] = "0" * 64
        entry["rationale"] = "Historical audit narrative that must not remain canonical."
        errors, warnings = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("refinement.nodes.L1_left_boundary" in error for error in errors))
        self.assertEqual(warnings, [])

        plan = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [
                {
                    "op": "set_refinement",
                    "node": "L1_left_boundary",
                    "status": "stable",
                    "rationale": "Should be rejected rather than persisted.",
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._apply_mutation_plan(copy.deepcopy(self.base), plan)
        self.assertIn("stable refinement state may not persist rationale", str(ctx.exception))

    def test_workflow_help_contracts(self):
        text = correctness.WORKFLOW_HELP
        self.assertIn("Mandatory clean-context isolation", text)
        self.assertIn("spawn_chatgpt_subagents", text)
        self.assertIn("tool discovery", text)
        self.assertIn("execution/capacity", text)
        self.assertIn("Orchestrator as skeptical judge/compiler", text)
        self.assertIn("MUST NOT edit `correctness.yaml` directly", text)
        self.assertIn("mutation-schema", text)
        self.assertIn("TOOLING_BLOCKED", text)
        self.assertIn("refinement-status --format compact-yaml", text)
        self.assertIn("assurance-status --format compact-yaml", text)
        self.assertIn(".venv/bin/python3 correctness.py", text)
        self.assertIn("NON-NORMATIVE HARNESS FEEDBACK", text)
        self.assertIn("HARNESS FEEDBACK: none", text)
        self.assertIn("`stable` is a refinement-maturity state only", text)
        self.assertIn("contract-realization obligation", text)
        self.assertIn("contract_realizations", text)
        self.assertIn("load these schemas once per tooling revision/session", text)
        self.assertIn("`--dry-run` is an optional", text)
        self.assertIn("ordinary DAG/workflow-state mutation", text)
        self.assertIn("Typed architecture catalog and synthesis boundary", text)
        self.assertIn("automation_reusable", text)
        self.assertIn("single-verifier sufficiency", text)
        self.assertIn("Assurance certification campaigns", text)
        self.assertIn("coverage-audit-prompt", text)
        self.assertIn("composition-status", text)
        self.assertIn("composition-audit-prompt", text)
        for signal_id in correctness.LEAF_STOPPING_SIGNALS:
            self.assertIn(f"`{signal_id}`", text)
        self.assertIn("Specification wording repair", text)
        self.assertIn("Decomposition artifact", text)
        self.assertNotIn("Only then may you read the full", text)

    def test_top_level_help_points_agents_to_workflow_help(self):
        help_text = correctness.build_parser().format_help()
        self.assertIn("workflow-help", help_text)
        self.assertIn("catalog", help_text)
        self.assertIn("graph-schema", help_text)
        self.assertIn("schema-fields", help_text)
        self.assertIn("mutation-schema", help_text)
        self.assertIn("assurance-status", help_text)
        self.assertIn("audit-prompt", help_text)
        self.assertIn("coverage-audit-prompt", help_text)
        self.assertIn("composition-status", help_text)
        self.assertIn("composition-audit-prompt", help_text)

    def test_duplicate_yaml_key_is_rejected(self):
        content = "claims:\n  A: {statement: one}\n  A: {statement: two}\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.yaml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(SystemExit):
                correctness.Graph.load(path)

    def test_assurance_snapshot_keeps_refinement_and_assurance_orthogonal(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        for node_id, entry in doc["refinement"]["nodes"].items():
            entry["status"] = "stable"
            entry["signature"] = correctness._node_semantic_signature(graph, node_id)
        refinement = correctness._refinement_snapshot(correctness.Graph(doc))
        assurance = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(refinement["state"], "COMPLETE")
        self.assertEqual(assurance["specification_coverage"]["effective"], "unaudited")
        self.assertEqual(assurance["counts"]["composition"], {"unaudited": 3})
        self.assertEqual(assurance["counts"]["implementation_evidence"], {"planned": 3})

    def test_composition_assurance_becomes_stale_after_dependency_semantics_change(self):
        doc = copy.deepcopy(self.base)
        plan = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [
                {
                    "op": "set_composition_assurance",
                    "node": "C1_composite_claim",
                    "status": "multi_agent_audited",
                    "auditor_count": 3,
                    "rationale": "Independent adversarial auditors found no dependency counterexample.",
                }
            ],
        }
        correctness._apply_mutation_plan(doc, plan)
        snapshot = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(snapshot["composition"]["C1_composite_claim"]["effective"], "multi_agent_audited")
        doc["claims"]["L1_left_boundary"]["statement"] = "Changed left boundary semantics."
        snapshot = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(snapshot["composition"]["C1_composite_claim"]["effective"], "stale")

    def test_leaf_evidence_becomes_stale_after_leaf_semantics_change(self):
        doc = copy.deepcopy(self.base)
        plan = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [
                {
                    "op": "set_evidence_assurance",
                    "node": "L1_left_boundary",
                    "status": "passing",
                    "implementation_revision": "deadbeef",
                    "artifact_refs": ["ci://synthetic/l1/42"],
                }
            ],
        }
        correctness._apply_mutation_plan(doc, plan)
        snapshot = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(snapshot["implementation_evidence"]["L1_left_boundary"]["effective"], "passing")
        doc["claims"]["L1_left_boundary"]["statement"] = "Changed left boundary semantics."
        snapshot = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(snapshot["implementation_evidence"]["L1_left_boundary"]["effective"], "stale")

    def test_coverage_audit_semantics_version_only_invalidates_model_coverage_signature(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        node_sig_before = correctness._node_semantic_signature(graph, "L1_left_boundary")
        model_sig_before = correctness._model_semantic_signature(graph)
        old_version = harness_signatures.COVERAGE_AUDIT_SEMANTICS_VERSION
        try:
            harness_signatures.COVERAGE_AUDIT_SEMANTICS_VERSION = old_version + "-changed"
            self.assertEqual(
                correctness._node_semantic_signature(graph, "L1_left_boundary"),
                node_sig_before,
            )
            self.assertNotEqual(correctness._model_semantic_signature(graph), model_sig_before)
        finally:
            harness_signatures.COVERAGE_AUDIT_SEMANTICS_VERSION = old_version

    def test_specification_coverage_becomes_stale_when_design_article_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            graph = correctness.Graph.load(path)
            doc = graph.doc
            doc["assurance"]["specification_coverage"] = {
                "status": "closed",
                "signature": correctness._model_semantic_signature(graph),
                "auditor_count": 4,
                "rationale": "Synthetic root-coverage audit closed.",
            }
            graph = correctness.Graph(doc, repository_root=Path(tmp))
            self.assertEqual(
                correctness._assurance_snapshot(graph)["specification_coverage"]["effective"],
                "closed",
            )
            article = Path(tmp) / "design" / "synthetic.md"
            article.write_text(article.read_text(encoding="utf-8") + "\nChanged design semantics.\n", encoding="utf-8")
            self.assertEqual(
                correctness._assurance_snapshot(graph)["specification_coverage"]["effective"],
                "stale",
            )

    def test_assurance_schema_rejects_wrong_role_and_incomplete_passing_evidence(self):
        doc = copy.deepcopy(self.base)
        doc["assurance"]["composition"]["L1_left_boundary"] = {"status": "unaudited"}
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("assurance.composition references leaf/unknown claim L1_left_boundary" in e for e in errors))

        doc = copy.deepcopy(self.base)
        doc["assurance"]["implementation_evidence"]["L1_left_boundary"] = {"status": "passing"}
        errors, _ = correctness.validate(correctness.Graph(doc))
        self.assertTrue(any("assurance.implementation_evidence.L1_left_boundary" in e for e in errors))

    def test_add_claim_initializes_matching_assurance_namespace(self):
        doc = copy.deepcopy(self.base)
        plan = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [
                {
                    "op": "add_claim",
                    "kind": "leaf",
                    "slug": "new_evidence_boundary",
                    "body": _leaf("A newly added evidence boundary holds."),
                    "alias": "new_leaf",
                }
            ],
        }
        aliases, _ = correctness._apply_mutation_plan(doc, plan)
        node_id = aliases["new_leaf"]
        self.assertEqual(doc["assurance"]["implementation_evidence"][node_id], {"status": "planned"})
        self.assertNotIn(node_id, doc["assurance"]["composition"])

    def test_model_signature_precondition_supports_global_assurance_mutation(self):
        doc = copy.deepcopy(self.base)
        graph = correctness.Graph(doc)
        signature = correctness._model_semantic_signature(graph)
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {"model_signature": signature},
            "operations": [
                {
                    "op": "set_specification_coverage",
                    "status": "closed",
                    "auditor_count": 4,
                    "rationale": "Independent synthetic root-coverage audit found no gap.",
                }
            ],
        }
        correctness._validate_mutation_plan_schema(plan)
        correctness._check_mutation_preconditions(graph, plan)
        correctness._apply_mutation_plan(doc, plan)
        snapshot = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(snapshot["specification_coverage"]["effective"], "closed")

    def test_stale_model_signature_precondition_is_rejected(self):
        graph = self.graph()
        plan = {
            "mutation_version": 1,
            "authority": "automation",
            "preconditions": {"model_signature": "0" * 64},
            "operations": [
                {
                    "op": "set_specification_coverage",
                    "status": "closed",
                    "auditor_count": 2,
                    "rationale": "Synthetic audit result.",
                }
            ],
        }
        with self.assertRaises(SystemExit) as ctx:
            correctness._check_mutation_preconditions(graph, plan)
        self.assertIn("model semantic signature changed", str(ctx.exception))

    def test_specification_assurance_mutation_uses_repository_design_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            graph = correctness.Graph.load(path)
            plan = {
                "mutation_version": 1,
                "authority": "automation",
                "preconditions": {"model_signature": correctness._model_semantic_signature(graph)},
                "operations": [
                    {
                        "op": "set_specification_coverage",
                        "status": "closed",
                        "auditor_count": 3,
                        "rationale": "Repository-context coverage audit closed.",
                    }
                ],
            }
            doc = copy.deepcopy(graph.doc)
            correctness._apply_mutation_plan(doc, plan, repository_root=Path(tmp))
            snapshot = correctness._assurance_snapshot(
                correctness.Graph(doc, repository_root=Path(tmp))
            )
            self.assertEqual(snapshot["specification_coverage"]["effective"], "closed")

    def test_parent_composition_assurance_ignores_grandchild_proof_changes(self):
        doc = copy.deepcopy(self.base)
        plan = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [
                {
                    "op": "set_composition_assurance",
                    "node": "G1_primary_goal",
                    "status": "multi_agent_audited",
                    "auditor_count": 2,
                    "rationale": "Direct dependency implication was independently audited.",
                }
            ],
        }
        correctness._apply_mutation_plan(doc, plan)
        before = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(before["composition"]["G1_primary_goal"]["effective"], "multi_agent_audited")
        doc["claims"]["L1_left_boundary"]["statement"] = "Changed grandchild leaf semantics."
        after = correctness._assurance_snapshot(correctness.Graph(doc))
        self.assertEqual(after["composition"]["G1_primary_goal"]["effective"], "multi_agent_audited")
        self.assertNotEqual(
            correctness._node_semantic_signature(correctness.Graph(self.base), "G1_primary_goal"),
            correctness._node_semantic_signature(correctness.Graph(doc), "G1_primary_goal"),
        )


    def _freeze_refinement_for_certification(self, doc):
        graph = correctness.Graph(doc)
        for node_id, entry in doc["refinement"]["nodes"].items():
            entry["status"] = "stable"
            entry["blocked_by"] = []
            entry["signature"] = correctness._node_semantic_signature(graph, node_id)
        return correctness.Graph(doc)

    def test_composition_status_exposes_only_frozen_unaudited_nonleaf_nodes(self):
        doc = copy.deepcopy(self.base)
        graph = self._freeze_refinement_for_certification(doc)
        snapshot = correctness._composition_campaign_snapshot(graph)
        self.assertEqual(snapshot["state"], "CONTINUE")
        self.assertEqual(snapshot["refinement_state"], "COMPLETE")
        nodes = {item["node"]: item for item in snapshot["runnable"]}
        self.assertEqual(set(nodes), {"G1_primary_goal", "G2_secondary_goal", "C1_composite_claim"})
        self.assertEqual(nodes["C1_composite_claim"]["recommended_auditors"], 2)
        self.assertEqual(nodes["C1_composite_claim"]["suggested_focuses"], ["execution", "quantifier"])
        self.assertNotIn("severity", nodes["C1_composite_claim"])
        self.assertNotIn("assurance_required", nodes["C1_composite_claim"])
        self.assertEqual(nodes["G1_primary_goal"]["recommended_auditors"], 3)
        self.assertEqual(nodes["G1_primary_goal"]["suggested_focuses"], ["execution", "quantifier", "premise"])
        self.assertEqual(snapshot["target_assurance"], "multi_agent_audited_or_machine_checked")
        self.assertNotIn("L1_left_boundary", nodes)

    def test_single_agent_composition_remains_on_certification_frontier(self):
        doc = copy.deepcopy(self.base)
        self._freeze_refinement_for_certification(doc)
        plan = {
            "mutation_version": 1,
            "authority": "human",
            "operations": [
                {
                    "op": "set_composition_assurance",
                    "node": "C1_composite_claim",
                    "status": "single_agent_audited",
                    "auditor_count": 1,
                    "rationale": "One clean synthetic auditor found no counterexample.",
                }
            ],
        }
        correctness._apply_mutation_plan(doc, plan)
        snapshot = correctness._composition_campaign_snapshot(correctness.Graph(doc))
        item = next(x for x in snapshot["runnable"] if x["node"] == "C1_composite_claim")
        self.assertEqual(item["effective"], "single_agent_audited")

    def test_composition_status_waits_for_refinement_complete(self):
        snapshot = correctness._composition_campaign_snapshot(self.graph())
        self.assertEqual(snapshot["state"], "REFINEMENT_INCOMPLETE")
        self.assertEqual(snapshot["runnable"], [])

    def test_composition_audit_prompt_contains_only_direct_premises(self):
        doc = copy.deepcopy(self.base)
        graph = self._freeze_refinement_for_certification(doc)
        args = argparse.Namespace(node="G1_primary_goal", focus="quantifier")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            correctness._cmd_composition_audit_prompt(graph, args)
        text = out.getvalue()
        self.assertIn("Direct premises — assume all are true", text)
        self.assertIn("C1_composite_claim", text)
        self.assertIn("L3_independent_boundary", text)
        self.assertIn("A1_fixture_environment", text)
        self.assertNotIn("L1_left_boundary", text)
        self.assertNotIn("L2_right_boundary", text)
        self.assertIn("quantifier/boundary auditor", text)
        self.assertIn("Composition signature", text)

    def test_composition_audit_prompt_refuses_unfrozen_refinement(self):
        args = argparse.Namespace(node="G1_primary_goal", focus="general")
        with self.assertRaises(SystemExit) as ctx:
            correctness._cmd_composition_audit_prompt(self.graph(), args)
        self.assertIn("requires a frozen refinement graph", str(ctx.exception))

    def test_composition_focuses_generate_distinct_attack_contracts(self):
        doc = copy.deepcopy(self.base)
        graph = self._freeze_refinement_for_certification(doc)
        rendered = {}
        for focus in correctness.COMPOSITION_AUDIT_FOCUS:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                correctness._emit_composition_audit_prompt(graph, "C1_composite_claim", focus)
            rendered[focus] = out.getvalue()
        self.assertEqual(len(set(rendered.values())), len(correctness.COMPOSITION_AUDIT_FOCUS))
        self.assertIn("execution counterexample generator", rendered["execution"])
        self.assertIn("hidden-premise auditor", rendered["premise"])

    def test_coverage_audit_prompt_binds_current_model_signature_and_roots(self):
        doc = copy.deepcopy(self.base)
        graph = self._freeze_refinement_for_certification(doc)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            correctness._cmd_coverage_audit_prompt(graph, argparse.Namespace())
        text = out.getvalue()
        self.assertIn(correctness._model_semantic_signature(graph), text)
        self.assertIn("G1_primary_goal", text)
        self.assertIn("G2_secondary_goal", text)
        self.assertIn("all-roots-true execution generator", text)
        self.assertIn("FINAL VERDICT: CLOSED", text)
        self.assertIn("semantic alignment", text)
        self.assertIn(".venv/bin/python3 correctness.py validate", text)

    def test_repository_validation_warns_on_unreferenced_semantic_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_fixture(tmp)
            graph = correctness.Graph.load(path)
            errors, warnings = correctness.validate(graph)
        self.assertEqual(errors, [])
        self.assertTrue(
            any("synthetic_safety_contract is not referenced by any claim" in warning for warning in warnings)
        )

    def test_coverage_audit_prompt_requires_semantic_closure_and_clean_assurance_bootstrap(self):
        doc = copy.deepcopy(self.base)
        graph = self._freeze_refinement_for_certification(doc)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            correctness._cmd_coverage_audit_prompt(graph, argparse.Namespace())
        text = out.getvalue()
        self.assertIn("Mandatory semantic-closure pass", text)
        self.assertIn("Inductive state-invariant closure", text)
        self.assertIn("Semantic provenance/binding closure", text)
        self.assertIn("composite identities or semantic tuples", text)
        self.assertIn("tuple closure", text)
        self.assertIn("spawn_chatgpt_subagents", text)
        self.assertIn("tool discovery", text)
        self.assertIn("FINAL VERDICT: EXECUTION INCOMPLETE", text)
        self.assertIn("--omit-specification-coverage", text)

    def test_assurance_status_can_omit_prior_specification_coverage(self):
        doc = copy.deepcopy(self.base)
        graph = self._freeze_refinement_for_certification(doc)
        doc = graph.doc
        doc["assurance"]["specification_coverage"] = {
            "status": "gap_found",
            "signature": "0" * 64,
            "auditor_count": 4,
            "rationale": "Prior finding that must not contaminate a fresh audit.",
        }
        graph = correctness.Graph(doc)
        args = argparse.Namespace(format="compact-yaml", omit_specification_coverage=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            correctness._cmd_assurance_status(graph, args)
        text = out.getvalue()
        self.assertIn("model_signature:", text)
        self.assertNotIn("specification_coverage:", text)
        self.assertNotIn("Prior finding", text)

    def test_coverage_audit_prompt_requires_refinement_complete(self):
        with self.assertRaises(SystemExit) as ctx:
            correctness._cmd_coverage_audit_prompt(self.graph(), argparse.Namespace())
        self.assertIn("requires refinement COMPLETE", str(ctx.exception))


    def test_generated_audit_contracts_keep_canonical_wording_history_free(self):
        frozen = self._freeze_refinement_for_certification(copy.deepcopy(self.base))
        outputs = []
        for render in (
            lambda: correctness._emit_audit_prompt(self.graph(), "L1_left_boundary"),
            lambda: correctness._emit_composition_audit_prompt(frozen, "G1_primary_goal", "general"),
            lambda: correctness._emit_coverage_audit_prompt(frozen),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                render()
            outputs.append(out.getvalue())
        self.assertIn("intended current semantics", outputs[0])
        self.assertIn("current-state semantics", outputs[1])
        self.assertIn("present-tense/current-state semantics", outputs[2])

    def test_generated_audit_contracts_append_non_normative_harness_feedback(self):
        doc = copy.deepcopy(self.base)
        frozen = self._freeze_refinement_for_certification(doc)

        outputs = []
        for render in (
            lambda: correctness._emit_audit_prompt(self.graph(), "L1_left_boundary"),
            lambda: correctness._emit_composition_audit_prompt(frozen, "G1_primary_goal", "general"),
            lambda: correctness._emit_coverage_audit_prompt(frozen),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                render()
            outputs.append(out.getvalue())

        for text in outputs:
            self.assertIn("NON-NORMATIVE HARNESS FEEDBACK", text)
            self.assertIn("HARNESS FEEDBACK: none", text)
            self.assertIn("correctness_model_defect", text)
            self.assertIn("harness_prompt_defect", text)
            self.assertIn("documentation_ergonomics", text)
            self.assertIn("it must not alter, reinterpret,", text)
            self.assertIn("serve as a premise for the canonical verdict", text)


if __name__ == "__main__":
    unittest.main()
