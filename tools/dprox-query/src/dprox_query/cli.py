"""dprox-query: ask dprox one question, and check that you can.

Exit codes:
    0  the question was answered, or the check passed
    1  the question was not answered (unreachable, refused, upstream fault)
    2  configuration is missing, unreadable or incomplete
    3  the dprox answering is outside the versions this tool reads
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from dprox_query.client import (
    IncompatibleServer,
    QueryError,
    ask,
    build_client,
    check_compatible,
    healthz,
    server_version,
)
from dprox_query.config import ConfigError, load_config, missing_files, resolve_config_path
from dprox_query.version import __version__, describe_support

_config_option = click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(),
    help="Path to config.yml. Falls back to DPROX_QUERY_CONFIG, then "
    "~/.config/dprox-query/config.yml.",
)


def _fail(message: str, code: int) -> None:
    click.echo(message, err=True)
    sys.exit(code)


def _load_or_exit(config_path: str | None):
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        _fail(str(exc), 2)
    gone = missing_files(config)
    if gone:
        _fail(
            "configured file(s) not found:\n  " + "\n  ".join(gone),
            2,
        )
    return config


def _format_result(index: int, hit: dict[str, Any]) -> str:
    """One passage: where it came from, then what it says."""
    org = hit.get("org") or "?"
    source = hit.get("source_path_rel") or "(source not recorded)"
    score = hit.get("score")
    chunk_index = hit.get("chunk_index")
    chunk_total = hit.get("chunk_total")
    part = ""
    if chunk_index is not None and chunk_total:
        part = f" passage {chunk_index + 1}/{chunk_total}"
    head = f"[{index}] {org}:{source}{part}"
    if isinstance(score, (int, float)):
        head += f"  score {score:.3f}"
    text = (hit.get("text") or "").strip()
    body = "\n".join(f"    {line}" for line in text.splitlines())
    return f"{head}\n{body}"


@click.group(help="dprox-query — ask dprox one question.")
@click.version_option(__version__, message="%(version)s")
def cli() -> None:
    pass


@cli.command("ask", help="Ask one question and print the passages.")
@_config_option
@click.option("--limit", type=int, default=None, help="How many passages to ask for.")
@click.option("--json", "as_json", is_flag=True, help="Print dprox's answer as JSON.")
@click.argument("question", nargs=-1, required=True)
def ask_command(
    config_path: str | None, limit: int | None, as_json: bool, question: tuple[str, ...]
) -> None:
    config = _load_or_exit(config_path)
    text = " ".join(question).strip()
    if not text:
        _fail("the question is empty", 2)

    try:
        with build_client(config) as client:
            check_compatible(server_version(client))
            answer = ask(client, text, limit)
    except IncompatibleServer as exc:
        _fail(str(exc), 3)
    except QueryError as exc:
        _fail(str(exc), 1)

    if as_json:
        click.echo(
            json.dumps({"results": answer.results, "metadata": answer.metadata}, indent=2)
        )
        return

    if not answer.results:
        # Not an error. dprox answered; nothing you may see matched.
        orgs = ", ".join(answer.metadata.get("orgs_applied") or []) or "none"
        click.echo(f"No passages matched. Orgs searched: {orgs}.")
        return

    for index, hit in enumerate(answer.results, start=1):
        click.echo(_format_result(index, hit))
        click.echo()

    orgs = ", ".join(answer.metadata.get("orgs_applied") or []) or "none"
    click.echo(f"{len(answer.results)} passage(s) from org(s): {orgs}.")


@cli.command("check", help="Check the configuration, the certificate and the server.")
@_config_option
def check_command(config_path: str | None) -> None:
    """The command agent-skeleton asked for, to run in an image build or a seat.

    It says which file it read, which certificate it will present, which dprox
    answered and whether this tool can read it. Every line is a fact someone
    can act on.
    """
    resolved = resolve_config_path(config_path)
    config = _load_or_exit(config_path)
    click.echo(f"[OK]   config          {resolved}")
    click.echo(f"[OK]   certificate     {config.client_cert_path}")
    click.echo(f"[OK]   endpoint        {config.endpoint}")

    ok = True
    try:
        with build_client(config) as client:
            version = server_version(client)
            click.echo(f"[OK]   dprox           {version}")
            try:
                check_compatible(version)
                click.echo(f"[OK]   compatibility   {describe_support()}")
            except IncompatibleServer as exc:
                click.echo(f"[FAIL] compatibility   {exc}", err=True)
                sys.exit(3)

            status, body = healthz(client)
            if status == 200:
                orgs = ", ".join(body.get("orgs") or []) or "none"
                click.echo(f"[OK]   health          ok; orgs: {orgs}")
            else:
                ok = False
                click.echo(
                    f"[FAIL] health          {body.get('status', status)}", err=True
                )
    except QueryError as exc:
        _fail(f"[FAIL] dprox           {exc}", 1)

    sys.exit(0 if ok else 1)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
