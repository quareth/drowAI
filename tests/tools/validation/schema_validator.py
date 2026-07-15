"""Reusable schema contract checks for tool argument models and commands."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Type, get_args, get_origin

from pydantic import BaseModel, ValidationError

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs
from tests.tools.fixtures.parameter_fixtures import load_param_fixture


@dataclass
class ValidationReport:
    tool_id: str
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def add_result(self, name: str, passed: bool, details: Optional[str] = None) -> None:
        self.results[name] = {"passed": passed, "details": details or ""}

    def all_passed(self) -> bool:
        return all(result["passed"] for result in self.results.values())

    def failures(self) -> Dict[str, Dict[str, Any]]:
        return {name: result for name, result in self.results.items() if not result["passed"]}


def _get_pydantic_fields(model_class: Type[BaseModel]) -> Dict[str, Any]:
    return model_class.model_fields


def _get_enum_values(enum_class: Type[Enum]) -> List[Any]:
    return [e.value for e in enum_class]


def _get_field_type_info(field_info: Any) -> Dict[str, Any]:
    annotation = field_info.annotation
    origin = get_origin(annotation)
    args = get_args(annotation)

    info = {
        "annotation": annotation,
        "origin": origin,
        "args": args,
        "is_optional": False,
        "is_list": False,
        "is_enum": False,
        "inner_type": None,
        "enum_values": [],
        "constraints": {},
    }

    if hasattr(field_info, "metadata"):
        for constraint in field_info.metadata:
            if hasattr(constraint, "ge"):
                info["constraints"]["ge"] = constraint.ge
            if hasattr(constraint, "gt"):
                info["constraints"]["gt"] = constraint.gt
            if hasattr(constraint, "le"):
                info["constraints"]["le"] = constraint.le
            if hasattr(constraint, "lt"):
                info["constraints"]["lt"] = constraint.lt

    if origin is type(None) or (origin and "Union" in str(origin)):
        info["is_optional"] = True
        if args:
            info["inner_type"] = next((arg for arg in args if arg is not type(None)), None)
    else:
        info["inner_type"] = annotation

    if origin in (list, List):
        info["is_list"] = True
        if args:
            info["inner_type"] = args[0]

    inner = info["inner_type"]
    if inner and inspect.isclass(inner) and issubclass(inner, Enum):
        info["is_enum"] = True
        info["enum_values"] = _get_enum_values(inner)

    return info


class SchemaValidator:
    """Reusable schema validation for any tool."""

    def validate_tool(self, tool_id: str, tool_cls: Type[BaseTool]) -> ValidationReport:
        report = ValidationReport(tool_id)
        minimal_args = self._get_fixture_minimal_args(tool_id)

        report.add_result(
            "schema_instantiation",
            self._test_instantiation(tool_cls, minimal_args),
        )
        report.add_result(
            "enum_values",
            self._test_enum_values(tool_cls, minimal_args),
        )
        report.add_result(
            "build_command_minimal",
            self._test_build_command_minimal(tool_cls, minimal_args),
        )
        report.add_result(
            "build_command_full",
            self._test_build_command_full(tool_cls),
        )
        report.add_result(
            "optional_fields",
            self._test_optional_fields(tool_cls, minimal_args),
        )
        report.add_result(
            "constraint_boundaries",
            self._test_constraints(tool_cls, minimal_args),
        )

        return report

    def _get_fixture_minimal_args(self, tool_id: str) -> Optional[Dict[str, Any]]:
        """Return a tool's fixture-backed minimal params when available."""
        try:
            fixture = load_param_fixture(tool_id)
        except Exception:
            return None

        minimal_case = fixture.get("test_cases", {}).get("minimal")
        if not isinstance(minimal_case, dict):
            return None

        params = minimal_case.get("params")
        return params if isinstance(params, dict) else None

    def _get_minimal_args(self, args_class: Type[BaseToolArgs]) -> Dict[str, Any]:
        fields = _get_pydantic_fields(args_class)
        minimal: Dict[str, Any] = {}

        for field_name, field_info in fields.items():
            if not field_info.is_required():
                continue

            type_info = _get_field_type_info(field_info)
            if field_name == "target":
                minimal[field_name] = "http://example.com"
            elif field_name in ("host", "hostname"):
                minimal[field_name] = "192.168.1.1"
            elif field_name == "wordlist":
                minimal[field_name] = "/usr/share/wordlists/common.txt"
            elif field_name == "protocol":
                if type_info["is_enum"] and type_info["enum_values"]:
                    minimal[field_name] = type_info["enum_values"][0]
                else:
                    minimal[field_name] = "tcp"
            elif field_name == "module":
                if type_info["is_enum"] and type_info["enum_values"]:
                    minimal[field_name] = type_info["enum_values"][0]
                else:
                    minimal[field_name] = "default"
            elif field_name == "module_path":
                minimal[field_name] = "exploit/test/module"
            elif type_info["is_enum"] and type_info["enum_values"]:
                minimal[field_name] = type_info["enum_values"][0]
            elif type_info["inner_type"] == str:
                minimal[field_name] = "test_value"
            elif type_info["inner_type"] == int:
                minimal[field_name] = 1
            elif type_info["inner_type"] == bool:
                minimal[field_name] = False
            elif type_info["is_list"]:
                minimal[field_name] = []
            else:
                minimal[field_name] = "default"

        return minimal

    def _build_full_args(self, args_class: Type[BaseToolArgs]) -> Dict[str, Any]:
        fields = _get_pydantic_fields(args_class)
        full_args = self._get_minimal_args(args_class)

        for field_name, field_info in fields.items():
            if field_name in full_args:
                continue
            if field_name == "capture_filter" and full_args.get("input_file"):
                continue

            # Skip deprecated compatibility aliases in exhaustive "full args" payloads.
            # They can conflict with canonical fields by design.
            extra = getattr(field_info, "json_schema_extra", None) or {}
            if isinstance(extra, dict) and extra.get("deprecated") is True:
                continue

            type_info = _get_field_type_info(field_info)
            
            # Handle List[Enum] - list of enum values
            if type_info["is_list"]:
                list_inner_type = type_info.get("inner_type")
                # Check if list contains enums
                if list_inner_type and inspect.isclass(list_inner_type) and issubclass(list_inner_type, Enum):
                    enum_values = [e.value for e in list_inner_type]
                    # Use first two enum values if available
                    if len(enum_values) >= 2:
                        full_args[field_name] = [enum_values[0], enum_values[1]]
                    elif enum_values:
                        full_args[field_name] = [enum_values[0]]
                    else:
                        full_args[field_name] = []
                else:
                    full_args[field_name] = ["frame.number"] if field_name == "fields" else ["option1", "option2"]
            elif type_info["is_enum"] and type_info["enum_values"]:
                full_args[field_name] = (
                    type_info["enum_values"][1]
                    if len(type_info["enum_values"]) > 1
                    else type_info["enum_values"][0]
                )
            elif type_info["inner_type"] == str:
                if field_name == "ports":
                    full_args[field_name] = "80,443"
                elif field_name in ("host", "hostname"):
                    full_args[field_name] = "192.168.1.1"
                elif field_name == "protocol":
                    full_args[field_name] = "tcp"
                else:
                    full_args[field_name] = f"full_{field_name}"
            elif type_info["inner_type"] == int:
                if "ge" in type_info["constraints"]:
                    full_args[field_name] = type_info["constraints"]["ge"]
                elif "gt" in type_info["constraints"]:
                    full_args[field_name] = type_info["constraints"]["gt"] + 1
                else:
                    full_args[field_name] = 100
            elif type_info["inner_type"] == bool:
                full_args[field_name] = True

        return full_args

    def _test_instantiation(
        self, tool_cls: Type[BaseTool], minimal_args: Optional[Dict[str, Any]] = None
    ) -> bool:
        args_class = tool_cls.args_model
        minimal_args = minimal_args or self._get_minimal_args(args_class)
        try:
            args_class(**minimal_args)
        except ValidationError:
            return False
        return True

    def _test_enum_values(
        self, tool_cls: Type[BaseTool], minimal_args: Optional[Dict[str, Any]] = None
    ) -> bool:
        args_class = tool_cls.args_model
        fields = _get_pydantic_fields(args_class)
        minimal_args = minimal_args or self._get_minimal_args(args_class)

        for field_name, field_info in fields.items():
            type_info = _get_field_type_info(field_info)
            
            # Handle List[Enum] fields
            if type_info["is_list"]:
                list_inner_type = type_info.get("inner_type")
                if list_inner_type and inspect.isclass(list_inner_type) and issubclass(list_inner_type, Enum):
                    enum_values = [e.value for e in list_inner_type]
                    # Test each enum value wrapped in a list
                    for enum_value in enum_values:
                        test_args = {**minimal_args, field_name: [enum_value]}
                        try:
                            args_instance = args_class(**test_args)
                            result = getattr(args_instance, field_name)
                            # The validator converts strings to enum instances
                            if result and len(result) > 0:
                                actual_val = result[0].value if isinstance(result[0], Enum) else result[0]
                                if actual_val != enum_value:
                                    return False
                        except ValidationError:
                            return False
                continue
            
            # Handle single Enum fields
            if not type_info["is_enum"]:
                continue
            for enum_value in type_info["enum_values"]:
                test_args = {**minimal_args, field_name: enum_value}
                try:
                    args_instance = args_class(**test_args)
                    actual = getattr(args_instance, field_name)
                    # Compare values (handle both enum instances and raw values)
                    actual_val = actual.value if isinstance(actual, Enum) else actual
                    if actual_val != enum_value:
                        return False
                except ValidationError:
                    return False
        return True

    def _test_build_command_minimal(
        self, tool_cls: Type[BaseTool], minimal_args: Optional[Dict[str, Any]] = None
    ) -> bool:
        tool = tool_cls()
        args_class = tool_cls.args_model
        minimal_args = minimal_args or self._get_minimal_args(args_class)

        try:
            args_instance = args_class(**minimal_args)
            command = tool.build_command(args_instance)
        except Exception:
            return False

        return isinstance(command, list) and command and all(isinstance(arg, str) for arg in command)

    def _test_optional_fields(
        self, tool_cls: Type[BaseTool], minimal_args: Optional[Dict[str, Any]] = None
    ) -> bool:
        args_class = tool_cls.args_model
        fields = _get_pydantic_fields(args_class)
        minimal_args = minimal_args or self._get_minimal_args(args_class)

        for field_name, field_info in fields.items():
            if field_name in minimal_args:
                continue
            if field_name == "capture_filter" and minimal_args.get("input_file"):
                continue

            type_info = _get_field_type_info(field_info)
            test_value = None
            
            if type_info["is_list"]:
                list_inner_type = type_info.get("inner_type")
                # Check if list contains enums
                if list_inner_type and inspect.isclass(list_inner_type) and issubclass(list_inner_type, Enum):
                    enum_values = [e.value for e in list_inner_type]
                    if enum_values:
                        test_value = [enum_values[0]]
                    else:
                        test_value = []
                else:
                    test_value = ["frame.number"] if field_name == "fields" else ["item1", "item2"]
            elif type_info["is_enum"] and type_info["enum_values"]:
                test_value = type_info["enum_values"][0]
            elif type_info["inner_type"] == str:
                if field_name == "ports":
                    test_value = "80,443"
                elif field_name in ("host", "hostname"):
                    test_value = "192.168.1.1"
                elif field_name == "protocol":
                    test_value = "tcp"
                else:
                    test_value = "test_optional"
            elif type_info["inner_type"] == int:
                if "ge" in type_info["constraints"]:
                    test_value = type_info["constraints"]["ge"]
                elif "gt" in type_info["constraints"]:
                    test_value = type_info["constraints"]["gt"] + 1
                else:
                    test_value = 100
            elif type_info["inner_type"] == bool:
                test_value = True

            if test_value is None:
                continue

            test_args = {**minimal_args, field_name: test_value}
            try:
                args_instance = args_class(**test_args)
                # For lists of enums, the validator converts strings to enum instances
                # So we need to compare the actual values, not the references
                actual_value = getattr(args_instance, field_name)
                if type_info["is_list"] and type_info.get("inner_type") and inspect.isclass(type_info["inner_type"]) and issubclass(type_info["inner_type"], Enum):
                    # Compare as values for List[Enum]
                    actual_as_values = [v.value if isinstance(v, Enum) else v for v in actual_value] if actual_value else []
                    if actual_as_values != test_value:
                        return False
                elif actual_value != test_value:
                    return False
            except ValidationError:
                return False

        return True

    def _test_build_command_full(self, tool_cls: Type[BaseTool]) -> bool:
        tool = tool_cls()
        args_class = tool_cls.args_model
        full_args = self._build_full_args(args_class)

        try:
            args_instance = args_class(**full_args)
            command = tool.build_command(args_instance)
        except Exception:
            return False

        return isinstance(command, list) and bool(command)

    def _test_constraints(
        self, tool_cls: Type[BaseTool], minimal_args: Optional[Dict[str, Any]] = None
    ) -> bool:
        args_class = tool_cls.args_model
        fields = _get_pydantic_fields(args_class)
        minimal_args = minimal_args or self._get_minimal_args(args_class)

        # Identify related constraint pairs (e.g., min_length/max_length)
        # These need to be tested together to avoid cross-field validation failures
        related_pairs = [
            ("min_length", "max_length"),
            ("min_port", "max_port"),
            ("min_value", "max_value"),
        ]
        
        def get_related_field(field_name: str) -> Optional[str]:
            for pair in related_pairs:
                if field_name == pair[0]:
                    return pair[1]
                if field_name == pair[1]:
                    return pair[0]
            return None

        for field_name, field_info in fields.items():
            type_info = _get_field_type_info(field_info)
            constraints = type_info["constraints"]
            if not constraints:
                continue

            if type_info["inner_type"] == int:
                related_field = get_related_field(field_name)
                
                if "ge" in constraints:
                    test_args = {**minimal_args, field_name: constraints["ge"]}
                    # Adjust related field if needed to maintain valid relationship
                    if related_field and related_field in minimal_args:
                        if "min" in field_name and constraints["ge"] > minimal_args.get(related_field, 0):
                            test_args[related_field] = constraints["ge"]
                    try:
                        args_class(**test_args)
                    except ValidationError:
                        return False
                        
                if "gt" in constraints:
                    test_args = {**minimal_args, field_name: constraints["gt"] + 1}
                    if related_field and related_field in minimal_args:
                        if "min" in field_name and constraints["gt"] + 1 > minimal_args.get(related_field, 0):
                            test_args[related_field] = constraints["gt"] + 1
                    try:
                        args_class(**test_args)
                    except ValidationError:
                        return False
                        
                if "le" in constraints:
                    test_args = {**minimal_args, field_name: constraints["le"]}
                    # When testing upper bound of min_*, ensure max_* is also at upper bound
                    if related_field and related_field in minimal_args:
                        if "min" in field_name:
                            # Get the related field's le constraint if it exists
                            related_info = fields.get(related_field)
                            if related_info:
                                related_type_info = _get_field_type_info(related_info)
                                related_le = related_type_info["constraints"].get("le")
                                if related_le:
                                    test_args[related_field] = related_le
                                else:
                                    test_args[related_field] = constraints["le"]
                    try:
                        args_class(**test_args)
                    except ValidationError:
                        return False
                        
                if "lt" in constraints:
                    test_args = {**minimal_args, field_name: constraints["lt"] - 1}
                    if related_field and related_field in minimal_args:
                        if "min" in field_name:
                            related_info = fields.get(related_field)
                            if related_info:
                                related_type_info = _get_field_type_info(related_info)
                                related_lt = related_type_info["constraints"].get("lt")
                                if related_lt:
                                    test_args[related_field] = related_lt - 1
                                else:
                                    test_args[related_field] = constraints["lt"] - 1
                    try:
                        args_class(**test_args)
                    except ValidationError:
                        return False

        return True
