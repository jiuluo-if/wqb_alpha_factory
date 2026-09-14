"""Pure catalog manifest and scope projections for FieldDiscovery."""


def normalize_dataset_ids(values):
    normalized = []
    if isinstance(values, (str, int)):
        values = [values]
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("id") or value.get("name")
        if isinstance(value, (str, int)):
            value = str(value).strip()
            if value and value not in normalized:
                normalized.append(value)
    return normalized


def valid_platform_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def manifest_is_complete(field_completeness, datasets, field_types):
    if not isinstance(field_completeness, dict) or not isinstance(datasets, dict):
        return False
    expected_types = set(field_types)
    for dataset_id in datasets:
        rows = field_completeness.get(dataset_id)
        if not isinstance(rows, dict) or set(rows) != expected_types:
            return False
        for field_type in field_types:
            row = rows.get(field_type)
            if (
                not isinstance(row, dict)
                or not valid_platform_count(row.get("expected_count"))
                or not valid_platform_count(row.get("loaded_count"))
                or row["expected_count"] != row["loaded_count"]
                or row.get("complete") is not True
                or row.get("truncation_reason") is not None
            ):
                return False
    return bool(datasets)


def catalog_scope(client):
    return {
        "instrument_type": getattr(client, "instrument_type", "EQUITY"),
        "region": getattr(client, "region", "USA"),
        "delay": getattr(client, "delay", 1),
        "universe": getattr(client, "universe", "TOP3000"),
    }
