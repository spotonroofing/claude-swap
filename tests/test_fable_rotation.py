"""The fork's default plan policy, using real persisted usage readings."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from claude_swap import cli, oauth
from claude_swap.autoswitch import TickOutcome
from claude_swap.menubar import format_account_label
from claude_swap.settings import settings_path
from claude_swap.usage_store import FetchRecord, UsageEntry, due_candidate
from tests.test_autoswitch import EngineHarness


IDENTITIES = {str(n): (f"account{n}@example.com", "") for n in range(1, 4)}


def reading(pct=10, *, fable=True):
    usage = {"five_hour": {"pct": pct}, "seven_day": {"pct": 0}}
    if fable:
        usage["scoped"] = [{"name": "Fable", "pct": 0}]
    return usage


@pytest.fixture
def fleet(temp_home):
    h = EngineHarness(temp_home)
    for num, (email, _) in IDENTITIES.items():
        h.seed(int(num), email)
    h.make_live(IDENTITIES["1"][0], 1)
    h.engine.dry_run = True
    return h


def record(h, values):
    h.switcher._usage_store.record(
        {num: FetchRecord(usage=value) for num, value in values.items()}, IDENTITIES
    )


def tick(h):
    return h.tick_with_entries(h.switcher._usage_store.entries(IDENTITIES))


@pytest.mark.parametrize("value, expected", [
    (None, None),
    (reading(fable=False), False),
    ({"scoped": [{"name": "Sonnet", "pct": 0}]}, False),
    (reading(), True),
    ({"scoped": [{"name": "fAbLe", "pct": 100}]}, True),
])
def test_plan_signal(value, expected):
    assert UsageEntry(last_good=value).has_fable is expected


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("strategy", ["best", "consume-first"])
def test_daemon_skips_no_fable_and_unseen(fleet, missing, strategy):
    fleet.engine.settings = replace(fleet.settings, strategy=strategy)
    record(fleet, {"1": reading(100), "3": reading(40)})
    if not missing:
        record(fleet, {"2": reading(0, fable=False)})
    assert tick(fleet) == TickOutcome.SWITCHED
    assert fleet.events[-1].to_ref["number"] == 3


@pytest.mark.parametrize("initial", [None, reading(fable=False)])
def test_storing_fable_reenters_rotation_without_enable(fleet, initial):
    record(fleet, {"1": reading(100), "3": reading(100)})
    if initial is not None:
        record(fleet, {"2": initial})
    assert tick(fleet) == TickOutcome.BLOCKED
    assert "2" not in fleet.switcher.rotation_account_numbers()
    record(fleet, {"2": reading()})
    assert "2" in fleet.switcher.rotation_account_numbers()
    assert tick(fleet) == TickOutcome.SWITCHED
    assert fleet.events[-1].to_ref["number"] == 2
    record(fleet, {"2": reading(fable=False)})
    assert "2" not in fleet.switcher.rotation_account_numbers()
    assert tick(fleet) == TickOutcome.BLOCKED


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("strategy", [None, "best", "next-available"])
def test_automatic_switch_skips_ineligible(fleet, missing, strategy):
    record(fleet, {"1": reading(90), "3": reading(40)})
    if not missing:
        record(fleet, {"2": reading(0, fable=False)})
    with patch.object(fleet.switcher, "_perform_switch") as perform:
        fleet.switcher.switch(strategy=strategy)
    assert perform.call_args.args[0] == "3"


def test_fresh_machine_skips_unknown_preferred(fleet):
    record(fleet, {"3": reading()})
    with (
        patch.object(fleet.switcher, "_get_current_account", return_value=None),
        patch.object(fleet.switcher, "_perform_switch") as perform,
    ):
        fleet.switcher.switch()
    assert perform.call_args.args[0] == "3"


@pytest.mark.parametrize("initial", [None, reading(fable=False)])
def test_opt_out_restores_plain_rotation(fleet, initial):
    if initial is not None:
        record(fleet, {"2": initial})
    settings_path(fleet.switcher.backup_dir).write_text('{"require_fable": false}')
    with patch.object(fleet.switcher, "_perform_switch") as perform:
        fleet.switcher.switch()
    assert perform.call_args.args[0] == "2"
    settings_path(fleet.switcher.backup_dir).write_text('{"require_fable": true}')
    assert fleet.switcher.rotation_account_numbers() == []


def test_opt_out_restores_daemon_ranking(fleet):
    record(fleet, {"1": reading(100), "2": reading(0, fable=False), "3": reading(40)})
    settings_path(fleet.switcher.backup_dir).write_text('{"require_fable": false}')
    assert tick(fleet) == TickOutcome.SWITCHED
    assert fleet.events[-1].to_ref["number"] == 2


def test_plan_exclusion_does_not_stop_polling(fleet):
    record(fleet, {"2": reading(fable=False)})
    fleet.clock.advance(1000)
    entries = fleet.switcher._usage_store.entries(IDENTITIES)
    assert "2" in fleet.switcher.switchable_account_numbers()
    assert due_candidate(["2"], entries, fleet.clock()) == "2"
    assert due_candidate(["3"], entries, fleet.clock()) == "3"
    assert set(fleet.switcher._usage_store.reserve({"2", "3"}, IDENTITIES, respect_plans=True)) == {"2", "3"}


@pytest.mark.parametrize("identifier", ["2", "account2@example.com"])
def test_manual_switch_succeeds_without_notice(fleet, identifier, capsys):
    record(fleet, {"2": reading(fable=False)})
    with patch.object(fleet.switcher, "list_accounts"):
        result = fleet.switcher.switch_to(identifier, json_output=True)
    assert result["switched"] is True
    assert result["to"]["number"] == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_manual_switch_human_without_notice(fleet, capsys):
    record(fleet, {"2": reading(fable=False)})
    with patch.object(fleet.switcher, "list_accounts"):
        fleet.switcher.switch_to("2")
    assert fleet.active_number() == 2
    assert "no Fable" not in capsys.readouterr().out


@pytest.mark.parametrize("identifier", ["2", "account2@example.com"])
def test_run_remains_available_without_notice(fleet, identifier, capsys):
    record(fleet, {"2": reading(fable=False)})
    with (
        patch("claude_swap.cli.ClaudeAccountSwitcher", return_value=fleet.switcher),
        patch("claude_swap.session.SessionManager.run") as run,
    ):
        cli._run_command([identifier])
    assert run.call_args.args[0] == identifier
    assert capsys.readouterr().out == ""


def test_list_and_status_expose_all_three_plan_states(fleet, capsys):
    record(fleet, {"1": reading(), "2": reading(fable=False)})
    fleet.switcher.list_accounts(fetch=set())
    output = capsys.readouterr().out
    headers = [line for line in output.splitlines() if "@example.com" in line]
    account_two = output.split("  2:", 1)[1].split("  3:", 1)[0]
    assert "Fable" not in account_two
    assert len(headers) == 3
    assert ["no Fable" in line for line in headers] == [False, False, False]
    with patch.object(fleet.switcher, "_active_account_usage", return_value=UsageEntry(last_good=reading())):
        payload = fleet.switcher.status(json_output=True)
    assert [a["has_fable"] for a in payload["accounts"]] == [True, False, None]
    assert payload["active"]["has_fable"] is True
    assert json.loads(json.dumps(payload))["accounts"][1]["has_fable"] is False


def test_status_human_no_fable(fleet, capsys):
    with patch.object(fleet.switcher, "_active_account_usage", return_value=UsageEntry(last_good=reading(fable=False))):
        fleet.switcher.status()
    assert "Fable" not in capsys.readouterr().out


def test_menubar_plan_tag():
    assert "(no Fable)" in format_account_label(2, "a@example.com", None, has_fable=False)
    for value in (True, None):
        assert "no Fable" not in format_account_label(2, "a@example.com", None, has_fable=value)


def test_plan_survives_staleness_and_fetch_failure(fleet):
    record(fleet, {"2": reading(fable=False)})
    fleet.clock.advance(10000)
    fleet.switcher._usage_store.record({"2": FetchRecord(error="timeout")}, IDENTITIES)
    assert fleet.switcher.fable_by_account()["2"] is False


def test_disabled_fable_account_stays_disabled(fleet):
    record(fleet, {"2": reading()})
    fleet.switcher.set_account_disabled("2", True)
    assert "2" not in fleet.switcher.rotation_account_numbers()
    fleet.switcher.set_account_disabled("2", False)
    assert "2" in fleet.switcher.rotation_account_numbers()


def test_api_key_fallback_cannot_bypass_plan_requirement(fleet):
    record(fleet, {"1": reading(100), "3": reading(100)})
    fleet.engine.settings = replace(fleet.settings, include_api_key_accounts=True)
    with patch.object(fleet.switcher, "account_kind_for", side_effect=lambda n: "api_key" if n == "2" else "oauth"):
        assert tick(fleet) == TickOutcome.BLOCKED
        settings_path(fleet.switcher.backup_dir).write_text('{"require_fable": false}')
        assert tick(fleet) == TickOutcome.SWITCHED
        assert fleet.events[-1].to_ref["number"] == 2


def test_daemon_rechecks_plan_before_switching(fleet):
    record(fleet, {"1": reading(100), "2": reading(), "3": reading(100)})
    fleet.engine.dry_run = False

    def downgrade(*_):
        record(fleet, {"2": reading(fable=False)})
        return "ok"

    with (
        patch.object(fleet.engine, "_freshen_target", side_effect=downgrade),
        patch.object(fleet.switcher, "switch_to") as switch,
    ):
        assert tick(fleet) == TickOutcome.BLOCKED
    switch.assert_not_called()
    assert fleet.events[-1].reason == "fable-unavailable"


@pytest.mark.parametrize("strategy", ["best", "consume-first"])
def test_scheduled_refresh_changes_plan_before_same_tick_decision(fleet, strategy):
    fleet.engine.settings = replace(fleet.settings, strategy=strategy)
    values = {"1": reading(100), "2": reading(fable=False), "3": reading(100)}
    record(fleet, values)
    assert tick(fleet) == TickOutcome.BLOCKED
    assert "2" not in fleet.switcher.rotation_account_numbers()

    def fetch(num, *_args, **_kwargs):
        return oauth.UsageOutcome(values[num])

    # Keep the same engine and switcher throughout. Only the server's usage
    # response changes; the real scheduled collector writes the new reading.
    with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=fetch) as fetched:
        for has_fable, expected in [(True, TickOutcome.SWITCHED), (False, TickOutcome.BLOCKED)]:
            fleet.clock.advance(1000)
            values["2"] = reading(fable=has_fable)
            fetched.reset_mock()
            assert fleet.engine.tick() == expected
            assert any(call.args[0] == "2" for call in fetched.call_args_list)
            entry = fleet.switcher._usage_store.entries(IDENTITIES)["2"]
            assert entry.fetched_at == fleet.clock()
            assert entry.has_fable is has_fable
            assert ("2" in fleet.switcher.rotation_account_numbers()) is has_fable
            if has_fable:
                assert fleet.events[-1].to_ref["number"] == 2
