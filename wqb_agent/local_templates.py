"""Local-only Alpha template reading and candidate construction.

This module is the standalone, network-free surface over the local private
template catalog:

* :func:`load_local_templates` reads the explicit private catalog (fail closed).
* :func:`describe_local_templates` returns read-only template metadata.
* :func:`build_local_candidates` / :func:`build_local_specs` render
  Agent-selected fields into non-submitting ``SimulationSpec`` values, either
  from an explicitly named template set or by mechanism rotation.

Every construction path requires a template with a declared economic
mechanism: explicit mode only renders the named templates, while rotation mode
round-robins distinct mechanisms (and their templates and field bundles) so one
mechanism cannot fill the whole batch.

It never creates a BRAIN client, never POSTs a Simulation, never writes research
state, caches or files, and never performs live capability or quota checks.
Live field/operator capability, gateway admission, remote dedup and the single
Simulation write chain stay where they belong (``simulation_gateway`` via
``research_api.simulate*``); Alpha submission stays human.  ``PARTIAL_OPERATOR``
templates still require an explicit live operator reference from the caller and
stay unmaterialized without one.
"""

import random
from collections import OrderedDict
from collections.abc import Mapping

from .alpha_factory import AlphaFactory
from .alpha_templates import (
    AlphaTemplateRegistry,
    PrivateTemplateCatalogError,
    load_private_templates,
)
from .alpha_templates.loader import resolve_private_catalog_path
from .alpha_templates.validation import (
    effective_relationship_contract,
    effective_semantic_contract,
)

MAX_LOCAL_FIELDS = 100
MAX_LOCAL_TEMPLATES = 100
MAX_LOCAL_COUNT = 100
DEFAULT_ROTATION_ATTEMPTS = 8

__all__ = [
    "DEFAULT_ROTATION_ATTEMPTS",
    "MAX_LOCAL_COUNT",
    "MAX_LOCAL_FIELDS",
    "MAX_LOCAL_TEMPLATES",
    "PrivateTemplateCatalogError",
    "build_local_candidates",
    "build_local_specs",
    "describe_local_templates",
    "load_local_templates",
    "local_template_registry",
    "normalize_local_request",
    "template_mechanism_keys",
]


def load_local_templates(catalog_path=None, *, environ=None, home=None):
    """Read the explicit local private catalog; fail closed when absent.

    Resolution never searches the working directory and never falls back to the
    packaged synthetic catalog.
    """
    return load_private_templates(catalog_path, environ=environ, home=home)


def local_template_registry(*, templates=None, catalog_path=None,
                            environ=None, home=None):
    """Build a read-only registry over explicit templates or the private catalog."""
    if templates is not None and catalog_path is not None:
        raise ValueError("choose templates or catalog_path, not both")
    if templates is not None:
        return AlphaTemplateRegistry(templates=tuple(templates))
    path = resolve_private_catalog_path(catalog_path, environ=environ, home=home)
    return AlphaTemplateRegistry(private_catalog=path)


def _selected_ids(template_ids):
    if isinstance(template_ids, str):
        template_ids = [template_ids]
    if not isinstance(template_ids, (list, tuple)) or not template_ids or any(
        not isinstance(item, str) or not item.strip() for item in template_ids
    ):
        raise ValueError("template_ids must be a non-empty explicit list")
    selected = [item.strip() for item in template_ids]
    if len(selected) > MAX_LOCAL_TEMPLATES:
        raise ValueError(f"template_ids cannot exceed {MAX_LOCAL_TEMPLATES} entries")
    if len(set(selected)) != len(selected):
        raise ValueError("template_ids must not contain duplicates")
    return selected


def _count(count):
    try:
        target = int(count)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("count must be an explicit integer") from exc
    if isinstance(count, bool) or str(target) != str(count).strip():
        raise ValueError("count must be an explicit integer")
    if not 0 <= target <= MAX_LOCAL_COUNT:
        raise ValueError(f"count must be between 0 and {MAX_LOCAL_COUNT}")
    return target


def normalize_local_request(*, template_ids, count, fields, dataset_id=None):
    """Validate an explicit local request and return its normalized parts."""
    if not isinstance(fields, (list, tuple)) or not fields:
        raise ValueError("fields must be a non-empty Agent-selected list")
    if len(fields) > MAX_LOCAL_FIELDS:
        raise ValueError(f"fields cannot exceed {MAX_LOCAL_FIELDS} entries")
    selected_ids = _selected_ids(template_ids)
    target = _count(count)

    selected_fields = []
    for item in fields:
        if isinstance(item, Mapping):
            field = dict(item)
            field_id = field.get("id") or field.get("field_id")
            dataset = field.get("dataset_id") or field.get("dataset")
            if isinstance(dataset, Mapping):
                dataset = dataset.get("id") or dataset.get("name")
            dataset = dataset or dataset_id
        else:
            field_id = item
            dataset = dataset_id
            field = {"id": field_id}
        if field_id is None or dataset is None or not str(dataset).strip():
            raise ValueError("each selected field requires a BRAIN dataset id")
        field["id"] = str(field_id).strip()
        field["dataset"] = str(dataset).strip()
        selected_fields.append(field)
    return selected_ids, target, selected_fields


def _normalize_fields(fields, dataset_id=None):
    return normalize_local_request(
        template_ids=["local-field-normalization"],
        count=1,
        fields=fields,
        dataset_id=dataset_id,
    )[2]


def mechanism_key(template):
    """Return the declared mechanism group of one template.

    The fine-grained declared family wins over the coarse source tag, so a real
    private catalog groups by mechanism (``PRIVATE_AMIHUD_ILLIQUIDITY``) instead
    of collapsing every entry into ``LIVE``/``PRIVATE``.
    """
    novelty = getattr(template, "novelty_family", None)
    if novelty and str(novelty).strip():
        return str(novelty)
    family = getattr(template, "family", None)
    if family and str(family).strip():
        return str(family)
    tags = tuple(getattr(template, "mechanism_tags", ()) or ())
    if tags:
        return str(tags[0])
    return str(template.template_id)


def template_mechanism_keys(template):
    """Return every declared identity that a mechanism filter may match."""
    keys = []
    for value in (
        mechanism_key(template),
        getattr(template, "novelty_family", None),
        getattr(template, "family", None),
    ):
        if value and str(value).strip() and str(value) not in keys:
            keys.append(str(value))
    for tag in getattr(template, "mechanism_tags", ()) or ():
        if str(tag).strip() and str(tag) not in keys:
            keys.append(str(tag))
    return tuple(keys)


def _fold_mechanisms(mechanisms):
    if isinstance(mechanisms, str):
        mechanisms = [mechanisms]
    if not isinstance(mechanisms, (list, tuple)) or not mechanisms or any(
        not isinstance(item, str) or not item.strip() for item in mechanisms
    ):
        raise ValueError("mechanisms must be a non-empty explicit list")
    return {item.strip().casefold() for item in mechanisms}


def _matches_mechanisms(template, wanted):
    if wanted is None:
        return True
    folded = {key.casefold() for key in template_mechanism_keys(template)}
    return bool(folded & wanted)


def _mechanism_groups(registry, mechanisms=None, template_ids=None):
    wanted_ids = None
    if template_ids is not None:
        wanted_ids = set(_selected_ids(template_ids))
    wanted = None
    if mechanisms is not None:
        wanted = _fold_mechanisms(mechanisms)
    groups = OrderedDict()
    for template in registry.economic_templates():
        if wanted_ids is not None and template.template_id not in wanted_ids:
            continue
        if not _matches_mechanisms(template, wanted):
            continue
        groups.setdefault(mechanism_key(template), []).append(template)
    if not groups:
        for template in registry.catalog():
            identifier = template.get("template_id") or template.get("id")
            candidate = registry.get(identifier)
            if candidate is None or candidate.template_mode != "CONCRETE":
                continue
            if wanted_ids is not None and candidate.template_id not in wanted_ids:
                continue
            if not _matches_mechanisms(candidate, wanted):
                continue
            groups.setdefault(mechanism_key(candidate), []).append(candidate)
    return groups


def _rotating_order(groups, rng):
    """Round-robin mechanisms so no single mechanism fills the batch."""
    keys = sorted(groups)
    rng.shuffle(keys)
    queues = [list(groups[key]) for key in keys]
    for queue in queues:
        rng.shuffle(queue)
    order = []
    while any(queues):
        for queue in queues:
            if queue:
                order.append(queue.pop())
    return order


def _field_sampler(fields, rng):
    by_dataset = OrderedDict()
    for row in fields:
        by_dataset.setdefault(str(row["dataset"]), []).append(row)
    used = set()

    def sample(template):
        width = max(1, int(getattr(template, "economic_field_count", 1) or 1))
        wants_vector = "vec_" in str(getattr(template, "expression", "") or "")
        datasets = sorted(by_dataset)
        rng.shuffle(datasets)
        for dataset in datasets:
            rows = by_dataset[dataset]
            preferred = [
                row for row in rows
                if not wants_vector
                or str(row.get("type") or "").upper() == "VECTOR"
            ] or rows
            if len(preferred) < width:
                continue
            if len(preferred) == width:
                pick = list(preferred)
            else:
                pick = rng.sample(preferred, width)
            identity = (
                template.template_id, dataset,
                tuple(sorted(str(row["id"]) for row in pick)),
            )
            if identity in used:
                continue
            used.add(identity)
            return pick
        if len(fields) >= width:
            return rng.sample(list(fields), width)
        return list(fields)

    return sample


def _render_candidates(factory, templates, sampler, target, settings,
                       operator_reference):
    records = []
    seen = set()
    if not templates:
        return records
    attempts = 0
    limit = max(DEFAULT_ROTATION_ATTEMPTS * target, target + len(templates))
    while len(records) < target and attempts < limit:
        template = templates[attempts % len(templates)]
        attempts += 1
        if template.template_mode == "PARTIAL_OPERATOR":
            if operator_reference is None:
                continue
            specs = factory.generate_probe_specs(
                {"template_ids": [template.template_id]}, sampler(template),
                operator_reference, target=1, simulation_settings=settings,
            )
        else:
            specs = factory.generate(
                {"template_ids": [template.template_id]}, sampler(template),
                count=1, settings=settings,
            )
        if not specs:
            continue
        spec = specs[0]
        identity = str(getattr(spec, "expression", "") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        records.append({
            "spec": spec,
            "template_id": template.template_id,
            "mechanism": getattr(template, "economic_mechanism", None),
            "mechanism_key": mechanism_key(template),
            "mechanism_tags": list(getattr(template, "mechanism_tags", ()) or ()),
            "novelty_family": getattr(template, "novelty_family", None),
            "field_ids": list(getattr(spec, "fields", ()) or ()),
        })
    return records


def describe_local_templates(*, templates=None, catalog_path=None,
                             environ=None, home=None, template_ids=None,
                             mechanisms=None):
    """Return read-only template metadata without rendering or touching BRAIN."""
    registry = local_template_registry(
        templates=templates, catalog_path=catalog_path,
        environ=environ, home=home,
    )
    wanted = None
    if template_ids is not None:
        wanted = set(_selected_ids(template_ids))
    wanted_mechanisms = None
    if mechanisms is not None:
        wanted_mechanisms = _fold_mechanisms(mechanisms)
    rows = []
    for entry in registry.catalog():
        template_id = entry.get("template_id") or entry.get("id")
        if wanted is not None and template_id not in wanted:
            continue
        template = registry.get(template_id)
        if template is None:
            continue
        if not _matches_mechanisms(template, wanted_mechanisms):
            continue
        row = dict(entry)
        row["simulation_type"] = template.simulation_type
        row["template_mode"] = template.template_mode
        row["economic_field_count"] = template.economic_field_count
        row["field_slots"] = list(template.field_slots)
        row["field_roles"] = list(template.field_roles)
        row["mechanism_key"] = mechanism_key(template)
        row["mechanism_keys"] = list(template_mechanism_keys(template))
        row["relationship_contract"] = effective_relationship_contract(template)
        row["semantic_contract"] = effective_semantic_contract(template)
        rows.append(row)
    return rows


def build_local_candidates(*, fields, count=6, template_ids=None, selection="auto",
                           settings=None, neutralization=None, mechanisms=None,
                           seed=None, templates=None, catalog_path=None,
                           environ=None, home=None, operator_reference=None,
                           dataset_id=None):
    """Render mechanism-tagged candidates locally.

    ``template_ids`` names the templates the construction may use — a specific
    mechanism can be forced this way.  With ``selection="explicit"`` (the
    ``auto`` default whenever ``template_ids`` are given) those templates are
    used in the given order.  With ``selection="rotate"`` the same set is
    rotated instead: mechanisms first, then their templates, then the field
    bundles inside each dataset.  Without ``template_ids`` rotation draws from
    every local template carrying a declared economic mechanism, optionally
    narrowed by ``mechanisms``.  ``seed`` makes a rotation reproducible.
    """
    target = _count(count)
    selected_fields = _normalize_fields(fields, dataset_id=dataset_id)
    if target == 0:
        return []
    mode = str(selection or "auto").strip().lower()
    if mode not in {"auto", "explicit", "rotate"}:
        raise ValueError("selection must be auto, explicit or rotate")
    if template_ids is not None:
        selected_ids = _selected_ids(template_ids)
        if mode == "auto":
            mode = "explicit"
    else:
        selected_ids = None
        if mode == "explicit":
            raise ValueError("selection=explicit requires template_ids")
        mode = "rotate"
    effective_settings = dict(settings or {})
    if neutralization is None:
        neutralization = effective_settings.get("neutralization")
    registry = local_template_registry(
        templates=templates, catalog_path=catalog_path,
        environ=environ, home=home,
    )
    factory = AlphaFactory(
        neutralization=str(neutralization or "SUBINDUSTRY"),
        registry=registry,
    )
    rng = random.Random(seed)
    if mode == "explicit":
        chosen = []
        for template_id in selected_ids:
            template = registry.get(template_id)
            if template is None:
                raise ValueError(f"unknown local template: {template_id}")
            if not getattr(template, "economic_mechanism", None):
                raise ValueError(f"template {template_id} declares no economic mechanism")
            if template.template_mode == "PARTIAL_OPERATOR" and operator_reference is None:
                raise ValueError(
                    f"template {template_id} needs an explicit live operator reference"
                )
            chosen.append(template)
        selection_label = "EXPLICIT"
    else:
        groups = _mechanism_groups(registry, mechanisms, selected_ids)
        order = _rotating_order(groups, rng)
        chosen = [
            template for template in order
            if template.template_mode != "PARTIAL_OPERATOR"
            or operator_reference is not None
        ]
        if not chosen:
            raise ValueError("no local template with a declared mechanism is available")
        selection_label = "ROTATE"
    records = _render_candidates(
        factory, chosen, _field_sampler(selected_fields, rng), target,
        effective_settings, operator_reference,
    )
    for record in records:
        record["selection"] = selection_label
    return records


def build_local_specs(*, fields, count=6, **kwargs):
    """Return the ``SimulationSpec`` values of :func:`build_local_candidates`.

    Construction is purely local: no client, no network, no file writes and no
    live capability check.  The returned specs are only eligible for the single
    Simulation write chain, where the gateway re-checks live field/operator
    capability and exact-once admission.
    """
    return [record["spec"] for record in build_local_candidates(
        fields=fields, count=count, **kwargs,
    )]
