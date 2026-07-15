"""Unit tests for secret redaction used in context and candidate prompt preparation."""

from agent.context.chunking.redactor import ArtifactRedactor


def test_redactor_masks_but_preserves_length():
    r = ArtifactRedactor()
    s = "Authorization: Bearer abcDEF123456-xyz"  # length 29 after prefix
    out = r.redact_equal_len(s)
    assert "Authorization: Bearer " in out
    secret_in = s.split("Bearer ", 1)[1]
    secret_out = out.split("Bearer ", 1)[1]
    assert len(secret_in) == len(secret_out)
    assert secret_out != secret_in

    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvZSIsImlhdCI6MTUxNjIzOTAyMn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"  # gitleaks:allow
    out2 = r.redact_equal_len(jwt)
    assert len(jwt) == len(out2)
    assert out2 != jwt


def test_redactor_masks_x_api_key_header():
    r = ArtifactRedactor()
    s = "X-API-Key: SECRETKEY123456"
    out = r.redact_equal_len(s)
    assert "X-API-Key: " in out
    assert "SECRETKEY123456" not in out
