"""Legacy-compatible passive survey metadata builders for TShark field rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict

from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.common import (
    _field_row_context,
    _flow_key,
    _none_if_empty,
    _safe_int,
)
from agent.tools.sniffing_spoofing.network_sniffers.tshark_parsing.protocols import (
    ftp,
    mail,
)


def parse_profile_field_summary(rows: list[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build legacy survey metadata from deterministic `-T fields` rows."""

    protocols: set[str] = set()
    hosts: set[str] = set()
    timestamps: list[float] = []
    conversations: dict[tuple[str, str, str | None, str | None, str | None], dict[str, Any]] = {}
    streams: dict[str, dict[str, Any]] = {}
    for fields in rows:
        context = _field_row_context(fields)
        if context["time_epoch"] is not None:
            timestamps.append(context["time_epoch"])
        for protocol in context["protocols"]:
            if protocol:
                protocols.add(protocol)
        for key in ("src", "dst"):
            if context[key]:
                hosts.add(context[key])
        if context["src"] and context["dst"]:
            conv_key = (
                context["src"],
                context["dst"],
                context["protocol"],
                context["src_port"],
                context["dst_port"],
            )
            conversation = conversations.setdefault(
                conv_key,
                {
                    "flow_key": _flow_key(context),
                    "src": context["src"],
                    "dst": context["dst"],
                    "protocol": context["protocol"],
                    "src_port": context["src_port"],
                    "dst_port": context["dst_port"],
                    "packet_count": 0,
                    "bytes": 0,
                },
            )
            conversation["packet_count"] += 1
            conversation["bytes"] += context["bytes"] or 0
        stream = context.get("stream")
        if stream:
            stream_summary = streams.setdefault(
                str(stream),
                {
                    "stream": str(stream),
                    "src": context["src"],
                    "dst": context["dst"],
                    "src_port": context["src_port"],
                    "dst_port": context["dst_port"],
                    "protocols": set(),
                    "frames": [],
                    "packet_count": 0,
                    "signals": set(),
                },
            )
            stream_summary["packet_count"] += 1
            if context["frame"]:
                stream_summary["frames"].append(context["frame"])
            for protocol in context["protocols"]:
                stream_summary["protocols"].add(protocol)
            for signal in survey_row_signals(fields):
                stream_summary["signals"].add(signal)
    duration_seconds = None
    time_start = None
    time_end = None
    if timestamps:
        time_start = min(timestamps)
        time_end = max(timestamps)
        if len(timestamps) >= 2:
            duration_seconds = round(time_end - time_start, 6)
    services = survey_services(conversations.values(), streams.values())
    interesting_streams = survey_interesting_streams(streams.values())
    recommended_next_queries = survey_recommended_next_queries(
        services=services,
        interesting_streams=interesting_streams,
    )
    return {
        "pcap_shape": {
            "packet_count": len(rows),
            "time_start": time_start,
            "time_end": time_end,
            "duration_seconds": duration_seconds,
        },
        "protocols": sorted(protocols),
        "hosts": sorted(hosts),
        "conversations": sorted(
            conversations.values(),
            key=lambda item: (
                str(item.get("src") or ""),
                str(item.get("dst") or ""),
                str(item.get("protocol") or ""),
                str(item.get("src_port") or ""),
                str(item.get("dst_port") or ""),
            ),
        ),
        "services": services,
        "interesting_streams": interesting_streams,
        "recommended_next_queries": recommended_next_queries,
    }


def survey_row_signals(fields: Mapping[str, Any]) -> list[str]:
    """Return non-secret survey signals present in one field row."""

    signals: list[str] = []
    signals.extend(ftp.survey_ftp_command_signals(fields))
    status = _safe_int(fields.get("http.response.code"))
    if status >= 400:
        signals.append("http_error_status")
    if _none_if_empty(fields.get("dns.flags.rcode")) not in (None, "0", "NoError"):
        signals.append("dns_error_rcode")
    if _none_if_empty(fields.get("tls.alert_message.desc")):
        signals.append("tls_alert")
    if any(
        _none_if_empty(fields.get(key))
        for key in (
            "tcp.analysis.retransmission",
            "tcp.analysis.fast_retransmission",
            "tcp.analysis.lost_segment",
            "tcp.analysis.duplicate_ack",
        )
    ):
        signals.append("tcp_analysis_warning")
    if _none_if_empty(fields.get("icmp.type")) or _none_if_empty(fields.get("icmp.code")):
        signals.append("icmp_message")
    signals.extend(mail.survey_mail_auth_command_signals(fields))
    return signals


def survey_services(
    conversations: Iterable[Mapping[str, Any]],
    streams: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build passive service hints from aggregated survey conversations."""

    stream_by_endpoint: dict[tuple[str | None, str | None, str | None], set[str]] = {}
    for stream in streams:
        dst = _none_if_empty(stream.get("dst"))
        dst_port = _none_if_empty(stream.get("dst_port"))
        protocol_hint = survey_protocol_hint(stream)
        if dst and dst_port:
            stream_by_endpoint.setdefault((dst, dst_port, protocol_hint), set()).add(
                str(stream.get("stream"))
            )

    services: dict[tuple[str, str, str], dict[str, Any]] = {}
    for conversation in conversations:
        dst = _none_if_empty(conversation.get("dst"))
        dst_port = _none_if_empty(conversation.get("dst_port"))
        if not dst or not dst_port:
            continue
        protocol_hint = survey_protocol_hint(conversation)
        if not protocol_hint:
            protocol_hint = well_known_protocol_for_port(dst_port)
        if not protocol_hint:
            continue
        key = (dst, dst_port, protocol_hint)
        service = services.setdefault(
            key,
            {
                "host": dst,
                "port": _safe_int(dst_port),
                "transport": _none_if_empty(conversation.get("protocol")) or "tcp",
                "protocol_hint": protocol_hint,
                "packet_count": 0,
                "bytes": 0,
                "streams": set(),
            },
        )
        service["packet_count"] += _safe_int(conversation.get("packet_count"))
        service["bytes"] += _safe_int(conversation.get("bytes"))
        service["streams"].update(stream_by_endpoint.get((dst, dst_port, protocol_hint), set()))
    return [
        {**service, "streams": sorted(service["streams"], key=natural_sort_key)}
        for service in sorted(
            services.values(),
            key=lambda item: (
                str(item.get("host") or ""),
                _safe_int(item.get("port")),
                str(item.get("protocol_hint") or ""),
            ),
        )
    ]


def survey_interesting_streams(streams: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rank streams that should direct the next intent selection."""

    interesting: list[dict[str, Any]] = []
    for stream in streams:
        signals = sorted(str(signal) for signal in stream.get("signals") or [])
        protocol_hint = survey_protocol_hint(stream)
        if not signals and protocol_hint not in {"ftp", "http", "dns", "tls", "smtp", "pop", "imap"}:
            continue
        reason, intent = survey_reason_and_intent(protocol_hint, signals)
        interesting.append(
            {
                "stream": str(stream.get("stream")),
                "protocol_hint": protocol_hint,
                "src": _none_if_empty(stream.get("src")),
                "dst": _none_if_empty(stream.get("dst")),
                "src_port": _none_if_empty(stream.get("src_port")),
                "dst_port": _none_if_empty(stream.get("dst_port")),
                "packet_count": _safe_int(stream.get("packet_count")),
                "frames": list(stream.get("frames") or [])[:5],
                "signals": signals,
                "reason": reason,
                "recommended_intent": intent,
            }
        )
    return sorted(
        interesting,
        key=lambda item: (
            0 if item.get("recommended_intent") == "find_security_relevant_artifacts" else 1,
            _safe_int(item.get("stream")),
        ),
    )


def survey_recommended_next_queries(
    *,
    services: list[Mapping[str, Any]],
    interesting_streams: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return planner-safe follow-up query recommendations for survey output."""

    recommendations: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()

    def add(intent: str, params: dict[str, Any], reason: str) -> None:
        normalized_params = {
            key: value
            for key, value in params.items()
            if value not in (None, "", [], {})
        }
        key = (intent, tuple(sorted(normalized_params.items())))
        if key in seen:
            return
        seen.add(key)
        recommendations.append(
            {
                "intent": intent,
                "params": normalized_params,
                "reason": reason,
            }
        )

    for stream in interesting_streams:
        protocol = _none_if_empty(stream.get("protocol_hint"))
        stream_id = _safe_int(stream.get("stream"), default=-1)
        if stream.get("recommended_intent") == "find_security_relevant_artifacts":
            add(
                "find_security_relevant_artifacts",
                {"protocol": protocol, "stream_id": stream_id if stream_id >= 0 else None},
                stream.get("reason") or "Security-relevant cleartext protocol indicators observed.",
            )
        elif stream.get("recommended_intent") == "anomaly_detection":
            add(
                "anomaly_detection",
                {"protocol": protocol, "stream_id": stream_id if stream_id >= 0 else None},
                stream.get("reason") or "Error or anomaly signal observed in stream.",
            )

    for service in services:
        protocol = _none_if_empty(service.get("protocol_hint"))
        if protocol:
            add(
                "investigate_protocol",
                {"protocol": protocol, "host": service.get("host"), "port": service.get("port")},
                f"{protocol.upper()} traffic observed on {service.get('host')}:{service.get('port')}.",
            )
    return recommendations[:10]


def survey_protocol_hint(value: Mapping[str, Any]) -> str | None:
    """Return the legacy protocol hint from protocols or destination port."""

    protocols = value.get("protocols") or []
    if isinstance(protocols, set):
        protocols = sorted(protocols)
    for protocol in reversed([str(item).lower() for item in protocols]):
        if protocol in {"ftp", "http", "dns", "tls", "ssl"}:
            return "tls" if protocol == "ssl" else protocol
        if protocol in mail.MAIL_PROTOCOLS:
            return protocol
    port = _none_if_empty(value.get("dst_port"))
    return well_known_protocol_for_port(port)


def well_known_protocol_for_port(port: Any) -> str | None:
    """Return the legacy protocol hint for well-known service ports."""

    direct_hints = {
        "21": "ftp",
        "53": "dns",
        "80": "http",
        "443": "tls",
    }
    return direct_hints.get(str(port or "").strip()) or mail.well_known_mail_protocol_for_port(port)


def survey_reason_and_intent(
    protocol_hint: str | None,
    signals: list[str],
) -> tuple[str, str]:
    """Return the legacy survey reason and follow-up intent for stream signals."""

    if any(signal.endswith("_auth_command") or signal == "cleartext_ftp_auth_command" for signal in signals):
        return (
            f"{(protocol_hint or 'cleartext protocol').upper()} authentication command observed.",
            "find_security_relevant_artifacts",
        )
    if any(signal in {"http_error_status", "dns_error_rcode", "tls_alert", "tcp_analysis_warning", "icmp_message"} for signal in signals):
        return ("Error, alert, or transport analysis signal observed.", "anomaly_detection")
    if protocol_hint in {"ftp", "smtp", "pop", "imap"}:
        return (
            f"{protocol_hint.upper()} is a cleartext-capable application protocol.",
            "find_security_relevant_artifacts",
        )
    return ("Application protocol traffic observed.", "investigate_protocol")


def natural_sort_key(value: Any) -> tuple[int, str]:
    """Return the legacy numeric-first sort key for stream identifiers."""

    text = str(value or "")
    try:
        return (int(text), text)
    except ValueError:
        return (0, text)


_parse_profile_field_summary = parse_profile_field_summary
_survey_row_signals = survey_row_signals
_survey_services = survey_services
_survey_interesting_streams = survey_interesting_streams
_survey_recommended_next_queries = survey_recommended_next_queries
_survey_protocol_hint = survey_protocol_hint
_well_known_protocol_for_port = well_known_protocol_for_port
_survey_reason_and_intent = survey_reason_and_intent
_natural_sort_key = natural_sort_key

__all__ = (
    "natural_sort_key",
    "parse_profile_field_summary",
    "survey_interesting_streams",
    "survey_protocol_hint",
    "survey_reason_and_intent",
    "survey_recommended_next_queries",
    "survey_row_signals",
    "survey_services",
    "well_known_protocol_for_port",
)
