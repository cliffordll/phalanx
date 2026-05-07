"""Slash-command registry + completer tests (Phase 2.6 wave 2)."""

from __future__ import annotations

import pytest

from hermes_cli.commands import (
    COMMAND_REGISTRY,
    COMMANDS,
    COMMANDS_BY_CATEGORY,
    SUBCOMMANDS,
    SlashCommandCompleter,
    _build_command_lookup,
    iter_active_commands,
    iter_stub_commands,
    resolve_command,
)


# ── Registry shape ───────────────────────────────────────────────────────


def test_registry_is_non_empty():
    assert len(COMMAND_REGISTRY) >= 10


def test_registry_has_no_duplicate_names():
    names = [c.name for c in COMMAND_REGISTRY]
    assert len(names) == len(set(names))


def test_registry_has_no_alias_collisions():
    """An alias must not shadow another command's canonical name."""
    canonical = {c.name for c in COMMAND_REGISTRY}
    for c in COMMAND_REGISTRY:
        for alias in c.aliases:
            assert alias not in canonical or alias == c.name, (
                f"alias /{alias} collides with /{alias}"
            )


def test_active_and_stub_partition_match_registry():
    active = iter_active_commands()
    stub = iter_stub_commands()
    assert len(active) + len(stub) == len(COMMAND_REGISTRY)
    assert {c.name for c in active}.isdisjoint({c.name for c in stub})


def test_help_command_is_active():
    cmd = resolve_command("help")
    assert cmd is not None
    assert cmd.stub is False


def test_quit_and_exit_share_one_def():
    assert resolve_command("quit") is resolve_command("exit")


# ── resolve_command ──────────────────────────────────────────────────────


def test_resolve_command_exact():
    cmd = resolve_command("model")
    assert cmd is not None
    assert cmd.name == "model"


def test_resolve_command_with_slash():
    assert resolve_command("/help") is resolve_command("help")


def test_resolve_command_alias():
    new = resolve_command("new")
    reset = resolve_command("reset")
    assert new is reset is not None
    assert new.name == "new"


def test_resolve_command_case_insensitive():
    assert resolve_command("HELP") is resolve_command("help")
    assert resolve_command("/Tools") is resolve_command("tools")


def test_resolve_command_unknown_returns_none():
    assert resolve_command("does-not-exist") is None
    assert resolve_command("") is None


# ── Derived dicts ────────────────────────────────────────────────────────


def test_commands_dict_keys_have_leading_slash():
    for k in COMMANDS:
        assert k.startswith("/")


def test_commands_dict_includes_aliases():
    assert "/exit" in COMMANDS
    assert "/quit" in COMMANDS
    assert "/reset" in COMMANDS  # alias of /new


def test_commands_dict_marks_stub_entries():
    """Canonical stub commands must surface the [stub] tag in /help text."""
    assert "[stub" in COMMANDS["/save"]
    # Active commands shouldn't carry the marker.
    assert "[stub" not in COMMANDS["/help"]


def test_commands_by_category_partitioning():
    expected = {
        "Session", "Configuration", "Tools", "Context", "Info", "Exit",
    }
    assert set(COMMANDS_BY_CATEGORY) == expected


def test_subcommands_only_for_commands_that_have_them():
    """Only commands with subcommands declared show up in SUBCOMMANDS."""
    for cmd in COMMAND_REGISTRY:
        if cmd.subcommands:
            assert SUBCOMMANDS[f"/{cmd.name}"] == list(cmd.subcommands)
        else:
            assert f"/{cmd.name}" not in SUBCOMMANDS


def test_build_command_lookup_round_trip():
    lookup = _build_command_lookup()
    for cmd in COMMAND_REGISTRY:
        assert lookup[cmd.name] is cmd
        for alias in cmd.aliases:
            assert lookup[alias] is cmd


# ── SlashCommandCompleter ────────────────────────────────────────────────


def _completions(text: str) -> list[str]:
    """Run the completer and return the suggested replacement strings."""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document
    sc = SlashCommandCompleter()
    return [c.text for c in sc.get_completions(Document(text), None)]


def test_completer_top_level_listing_for_just_slash():
    out = _completions("/")
    # Every COMMANDS key is a hit for the bare slash prefix.
    assert set(out) == set(COMMANDS)


def test_completer_top_level_filters_by_prefix():
    out = _completions("/he")
    assert out == ["/help"]


def test_completer_top_level_handles_alias_prefix():
    out = _completions("/ex")
    # /exit is the alias of /quit; alias completes too.
    assert "/exit" in out


def test_completer_no_results_for_non_slash():
    assert _completions("hello") == []


def test_completer_no_results_for_empty_buffer():
    assert _completions("") == []


def test_completer_subcommand_after_known_command():
    out = _completions("/tools ")
    assert set(out) == {"list", "disable", "enable"}


def test_completer_subcommand_filters_by_prefix():
    out = _completions("/debug o")
    assert set(out) == {"on", "off"}


def test_completer_subcommand_no_results_for_unknown_head():
    """No subcommands defined for /help → no second-token completions."""
    assert _completions("/help ") == []


def test_completer_no_completions_after_two_spaces():
    """We don't try to complete arbitrary args (yet)."""
    assert _completions("/tools list ") == []


def test_completer_supplies_display_meta():
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.document import Document
    sc = SlashCommandCompleter()
    completions = list(sc.get_completions(Document("/he"), None))
    assert len(completions) == 1
    assert "Show available commands" in str(completions[0].display_meta)


# ── _dispatch_slash + handlers ───────────────────────────────────────────


def test_dispatch_help_renders_categories(capsys):
    import cli
    rc = cli._dispatch_slash("/help", {})
    captured = capsys.readouterr()
    assert rc is None
    # Each category header should show up.
    for cat in ("[Session]", "[Configuration]", "[Tools]", "[Info]", "[Exit]"):
        assert cat in captured.out
    # Stub tag on /save.
    assert "[stub]" in captured.out


def test_dispatch_exit_returns_sentinel():
    import cli
    assert cli._dispatch_slash("/exit", {}) == cli._DISPATCH_EXIT
    assert cli._dispatch_slash("/quit", {}) == cli._DISPATCH_EXIT


def test_dispatch_unknown_prints_hint(capsys):
    import cli
    rc = cli._dispatch_slash("/no-such-command", {})
    captured = capsys.readouterr()
    assert rc is None
    assert "unknown command" in captured.out
    assert "/help" in captured.out


def test_dispatch_stub_command_prints_message(capsys):
    """A registered command with no handler yet emits the stub line."""
    import cli
    rc = cli._dispatch_slash("/save my-title", {})
    captured = capsys.readouterr()
    assert rc is None
    assert "not yet implemented" in captured.out


def test_dispatch_alias_routes_to_canonical(capsys):
    """``/reset`` (alias of /new) hits /new's handler, not 'unknown'."""
    import cli
    import uuid

    class _FakeAgent:
        session_id = str(uuid.uuid4())
        _session_db_created = True
        _last_flushed_db_idx = 5

    state = {"agent": _FakeAgent(), "history": [{"role": "user", "content": "x"}]}
    cli._dispatch_slash("/reset", state)
    captured = capsys.readouterr()
    # /new prints "started new session (<8-char id>)" — not the stub
    # message.  This proves /reset routed through resolve_command to
    # the canonical /new handler.
    assert "started new session" in captured.out
    assert "unknown command" not in captured.out
    assert state["history"] == []
    assert state["agent"]._session_db_created is False
    assert state["agent"]._last_flushed_db_idx == 0
