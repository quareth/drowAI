from __future__ import annotations

import json
import subprocess
from enum import Enum
import inspect
from pathlib import Path
from shutil import which
from typing import Any, Dict, List, Optional, Type, get_args, get_origin

from pydantic import BaseModel

from agent.tools.base_tool import BaseTool

from tests.tools.registry.manifest_manager import ManifestManager
from .parameter_fixtures import save_param_fixture


class FixtureGenerator:
    """Generate tool fixtures using schema defaults and cached outputs."""

    def __init__(
        self,
        *,
        params_dir: Optional[Path] = None,
        outputs_dir: Optional[Path] = None,
        help_cache_dir: Optional[Path] = None,
        manifest: Optional[ManifestManager] = None,
    ) -> None:
        self.params_dir = params_dir or Path("tests") / "tools" / "fixtures" / "params"
        self.outputs_dir = outputs_dir or Path("tests") / "tools" / "fixtures" / "outputs"
        self.help_cache_dir = help_cache_dir or Path("tests") / "tools" / "fixtures" / "help_cache"
        self.manifest = manifest or ManifestManager()

    def generate_all(self, tool_id: str, tool_cls: Type[BaseTool]) -> None:
        self.generate_param_fixture(tool_id, tool_cls)
        self.generate_help_fixture(tool_id)
        self.generate_output_fixture(tool_id)

    def generate_help_fixture(self, tool_id: str, binary: Optional[str] = None) -> Path:
        self.help_cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.help_cache_dir / f"{tool_id.replace('.', '_')}.txt"
        if path.exists():
            return path

        resolved_binary = binary or self._infer_binary_name(tool_id)
        output = ""
        if resolved_binary and which(resolved_binary):
            try:
                result = subprocess.run(
                    [resolved_binary, "--help"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = (result.stdout or "") + (result.stderr or "")
            except Exception:
                output = ""

        if not output:
            output = f"Help output not available for {tool_id}."

        path.write_text(output, encoding="utf-8")
        self.manifest.update_fixture_status(tool_id, help_cached=True)
        self.manifest.save_manifest()
        return path

    def generate_param_fixture(self, tool_id: str, tool_cls: Type[BaseTool]) -> Path:
        args_class = tool_cls.args_model
        minimal_params = self._get_minimal_args(args_class)
        full_params = self._get_full_args(args_class)
        edge_cases = self._get_edge_cases(args_class, minimal_params)
        invalid_cases = self._get_invalid_cases(args_class, minimal_params)

        content = {
            "tool_id": tool_id,
            "test_cases": {
                "minimal": {
                    "description": "Required fields only",
                    "params": minimal_params,
                    "expected_valid": True,
                },
                "full": {
                    "description": "All optional fields populated",
                    "params": full_params,
                    "expected_valid": True,
                },
                "edge_cases": edge_cases,
                "invalid": invalid_cases,
            },
            "expected_command_patterns": {
                "minimal": [f"^{self._infer_binary_name(tool_id)}"],
                "full": [f"^{self._infer_binary_name(tool_id)}"],
            },
        }

        path = save_param_fixture(tool_id, content)
        self.manifest.update_fixture_status(tool_id, fixture_available=True)
        self.manifest.save_manifest()
        return path

    def generate_output_fixture(self, tool_id: str) -> Path:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        path = self.outputs_dir / f"{tool_id.replace('.', '_')}.txt"
        if path.exists():
            return path

        placeholder = f"[fixture] Sample output for {tool_id} not available yet."
        path.write_text(placeholder, encoding="utf-8")
        return path

    def _get_minimal_args(self, args_class: Type[BaseModel]) -> Dict[str, Any]:
        minimal: Dict[str, Any] = {}
        for field_name, field_info in args_class.model_fields.items():
            if not field_info.is_required():
                continue
            type_info = self._get_field_type_info(field_info.annotation)
            if field_name == "target":
                minimal[field_name] = "http://example.com"
            elif field_name in ("host", "hostname"):
                minimal[field_name] = "192.168.1.1"
            elif field_name == "wordlist":
                minimal[field_name] = "/usr/share/wordlists/common.txt"
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

    def _get_full_args(self, args_class: Type[BaseModel]) -> Dict[str, Any]:
        full_args = self._get_minimal_args(args_class)
        for field_name, field_info in args_class.model_fields.items():
            if field_name in full_args:
                continue
            type_info = self._get_field_type_info(field_info.annotation)
            if type_info["is_enum"] and type_info["enum_values"]:
                full_args[field_name] = (
                    type_info["enum_values"][1]
                    if len(type_info["enum_values"]) > 1
                    else type_info["enum_values"][0]
                )
            elif type_info["is_list"]:
                full_args[field_name] = ["option1", "option2"]
            elif type_info["inner_type"] == str:
                full_args[field_name] = f"full_{field_name}"
            elif type_info["inner_type"] == int:
                full_args[field_name] = 100
            elif type_info["inner_type"] == bool:
                full_args[field_name] = True
        return full_args

    def _get_edge_cases(self, args_class: Type[BaseModel], minimal: Dict[str, Any]) -> List[Dict[str, Any]]:
        edge_cases: List[Dict[str, Any]] = []
        for field_name, field_info in args_class.model_fields.items():
            type_info = self._get_field_type_info(field_info.annotation)
            constraints = self._extract_constraints(field_info)
            if type_info["inner_type"] != int or not constraints:
                continue
            if "ge" in constraints:
                edge_cases.append(
                    {
                        "name": f"{field_name}_min",
                        "params": {**minimal, field_name: constraints["ge"]},
                        "expected_valid": True,
                    }
                )
            if "le" in constraints:
                edge_cases.append(
                    {
                        "name": f"{field_name}_max",
                        "params": {**minimal, field_name: constraints["le"]},
                        "expected_valid": True,
                    }
                )
        return edge_cases

    def _get_invalid_cases(self, args_class: Type[BaseModel], minimal: Dict[str, Any]) -> List[Dict[str, Any]]:
        invalid_cases: List[Dict[str, Any]] = [
            {
                "name": "missing_required",
                "params": {},
                "expected_error": "required",
            }
        ]
        for field_name, field_info in args_class.model_fields.items():
            constraints = self._extract_constraints(field_info)
            if "ge" in constraints:
                invalid_cases.append(
                    {
                        "name": f"{field_name}_below_min",
                        "params": {**minimal, field_name: constraints["ge"] - 1},
                        "expected_error": field_name,
                    }
                )
            if "le" in constraints:
                invalid_cases.append(
                    {
                        "name": f"{field_name}_above_max",
                        "params": {**minimal, field_name: constraints["le"] + 1},
                        "expected_error": field_name,
                    }
                )
        return invalid_cases

    def _get_field_type_info(self, annotation: Any) -> Dict[str, Any]:
        origin = get_origin(annotation)
        args = get_args(annotation)
        info = {
            "origin": origin,
            "args": args,
            "is_optional": False,
            "is_list": False,
            "is_enum": False,
            "inner_type": None,
            "enum_values": [],
        }
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
            info["enum_values"] = [e.value for e in inner]

        return info

    def _extract_constraints(self, field_info: Any) -> Dict[str, Any]:
        constraints: Dict[str, Any] = {}
        if hasattr(field_info, "metadata"):
            for constraint in field_info.metadata:
                if hasattr(constraint, "ge"):
                    constraints["ge"] = constraint.ge
                if hasattr(constraint, "gt"):
                    constraints["gt"] = constraint.gt
                if hasattr(constraint, "le"):
                    constraints["le"] = constraint.le
                if hasattr(constraint, "lt"):
                    constraints["lt"] = constraint.lt
        return constraints

    def _infer_binary_name(self, tool_id: str) -> str:
        slug = tool_id.split(".")[-1]
        return slug.replace("_", "-")
