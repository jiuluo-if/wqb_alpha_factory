import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from wqb_agent.alpha_factory import AlphaFactory
from wqb_agent.checkpoints import CheckpointStore
from wqb_agent.diversity import (
    select_budget_candidates,
    semantic_mechanism_key,
)
from wqb_agent.factory_runner import AIFactoryRunner
from wqb_agent.memory import ExperienceMemory
from wqb_agent.optimizer_workflow import OptimizerHooks, OptimizerWorkflow
from wqb_agent.proposal_contract import _operator_reference
from wqb_agent.reflection import Reflector
from wqb_agent.state import Experiment, Trajectory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPERATOR_REFERENCE = _operator_reference(
    os.path.join(ROOT, "docs", "reference", "OPERATORS_CHEATSHEET.md")
)


class TestResearchLoopIntegration(unittest.TestCase):
    @staticmethod
    def _profile(field_id, description="daily close price", dataset="pv1"):
        return {
            "id": field_id,
            "description": description,
            "dataset": dataset,
            "type": "MATRIX",
            "frequency": "daily",
            "category": "market",
            "semantic_status": "KNOWN",
        }

    @staticmethod
    def _parent(question=None):
        parent = Experiment(
            1, "h-parent", "rank(close_a)", {}, ["close_a"], ["pv1"]
        )
        parent.status = "DONE"
        parent.lineage_id = "lineage-parent"
        parent.metrics = {
            "sharpe": 1.1,
            "fitness": 0.8,
            "turnover": 0.2,
            "checks": [{"name": "SELF_CORRELATION", "status": "UNKNOWN"}],
        }
        parent.health = {"ok": True}
        parent.field_understanding = {"close_a": "daily close price"}
        parent.field_analysis = {
            "close_a": {
                "data_type": "MATRIX",
                "semantic_traits": {
                    "concept": "market_price",
                    "measurement": "level",
                    "behavior": "signed",
                    "status": "KNOWN",
                },
            }
        }
        parent.field_source = {"kind": "brain_api"}
        parent.field_hypothesis_basis = {"close_a": {"mechanism": "price level"}}
        parent.economic_mechanism = "price level carries cross-sectional information"
        parent.child_economic_hypothesis = {
            "expression": "rank(group_neutralize(close_a, SUBINDUSTRY))",
            "economic_mechanism": "group-relative price information",
            "change_type": "neutralization",
            "experiment_question": question or "does group normalization preserve the effect?",
        }
        return parent

    def test_real_factory_proposals_carry_semantic_traits_and_stable_identity(self):
        factory = AlphaFactory()
        proposals = factory.assemble_proposals(
            {"id": "h-real"},
            [self._profile("close_a"), self._profile("close_b")],
            OPERATOR_REFERENCE,
            max_candidates=2,
        )

        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(item.get("field_analysis") for item in proposals))
        keys = {semantic_mechanism_key(item) for item in proposals}
        self.assertEqual(len(keys), 2)
        self.assertNotIn("UNKNOWN", keys)
        self.assertTrue(all(
            item["field_analysis"][item["fields"][0]].get("semantic_traits")
            for item in proposals
        ))

    def test_real_optimizer_child_preserves_parent_lineage_into_budget(self):
        factory = AlphaFactory()
        parent = self._parent()
        trajectory = Trajectory(persist=False)
        trajectory.add(parent)
        optimizer = OptimizerWorkflow(
            trajectory=trajectory,
            alpha_feed_cache=SimpleNamespace(load=lambda: {}),
            alpha_factory=factory,
            quality_policy={},
            operator_reference=OPERATOR_REFERENCE,
            hooks=OptimizerHooks(
                ensure_loaded=lambda: None,
                terminal_expressions=lambda: set(),
            ),
        )
        records = optimizer.optimizable_signal_records()
        children = optimizer.generate(records, max_candidates=1)

        self.assertEqual(optimizer.last_handoff_report["trajectory_recorded"], 1)
        self.assertEqual(optimizer.last_handoff_report["optimizer_candidates"], 1)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["lineage_id"], "lineage-parent")
        self.assertEqual(children[0]["research_layer"], "optimization")
        selected, audit = select_budget_candidates(children, target=1)
        self.assertEqual(selected, [])
        self.assertEqual(audit["selected_count"], 0)

    def test_real_multifield_relationship_reaches_budget_with_audit(self):
        factory = AlphaFactory()
        fields = [
            self._profile(
                "put_iv", "put option implied volatility", "option8"
            ) | {"category": "options"},
            self._profile(
                "call_iv", "call option implied volatility", "option8"
            ) | {"category": "options"},
        ]
        proposals = factory.assemble_proposals(
                 {"id": "h-pair", "template_ids": ["toy_pair_spread"]},
            fields,
            OPERATOR_REFERENCE,
            max_candidates=1,
        )

        self.assertEqual(len(proposals), 1)
        relationship = proposals[0]["relationship_audit"]
        self.assertEqual(relationship["relationship_admission"], "ALLOW")
        self.assertEqual(relationship["slot_assignment"], {
            "p": "put_iv", "s": "call_iv",
        })
        for item in proposals:
            item.update({
                "proposal_origin": "factory",
                "research_layer": "exploration",
                "research_role": "EXPLORE",
                "experiment_stage": "BASELINE",
            })
        selected, audit = select_budget_candidates(proposals, target=1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(
            selected[0]["relationship_audit"], relationship
        )
        self.assertNotEqual(semantic_mechanism_key(selected[0]), "UNKNOWN")
        self.assertEqual(audit["selected_count"], 1)

    def test_reflection_memory_context_reaches_real_optimizer_budget(self):
        question = "does normalization preserve the effect?"
        with tempfile.TemporaryDirectory() as tmp:
            memory = ExperienceMemory(state_dir=tmp)
            reflector = Reflector(memory)
            experiment = Experiment(1, "h-parent", "rank(close_a)", {}, ["close_a"])
            experiment.status = "DONE"
            experiment.metrics = {
                "sharpe": 0.8,
                "fitness": 0.6,
                "turnover": 0.2,
                "returns": 0.03,
                "drawdown": 0.1,
                "margin": 0.003,
                "checks": [{"name": "LIMITS", "pass": True}],
            }
            hypothesis = {
                "id": "h-parent",
                "statement": "price information has a group-relative component",
                "direction": "long",
                "agent_interpretation": {
                    "outcome": "INCONCLUSIVE",
                    "mechanism_learning": "group normalization remains unresolved",
                    "evidence_refs": [experiment.id],
                    "direct_relevance": True,
                },
                "next_experiment": {
                    "idea": "test group normalization",
                    "expression": "rank(group_neutralize(close_a, SUBINDUSTRY))",
                    "change_reason": "separate absolute from group-relative information",
                    "unresolved_question": "is the effect absolute or relative?",
                    "next_discriminating_question": question,
                    "competing_explanations": ["absolute", "relative"],
                    "evidence_needed": ["same horizon"],
                },
            }
            memory.register_hypothesis(hypothesis)
            reflector.reflect(1, hypothesis, [experiment])
            context = memory.context()
            children = AlphaFactory().optimize_signal_proposals(
                [self._parent(question).to_dict()],
                OPERATOR_REFERENCE,
                max_candidates=1,
            )
            selected = children

        self.assertTrue(context["next_discriminating_questions"])
        self.assertEqual(context["next_discriminating_questions"][0], question)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["research_layer"], "optimization")

    def test_priority_selection_orders_normal_before_low(self):
        def candidate(expression, **metadata):
            item = {
                "expression": expression,
                "field_analysis": {"f": {"semantic_traits": {
                    "concept": "market_price", "measurement": "level",
                    "behavior": "signed", "status": "KNOWN",
                }}},
            }
            item.update(metadata)
            return item

        low = candidate(
            "rank(low)", hypothesis_outcome="SUPPORTED",
            confirmation_status="INDEPENDENT_CONFIRMED",
        )
        normal = candidate("rank(normal)")
        for item in (low, normal):
            item.update({
                "proposal_origin": "factory", "research_layer": "exploration",
                "research_role": "EXPLORE", "experiment_stage": "BASELINE",
            })
        selected, _audit = select_budget_candidates([low, normal], target=1)
        self.assertEqual(selected[0]["expression"], "rank(normal)")

    def test_route_comparison_ignores_order_but_accepts_new_question(self):
        previous = {
            "candidate_expression_fingerprints": ["rank(a)"],
            "semantic_mechanism_fingerprints": ["price:level:persistent"],
            "relationship_fingerprints": ["pair:relative"],
            "dataset_route": ["pv1", "fundamental6"],
            "research_question_fingerprints": ["is the effect relative?"],
        }
        reordered = {
            "candidate_expression_fingerprints": ["rank(b)"],
            "semantic_mechanism_fingerprints": ["price:level:persistent"],
            "relationship_fingerprints": ["pair:relative"],
            "dataset_route": ["fundamental6", "pv1"],
            "research_question_fingerprints": ["is the effect relative?"],
        }
        decision = AIFactoryRunner.route_decision(
            previous, reordered, route_attempt=0, no_gain_attempts=0,
        )
        self.assertFalse(decision["information_gain"])
        questioned = dict(reordered, research_question_fingerprints=[
            "is the effect absolute or relative?"
        ])
        decision = AIFactoryRunner.route_decision(
            previous, questioned, route_attempt=0, no_gain_attempts=0,
        )
        self.assertTrue(decision["information_gain"])
        self.assertIn("question_change", decision["information_changes"])

    def test_budget_audit_reports_candidate_pool_saturation(self):
        def candidate(expression):
            return {
                "expression": expression,
                "field_analysis": {"f": {"semantic_traits": {
                    "concept": "market_price", "measurement": "level",
                    "behavior": "signed", "status": "KNOWN",
                }}},
                "lineage_id": "same-lineage",
            }

        candidates = [candidate("rank(a)"), candidate("rank(b)"), candidate("rank(c)")]
        for item in candidates:
            item.update({
                "proposal_origin": "factory", "research_layer": "exploration",
                "research_role": "EXPLORE", "experiment_stage": "BASELINE",
            })
        selected, audit = select_budget_candidates(candidates, target=2)
        self.assertEqual(len(selected), 2)
        self.assertEqual(audit["saturation"]["mechanism_groups"], 1)
        self.assertEqual(audit["saturation"]["lineage_groups"], 1)
        self.assertGreater(
            audit["saturation_dropped_or_deprioritized"]["mechanism"], 0
        )

    def test_factory_batch_clears_stale_budget_audit_on_invalid_input(self):
        factory = AlphaFactory()
        factory.last_budget_audit = {"selected_count": 100}
        self.assertEqual(
            factory.generate_factory_batch({}, None, OPERATOR_REFERENCE, target=100),
            [],
        )
        self.assertEqual(factory.last_budget_audit, {})

    def test_real_scarcity_routes_and_stops_without_idle_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]
            profile = self._profile("close_a")
            agent = SimpleNamespace(
                state_dir=tmp,
                alpha_factory=AlphaFactory(),
                factory_config={},
                min_factory_datasets=1,
                min_cross_dataset_pairs=0,
                next_round_no=lambda: 1,
                run_suggestion_round=lambda _round: {
                    "research_space": {
                        "id": "scarce-route",
                        "statement": "test scarcity route",
                        "datasets": ["pv1"],
                    },
                    "fields": [profile],
                    "operator_reference": OPERATOR_REFERENCE,
                    "field_source": {"kind": "test"},
                    "context": {},
                    "epoch_label": "rb0",
                },
                run_proposals=Mock(),
                checkpoints=CheckpointStore(tmp),
                memory=SimpleNamespace(seen_expressions=set()),
                trajectory=SimpleNamespace(experiments=[]),
            )

            def sleep(seconds):
                now[0] += seconds

            result = AIFactoryRunner(
                agent, factory=agent.alpha_factory, clock=lambda: now[0],
                sleeper=sleep,
            ).run(
                duration_sec=10,
                idle_sleep_sec=1,
                max_simulations=100,
                daily_simulation_cap=100,
                weekly_simulation_cap=100,
            )

        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["last_action"], "STOP_BUDGET_SHORTAGE")
        self.assertIn("budget_audit", result["last_result"])
        self.assertGreater(result["last_result"]["budget_audit"]["shortage_count"], 0)
        agent.run_proposals.assert_not_called()

    def test_restart_preserves_budget_route_and_no_gain_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.0]
            profile = self._profile("close_a")

            def build_agent(factory):
                return SimpleNamespace(
                    state_dir=tmp,
                    alpha_factory=factory,
                    factory_config={},
                    min_factory_datasets=1,
                    min_cross_dataset_pairs=0,
                    next_round_no=lambda: 1,
                    run_suggestion_round=lambda _round: {
                        "research_space": {
                            "id": "restart-scarce",
                            "statement": "test restart scarcity",
                            "datasets": ["pv1"],
                        },
                        "fields": [profile],
                        "operator_reference": OPERATOR_REFERENCE,
                        "field_source": {"kind": "test"},
                        "context": {},
                        "epoch_label": "rb-restart",
                    },
                    run_proposals=Mock(),
                    checkpoints=CheckpointStore(tmp),
                    memory=SimpleNamespace(seen_expressions=set()),
                    trajectory=SimpleNamespace(experiments=[]),
                )

            def sleep(seconds):
                now[0] += seconds

            first_agent = build_agent(AlphaFactory())
            first = AIFactoryRunner(
                first_agent, factory=first_agent.alpha_factory,
                clock=lambda: now[0], sleeper=sleep,
            ).run(
                duration_sec=1,
                idle_sleep_sec=1,
                max_simulations=100,
                daily_simulation_cap=100,
                weekly_simulation_cap=100,
            )
            self.assertEqual(first["last_action"], "REROUTE_BUDGET_SHORTAGE")
            self.assertEqual(first["route_attempt"], 1)
            self.assertEqual(first["no_gain_attempts"], 1)

            restarted_agent = build_agent(AlphaFactory())
            second = AIFactoryRunner(
                restarted_agent, factory=restarted_agent.alpha_factory,
                clock=lambda: now[0], sleeper=sleep,
            ).run(
                duration_sec=10,
                idle_sleep_sec=1,
                max_simulations=100,
                daily_simulation_cap=100,
                weekly_simulation_cap=100,
            )
            checkpoint_created = os.path.exists(
                os.path.join(tmp, "round_1.checkpoint.json")
            )

        self.assertEqual(second["status"], "STOPPED")
        self.assertEqual(second["last_action"], "STOP_BUDGET_SHORTAGE")
        self.assertEqual(second["route_attempt"], 2)
        self.assertEqual(second["no_gain_attempts"], 2)
        self.assertEqual(second["simulations_reserved"], 0)
        restarted_agent.run_proposals.assert_not_called()
        self.assertFalse(checkpoint_created)


if __name__ == "__main__":
    unittest.main()
