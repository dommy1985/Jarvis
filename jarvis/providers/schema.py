"""Validatore minimo di JSON Schema, per le risposte dei provider senza schema nativo (solo stdlib).

Copre le parole chiave usate dagli schemi di Jarvis: type (anche lista), enum, required, properties,
additionalProperties: false, items, minimum, maximum."""

from jarvis.i18n import t

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Elenco degli errori (vuoto se il valore è conforme), ognuno con il suo percorso."""
    errors = []
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[k](value) for k in kinds):
            return [t("schema.expected", path=path, kinds=t("schema.or").join(kinds), found=type(value).__name__)]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(t("schema.enum", path=path, value=repr(value), allowed=schema["enum"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(t("schema.minimum", path=path, value=value, limit=schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(t("schema.maximum", path=path, value=value, limit=schema["maximum"]))
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        errors += [t("schema.missing", path=path, key=key) for key in schema.get("required", []) if key not in value]
        if schema.get("additionalProperties") is False:
            errors += [t("schema.extra", path=path, key=key) for key in value if key not in properties]
        for key, sub in properties.items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors
