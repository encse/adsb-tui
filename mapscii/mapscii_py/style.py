import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class Styler:
    """The small Mapbox Style subset understood by the original MapSCII."""

    def __init__(self, style: Dict[str, Any]) -> None:
        style = copy.deepcopy(style)
        self.name = style.get("name", "unnamed")
        self.by_id: Dict[str, Dict[str, Any]] = {}
        self.by_layer: Dict[str, List[Dict[str, Any]]] = {}
        constants = style.get("constants", {})
        layers = self._replace_constants(style.get("layers", []), constants)

        for layer in layers:
            reference = layer.get("ref")
            if reference in self.by_id:
                parent = self.by_id[reference]
                for key in ("type", "source-layer", "minzoom", "maxzoom", "filter"):
                    if key not in layer and key in parent:
                        layer[key] = parent[key]

            layer["applies_to"] = self._compile_filter(layer.get("filter"))
            source_layer = layer.get("source-layer")
            if source_layer:
                self.by_layer.setdefault(source_layer, []).append(layer)
            if layer.get("id"):
                self.by_id[layer["id"]] = layer

    @classmethod
    def from_file(cls, path: Path) -> "Styler":
        with path.open("r", encoding="utf-8") as style_file:
            return cls(json.load(style_file))

    def style_for(self, layer: str, properties: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for style in self.by_layer.get(layer, []):
            if style["applies_to"](properties):
                return style
        return None

    def background(self) -> Optional[str]:
        layer = self.by_id.get("background")
        return layer and layer.get("paint", {}).get("background-color")

    def _replace_constants(self, value: Any, constants: Dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: self._replace_constants(item, constants) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace_constants(item, constants) for item in value]
        if isinstance(value, str) and value.startswith("@"):
            return constants.get(value, value)
        return value

    def _compile_filter(self, expression: Any) -> Callable[[Dict[str, Any]], bool]:
        if not expression:
            return lambda properties: True

        operator = expression[0]
        arguments = expression[1:]

        if operator in ("all", "any", "none"):
            filters = [self._compile_filter(argument) for argument in arguments]
            if operator == "all":
                return lambda properties: all(test(properties) for test in filters)
            if operator == "any":
                return lambda properties: any(test(properties) for test in filters)
            return lambda properties: not any(test(properties) for test in filters)

        key = arguments[0] if arguments else None
        expected = arguments[1] if len(arguments) > 1 else None

        if operator == "==":
            return lambda properties: properties.get(key) == expected
        if operator == "!=":
            return lambda properties: properties.get(key) != expected
        if operator == "in":
            values = set(arguments[1:])
            return lambda properties: properties.get(key) in values
        if operator == "!in":
            values = set(arguments[1:])
            return lambda properties: properties.get(key) not in values
        if operator == "has":
            return lambda properties: key in properties
        if operator == "!has":
            return lambda properties: key not in properties
        if operator in (">", ">=", "<", "<="):
            def compare(properties: Dict[str, Any]) -> bool:
                actual = properties.get(key)
                if actual is None:
                    return False
                try:
                    return {
                        ">": actual > expected,
                        ">=": actual >= expected,
                        "<": actual < expected,
                        "<=": actual <= expected,
                    }[operator]
                except TypeError:
                    return False
            return compare

        return lambda properties: True


def paint_value(style: Dict[str, Any], *names: str) -> Any:
    paint = style.get("paint", {})
    for name in names:
        value = paint.get(name)
        if value is not None:
            if isinstance(value, dict) and value.get("stops"):
                return value["stops"][0][1]
            return value
    return None

