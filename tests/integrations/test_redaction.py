"""Tests for redact() - the function every capability's output passes
through before it can reach agent context (C6)."""

from integrations.redaction import redact


def test_authorization_bearer_header():
    out = redact("POST /x Authorization: Bearer sk-sandbox-placeholder HTTP/1.1")
    assert "sk-sandbox-placeholder" not in out
    assert "<redacted>" in out
    assert "Authorization: Bearer" in out  # header shape survives


def test_bare_bearer_token():
    out = redact("upstream reply: Bearer abcdef12345")
    assert "abcdef12345" not in out


def test_sk_token_anywhere_in_string():
    out = redact("call failed using key sk-sandbox-placeholder at line 4")
    assert "sk-sandbox-placeholder" not in out


def test_well_known_token_shapes():
    for token in (
        "ghp_0123456789abcdefghij",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuv",
        "glpat-0123456789abcdefghij",
        "xoxb-1234567890-abcdef",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0In0.signaturepart",
    ):
        assert token not in redact(f"saw {token} here"), token


def test_sensitive_keys_at_any_depth():
    doc = {
        "level1": {
            "level2": {
                "api_key": "abc123",
                "PASSWORD": "hunter2",
                "db_token": "tok",
                "clientSecret": "s3cr3t",
                "credential": "cred",
                "harmless": "kept",
            }
        }
    }
    out = redact(doc)
    inner = out["level1"]["level2"]
    for k in ("api_key", "PASSWORD", "db_token", "clientSecret", "credential"):
        assert inner[k] == "<redacted>", k
    assert inner["harmless"] == "kept"


def test_sensitive_key_inside_list():
    out = redact({"items": [{"token": "x"}, {"name": "y"}]})
    assert out["items"][0]["token"] == "<redacted>"
    assert out["items"][1]["name"] == "y"


def test_connection_string_credentials():
    out = redact({"dsn": "postgres://helix:secretpw@db.internal:5432/helix"})
    assert "secretpw" not in out["dsn"]
    assert "postgres://<redacted>@db.internal:5432/helix" == out["dsn"]


def test_plain_url_without_credentials_survives():
    url = "http://downstream-stub.payment-api.svc.cluster.local"
    assert redact(url) == url


def test_kubernetes_secret_data_values():
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "payment-api-secret", "namespace": "payment-api"},
        "type": "Opaque",
        "data": {"PAYMENT_API_API_KEY": "c2stc2FuZGJveC1wbGFjZWhvbGRlcg=="},
        "stringData": {"EXTRA": "plainvalue"},
    }
    out = redact(secret)
    assert out["data"]["PAYMENT_API_API_KEY"] == "<redacted>"
    assert out["stringData"]["EXTRA"] == "<redacted>"
    # key names and non-secret metadata survive
    assert list(out["data"].keys()) == ["PAYMENT_API_API_KEY"]
    assert out["metadata"]["name"] == "payment-api-secret"
    assert "c2stc2FuZGJveC1wbGFjZWhvbGRlcg==" not in str(out)


def test_configmap_data_is_not_secret():
    # ConfigMaps legitimately carry config values; only kind: Secret gets
    # the data/stringData treatment.
    cm = {"kind": "ConfigMap", "data": {"LOG_LEVEL": "debug"}}
    assert redact(cm)["data"]["LOG_LEVEL"] == "debug"


def test_metadata_name_containing_secret_survives():
    # "secret" in a VALUE is fine - only matching dict keys are redacted.
    out = redact({"name": "payment-api-secret"})
    assert out["name"] == "payment-api-secret"


def test_scalars_and_none_passthrough():
    assert redact(None) is None
    assert redact(42) == 42
    assert redact(True) is True
    assert redact("plain text") == "plain text"


def test_nested_mixed_structure():
    doc = {
        "pods": [
            {"spec": {"containers": [{"env": [{"name": "K", "value": "v"}]}]}},
        ],
        "headers": {"Authorization": "Bearer tok123456"},
    }
    out = redact(doc)
    # "Authorization" is not a sensitive key name, but its Bearer value is
    # scrubbed by the string patterns.
    assert out["headers"]["Authorization"] == "Bearer <redacted>"
    assert out["pods"][0]["spec"]["containers"][0]["env"][0]["value"] == "v"
