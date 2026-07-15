from agent.context.chunking.universal_splitters import boundary_score, split_text_to_chunks


def test_boundary_score_and_split_basic():
    text = (
        "[+] Starting scan\n"
        "2024-01-01 10:00:00 info boot\n"
        "GET /index.html\n\n"
        "-----\n"
        "<xml>data</xml>\n"
    )
    # Boundary score detects blank/header/timestamp/prefix
    assert boundary_score("") >= 1
    assert boundary_score("2024-05-05 00:00:00 starting") >= 1
    assert boundary_score("--- header ---") >= 1
    # Splitting produces >1 chunk for mixed boundaries
    chunks = split_text_to_chunks(text, tool="test", max_tokens=80, min_tokens=10)
    assert len(chunks) >= 2

