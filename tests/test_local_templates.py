"""Local template reading/construction API stays standalone, local and safe."""

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wqb_agent.alpha_templates import AlphaTemplate, PrivateTemplateCatalogError
from wqb_agent.local_templates import (
    MAX_LOCAL_COUNT,
    MAX_LOCAL_FIELDS,
    MAX_LOCAL_TEMPLATES,
    build_local_candidates,
    build_local_specs,
    describe_local_templates,
    load_local_templates,
    local_template_registry,
    mechanism_key,
    normalize_local_request,
    template_mechanism_keys,
)


def _template(template_id="local-fixture", mechanism="fixture mechanism",
              family="local-fixture-family", tags=("fixture_tags",), novelty=None):
    return AlphaTemplate(
        template_id=template_id,
        family=family,
        expression="rank({p})",
        required_slots=("p",),
        role="CONTROL_ALPHA",
        economic_mechanism=mechanism,
        field_relationship="single field",
        direction_reason="fixture direction",
        expected_horizon="short-term",
        falsification="fixture fails",
        semantic_contract="SYNTHETIC_FIXTURE",
        mechanism_tags=tags,
        novelty_family=novelty if novelty is not None else family.upper(),
    )


def _field(field_id="arbitrary-field", description="daily close price",
           dataset="arbitrary-dataset"):
    return {
        "id": field_id,
        "description": description,
        "dataset": dataset,
        "type": "MATRIX",
        "frequency": "daily",
        "category": "market",
        "semantic_status": "KNOWN",
    }


class TestLocalTemplateReading(unittest.TestCase):
    def test_missing_private_catalog_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PrivateTemplateCatalogError):
                load_local_templates(environ={}, home=tmp)

    def test_explicit_templates_read_without_a_catalog(self):
        registry = local_template_registry(templates=(_template(),))
        self.assertIsNotNone(registry.get("local-fixture"))
        self.assertIsNone(registry.get("missing-template"))

    def test_ambiguous_template_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing-catalog.toml")
            with self.assertRaises(ValueError):
                local_template_registry(
                    templates=(_template(),), catalog_path=missing
                )

    def test_describe_reports_contracts_mechanisms_and_filtering(self):
        rows = describe_local_templates(templates=(_template(),))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["template_id"], "local-fixture")
        self.assertEqual(row["economic_field_count"], 1)
        self.assertEqual(row["field_slots"], ["p"])
        self.assertEqual(row["simulation_type"], "REGULAR")
        self.assertEqual(row["mechanism_key"], "LOCAL-FIXTURE-FAMILY")
        self.assertIn("fixture_tags", row["mechanism_keys"])
        self.assertIn("local-fixture-family", row["mechanism_keys"])
        self.assertTrue(row["relationship_contract"])
        self.assertTrue(row["semantic_contract"])
        self.assertEqual(
            len(describe_local_templates(
                templates=(_template(),), template_ids=["local-fixture"]
            )),
            1,
        )
        self.assertEqual(
            describe_local_templates(
                templates=(_template(),), template_ids=["other-template"]
            ),
            [],
        )
        self.assertEqual(
            describe_local_templates(
                templates=(_template(),), mechanisms=["other_mechanism"]
            ),
            [],
        )

    def test_mechanism_key_prefers_the_declared_family(self):
        self.assertEqual(mechanism_key(_template()), "LOCAL-FIXTURE-FAMILY")
        self.assertEqual(
            mechanism_key(_template(novelty=None, family="family-only")),
            "FAMILY-ONLY",
        )
        keys = template_mechanism_keys(_template())
        self.assertEqual(keys[0], "LOCAL-FIXTURE-FAMILY")
        self.assertIn("local-fixture-family", keys)
        self.assertIn("fixture_tags", keys)

    def test_mechanism_filter_matches_any_declared_key(self):
        templates = (_template(),)
        for mechanisms in (["LOCAL-FIXTURE-FAMILY"], ["local-fixture-family"],
                           ["fixture_tags"], ["fixture_TAGS"]):
            with self.subTest(mechanisms=mechanisms):
                self.assertEqual(
                    len(describe_local_templates(templates=templates,
                                                 mechanisms=mechanisms)),
                    1,
                )
        self.assertEqual(
            describe_local_templates(templates=templates,
                                     mechanisms=["unrelated_mechanism"]),
            [],
        )
        self.assertEqual(
            [record["mechanism_key"] for record in build_local_candidates(
                fields=[_field()], count=1, mechanisms=["fixture_tags"],
                templates=templates,
            )],
            ["LOCAL-FIXTURE-FAMILY"],
        )


class TestLocalTemplateConstruction(unittest.TestCase):
    def test_normalization_rejects_duplicate_field_id_across_datasets(self):
        with self.assertRaisesRegex(ValueError, "field id.*multiple datasets"):
            normalize_local_request(
                template_ids=["local-fixture"], count=1,
                fields=[_field("same", dataset="d1"), _field("same", dataset="d2")],
            )

    def test_build_renders_specs_without_client_network_or_disk(self):
        settings = {
            "region": "USA", "universe": "TOP3000", "delay": 1,
            "decay": 4, "neutralization": "SUBINDUSTRY",
        }
        before = set(Path.cwd().iterdir())
        with mock.patch(
            "wqb_agent.client.WQBClient",
            side_effect=AssertionError("local construction must not build a client"),
        ):
            specs = build_local_specs(
                template_ids=["local-fixture"],
                fields=[_field()],
                count=1,
                settings=settings,
                templates=(_template(),),
            )
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.expression, "rank(arbitrary-field)")
        self.assertEqual(tuple(spec.fields), ("arbitrary-field",))
        self.assertEqual(spec.template_id, "local-fixture")
        self.assertEqual(dict(spec.settings), settings)
        self.assertEqual(set(Path.cwd().iterdir()), before)

    def test_explicit_candidates_carry_template_and_mechanism(self):
        records = build_local_candidates(
            template_ids=["local-fixture"],
            fields=[_field()],
            count=1,
            templates=(_template(),),
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["selection"], "EXPLICIT")
        self.assertEqual(records[0]["template_id"], "local-fixture")
        self.assertEqual(records[0]["mechanism"], "fixture mechanism")
        self.assertEqual(records[0]["mechanism_key"], "LOCAL-FIXTURE-FAMILY")
        self.assertEqual(records[0]["field_ids"], ["arbitrary-field"])

    def test_rotation_spreads_distinct_mechanisms(self):
        templates = (
            _template(template_id="template-a", mechanism="mechanism a",
                      tags=("mechanism_a",), family="family-a"),
            _template(template_id="template-b", mechanism="mechanism b",
                      tags=("mechanism_b",), family="family-b"),
        )
        fields = [
            _field(field_id="field_a", dataset="dataset_one"),
            _field(field_id="field_b", dataset="dataset_one"),
            _field(field_id="field_c", dataset="dataset_two"),
            _field(field_id="field_d", dataset="dataset_two"),
        ]
        records = build_local_candidates(fields=fields, count=2, seed=7,
                                         templates=templates)
        self.assertEqual(len(records), 2)
        self.assertEqual({record["selection"] for record in records}, {"ROTATE"})
        self.assertEqual(
            {record["mechanism_key"] for record in records},
            {"FAMILY-A", "FAMILY-B"},
        )
        self.assertEqual(
            len({record["spec"].expression for record in records}), 2
        )

    def test_rotation_respects_mechanism_filter(self):
        templates = (
            _template(template_id="template-a", tags=("mechanism_a",)),
            _template(template_id="template-b", tags=("mechanism_b",)),
        )
        records = build_local_candidates(
            fields=[_field()], count=1, seed=3, templates=templates,
            mechanisms=["mechanism_b"],
        )
        self.assertEqual([record["template_id"] for record in records],
                         ["template-b"])

    def test_rotation_can_be_scoped_to_explicit_templates(self):
        templates = (
            _template(template_id="template-a", tags=("mechanism_a",)),
            _template(template_id="template-b", tags=("mechanism_b",)),
            _template(template_id="template-c", tags=("mechanism_c",)),
        )
        records = build_local_candidates(
            fields=[
                _field(field_id="field_a", dataset="dataset_one"),
                _field(field_id="field_b", dataset="dataset_two"),
            ],
            count=2, seed=5, selection="rotate",
            template_ids=["template-a", "template-b"], templates=templates,
        )
        self.assertEqual(len(records), 2)
        self.assertEqual({record["selection"] for record in records}, {"ROTATE"})
        self.assertEqual({record["template_id"] for record in records},
                         {"template-a", "template-b"})

    def test_rotation_is_reproducible_for_a_seed(self):
        templates = (
            _template(template_id="template-a", tags=("mechanism_a",)),
            _template(template_id="template-b", tags=("mechanism_b",)),
        )
        fields = [_field(field_id="field_a"), _field(field_id="field_b")]
        first = build_local_candidates(fields=fields, count=2, seed=11,
                                       templates=templates)
        second = build_local_candidates(fields=fields, count=2, seed=11,
                                        templates=templates)
        self.assertEqual(
            [(record["template_id"], record["field_ids"]) for record in first],
            [(record["template_id"], record["field_ids"]) for record in second],
        )

    def test_unknown_or_conflicting_selection_is_rejected_locally(self):
        with self.assertRaises(ValueError):
            build_local_specs(
                template_ids=["missing-template"], fields=[_field()], count=1,
                templates=(_template(),),
            )
        with self.assertRaises(ValueError):
            build_local_specs(
                fields=[_field()], count=1, selection="explicit",
                templates=(_template(),),
            )
        with self.assertRaises(ValueError):
            build_local_specs(
                fields=[_field()], count=1, selection="shuffle",
                templates=(_template(),),
            )

    def test_zero_count_returns_no_specs(self):
        self.assertEqual(
            build_local_candidates(
                template_ids=["local-fixture"], fields=[_field()], count=0,
                templates=(_template(),),
            ),
            [],
        )

    def test_invalid_requests_are_rejected_locally(self):
        cases = (
            {"template_ids": ["local-fixture"], "count": 1, "fields": []},
            {"template_ids": [], "count": 1, "fields": [_field()]},
            {"template_ids": ["a", "a"], "count": 1, "fields": [_field()]},
            {"template_ids": ["local-fixture"], "count": MAX_LOCAL_COUNT + 1,
             "fields": [_field()]},
            {"template_ids": ["local-fixture"], "count": 1,
             "fields": [{"id": f"field_{index}"}
                        for index in range(MAX_LOCAL_FIELDS + 1)]},
            {"template_ids": ["local-fixture"], "count": 1,
             "fields": [{"id": "no-dataset"}]},
            {"template_ids": [f"template-{index}"
                              for index in range(MAX_LOCAL_TEMPLATES + 1)],
             "count": 1, "fields": [_field()]},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                normalize_local_request(**kwargs)

    def test_normalized_fields_carry_dataset_and_provenance(self):
        ids, target, fields = normalize_local_request(
            template_ids=[" local-fixture "],
            count="2",
            fields=[{"field_id": "field_a", "dataset_id": {"id": "dataset_a"},
                     "type": "MATRIX"}],
        )
        self.assertEqual(ids, ["local-fixture"])
        self.assertEqual(target, 2)
        self.assertEqual(
            fields,
            [{"field_id": "field_a", "dataset_id": {"id": "dataset_a"},
              "type": "MATRIX", "id": "field_a", "dataset": "dataset_a"}],
        )


class TestFacadeDelegation(unittest.TestCase):
    def test_facade_delegates_validation_to_local_api(self):
        import wqb_agent.research_api as api

        self.assertEqual(api.MAX_PROBE_FIELDS, MAX_LOCAL_FIELDS)
        self.assertEqual(api.MAX_PROBE_TEMPLATES, MAX_LOCAL_TEMPLATES)
        source = inspect.getsource(api.generate_probes)
        self.assertIn("normalize_local_request", source)
        self.assertNotIn("each selected field requires a BRAIN dataset id", source)


if __name__ == "__main__":
    unittest.main()
