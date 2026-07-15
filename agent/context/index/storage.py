"""On-disk storage helpers for chunk manifests.

Stores and loads chunk manifests as JSONL files under a per-run index dir.
Implements atomic writes (tmp -> rename), fsync for durability, incremental
append updates, and a simple compaction routine to deduplicate entries.
"""

from __future__ import annotations

import json
import os
from glob import glob
from pathlib import Path
from typing import Iterable, List
import hashlib
import re

from agent.context.index.schemas import Chunk


class ChunkStorage:
    def __init__(self, index_dir: str) -> None:
        self.index_dir = Path(index_dir)
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # best-effort; reading may still work if dir exists
            pass

    def list_manifests(self) -> List[Path]:
        pattern = str(self.index_dir / "chunks_*.jsonl")
        files = [Path(p) for p in glob(pattern)]
        # deterministic order: by name
        files.sort(key=lambda p: p.name)
        return files

    def load_all(self) -> List[Chunk]:
        chunks: List[Chunk] = []
        for mf in self.list_manifests():
            try:
                with mf.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            chunks.append(Chunk(**data))
                        except Exception:
                            continue
            except Exception:
                continue
        return chunks

    def write_manifest(self, run_id: str, chunks: List[Chunk]) -> None:
        """Write a complete manifest for a run. Usually called by ingestor.

        Provided here for completeness to support potential future callers.
        """
        tmp = self.index_dir / f"chunks_{run_id}.jsonl.tmp"
        final = self.index_dir / f"chunks_{run_id}.jsonl"
        with tmp.open("w", encoding="utf-8") as f:
            for ch in chunks:
                f.write(json.dumps(ch.model_dump(), ensure_ascii=False) + "\n")
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                # best-effort; fsync not available on some systems
                pass
        os.replace(tmp, final)

    def append_manifest(self, run_id: str, chunks: Iterable[Chunk]) -> None:
        """Append new chunk records to the manifest for a run (incremental update).

        Ensures data is flushed and fsynced for durability.
        """
        final = self.index_dir / f"chunks_{run_id}.jsonl"
        # Ensure file exists
        if not final.exists():
            # Use write_manifest to create atomically
            self.write_manifest(run_id, list(chunks))
            return
        with final.open("a", encoding="utf-8") as f:
            for ch in chunks:
                f.write(json.dumps(ch.model_dump(), ensure_ascii=False) + "\n")
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass

    def compact(self, run_id: str) -> int:
        """Deduplicate entries in run manifest and rewrite atomically.

        Returns the number of unique chunks written.
        """
        final = self.index_dir / f"chunks_{run_id}.jsonl"
        if not final.exists():
            return 0
        # Deduplicate by normalized content (not just chunk_id) to collapse
        # stdout vs file duplicates that contain identical semantic text.
        seen_content: set[str] = set()
        uniq: List[Chunk] = []
        try:
            with final.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        txt = str(data.get("text") or "")
                        # Normalize content like index (strip tool prefix, align Nmap XML)
                        norm = self._normalize_for_dedupe(txt)
                        key = hashlib.sha1(norm.encode("utf-8")).hexdigest()
                        if key in seen_content:
                            continue
                        seen_content.add(key)
                        uniq.append(Chunk(**data))
                    except Exception:
                        continue
        except Exception:
            return 0
        self.write_manifest(run_id, uniq)
        return len(uniq)

    # Keep normalization logic local to storage for independence
    def _normalize_for_dedupe(self, text: str) -> str:
        try:
            s = text.replace("\r\n", "\n").replace("\r", "\n")
            s = re.sub(r"^\[[^\]]+\]\s*", "", s, count=1)
            idx = s.find("<nmaprun")
            if idx >= 0:
                s = s[idx:]
            return s.strip()
        except Exception:
            return text.strip()
