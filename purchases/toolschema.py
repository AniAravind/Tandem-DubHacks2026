"""
A tiny checker for the agent's tool arguments.

agent/tools.json describes what arguments each tool accepts (JSON Schema). Instead of adding the `jsonschema`
library, this file understands just the handful of keywords our schemas actually use:

    type, enum, minimum, maximum, exclusiveMinimum, maxLength, minItems, maxItems,
    required, properties, additionalProperties, items

`validate(schema, value)` returns a list of plain-English problems (an empty list means the arguments are fine).
Nothing here touches the database.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings

TOOLS_PATH = Path(settings.BASE_DIR) / "agent" / "tools.json"


def load_tool_schemas() -> dict:
    """{"search_options": <its parameters schema>, ...} read straight from agent/tools.json."""
    tools = json.loads(TOOLS_PATH.read_text())
    return {t["function"]["name"]: t["function"]["parameters"] for t in tools}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)      # True/False are not numbers here


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": _is_number,
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def validate(schema: dict, value, path: str = "arguments") -> list[str]:
    problems: list[str] = []
    wanted = schema.get("type")
    if wanted and not _TYPE_CHECKS[wanted](value):
        return [f"{path} must be of type {wanted}"]

    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path} must be one of {schema['enum']}")

    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path} must be at most {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            problems.append(f"{path} must be greater than {schema['exclusiveMinimum']}")

    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        problems.append(f"{path} is longer than {schema['maxLength']} characters")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path} needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path} can have at most {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                problems += validate(schema["items"], item, f"{path}[{i}]")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{path}.{name} is required")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                problems += validate(properties[name], item, f"{path}.{name}")
            elif extra is False:
                problems.append(f"{path}.{name} is not an allowed field")
            elif isinstance(extra, dict):
                problems += validate(extra, item, f"{path}.{name}")
    return problems
