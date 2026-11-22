"""The commands the operations documents show parse, and the container first run
is possible as written (H11, OPS-3).

``docs/DOCKER.md`` told an operator to run ``athena-backup`` with one argument;
the command needs two and stops with a usage error. Nothing checked it, because
nothing reads the operations documents' commands. The first test finds every
``athena-*`` invocation in a shell block of ``docs/DOCKER.md`` and
``docs/OPERATIONS.md`` (directly, through ``docker compose exec``/``docker
exec``, or as the arguments ``docker compose run`` hands the image's entry
point) and parses its arguments with that command's own ``argparse`` parser.
Nothing is executed: the parser's ``parse_args`` is replaced for the call by one
that parses for real and then stops the command before it does any work.

The other tests hold the container first run to what the image and compose file
can do, read rather than run (no container runs in this suite):

- ``compose.yaml`` forwards ``ATHENA_BOOTSTRAP_TOKEN`` from the shell by naming
  it without a value. Compose passes only the variables a service lists, so an
  unlisted token never reached the bootstrap run its own comment describes; a
  ``${ATHENA_BOOTSTRAP_TOKEN}`` value would instead set it to an empty string
  when the shell has none.
- ``docs/DOCKER.md`` runs nothing in the container the image lacks (the
  runtime stage installs Python and the Athena wheel, no ``curl``), and nothing
  on the host that would need a published port (``compose.yaml`` publishes
  none).
- The health endpoint ``docs/DOCKER.md`` says the image's ``HEALTHCHECK`` asks is
  the one the Dockerfile asks.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
import contextlib
import importlib
import io
import json
from pathlib import Path
import re
import shlex
import tomllib
from typing import Any
from unittest import mock

import yaml

from doc_examples import fenced_blocks, shell_commands

REPO = Path(__file__).resolve().parent.parent
DOCS = ("docs/DOCKER.md", "docs/OPERATIONS.md")
SHELL = frozenset({"bash", "sh", "shell", "console"})

#: Console scripts that have no argument parser to check an invocation against,
#: each with why. Their documented invocations must pass no arguments.
NO_PARSER = {
    "athena-mcp": (
        "configured only by environment variables (ATHENA_BASE_URL, ATHENA_TOKEN, "
        "...); its entry point takes no arguments and serves on stdio when called"
    ),
}

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = re.compile(r"^(?:\d*|&)(?:>>?|<)(?:&\d+|&-)?")
# Options of the docker subcommands the documents use. An option in neither set
# fails the test: the table grows when a document needs it, rather than guessing
# which word is the service and which is the command.
_DOCKER_VALUE_OPTIONS = {
    ("compose", "run"): {
        "--name",
        "-e",
        "--env",
        "--entrypoint",
        "-u",
        "--user",
        "-w",
        "--workdir",
        "-v",
        "--volume",
        "-l",
        "--label",
        "-p",
        "--publish",
    },
    ("compose", "exec"): {"-e", "--env", "-u", "--user", "-w", "--workdir", "--index"},
    ("exec",): {"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir"},
}
_DOCKER_FLAGS = {
    ("compose", "run"): {
        "--rm",
        "-d",
        "--detach",
        "-T",
        "-i",
        "-it",
        "--no-deps",
        "--service-ports",
        "--build",
    },
    ("compose", "exec"): {"-d", "--detach", "-T", "-i", "-t", "-it", "--privileged"},
    ("exec",): {"-d", "--detach", "-i", "-t", "-it", "--privileged"},
}


def console_scripts() -> dict[str, str]:
    """``{script: "module:function"}`` from ``pyproject.toml``."""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))
    return dict(pyproject["project"]["scripts"])


def image_entrypoint() -> list[str]:
    """The Dockerfile's ``ENTRYPOINT``, exec form."""
    for line in (REPO / "Dockerfile").read_text("utf-8").splitlines():
        if line.startswith("ENTRYPOINT "):
            return json.loads(line.removeprefix("ENTRYPOINT "))
    raise AssertionError("the Dockerfile declares no ENTRYPOINT")


def compose_service() -> dict[str, Any]:
    services = yaml.safe_load((REPO / "compose.yaml").read_text("utf-8"))["services"]
    assert list(services) == ["athena"], "compose.yaml is one service, athena"
    return services["athena"]


def _without_substitutions(text: str) -> str:
    """``$( ... )`` replaced by a placeholder word, so ``athena-$(date -u +%F).db``
    stays one argument instead of lending ``-u`` to the command around it."""
    out: list[str] = []
    i = 0
    while (start := text.find("$(", i)) != -1:
        out.append(text[i:start])
        depth, j = 0, start + 1
        while j < len(text):
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            j += 1
            if depth == 0:
                break
        out.append("SUBSTITUTED")
        i = j
    out.append(text[i:])
    return "".join(out)


def _without_redirections(words: list[str]) -> list[str]:
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        match = _REDIRECT.match(word)
        if match:
            skip = match.end() == len(word)  # the target is the next word
            continue
        kept.append(word)
    return kept


def _split_docker(key: tuple[str, ...], words: list[str]) -> tuple[dict, list[str]]:
    """``(options, rest)``: the options before the first operand, and the operand
    with everything after it."""
    options: dict[str, str | bool] = {}
    i = 0
    while i < len(words) and words[i].startswith("-"):
        name, _, value = words[i].partition("=")
        if name in _DOCKER_VALUE_OPTIONS[key]:
            if not value:
                i += 1
                value = words[i]
            options[name] = value
        elif name in _DOCKER_FLAGS[key] and not value:
            options[name] = True
        else:
            raise AssertionError(
                f"docker {' '.join(key)} option {words[i]!r} is in neither option "
                "table of tests/test_ops_doc_invocations.py; add it there"
            )
        i += 1
    return options, words[i:]


def invocation(words: list[str], scripts: dict[str, str]) -> list[str] | None:
    """The ``athena-*`` command line a shell command runs, or None."""
    words = _without_redirections(words)
    while words and _ENV_ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words:
        return None
    if words[0] in scripts:
        return words
    if words[:3] == ["docker", "compose", "run"]:
        options, rest = _split_docker(("compose", "run"), words[3:])
        if not rest or rest[0] not in compose_service_names():
            return None
        entrypoint = (
            [str(options["--entrypoint"])]
            if "--entrypoint" in options
            else image_entrypoint()
        )
        command = [*entrypoint, *rest[1:]]
        return command if command[0] in scripts else None
    if words[:3] == ["docker", "compose", "exec"]:
        _, rest = _split_docker(("compose", "exec"), words[3:])
        command = rest[1:]
    elif words[:2] == ["docker", "exec"]:
        _, rest = _split_docker(("exec",), words[2:])
        command = rest[1:]
    else:
        return None
    return command if command and command[0] in scripts else None


def compose_service_names() -> set[str]:
    services = yaml.safe_load((REPO / "compose.yaml").read_text("utf-8"))["services"]
    return set(services)


def documented_commands(doc: str) -> Iterator[tuple[str, list[str]]]:
    """``(where, words)`` for every command in the document's shell blocks."""
    text = (REPO / doc).read_text("utf-8")
    for block in fenced_blocks(doc, text):
        if block.lang not in SHELL:
            continue
        for words in shell_commands(_without_substitutions(block.body)):
            yield f"{doc}:{block.line}", words


def documented_invocations() -> list[tuple[str, list[str]]]:
    scripts = console_scripts()
    found: list[tuple[str, list[str]]] = []
    for doc in DOCS:
        for where, words in documented_commands(doc):
            command = invocation(words, scripts)
            if command is not None:
                found.append((where, command))
    return found


class _Parsed(Exception):
    """Raised in place of the command's work once its arguments have parsed."""


def parse_error(script: str, argv: list[str]) -> str | None:
    """The usage error ``script``'s own parser gives ``argv``, or None if it
    parses. The entry point is called, but stops at its parser."""
    module, _, function = console_scripts()[script].partition(":")
    entry: Callable[[list[str]], Any] = getattr(
        importlib.import_module(module), function
    )
    real_parse = argparse.ArgumentParser.parse_args
    real_known = argparse.ArgumentParser.parse_known_args
    # parse_args calls parse_known_args and then refuses what is left over; inside
    # it, the real parse_known_args must run, or an unknown option would pass.
    inside_parse_args = False

    def parse_args(self, args=None, namespace=None):
        nonlocal inside_parse_args
        inside_parse_args = True
        try:
            parsed = real_parse(self, args, namespace)
        finally:
            inside_parse_args = False
        raise _Parsed(parsed)

    def parse_known_args(self, args=None, namespace=None):
        if inside_parse_args:
            return real_known(self, args, namespace)
        raise _Parsed(real_known(self, args, namespace))

    stderr = io.StringIO()
    with (
        mock.patch.object(argparse.ArgumentParser, "parse_args", parse_args),
        mock.patch.object(
            argparse.ArgumentParser, "parse_known_args", parse_known_args
        ),
        contextlib.redirect_stderr(stderr),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        try:
            entry(argv)
        except _Parsed:
            return None
        except SystemExit as exc:
            return stderr.getvalue().strip() or f"exit {exc.code}"
    return "the entry point returned without reaching its argument parser"


def test_every_documented_invocation_parses_with_its_own_parser():
    invocations = documented_invocations()
    # The scan is real: it sees the commands an operator runs most, in both
    # documents, including the ones run inside the container.
    seen = {(where.split(":")[0], command[0]) for where, command in invocations}
    for expected in (
        ("docs/OPERATIONS.md", "athena-serve"),
        ("docs/OPERATIONS.md", "athena-backup"),
        ("docs/OPERATIONS.md", "athena-doctor"),
        ("docs/OPERATIONS.md", "athena-restore"),
        ("docs/DOCKER.md", "athena-serve"),
        ("docs/DOCKER.md", "athena-backup"),
    ):
        assert expected in seen, f"the scan no longer finds {expected}"

    failures = []
    for where, command in invocations:
        script, argv = command[0], command[1:]
        if script in NO_PARSER:
            if argv:
                failures.append(
                    f"{where}: {shlex.join(command)}: passes arguments to a "
                    f"command with no parser ({NO_PARSER[script]})"
                )
            continue
        error = parse_error(script, argv)
        if error is not None:
            failures.append(f"{where}: {shlex.join(command)}\n    {error}")
    assert not failures, "\n".join(failures)


def test_the_parse_check_refuses_what_the_parser_refuses():
    """Negative self-test: the documented-invocation check fails a command line
    its parser rejects, and passes one it accepts, without running either."""
    assert "backup_path" in (
        parse_error("athena-backup", ["/var/lib/athena/backup.db"]) or ""
    )
    assert parse_error("athena-backup", ["/a.db", "/b.db", "--keep", "3"]) is None
    assert "unrecognized" in (parse_error("athena-serve", ["--bogus"]) or "")
    scripts = console_scripts()
    assert invocation(
        shlex.split("docker compose run --rm athena --bootstrap"), scripts
    ) == ["athena-serve", "--bootstrap"]
    assert invocation(
        shlex.split("docker compose exec athena athena-backup /a.db /b.db"), scripts
    ) == ["athena-backup", "/a.db", "/b.db"]
    assert invocation(shlex.split("docker exec -it c python -c x"), scripts) is None


def test_compose_forwards_the_bootstrap_token_only_when_the_shell_has_one():
    environment = compose_service()["environment"]
    assert isinstance(environment, dict)
    # Key with no value: Compose takes it from the shell, and leaves it unset in
    # the container when the shell has none. Never a default, never "${...}".
    assert "ATHENA_BOOTSTRAP_TOKEN" in environment
    assert environment["ATHENA_BOOTSTRAP_TOKEN"] is None
    assert not any(
        "ATHENA_BOOTSTRAP_TOKEN" in str(value) for value in environment.values()
    )
    # And the image never carries one of its own.
    assert "ATHENA_BOOTSTRAP_TOKEN" not in (REPO / "Dockerfile").read_text("utf-8")


def test_the_container_document_runs_nothing_the_image_or_host_cannot():
    scripts = console_scripts()
    in_container = {*scripts, "python", "python3"}
    service = compose_service()
    assert "ports" not in service, "compose.yaml publishes no port, by design"
    problems = []
    for where, words in documented_commands("docs/DOCKER.md"):
        words = _without_redirections(words)
        while words and _ENV_ASSIGNMENT.match(words[0]):
            words = words[1:]
        if words[:3] == ["docker", "compose", "exec"]:
            program = _split_docker(("compose", "exec"), words[3:])[1][1]
        elif words[:2] == ["docker", "exec"]:
            program = _split_docker(("exec",), words[2:])[1][1]
        else:
            if words and words[0] == "curl":
                problems.append(f"{where}: a host curl, with no published port")
            continue
        if program not in in_container:
            problems.append(f"{where}: runs {program!r}, which the image lacks")
    assert not problems, problems

    # The first admin is created by a request the reader can run as shown: made
    # from inside the bootstrap container (named on the `docker compose run`, so
    # `docker exec` reaches that one-off container), with the image's Python,
    # carrying the token the container already holds.
    named = [
        options["--name"]
        for _, words in documented_commands("docs/DOCKER.md")
        if words[:3] == ["docker", "compose", "run"]
        for options in [_split_docker(("compose", "run"), words[3:])[0]]
        if "--bootstrap" in words and "--name" in options
    ]
    assert len(named) == 1, "the bootstrap run must name its container"
    text = (REPO / "docs/DOCKER.md").read_text("utf-8")
    first_admin = [
        block.body
        for block in fenced_blocks("docs/DOCKER.md", text)
        if block.lang in SHELL
        and re.search(rf"\bdocker exec\b[^\n]*\b{re.escape(named[0])}\b", block.body)
        and "/users" in block.body
    ]
    assert len(first_admin) == 1, (
        "docs/DOCKER.md shows no first-admin request made inside the bootstrap "
        "container"
    )
    assert "X-Athena-Bootstrap-Token" in first_admin[0]
    assert "ATHENA_BOOTSTRAP_TOKEN" in first_admin[0]
    assert "127.0.0.1:8000" in first_admin[0]

    # The runtime stage installs the wheel with pip and nothing with apt.
    dockerfile = (REPO / "Dockerfile").read_text("utf-8")
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert "apt-get" not in runtime and "curl" not in runtime


def test_the_documented_healthcheck_is_the_dockerfiles():
    dockerfile = (REPO / "Dockerfile").read_text("utf-8")
    probe = re.search(r"HEALTHCHECK[^\n]*\\\n\s*CMD .*?(/[a-z]+z)\b", dockerfile)
    assert probe is not None, "the Dockerfile's HEALTHCHECK names no endpoint"
    endpoint = probe.group(1)
    docker_md = (REPO / "docs/DOCKER.md").read_text("utf-8")
    stated = set(re.findall(r"`HEALTHCHECK` asks `(/[a-z]+z)`", docker_md))
    assert stated == {endpoint}, (
        f"docs/DOCKER.md says the image's HEALTHCHECK asks {sorted(stated)}; the "
        f"Dockerfile asks {endpoint}"
    )
