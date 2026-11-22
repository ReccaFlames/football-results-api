from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from app import access
from app import config as config_module
from app.competitions import source_from_url
from app.config import ConfigFileWatcher, cli, load_config
from app.scheduling import SchedulePolicy

DEFAULT_CONFIG = Path(__file__).parents[1] / "config/competitions.yaml"


def test_default_config_is_valid_and_preserves_competitions() -> None:
    config = load_config(DEFAULT_CONFIG)
    competitions = config.to_competitions()

    assert set(competitions) == {
        "ekstraklasa",
        "pierwsza-liga",
        "europa-league",
        "premier-league",
        "3-liga",
    }
    assert competitions["ekstraklasa"].current_season == "2026-2027"
    assert config.providers["90minut"].min_request_interval_seconds == 2
    assert config.defaults.schedule.result_poll_minutes == [5, 15, 60]


def test_config_rejects_undeclared_provider(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    data["competitions"][0]["provider"] = "unknown"
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ValueError, match="undeclared provider"):
        load_config(path)


def test_config_rejects_source_from_a_different_provider(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    data["competitions"][0]["seasons"]["2026-2027"]["source_url"] = (
        "https://api.football-data.org/v4/competitions/PL/matches?season=2026"
    )
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ValueError, match="not '90minut'"):
        load_config(path)


def test_config_allows_competitions_without_a_current_season(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    del data["competitions"][0]["current_season"]
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))

    competition = load_config(path).to_competitions()["ekstraklasa"]

    assert competition.current_season is None


def test_config_file_watcher_reloads_only_when_contents_change(tmp_path: Path) -> None:
    path = tmp_path / "competitions.yaml"
    path.write_bytes(DEFAULT_CONFIG.read_bytes())
    watcher = ConfigFileWatcher(path)

    initial, changed = watcher.load_if_changed()
    watcher.mark_applied()
    unchanged, changed_again = watcher.load_if_changed()
    data = yaml.safe_load(path.read_text())
    data["competitions"][0]["name"] = "Renamed League"
    path.write_text(yaml.safe_dump(data))
    updated, changed_after_edit = watcher.load_if_changed()
    pending, still_changed = watcher.load_if_changed()
    watcher.mark_applied()

    assert changed and not changed_again and changed_after_edit
    assert still_changed and pending is updated
    assert initial is unchanged
    assert updated.competitions[0].name == "Renamed League"


def test_config_check_cli_validates_the_requested_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli(["check", "--config-file", str(DEFAULT_CONFIG)]) == 0
    assert "Configuration valid" in capsys.readouterr().out

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("invalid: [")
    with pytest.raises(SystemExit) as error:
        cli(["check", "--config-file", str(invalid)])
    assert error.value.code == 2


def write_config(tmp_path: Path, competition: dict[str, object]) -> Path:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    data["competitions"].append(competition)
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def calendar_competition(**overrides: object) -> dict[str, object]:
    return {
        "slug": "brasileirao",
        "name": "Campeonato Brasileiro Série A",
        "format": "league",
        "provider": "football-data",
        "season_format": "calendar",
        "current_season": "2026",
        "seasons": {
            "2026": {
                "source_url": (
                    "https://api.football-data.org/v4/competitions/BSA/matches?season=2026"
                )
            }
        },
    } | overrides


def test_config_accepts_calendar_year_seasons(tmp_path: Path) -> None:
    unquoted_years = calendar_competition(
        current_season=2026,
        seasons={
            2026: {
                "source_url": (
                    "https://api.football-data.org/v4/competitions/BSA/matches?season=2026"
                )
            }
        },
    )
    competitions = load_config(write_config(tmp_path, unquoted_years)).to_competitions()

    brasileirao = competitions["brasileirao"]
    assert (brasileirao.season_format, brasileirao.season_start_month) == ("calendar", 1)
    assert brasileirao.current_season == "2026"
    assert competitions["ekstraklasa"].season_start_month == 7


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"season_format": "split"}, "split format"),
        ({"season_start_month": 2}, "calendar seasons start in January"),
        (
            {
                "season_format": "split",
                "season_start_month": 1,
                "current_season": None,
                "seasons": {},
            },
            "cannot start in January",
        ),
        (
            {
                "seasons": {
                    "2025": {
                        "source_url": (
                            "https://api.football-data.org/v4/competitions/BSA/matches?season=2025"
                        )
                    },
                    "2026": {
                        "source_url": (
                            "https://api.football-data.org/v4/competitions/BL1/matches?season=2026"
                        )
                    },
                }
            },
            "mixes provider competition codes",
        ),
    ],
)
def test_config_rejects_inconsistent_season_settings(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(write_config(tmp_path, calendar_competition(**overrides)))


def test_team_aliases_are_returned_per_competition(tmp_path: Path) -> None:
    path = write_config(tmp_path, calendar_competition(team_aliases={"Flamengo": "CR Flamengo"}))

    assert load_config(path).team_aliases()["brasileirao"] == {"Flamengo": "CR Flamengo"}


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        ({"": "CR Flamengo"}, "empty"),
        ({"Flamengo": " "}, "empty"),
        ({"Flamengo": "Flamengo"}, "maps to itself"),
    ],
)
def test_config_rejects_invalid_team_aliases(
    tmp_path: Path, aliases: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(write_config(tmp_path, calendar_competition(team_aliases=aliases)))


def test_shipped_schedule_defaults_match_the_built_in_policy() -> None:
    config = load_config(DEFAULT_CONFIG)

    assert config.schedules() == dict.fromkeys(config.schedules(), SchedulePolicy())
    assert config.auto_rollover_slugs() == {competition.slug for competition in config.competitions}


def test_competition_schedule_override_merges_over_defaults(tmp_path: Path) -> None:
    override = {
        "pre_match_check_minutes": 45,
        "match_duration_minutes": {"league": 120, "playoff": 150},
        "season_rollover": "manual",
        "season_check_interval": "12h",
    }
    config = load_config(write_config(tmp_path, calendar_competition(schedule=override)))

    policy = config.schedule_for("brasileirao")

    assert policy.pre_match_check == timedelta(minutes=45)
    assert policy.match_duration == {
        "league": timedelta(minutes=120),
        "cup": timedelta(minutes=135),
        "playoff": timedelta(minutes=150),
    }
    assert policy.season_check_interval == timedelta(hours=12)
    assert policy.failure_backoff == SchedulePolicy().failure_backoff
    assert "brasileirao" not in config.auto_rollover_slugs()
    assert config.schedule_for("ekstraklasa") == SchedulePolicy()


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ({"result_poll_minutes": [5, 10]}, "one entry fewer"),
        ({"result_poll_until_minutes": [240, 30]}, "strictly increasing"),
        ({"idle_interval": "1w"}, "idle_interval"),
        ({"season_rollover": "sometimes"}, "season_rollover"),
        ({"unknown_threshold": 1}, "unknown_threshold"),
    ],
)
def test_config_rejects_invalid_schedule_overrides(
    tmp_path: Path, schedule: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(write_config(tmp_path, calendar_competition(schedule=schedule)))


def test_config_requires_a_match_duration_for_each_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not define it"):
        load_config(write_config(tmp_path, calendar_competition(format="playoff")))


def premier_league_config(tmp_path: Path, **changes: object) -> Path:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    competition = next(item for item in data["competitions"] if item["slug"] == "premier-league")
    for key, value in changes.items():
        if value is None:
            competition.pop(key, None)
        else:
            competition[key] = value
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_seasons_carry_their_fallback_source() -> None:
    source = load_config(DEFAULT_CONFIG).to_competitions()["premier-league"].seasons["2026-2027"]

    assert source.fallback() is not None
    assert source.fallback().provider == "football-data"
    assert source.fallback().source_url.endswith("/competitions/PL/matches?season=2026")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"fallback_provider": None}, "fallback_url but no fallback_provider"),
        ({"fallback_provider": "espn"}, "must differ from provider"),
        ({"fallback_provider": "90minut"}, "fallback_url uses provider 'football-data'"),
        ({"fallback_provider": "unknown"}, "undeclared fallback_provider"),
    ],
)
def test_config_rejects_invalid_fallback_sources(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(premier_league_config(tmp_path, **changes))


def test_fallback_without_an_api_token_is_only_a_warning() -> None:
    config = load_config(DEFAULT_CONFIG)

    assert config.fallback_warnings("token", "paid-key") == []
    [warning] = config.fallback_warnings(None, "paid-key")
    assert "premier-league" in warning and "FOOTBALL_DATA_API_TOKEN" in warning


def test_free_thesportsdb_key_warns_about_missed_matches() -> None:
    config = load_config(DEFAULT_CONFIG)

    warnings = config.fallback_warnings("token", "123")

    assert [warning.split("'")[1] for warning in warnings] == ["ekstraklasa", "pierwsza-liga"]
    for warning in warnings:
        assert "busy matchday can miss matches" in warning
        assert "until the primary source recovers or a later window covers them" in warning
        assert "FOOTBALL_RESULTS_THESPORTSDB_API_KEY" in warning
        assert "123" not in warning
    assert config.fallback_warnings("token", "paid-secret-key") == []


@pytest.mark.parametrize("key", ["123", "paid-secret-key"])
def test_config_check_never_prints_the_thesportsdb_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    monkeypatch.setattr(config_module.settings, "football_data_api_token", "token")
    monkeypatch.setattr(config_module.settings, "thesportsdb_api_key", SecretStr(key))

    assert cli(["check", "--config-file", str(DEFAULT_CONFIG)]) == 0

    output = capsys.readouterr()
    assert key not in output.out + output.err
    assert ("busy matchday" in output.err) == (key == "123")


@pytest.mark.parametrize(("slug", "league"), [("ekstraklasa", 4422), ("pierwsza-liga", 4661)])
def test_shipped_polish_competitions_fall_back_to_thesportsdb(slug: str, league: int) -> None:
    competition = load_config(DEFAULT_CONFIG).to_competitions()[slug]

    fallback = competition.seasons["2026-2027"].fallback()

    assert fallback is not None
    assert fallback.provider == "thesportsdb"
    assert fallback.source_url == f"https://www.thesportsdb.com/league/{league}?season=2026-2027"
    assert source_from_url("2026-2027", fallback.source_url).provider == "thesportsdb"


def test_api_keys_file_is_validated_by_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        "keys:\n"
        f"  - {{name: ops, hash: '{access.hash_key('a')}', role: admin}}\n"
        f"  - {{name: partner-app, hash: '{access.hash_key('b')}', read_per_minute: 600}}\n"
    )
    args = ["check", "--config-file", str(DEFAULT_CONFIG), "--api-keys-file", str(keys)]
    assert cli(args) == 0
    assert f"API keys file valid: {keys} (2 keys, 1 admin)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("{name: app, hash: plain-secret-key}", "String should match pattern"),
        ("{name: env, hash: H}", "reserved"),
        ("{name: Bad_Name, hash: H}", "String should match pattern"),
        ("{name: app, hash: H, write_per_minute: 5}", "only for admin keys"),
        ("{name: app, hash: H, role: owner}", "Input should be 'client' or 'admin'"),
        ("{name: app, hash: H, read_per_minute: 0}", "greater than or equal to 1"),
        ("{name: app, hash: H, quota: 5}", "Extra inputs are not permitted"),
    ],
)
def test_invalid_api_keys_files_are_rejected_without_echoing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], entry: str, message: str
) -> None:
    keys = tmp_path / "keys.yaml"
    keys.write_text(f"keys:\n  - {entry.replace('H', repr(access.hash_key('k')))}\n")
    with pytest.raises(SystemExit):
        cli(["check", "--config-file", str(DEFAULT_CONFIG), "--api-keys-file", str(keys)])
    error = capsys.readouterr().err
    assert message in error
    assert "plain-secret-key" not in error


def test_duplicate_api_key_names_are_rejected(tmp_path: Path) -> None:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        "keys:\n"
        f"  - {{name: app, hash: '{access.hash_key('a')}'}}\n"
        f"  - {{name: app, hash: '{access.hash_key('b')}'}}\n"
    )
    with pytest.raises(ValueError, match="duplicate key names: app"):
        access.load_keys(keys)


def test_hash_key_prints_a_new_key_and_its_entry(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["hash-key", "--name", "ops", "--role", "admin"]) == 0
    output = capsys.readouterr()
    key = output.err.split("it is not stored): ", 1)[1].split("\n", 1)[0]
    assert len(key) >= 43
    entry = yaml.safe_load("keys:\n" + output.out)["keys"][0]
    assert entry == {"name": "ops", "hash": access.hash_key(key), "role": "admin"}
    assert key not in output.out

    with pytest.raises(SystemExit):
        cli(["hash-key", "--name", "anonymous"])


def test_keys_resolve_by_hash_and_environment_keys_are_admin(tmp_path: Path) -> None:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        f"keys:\n  - {{name: partner-app, hash: '{access.hash_key('k1')}', read_per_minute: 5}}\n"
    )
    file_keys = access.load_keys(keys)
    partner = access.resolve("k1", ("env-key",), file_keys)
    assert partner is not None
    assert (partner.name, partner.role, partner.read_per_minute) == ("partner-app", "client", 5)
    env = access.resolve("env-key", ("env-key",), file_keys)
    assert env is not None and (env.name, env.role) == ("env", "admin")
    assert access.resolve("wrong", ("env-key",), file_keys) is None
    assert access.resolve("", ("",), ()) is None
    assert not access.has_admin_key((), file_keys)
