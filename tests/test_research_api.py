import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from wqb_agent.research_api import (
    ExperimentSpec,
    SimulationSpec,
    compare_experiments,
    discover_fields,
    find_similar_alphas,
    generate_probes,
    get_capabilities,
    get_experiment,
    get_operator_reference,
    inspect_optimizer_context,
    inspect_state,
    inspect_template,
    list_templates,
    reconcile,
    run_experiment,
    search_history,
)
from wqb_agent.state import Experiment, Trajectory


class _Discovery:
    fields_per_discovery = 3

    def discover(self, query, target_count):
        return [{"id": "close", "type": "MATRIX"}][:target_count]

    def source_provenance(self):
        return {"kind": "brain_api", "snapshot_date": None}


class _FakeAgent:
    state_dir = None
    fields_per_discovery = 3
    discovery = _Discovery()

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.received = None
        self.optimizer_limit = None

    def next_round_no(self):
        return 7

    def run_proposals(self, path):
        with open(path, encoding="utf-8") as handle:
            self.received = json.load(handle)
        return {"accepted": 1, "status": "DONE"}

    def optimizer_context(self, *, limit=8):
        self.optimizer_limit = limit
        return {"limit": limit, "parents": []}


class _ProbeFactory:
    def generate_probe_specs(self, hypothesis, fields, operator_reference, **kwargs):
        self.received = (hypothesis, fields, operator_reference, kwargs)
        return [SimulationSpec("rank(close)", {"delay": 1}, ("close",))]


class _ProbeAgent(_FakeAgent):
    def __init__(self, state_dir):
        super().__init__(state_dir)
        self.alpha_factory = _ProbeFactory()
        self.operator_reference = {
            "status": "LIVE_VERIFIED", "availability": "AVAILABLE",
            "source": "BRAIN_LIVE_ONLY", "operators": ["rank"],
        }


class _FakeClient:
    def __init__(self):
        self.urls = []

    def get_progress_snapshot(self, url, timeout=60):
        self.urls.append((url, timeout))
        return {"url": url, "status": "RUNNING"}


class _RemoteFirstClient:
    def __init__(self):
        self.submissions = []

    def submit_simulation(self, expression, settings, **kwargs):
        self.submissions.append((expression, settings, kwargs))
        return "progress-remote-first"

    def poll_progress(self, progress_url, **kwargs):
        self.polled = progress_url
        return "alpha-remote-first"

    def get_alpha(self, alpha_id):
        return {"id": alpha_id, "is": {"sharpe": 1.2}}


class TestResearchApi(unittest.TestCase):
    def test_remote_first_tool_surface_has_capabilities_and_templates(self):
        capabilities = get_capabilities(client=SimpleNamespace(
            get_operator_capability=lambda: {
                "status": "LIVE_VERIFIED", "availability": "AVAILABLE",
                "source": "BRAIN_LIVE_ONLY", "operators": ["rank"],
            }
        ))
        self.assertEqual(capabilities["operators"], ["rank"])
        templates = list_templates()
        self.assertTrue(templates)
        template_id = templates[0]["template_id"]
        self.assertEqual(inspect_template(template_id)["template_id"], template_id)

    def test_similarity_is_advisory_and_does_not_require_local_trajectory(self):
        rows = [
            {"alpha_id": "a1", "alpha": {"regular": "rank(close)"}, "settings": {"delay": 1}},
            {"alpha_id": "a2", "alpha": {"regular": "rank(open)"}, "settings": {"delay": 1}},
        ]
        result = find_similar_alphas("rank(close)", rows=rows)
        self.assertEqual(result["kind"], "STRUCTURALLY_SIMILAR")
        self.assertEqual([item["alpha_id"] for item in result["matches"]], ["a2"])
    def test_experiment_spec_is_lightweight_and_adapts_to_existing_contract(self):
        spec = ExperimentSpec(
            hypothesis="short-term reversal",
            expression="rank(returns)",
            fields=("returns",),
            settings={"decay": 2},
            rationale="test a falsifiable reversal mechanism",
        )
        proposal = spec.to_proposal(round_no=4)
        self.assertEqual(proposal["round"], 4)
        self.assertEqual(proposal["fields"], ["returns"])
        self.assertEqual(proposal["experiment_stage"], "BASELINE")
        self.assertEqual(proposal["research_role"], "EXPLORE")
        self.assertNotIn("operator_mapping", proposal)
        self.assertNotIn("experiment_question", proposal)
        self.assertNotIn("expected_failure_modes", proposal)
        self.assertNotIn("tuning_risk", proposal)

    def test_legacy_spec_metadata_is_ignored(self):
        spec = ExperimentSpec.from_mapping({
            "hypothesis": "test", "expression": "rank(close)",
            "external_evidence_refs": ["legacy"],
        })
        self.assertFalse(hasattr(spec, "external_evidence_refs"))
        self.assertNotIn("external_evidence_refs", spec.to_proposal())

    def test_discovery_facade_uses_existing_discovery_component(self):
        result = discover_fields("price reversal", agent=_FakeAgent(tempfile.gettempdir()))
        self.assertEqual(result["fields"][0]["id"], "close")
        self.assertEqual(result["field_source"]["kind"], "brain_api")

    def test_generate_probes_returns_simulation_specs_without_inbox_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = _ProbeAgent(directory)
            result = generate_probes(
                query="short-term reversal", agent=agent, count=1,
            )
            self.assertEqual(result, [SimulationSpec("rank(close)", {"delay": 1}, ("close",))])
            self.assertEqual(agent.alpha_factory.received[0]["template_ids"], [])

    def test_run_experiment_delegates_to_guarded_agent_path(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = _FakeAgent(directory)
            result = run_experiment(
                {"hypothesis": "test", "expression": "rank(close)", "fields": ["close"]},
                agent=agent,
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(agent.received["round_no"], 7)
            self.assertEqual(agent.received["proposals"][0]["expression"], "rank(close)")
            self.assertEqual(os.listdir(directory), [])

    def test_run_experiment_simulation_spec_uses_remote_first_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            client = _RemoteFirstClient()
            result = run_experiment(
                SimulationSpec("rank(close)", {"delay": 1}),
                client=client,
                state_dir=directory,
            )
            self.assertEqual(result["status"], "DONE")
            self.assertEqual(result["alpha_id"], "alpha-remote-first")
            self.assertEqual(len(client.submissions), 1)
            self.assertFalse(os.path.exists(os.path.join(directory, "trajectory.jsonl")))
            self.assertFalse(os.path.exists(os.path.join(directory, "trial_ledger.jsonl")))

    def test_reconcile_only_polls_the_known_url(self):
        client = _FakeClient()
        result = reconcile("https://brain.example/progress/1", client=client, timeout=12)
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(client.urls, [("https://brain.example/progress/1", 12)])

    def test_operator_reference_requires_a_live_client(self):
        with self.assertRaises(RuntimeError):
            get_operator_reference()

    def test_operator_syntax_reference_is_explicitly_static(self):
        from wqb_agent.research_api import get_operator_syntax_reference

        reference = get_operator_syntax_reference()
        self.assertTrue(reference["sha256"])
        self.assertIn("rank", reference["operators"])
        self.assertEqual(reference["source"], "STATIC_SYNTAX_REFERENCE")
        self.assertEqual(reference["availability"], "UNKNOWN")

    def test_operator_reference_uses_client_capability(self):
        client = SimpleNamespace(get_operator_capability=lambda: {
            "key": "operators", "status": "LIVE_VERIFIED",
            "availability": "AVAILABLE", "source": "BRAIN_LIVE_ONLY",
            "operators": ["rank"], "capability_fingerprint": "fp",
        })
        self.assertEqual(get_operator_reference(client=client)["operators"], ["rank"])

    def test_state_queries_are_small_and_composable(self):
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_state(state_dir=directory, limit=2)
            self.assertEqual(result["experiment_count"], 0)
            self.assertEqual(result["recent_experiments"], [])
            self.assertEqual(compare_experiments(["missing"], state_dir=directory)["missing"], ["missing"])

    def test_inspect_state_streams_trajectory_once(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                Trajectory, "iter_rows", autospec=True, side_effect=Trajectory.iter_rows
            ) as reader:
                inspect_state(state_dir=directory, limit=2)
            self.assertEqual(reader.call_count, 1)

    def test_operator_reference_uses_installed_package_resource(self):
        with mock.patch(
            "wqb_agent.research_api.load_packaged_operator_syntax_reference",
            return_value={"source": "STATIC_SYNTAX_REFERENCE", "operators": ["rank"]},
        ) as loader:
            result = __import__("wqb_agent.research_api", fromlist=["get_operator_syntax_reference"]).get_operator_syntax_reference()
        self.assertEqual(result["operators"], ["rank"])
        loader.assert_called_once_with()

    def test_clean_wheel_reads_operator_resource_outside_checkout(self):
        import pathlib
        import subprocess
        import sys
        import zipfile

        root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            wheel_dir = pathlib.Path(directory) / "wheel"
            install_dir = pathlib.Path(directory) / "install"
            outside_dir = pathlib.Path(directory) / "outside"
            wheel_dir.mkdir()
            install_dir.mkdir()
            outside_dir.mkdir()
            subprocess.run(
                [sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "--no-build-isolation", "-w", str(wheel_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(wheel_dir.glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(install_dir)
            probe = (
                "import os, sys; "
                f"sys.path.insert(0, {str(install_dir)!r}); "
                f"os.chdir({str(outside_dir)!r}); "
                "from wqb_agent.research_api import get_operator_syntax_reference; "
                "assert 'rank' in get_operator_syntax_reference()['operators']"
            )
            subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True, text=True)

    def test_optimizer_context_facade_is_bounded_and_needs_no_agent_object(self):
        agent = _FakeAgent(tempfile.gettempdir())
        self.assertEqual(inspect_optimizer_context(agent=agent, limit=32), {"limit": 8, "parents": []})
        self.assertEqual(agent.optimizer_limit, 8)
        self.assertEqual(inspect_optimizer_context(agent=agent, limit=3)["limit"], 3)
        self.assertEqual(agent.optimizer_limit, 3)

    def test_history_queries_read_experiment_evidence_by_id_and_expression(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "trajectory.jsonl")
            experiment = Experiment(1, "h1", "rank(close)", {}, ["close"], ["pv1"])
            experiment.proposal_id = "proposal-1"
            experiment.status = "DONE"
            Trajectory(path=path).add(experiment)

            by_id = get_experiment(experiment.id, state_dir=directory)
            self.assertEqual(by_id["proposal_id"], "proposal-1")
            self.assertEqual(get_experiment("proposal-1", state_dir=directory)["id"], experiment.id)
            compared = compare_experiments([experiment.id, "missing"], state_dir=directory)
            self.assertEqual(len(compared["experiments"]), 1)
            self.assertEqual(compared["missing"], ["missing"])
            self.assertEqual(search_history("rank(close)", state_dir=directory)[0]["id"], experiment.id)


if __name__ == "__main__":
    unittest.main()
