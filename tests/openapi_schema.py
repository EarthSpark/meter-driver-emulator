"""A small JSON Schema checker for the subset the spec's OpenAPI document uses.

Covers $ref (with sibling keywords kept), type (single or list), required,
properties, additionalProperties (boolean or schema), items, enum, const,
minimum, maximum, exclusiveMinimum, exclusiveMaximum, pattern, minItems,
maxItems, allOf and oneOf. Its purpose in the tests is the acceptance
criterion: every response body and event has every required property of
its schema and no value of a wrong JSON type.

Not implemented (unused by the spec document): anyOf, not, if/then/else,
patternProperties, propertyNames, dependentRequired, uniqueItems,
minLength/maxLength, multipleOf, format, contains, prefixItems, $defs and
references outside #/components/schemas.
"""

import re


class SchemaError(AssertionError):
    """A value does not satisfy its schema."""


def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise SchemaError(f"unsupported Python value {value!r}")


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class SchemaValidator:
    """Validates values against named schemas in an OpenAPI document."""

    def __init__(self, document):
        self.schemas = document["components"]["schemas"]

    def resolve(self, schema):
        """Follow a `#/components/schemas/<name>` reference, keeping the referrer's sibling keywords."""
        while "$ref" in schema:
            ref = schema["$ref"]
            prefix = "#/components/schemas/"
            if not ref.startswith(prefix):
                raise SchemaError(f"unsupported $ref {ref}")
            siblings = {key: value for key, value in schema.items() if key != "$ref"}
            schema = {**self.schemas[ref[len(prefix) :]], **siblings}
        return schema

    def assert_valid(self, name, value, strict=True):
        """Raise SchemaError unless `value` satisfies the named schema.

        `strict=False` skips the value constraints `enum`, `minimum`,
        `maximum`, `exclusiveMinimum` and `exclusiveMaximum` while still
        enforcing structure: required properties, JSON types, const
        discriminators, patterns and array bounds.
        """
        self.check(self.schemas[name], value, name, strict)

    def check(self, schema, value, path, strict=True):
        """Raise SchemaError unless `value` satisfies `schema`."""
        schema = self.resolve(schema)
        if "allOf" in schema:
            for index, part in enumerate(schema["allOf"]):
                self.check(part, value, f"{path}(allOf[{index}])", strict)
        if "oneOf" in schema:
            failures = []
            passed = 0
            for index, part in enumerate(schema["oneOf"]):
                try:
                    self.check(part, value, f"{path}(oneOf[{index}])", strict)
                    passed += 1
                except SchemaError as exc:
                    failures.append(str(exc))
            if passed != 1:
                raise SchemaError(f"{path}: {passed} oneOf branches matched; failures: {failures}")
        if "const" in schema and value != schema["const"]:
            raise SchemaError(f"{path}: expected const {schema['const']!r}, got {value!r}")
        if strict and "enum" in schema and value not in schema["enum"]:
            raise SchemaError(f"{path}: {value!r} not in enum {schema['enum']}")
        if "type" in schema:
            allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            actual = _json_type(value)
            if actual == "integer" and "number" in allowed:
                actual = "number"
            if actual not in allowed:
                raise SchemaError(f"{path}: expected type {allowed}, got {actual} ({value!r})")
        if strict and _is_number(value):
            self._check_bounds(schema, value, path)
        if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
            raise SchemaError(f"{path}: {value!r} does not match {schema['pattern']}")
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                raise SchemaError(f"{path}: fewer than {schema['minItems']} items")
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                raise SchemaError(f"{path}: more than {schema['maxItems']} items")
            if "items" in schema:
                for index, item in enumerate(value):
                    self.check(schema["items"], item, f"{path}[{index}]", strict)
        if isinstance(value, dict):
            self._check_object(schema, value, path, strict)

    @staticmethod
    def _check_bounds(schema, value, path):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: {value!r} below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: {value!r} above maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise SchemaError(f"{path}: {value!r} not above exclusiveMinimum {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise SchemaError(f"{path}: {value!r} not below exclusiveMaximum {schema['exclusiveMaximum']}")

    def _check_object(self, schema, value, path, strict):
        for name in schema.get("required", []):
            if name not in value:
                raise SchemaError(f"{path}: missing required property {name!r}")
        properties = schema.get("properties", {})
        for name, subschema in properties.items():
            if name in value:
                self.check(subschema, value[name], f"{path}.{name}", strict)
        additional = schema.get("additionalProperties", True)
        if additional is True:
            return
        extras = [name for name in value if name not in properties]
        if additional is False:
            if extras:
                raise SchemaError(f"{path}: unexpected properties {extras}")
            return
        for name in extras:
            self.check(additional, value[name], f"{path}.{name}", strict)
