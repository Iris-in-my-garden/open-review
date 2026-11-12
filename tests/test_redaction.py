from openreview.review.redaction import redact_many, redact_text


def test_redacts_private_key_block():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890\n"
        "-----END RSA PRIVATE KEY-----"
    )

    redacted, count = redact_text(text)

    assert count == 1
    assert "BEGIN RSA PRIVATE KEY" not in redacted
    assert "<REDACTED:PRIVATE_KEY>" in redacted


def test_redacts_well_known_cloud_and_vcs_tokens():
    text = " ".join(
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
            "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
            "sk-1234567890abcdefghijklmnopqrstuvwxyz",
            "AIzaSyA1234567890abcdefghijklmnopqrstuv",
            "xoxb-1234567890-abcdefghij",
        ]
    )

    redacted, count = redact_text(text)

    assert count == 6
    for leaked in (
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        "github_pat_",
        "sk-1234567890",
        "AIzaSyA",
        "xoxb-",
    ):
        assert leaked not in redacted


def test_redacts_jwt_and_bearer_token():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    text = f"Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\njwt={jwt}"

    redacted, count = redact_text(text)

    assert count == 2
    assert jwt not in redacted
    assert "abcdefghijklmnopqrstuvwxyz012345" not in redacted


def test_redacts_long_credential_assignment_but_keeps_short_values():
    text = 'password = "hunter2"\napi_key = "sk-live-1234567890abcdef"'

    redacted, count = redact_text(text)

    # 短值不是高置信度凭证，保留代码语义；长赋值必须被抹掉。
    assert 'password = "hunter2"' in redacted
    assert "sk-live-1234567890abcdef" not in redacted
    assert count >= 1


def test_redacts_url_credentials_and_internal_ips():
    text = "postgres://user:secret@db.internal:5432/x 10.1.2.3 192.168.0.8"

    redacted, count = redact_text(text)

    assert "user:secret@" not in redacted
    assert "<REDACTED:URL_CREDENTIALS>@" in redacted
    assert "10.1.2.3" not in redacted
    assert "192.168.0.8" not in redacted
    assert count == 3


def test_leaves_ordinary_code_untouched():
    text = "def add(a, b):\n    return a + b\n# version 1.2.3"

    redacted, count = redact_text(text)

    assert redacted == text
    assert count == 0


def test_redact_many_handles_none_and_counts_total():
    values, count = redact_many(
        [None, "ghp_1234567890abcdefghijklmnopqrstuvwxyz"]
    )

    assert values[0] == ""
    assert "ghp_" not in values[1]
    assert count == 1
