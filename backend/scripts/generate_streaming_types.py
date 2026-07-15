from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Union, get_args, get_origin

ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from backend.services.streaming.stream_event_schema import (  # noqa: E402
    StreamEventMetadata,
    StreamEventType,
)
OUTPUT_PATH = ROOT / "client" / "src" / "types" / "packets.ts"


def _ts_type(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is Union:
        parts = []
        has_none = False
        for arg in get_args(annotation):
            if arg is type(None):  # noqa: E721 - intentional None check
                has_none = True
                continue
            parts.append(_ts_type(arg))
        unique = sorted({p for p in parts if p})
        union = " | ".join(unique) if unique else "unknown"
        if has_none:
            union = f"{union} | null"
        return union
    if origin in (list, List):
        args = get_args(annotation)
        inner = _ts_type(args[0]) if args else "unknown"
        return f"Array<{inner}>"
    if origin in (dict, Dict, Mapping):
        return "Record<string, unknown>"
    if origin is None:
        if annotation is str:
            return "string"
        if annotation in (int, float):
            return "number"
        if annotation is bool:
            return "boolean"
        if annotation is Any:
            return "unknown"
    return "unknown"


def _pascal_case(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _emit_metadata_interface() -> str:
    lines = ["export interface StreamEventMetadata {"]
    for field_name, field_info in StreamEventMetadata.model_fields.items():
        ts_type = _ts_type(field_info.annotation)
        lines.append(f"  {field_name}?: {ts_type};")
    lines.append("  [key: string]: unknown;")
    lines.append("}")
    return "\n".join(lines)


def _emit_stream_event_types(event_types: List[str]) -> str:
    union = " | ".join(f"\"{value}\"" for value in event_types)
    return f"export type StreamEventType = {union};"


def _emit_stream_event_base() -> str:
    return "\n".join(
        [
            "export interface StreamEvent {",
            "  type: StreamEventType;",
            "  content: string;",
            "  metadata?: StreamEventMetadata;",
            "  sequence?: number;",
            "  task_id?: number;",
            "  timestamp?: string;",
            "  [key: string]: unknown;",
            "}",
        ]
    )


def _emit_event_interfaces(event_types: List[str]) -> str:
    lines: List[str] = []
    for event_type in event_types:
        interface_name = f"{_pascal_case(event_type)}Event"
        if event_type == "graph_interrupt":
            lines.append(f"export interface {interface_name} extends StreamEvent {{")
            lines.append(f"  type: \"{event_type}\";")
            lines.append("  thread_id?: string;")
            lines.append("  interrupt_id?: string;")
            lines.append("  checkpoint_id?: string;")
            lines.append("  interrupt_type?: \"tool_approval\" | \"plan_review\" | \"clarify_request\";")
            lines.append("  payload?: Record<string, unknown>;")
            lines.append("  graph_name?: string;")
            lines.append("}")
        else:
            lines.append(
                f"export type {interface_name} = StreamEvent & {{ type: \"{event_type}\" }};"
            )
    return "\n".join(lines)


def _emit_packet_union(event_types: List[str]) -> str:
    variants = " | ".join(f"{_pascal_case(t)}Event" for t in event_types)
    return f"export type PacketObj = {variants};"


def _emit_packet_interface() -> str:
    return "\n".join(
        [
            "export interface Placement {",
            "  turn_index: number;",
            "  tab_index?: number;",
            "  sub_turn_index?: number | null;",
            "  [key: string]: unknown;",
            "}",
            "",
            "export interface Packet {",
            "  placement: Placement;",
            "  obj: PacketObj;",
            "  sequence?: number;",
            "  task_id?: number;",
            "  conversation_id?: string;",
            "  turn_id?: string;",
            "  [key: string]: unknown;",
            "}",
            "",
            "export type StreamPacket = Packet;",
            "",
            "export function isStreamPacket(value: unknown): value is StreamPacket {",
            "  if (!value || typeof value !== \"object\") {",
            "    return false;",
            "  }",
            "  const candidate = value as StreamPacket;",
            "  return Boolean(candidate.placement && candidate.obj);",
            "}",
        ]
    )


def generate() -> str:
    event_types = list(get_args(StreamEventType))
    sections = [
        "// AUTO-GENERATED by backend/scripts/generate_streaming_types.py. DO NOT EDIT.",
        "",
        _emit_stream_event_types(event_types),
        "",
        _emit_metadata_interface(),
        "",
        _emit_stream_event_base(),
        "",
        _emit_event_interfaces(event_types),
        "",
        _emit_packet_union(event_types),
        "",
        _emit_packet_interface(),
        "",
    ]
    return "\n".join(sections)


def main() -> None:
    content = generate()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
