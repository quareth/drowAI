import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.prompts.builders.simple_tool import SimpleToolPromptBuilder


def _build_state():
    return {
        "facts": {
            "message": "Scan 127.0.0.1 using nmap",
            "capability": "scan_ports",
            "intent_hints": {
                "tool_hints": ["network_scan"],
                "targets": ["127.0.0.1"],
            },
            "eligible_routes": ["simple_tool_execution"],
            "tool_candidates": ["information_gathering.network_discovery.nmap"],
            "metadata": {
                "tool_catalog": {
                    "entries": [
                        {
                            "tool_id": "information_gathering.network_discovery.nmap",
                            "name": "Nmap",
                            "description": "Run nmap scans and parse the results.",
                        }
                    ]
                }
            },
        }
    }


def test_simple_tool_system_prompt_includes_context():
    builder = SimpleToolPromptBuilder()
    prompt = builder.build_system_prompt(_build_state())
    assert "Scan 127.0.0.1 using nmap" in prompt
    assert "network_scan" in prompt
    assert "127.0.0.1" in prompt


def test_simple_tool_decision_prompt_lists_catalog():
    builder = SimpleToolPromptBuilder()
    prompt = builder.build_decision_prompt(_build_state())
    assert "Nmap" in prompt
    assert "information_gathering.network_discovery.nmap" in prompt


def test_simple_tool_summary_prompt_uses_template():
    builder = SimpleToolPromptBuilder()
    prompt = builder.build_tool_summary_prompt(
        {"summary": "Scan completed", "errors": ["no errors"], "status": "success"}
    )
    assert "Scan completed" in prompt
    assert "no errors" in prompt
    assert "success" in prompt
