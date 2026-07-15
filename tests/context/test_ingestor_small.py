from pathlib import Path

from agent.context.chunking.artifact_ingestor import SimpleArtifactIngestor


def test_ingest_small_gobuster(tmp_path: Path):
    # Create small gobuster-like artifact
    content = """/admin Status: 403\n/index Status: 200\n/api Status: 302\n"""
    art = tmp_path / "gobuster.txt"
    art.write_text(content, encoding="utf-8")

    ing = SimpleArtifactIngestor(index_dir=str(tmp_path), max_chunk_tokens=200)
    chunks = ing.ingest("run1", str(art), tool_name="gobuster", meta={"cli": "gobuster dir"})
    assert chunks, "no chunks returned"
    # Stable IDs on repeat
    chunks2 = ing.ingest("run1", str(art), tool_name="gobuster", meta={"cli": "gobuster dir"})
    assert [c.id for c in chunks] == [c.id for c in chunks2]
    # Extracted metadata present
    metas = [c.meta for c in chunks]
    assert any(m.get("url_path") == "/index" for m in metas)
    # Derived fields populated where applicable
    assert any(m.get("status_class") in {"2xx", "3xx", "4xx", "5xx"} for m in metas)
    # Group key attached if profile group_by present
    assert any("group_key" in m for m in metas)

