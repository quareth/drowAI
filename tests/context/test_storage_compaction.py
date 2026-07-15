from agent.context.index.storage import ChunkStorage
from agent.context.index.schemas import Chunk


def test_append_and_compact(tmp_path):
    storage = ChunkStorage(str(tmp_path))
    run_id = "r1"
    ch1 = Chunk(id="x1", run_id=run_id, artifact_path="/a", offset_start=0, offset_end=1, text="a", meta={}, digest="a", token_count=1)
    ch2 = Chunk(id="x1", run_id=run_id, artifact_path="/a", offset_start=0, offset_end=1, text="a", meta={}, digest="a", token_count=1)
    storage.append_manifest(run_id, [ch1, ch2])
    # compaction should leave a single unique record
    n = storage.compact(run_id)
    assert n == 1

