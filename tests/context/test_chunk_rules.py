from agent.context.chunking.chunk_rules import compile_profile, apply_extractors, group_key_for, render_group_summary


def test_compile_and_apply_extractors_group_and_summary():
    profile = {
        "detect": {"cli": "gobuster"},
        "extract": [
            {"field": "url_path", "regex": r"^(/\S+)"},
            {"field": "status", "regex": r"Status:\s*(\d+)"},
        ],
        "chunk": [{"group_by": ["status"]}, {"max_tokens": 800}],
        "summarize": {"template": "{count} findings 2xx:{c2xx} 3xx:{c3xx} top:{top_paths}"},
    }
    rules = compile_profile(profile)
    txt = "/admin Status: 403\n"
    meta = apply_extractors(rules, txt)
    assert meta["url_path"] == "/admin"
    assert meta["status"] == "403"
    # group key uses requested fields
    gk = group_key_for(rules, meta)
    assert gk == ("403",)
    # render summary with minimal meta set
    summ = render_group_summary(profile["summarize"]["template"], [meta])
    assert "1 findings" in summ

