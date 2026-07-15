"""Artifact ingestion pipeline.

Detects tool/profile, splits artifacts into semantic chunks using universal
heuristics (and profiles in future), computes digests and token counts, and
returns Chunk objects for indexing. Also persists a simple manifest per run.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import re
import time
import logging

from agent.context.index.schemas import Chunk, IngestionError
from agent.context.chunking.universal_splitters import split_text_to_chunks
from agent.context.chunking.chunk_rules import (
    load_profile_for_tool,
    validate_profile,
    compile_profile,
    apply_extractors,
    group_key_for,
    render_group_summary,
)
from agent.context.token_utils import count_tokens
from agent.context.index.storage import ChunkStorage
from agent.context.chunking.redactor import ArtifactRedactor, is_redaction_enabled
from runtime_shared.durable_secret_masking import mask_durable_secrets


class SimpleArtifactIngestor:
    """Lightweight ingestor implementation matching the ArtifactIngestor Protocol."""

    def __init__(
        self,
        *,
        profiles_dir: Optional[str] = None,
        index_dir: Optional[str] = None,
        max_chunk_tokens: int = 800,
    ) -> None:
        self.profiles_dir = profiles_dir
        self.index_dir = index_dir
        self.max_chunk_tokens = max_chunk_tokens
        self._log = logging.getLogger(__name__)
        # Initialize optional redactor
        try:
            self._redactor = ArtifactRedactor()
        except Exception:
            self._redactor = None

    def _stable_chunk_id(self, artifact_path: str, start: int, end: int) -> str:
        raw = f"{artifact_path}|{start}|{end}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()

    def _persist_manifest(self, run_id: str, chunks: List[Chunk]) -> None:
        if not self.index_dir:
            return
        run_index_dir = Path(self.index_dir)
        run_index_dir.mkdir(parents=True, exist_ok=True)
        storage = ChunkStorage(str(run_index_dir))
        # Incremental append for efficiency; storage ensures durability
        storage.append_manifest(run_id, chunks)

    def ingest(
        self,
        run_id: str,
        artifact_path: str,
        tool_name: str,
        meta: Dict[str, Any] | None = None,
    ) -> List[Chunk]:
        t0 = time.perf_counter()
        meta = meta or {}
        if not os.path.exists(artifact_path):
            raise IngestionError(f"artifact not found: {artifact_path}")
        try:
            self._log.info(
                "artifact_ingest:start run_id=%s tool=%s path=%s",
                run_id,
                tool_name,
                artifact_path,
            )
        except Exception:
            pass
        with open(artifact_path, "rb") as f:
            raw = f.read()
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:  # pragma: no cover
            raise IngestionError(f"failed to decode artifact: {e}")

        # Detect very simple formats to improve boundary behavior if needed
        is_xml = text.lstrip().startswith("<?xml")

        # Tool type detection via metadata or content hints
        tool_detected = (tool_name or "").strip().lower()
        if not tool_detected or tool_detected in {"unknown", "misc", "scan"}:
            cli = str(meta.get("cli", "")).lower()
            if cli:
                parts = cli.split()
                if parts:
                    tool_detected = parts[0]
        if not tool_detected:
            low = text.lower()
            if "<nmaprun" in low or " nmap " in low or low.startswith("nmap "):
                tool_detected = "nmap"
            elif "gobuster" in low:
                tool_detected = "gobuster"
            elif "sqlmap" in low:
                tool_detected = "sqlmap"
            elif "nuclei" in low:
                tool_detected = "nuclei"
            elif "masscan" in low:
                tool_detected = "masscan"
        if not tool_detected:
            tool_detected = tool_name or meta.get("tool", "") or "unknown"

        # Load YAML profile if available
        profile = None
        prof_dir = self.profiles_dir or str(Path(__file__).parent / "profiles")
        try:
            if prof_dir and os.path.isdir(prof_dir):
                profile = load_profile_for_tool(prof_dir, tool_detected)
                if profile:
                    validate_profile(profile)
        except Exception as e:
            profile = None
            try:
                self._log.warning("profile_load_failed tool=%s error=%s", tool_detected, e)
            except Exception:
                pass

        # Allow profile to override target max tokens
        max_tokens = self.max_chunk_tokens
        compiled = None
        if profile and isinstance(profile.get("chunk"), list):
            try:
                for rule in profile["chunk"]:
                    if isinstance(rule, dict) and "max_tokens" in rule:
                        max_tokens = int(rule["max_tokens"]) or max_tokens
                        break
            except Exception:
                pass
        # Pre-compile profile for extract/group/summarize if present
        if profile:
            try:
                compiled = compile_profile(profile)
            except Exception as e:
                compiled = None
                try:
                    self._log.warning("profile_compile_failed tool=%s error=%s", tool_detected, e)
                except Exception:
                    pass

        # Split using universal heuristics (profile grouping can be layered later)
        ranges = split_text_to_chunks(text, tool=tool_detected, max_tokens=max_tokens)
        chunks: List[Chunk] = []
        for start, end in ranges:
            excerpt = text.encode("utf-8")[start:end].decode("utf-8", errors="replace")
            # Optional redaction with same-length masking to preserve offsets semantics
            try:
                if is_redaction_enabled() and self._redactor is not None:
                    excerpt = self._redactor.redact_equal_len(excerpt)
            except Exception:
                pass
            durable_excerpt = str(
                mask_durable_secrets(excerpt, source="artifact_chunk_text")
            )
            # Minimal metadata extraction for common tools
            meta_extra: Dict[str, Any] = {}
            try:
                if is_xml:
                    # nmap XML common bits
                    m_ip = re.search(r"<address[^>]*addr=\"([^\"]+)\"[^>]*addrtype=\"ipv4\"", excerpt)
                    if m_ip:
                        meta_extra["ip"] = m_ip.group(1)
                    m_host = re.search(r"<hostname[^>]*name=\"([^\"]+)\"", excerpt)
                    if m_host:
                        meta_extra["host"] = m_host.group(1)
                else:
                    # generic URL/host sniffs
                    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", excerpt)
                    if m:
                        meta_extra["ip"] = m.group(1)
                # Apply compiled extractors if available
                if compiled:
                    try:
                        meta_extra.update(apply_extractors(compiled, excerpt))
                    except Exception:
                        pass
                # Special handling for gobuster-style outputs within a chunk: prefer a 2xx/3xx finding
                if (tool_detected or "").lower() == "gobuster":
                    try:
                        findings = re.findall(r"^(\/\S+)\s+Status:\s*(\d+)", excerpt, flags=re.MULTILINE)
                        if findings:
                            # Prefer 2xx, then 3xx, otherwise first
                            def _pick(items):
                                for pref in ("2", "3"):
                                    for pth, code in items:
                                        if code and code[0] == pref:
                                            return pth, code
                                return items[0]
                            chosen_path, chosen_status = _pick(findings)
                            meta_extra["url_path"] = chosen_path
                            try:
                                meta_extra["status"] = int(chosen_status)
                            except Exception:
                                meta_extra["status"] = meta_extra.get("status") or chosen_status
                    except Exception:
                        pass
            except Exception as e:
                try:
                    self._log.debug("meta_extraction_failed error=%s", e)
                except Exception:
                    pass
            # Normalize/augment common metadata fields
            try:
                if "status" in meta_extra and meta_extra.get("status"):
                    s = str(meta_extra.get("status"))
                    # derive status_class like 2xx, 3xx
                    if s.isdigit() and len(s) == 3:
                        meta_extra["status_class"] = f"{s[0]}xx"
                if "url_path" in meta_extra and meta_extra.get("url_path"):
                    up = str(meta_extra.get("url_path"))
                    # store depth for potential grouping/analysis
                    meta_extra["path_depth"] = max(0, up.count("/") - (0 if up.startswith("/") else 1))
            except Exception as e:
                try:
                    self._log.debug("meta_derivation_failed error=%s", e)
                except Exception:
                    pass
            durable_meta = mask_durable_secrets(
                {"tool": tool_detected, **meta, **meta_extra},
                source="artifact_chunk_metadata",
            )
            digest = durable_excerpt.splitlines()[0][:200] if durable_excerpt else ""
            ch = Chunk(
                id=self._stable_chunk_id(artifact_path, start, end),
                run_id=run_id,
                artifact_path=artifact_path,
                offset_start=start,
                offset_end=end,
                text=durable_excerpt,
                meta=durable_meta if isinstance(durable_meta, dict) else {},
                digest=digest,
                token_count=count_tokens(durable_excerpt),
            )
            chunks.append(ch)

        # Apply grouping and summarization if profile specifies group_by/summarize
        if compiled and compiled.group_by:
            # Assign group keys and stable sort to cluster groups
            for ch in chunks:
                try:
                    gk = group_key_for(compiled, ch.meta)
                    ch.meta["group_key"] = "|".join(gk) if gk else ""
                except Exception:
                    ch.meta["group_key"] = ""
            chunks.sort(key=lambda c: c.meta.get("group_key", ""))
            # Optional: compute group-level summaries and attach to meta
            if compiled.summarize_template:
                # Build groups
                from collections import defaultdict
                groups = defaultdict(list)
                for ch in chunks:
                    groups[ch.meta.get("group_key", "")].append(ch)
                for key, items in groups.items():
                    try:
                        summary = render_group_summary(compiled.summarize_template, [c.meta for c in items])
                        for c in items:
                            c.meta["group_summary"] = summary
                    except Exception:
                        continue

        self._persist_manifest(run_id, chunks)
        # Metrics-like log: ingestion stats
        try:
            dt_ms = int((time.perf_counter() - t0) * 1000)
            avg_tokens = int(sum(c.token_count for c in chunks) / max(len(chunks), 1)) if chunks else 0
            self._log.info(
                "artifact_ingest:done run_id=%s tool=%s path=%s chunks=%d avg_tokens=%d time_ms=%d",
                run_id,
                tool_detected,
                artifact_path,
                len(chunks),
                avg_tokens,
                dt_ms,
            )
        except Exception:
            pass
        return chunks
