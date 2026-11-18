"""dprox-query: configuration that refuses to guess, and the version gate.

The transport is stubbed with httpx.MockTransport. What is not stubbed is the
decision about access: there is nothing here to stub, because this tool makes
none — which is itself worth a test (`test_ask_sends_only_the_question`).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner
from dprox_query import cli as cli_mod
from dprox_query import client as client_mod
from dprox_query.cli import cli
from dprox_query.config import ConfigError, load_config, missing_files
from dprox_query.version import UnreadableVersion, parse_version, supports

# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def cert_files(tmp_path: Path) -> dict[str, Path]:
    files = {}
    for name in ("ca.crt", "client.crt", "client.key"):
        path = tmp_path / name
        path.write_text("not a real certificate", encoding="utf-8")
        files[name] = path
    return files


@pytest.fixture
def config_file(tmp_path: Path, cert_files: dict[str, Path]) -> Path:
    target = tmp_path / "config.yml"
    target.write_text(
        yaml.safe_dump(
            {
                "endpoint": "https://dprox.test:8443",
                "ca_cert_path": str(cert_files["ca.crt"]),
                "client_cert_path": str(cert_files["client.crt"]),
                "client_key_path": str(cert_files["client.key"]),
            }
        ),
        encoding="utf-8",
    )
    return target


def _write(target: Path, **changes) -> Path:
    data = yaml.safe_load(target.read_text())
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return target


# --- configuration ------------------------------------------------------------


def test_config_loads(config_file: Path) -> None:
    config = load_config(config_file)
    assert config.endpoint == "https://dprox.test:8443"
    assert config.timeout_seconds == 30
    assert missing_files(config) == []


def test_a_missing_config_names_the_file_it_looked_for(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nothing-here.yml")


@pytest.mark.parametrize(
    "key", ["endpoint", "ca_cert_path", "client_cert_path", "client_key_path"]
)
def test_every_required_value_is_required(config_file: Path, key: str) -> None:
    """None of the four has a default: a guessed address or key is silent."""
    _write(config_file, **{key: None})
    with pytest.raises(ConfigError, match=key):
        load_config(config_file)


def test_a_blank_value_counts_as_missing(config_file: Path) -> None:
    _write(config_file, endpoint="   ")
    with pytest.raises(ConfigError, match="endpoint"):
        load_config(config_file)


def test_a_key_the_tool_does_not_read_is_refused(config_file: Path) -> None:
    """A setting nobody honours must not sit in the file looking as if it is."""
    _write(config_file, verify_tls=False)
    with pytest.raises(ConfigError, match="verify_tls"):
        load_config(config_file)


def test_a_plain_http_endpoint_is_refused(config_file: Path) -> None:
    _write(config_file, endpoint="http://dprox.test:8443")
    with pytest.raises(ConfigError, match="https"):
        load_config(config_file)


def test_a_missing_certificate_file_is_named_before_any_connection(
    config_file: Path, cert_files: dict[str, Path]
) -> None:
    cert_files["client.key"].unlink()
    config = load_config(config_file)
    gone = missing_files(config)
    assert len(gone) == 1
    assert gone[0].startswith("client_key_path:")


def test_env_var_selects_the_config(monkeypatch, config_file: Path) -> None:
    monkeypatch.setenv("DPROX_QUERY_CONFIG", str(config_file))
    assert load_config().endpoint == "https://dprox.test:8443"


# --- the version gate ---------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "ok"),
    [
        ("0.3.0", True),
        ("0.3.7", True),
        ("0.3.0-rc1", True),
        ("v0.3.1", True),
        ("0.2.0", False),
        ("0.4.0", False),
        ("1.0.0", False),
    ],
)
def test_supported_versions(version: str, ok: bool) -> None:
    assert supports(version) is ok


@pytest.mark.parametrize("version", ["0.3", "", "latest", "0.3.x"])
def test_an_unreadable_version_is_an_error_not_a_guess(version: str) -> None:
    """Treating it as old or as new would both be silent when wrong."""
    with pytest.raises(UnreadableVersion):
        parse_version(version)


def test_the_gate_refuses_an_old_server_with_both_versions_named() -> None:
    with pytest.raises(client_mod.IncompatibleServer) as exc:
        client_mod.check_compatible("0.2.0")
    assert "0.2.0" in str(exc.value)
    assert "0.3.0" in str(exc.value)


# --- talking to dprox ---------------------------------------------------------


def _transport(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://dprox.test:8443", transport=httpx.MockTransport(handler)
    )


def _ok_handler(
    *, version: str = "0.3.0", results: list | None = None, metadata: dict | None = None
):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["last"] = request
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": version, "image": "x"})
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "orgs": ["arc", "job"]})
        if request.url.path == "/v1/query":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "results": results if results is not None else [],
                    "metadata": metadata
                    or {"agent": "a", "orgs_applied": ["arc"], "result_count": 0},
                },
            )
        return httpx.Response(404, json={"error": "not_found", "message": "no"})

    return handler, seen


def test_ask_sends_only_the_question() -> None:
    """No group, no org, no identity. There is no parameter for one."""
    handler, seen = _ok_handler()
    with _transport(handler) as c:
        client_mod.ask(c, "what is the wage policy", limit=3)

    assert seen["body"] == {"query": "what is the wage policy", "limit": 3}


def test_ask_omits_the_limit_when_not_given() -> None:
    """dprox's own default_limit applies; the tool does not supply one."""
    handler, seen = _ok_handler()
    with _transport(handler) as c:
        client_mod.ask(c, "x")

    assert seen["body"] == {"query": "x"}


def test_an_error_body_from_dprox_is_reported_with_its_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": "unknown_agent", "message": "not in the plan"}
        )

    with _transport(handler) as c, pytest.raises(client_mod.QueryError) as exc:
        client_mod.ask(c, "x")

    assert "unknown_agent" in str(exc.value)
    assert "not in the plan" in str(exc.value)


# --- the commands -------------------------------------------------------------


def _run(args, monkeypatch, handler, config_file: Path):
    # cli.py imported build_client by name, so the name in cli's namespace is
    # the one to replace.
    monkeypatch.setattr(cli_mod, "build_client", lambda _config: _transport(handler))
    monkeypatch.setenv("DPROX_QUERY_CONFIG", str(config_file))
    return CliRunner().invoke(cli, args)


def test_ask_prints_each_passage_with_its_org_and_source(
    monkeypatch, config_file: Path
) -> None:
    handler, _seen = _ok_handler(
        results=[
            {
                "text": "the wage policy is this",
                "org": "arc",
                "source_path_rel": "hr/wages.docx",
                "score": 0.912,
                "chunk_index": 0,
                "chunk_total": 2,
                "classification_group": "arc_g1_eng",
            }
        ],
        metadata={"agent": "a", "orgs_applied": ["arc"], "result_count": 1},
    )
    result = _run(["ask", "wage", "policy"], monkeypatch, handler, config_file)

    assert result.exit_code == 0, result.output
    assert "arc:hr/wages.docx" in result.output
    assert "passage 1/2" in result.output
    assert "the wage policy is this" in result.output
    assert "0.912" in result.output


def test_ask_says_which_orgs_were_searched_when_nothing_matched(
    monkeypatch, config_file: Path
) -> None:
    """An empty answer is not an error, and it must not read like a fault."""
    handler, _seen = _ok_handler(
        results=[], metadata={"agent": "a", "orgs_applied": ["arc", "job"]}
    )
    result = _run(["ask", "x"], monkeypatch, handler, config_file)

    assert result.exit_code == 0
    assert "No passages matched" in result.output
    assert "arc, job" in result.output


def test_ask_json_prints_what_dprox_returned(monkeypatch, config_file: Path) -> None:
    handler, _seen = _ok_handler(
        results=[{"text": "t", "org": "arc"}],
        metadata={"agent": "a", "orgs_applied": ["arc"]},
    )
    result = _run(["ask", "--json", "x"], monkeypatch, handler, config_file)

    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["results"][0]["org"] == "arc"


def test_ask_against_an_old_dprox_exits_three(monkeypatch, config_file: Path) -> None:
    """The gate runs on every question, not only in `check`."""
    handler, _seen = _ok_handler(version="0.2.0")
    result = _run(["ask", "x"], monkeypatch, handler, config_file)

    assert result.exit_code == 3
    assert "0.2.0" in result.output


def test_a_missing_config_exits_two(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DPROX_QUERY_CONFIG", str(tmp_path / "absent.yml"))
    result = CliRunner().invoke(cli, ["ask", "x"])

    assert result.exit_code == 2
    assert "not found" in result.output


def test_check_reports_config_server_and_health(
    monkeypatch, config_file: Path
) -> None:
    handler, _seen = _ok_handler()
    result = _run(["check"], monkeypatch, handler, config_file)

    assert result.exit_code == 0, result.output
    assert "[OK]   config" in result.output
    assert "[OK]   dprox           0.3.0" in result.output
    assert "[OK]   compatibility" in result.output
    assert "orgs: arc, job" in result.output


def test_check_against_an_old_dprox_exits_three(
    monkeypatch, config_file: Path
) -> None:
    handler, _seen = _ok_handler(version="0.2.0")
    result = _run(["check"], monkeypatch, handler, config_file)

    assert result.exit_code == 3
    assert "[FAIL] compatibility" in result.output


def test_check_reports_a_degraded_dprox_as_a_failure(
    monkeypatch, config_file: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "0.3.0"})
        return httpx.Response(503, json={"status": "degraded", "orgs": ["arc"]})

    result = _run(["check"], monkeypatch, handler, config_file)

    assert result.exit_code == 1
    assert "[FAIL] health" in result.output
