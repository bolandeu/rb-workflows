"""The Windmill contract and the command surface: every workflow, every step, every command is reachable and JSON."""

from __future__ import annotations

import inspect
import json

import pytest

from rb_workflows import cli
from rb_workflows.flows import web_fetch, web_search


def _commands() -> dict:
    """Every subcommand of the root parser, by name."""
    for action in cli._parser()._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            return dict(action.choices)
    raise AssertionError("the root parser has no subcommands")


def _steps() -> dict[str, list]:
    return {
        "web-fetch": [adapter for _, adapter in web_fetch.ADAPTERS],
        "web-search": [*web_search.SOURCES.values(), web_search._main_text],
    }


def test_every_registered_workflow_is_a_workflow() -> None:
    for name, flow in cli.WORKFLOWS.items():
        assert getattr(flow, "_is_workflow", False), f"{name} is not an @workflow"


def test_every_step_of_a_workflow_is_a_task() -> None:
    assert set(_steps()) == set(cli.WORKFLOWS)  # a new workflow must bring its steps here
    for name, functions in _steps().items():
        assert functions, name
        for function in functions:
            assert getattr(function, "_is_task", False), f"{name}: {function.__name__} is not an @task"


JSON_ANNOTATIONS = {
    "str",
    "int",
    "float",
    "bool",
    "list[str]",
    "float | None",
    "str | None",
    "list[str] | None",
}


def test_every_workflow_input_is_json() -> None:
    """Windmill builds the form from the signature, so every parameter must be a plain JSON type."""
    for name, flow in cli.WORKFLOWS.items():
        parameters = inspect.signature(flow).parameters
        assert parameters, name
        for parameter in parameters.values():
            assert parameter.annotation in JSON_ANNOTATIONS, (name, parameter.name, parameter.annotation)


def test_every_command_has_help(capsys) -> None:
    """`--help` prints and exits 0 (`auth` prints its own list and returns, so both answers are fine)."""
    for name in _commands():
        try:
            code = cli.main([name, "--help"])
        except SystemExit as stopped:
            code = stopped.code
        assert code == 0, name
        assert capsys.readouterr().out, name


def test_the_bare_help_lists_every_command(capsys) -> None:
    assert cli.main([]) == 0
    printed = capsys.readouterr().out
    for name in _commands():
        assert name in printed, name


def test_describe_covers_every_command(capsys) -> None:
    assert cli.main(["describe", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    described = {command["name"] for command in payload["command"]["commands"]}
    assert described == set(_commands())
    assert payload["command"]["name"] == "wf"


def test_every_command_refuses_an_empty_call_with_the_envelope(capsys) -> None:
    """A command that needs input says so in the Standard's envelope (exit 2), never with a traceback."""
    silent = {"run": "unknown workflow", "doctor": None, "auth": None, "describe": None}
    for name in _commands():
        if name in silent:
            continue
        with pytest.raises(SystemExit) as stopped:
            cli.main([name, "--json"])
        printed = capsys.readouterr().out
        payload = json.loads(printed)
        assert (stopped.value.code, payload["ok"], payload["error"]["code"]) == (2, False, "INVALID_ARGUMENTS"), name


def test_an_unknown_option_is_the_envelope_too(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["top", "--nope"])
    payload = json.loads(capsys.readouterr().out)
    assert (stopped.value.code, payload["error"]["code"]) == (2, "INVALID_ARGUMENTS")


def test_a_broken_input_file_is_the_envelope(capsys, tmp_path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["top", "--input", str(broken)])
    payload = json.loads(capsys.readouterr().out)
    assert (stopped.value.code, payload["error"]["code"]) == (2, "INVALID_ARGUMENTS")


def test_every_workflow_has_its_own_command() -> None:
    commands = _commands()
    for name in cli.WORKFLOWS:
        assert name in commands, name
        assert name in cli.WORKFLOW_ARGS, name
        assert name in cli.WORKFLOW_HELP, name
