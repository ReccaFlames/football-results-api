from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr, ValidationError

from app import access
from app import config as config_module
from app.competitions import source_from_url
from app.config import ConfigFileWatcher, cli, load_config
from app.scheduling import SchedulePolicy

NOW = datetime.now(UTC).replace(microsecond=0)
EXPIRY = (
    f"issued_at: '{access.format_time(NOW - timedelta(days=1))}', "
    f"expires_at: '{access.format_time(NOW + timedelta(days=29))}'"
)


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
        f"  - {{name: ops, hash: '{access.hash_key('a')}', {EXPIRY}, role: admin}}\n"
        f"  - {{name: partner-app, hash: '{access.hash_key('b')}', {EXPIRY}, read_per_minute: 600}}\n"
        "  - {name: operator, subject: 3f6c-user, role: admin}\n"
    )
    args = ["check", "--config-file", str(DEFAULT_CONFIG), "--api-keys-file", str(keys)]
    assert cli(args) == 0
    expected = f"API keys file valid: {keys} (2 keys, 2 admin, 1 OAuth subjects)"
    assert expected in capsys.readouterr().out


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
        f"  - {{name: app, hash: '{access.hash_key('a')}', {EXPIRY}}}\n"
        f"  - {{name: app, hash: '{access.hash_key('b')}', {EXPIRY}}}\n"
    )
    with pytest.raises(ValueError, match="duplicate key names: app"):
        access.load_keys(keys)


def test_hash_key_prints_a_new_key_and_its_entry(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["hash-key", "--name", "ops", "--role", "admin"]) == 0
    output = capsys.readouterr()
    key = output.err.split("it is not stored): ", 1)[1].split("\n", 1)[0]
    assert len(key) >= 43
    assert key.startswith(access.KEY_PREFIX)
    entry = yaml.safe_load("keys:\n" + output.out)["keys"][0]
    issued = access.as_utc(datetime.fromisoformat(str(entry.pop("issued_at"))))
    expires = access.as_utc(datetime.fromisoformat(str(entry.pop("expires_at"))))
    assert expires - issued == timedelta(days=access.DEFAULT_KEY_DAYS)
    assert entry == {"name": "ops", "hash": access.hash_key(key), "role": "admin"}
    assert key not in output.out

    with pytest.raises(SystemExit):
        cli(["hash-key", "--name", "anonymous"])


def test_keys_resolve_by_hash_and_environment_keys_are_admin(tmp_path: Path) -> None:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        f"keys:\n  - {{name: partner-app, hash: '{access.hash_key('k1')}', {EXPIRY}, read_per_minute: 5}}\n"
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


DEMO_CONFIG = Path(__file__).parents[1] / "config/demo-competitions.yaml"
DEMO_COMPOSE = Path(__file__).parents[1] / "compose.demo.yaml"
DEMO_DATABASE_URL = "postgresql+psycopg_async://football:0123456789abcdef@db:5432/football"


@pytest.fixture
def demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module.settings, "demo_mode", True)
    monkeypatch.setattr(config_module.settings, "operator_contact", "demo@example.com")
    monkeypatch.setattr(config_module.settings, "database_url", DEMO_DATABASE_URL)
    monkeypatch.setattr(config_module.settings, "api_keys", "")


def check_fails(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    with pytest.raises(SystemExit) as error:
        cli(["check", *argv])
    assert error.value.code == 2
    return capsys.readouterr().err


@pytest.mark.usefixtures("demo_mode")
def test_demo_mode_accepts_the_demo_competitions_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        f"keys:\n  - {{name: partner, hash: '{access.hash_key('client')}', {EXPIRY}}}\n"
    )

    assert cli(["check", "--config-file", str(DEMO_CONFIG), "--api-keys-file", str(keys)]) == 0
    output = capsys.readouterr().out
    assert "(1 competitions)" in output
    assert "Demo mode: read-only (no admin keys), providers limited to openligadb" in output
    assert set(load_config(DEMO_CONFIG).to_competitions()) == {"3-liga"}


@pytest.mark.usefixtures("demo_mode")
def test_demo_mode_requires_an_operator_contact(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config_module.settings, "operator_contact", " ")

    error = check_fails(capsys, "--config-file", str(DEMO_CONFIG))

    assert "FOOTBALL_RESULTS_OPERATOR_CONTACT is not set" in error


@pytest.mark.usefixtures("demo_mode")
@pytest.mark.parametrize("password", ["football", "change-me", "short-password", ""])
def test_demo_mode_rejects_a_weak_database_password_without_printing_it(
    password: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"postgresql+psycopg_async://football:{password}@db:5432/football"
    monkeypatch.setattr(config_module.settings, "database_url", url)

    error = check_fails(capsys, "--config-file", str(DEMO_CONFIG))

    assert "the database password is missing, a shipped default, or shorter than 16" in error
    assert not password or password not in error


@pytest.mark.usefixtures("demo_mode")
def test_demo_mode_rejects_the_shipped_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    error = check_fails(capsys, "--config-file", str(DEFAULT_CONFIG))

    assert "FOOTBALL_RESULTS_DEMO_MODE is on" in error
    assert (
        "competition 'premier-league' uses provider 'espn', which is not publicly "
        "redistributable (allowed: openligadb)"
    ) in error
    assert "competition 'ekstraklasa' uses fallback_provider 'thesportsdb'" in error
    assert "'3-liga'" not in error


@pytest.mark.usefixtures("demo_mode")
def test_demo_mode_rejects_a_non_redistributable_fallback_provider(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_load(DEMO_CONFIG.read_text())
    data["providers"]["thesportsdb"] = {"min_request_interval_seconds": 3}
    competition = data["competitions"][0]
    competition["fallback_provider"] = "thesportsdb"
    competition["seasons"]["2026-2027"]["fallback_url"] = (
        "https://www.thesportsdb.com/league/4426?season=2026-2027"
    )
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))
    assert load_config(path).competitions[0].fallback_provider == "thesportsdb"

    error = check_fails(capsys, "--config-file", str(path))

    assert "competition '3-liga' uses fallback_provider 'thesportsdb'" in error
    assert "uses provider 'openligadb'" not in error


@pytest.mark.usefixtures("demo_mode")
def test_demo_mode_rejects_admin_keys_without_printing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(config_module.settings, "api_keys", "env-secret-key")
    keys = tmp_path / "keys.yaml"
    admin_hash = access.hash_key("admin-secret-key")
    keys.write_text(
        "keys:\n"
        f"  - {{name: partner, hash: '{access.hash_key('client')}', {EXPIRY}}}\n"
        f"  - {{name: ops, hash: '{admin_hash}', {EXPIRY}, role: admin}}\n"
        "  - {name: sso-admin, subject: user-123, role: admin}\n"
    )

    error = check_fails(capsys, "--config-file", str(DEMO_CONFIG), "--api-keys-file", str(keys))

    assert "FOOTBALL_RESULTS_API_KEYS is set" in error
    assert "API keys file entry 'ops' has role admin" in error
    assert "API keys file entry 'sso-admin' has role admin" in error
    assert "'partner'" not in error
    for secret in ("env-secret-key", "admin-secret-key", admin_hash, "user-123"):
        assert secret not in error


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's `!override` merge tag as the plain sequence."""


ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def test_demo_compose_override_hardens_the_public_origin() -> None:
    text = DEMO_COMPOSE.read_text()
    compose = yaml.load(text, Loader=ComposeLoader)
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:${API_PORT:-8000}:8000"]
    required = "${POSTGRES_PASSWORD:?"
    assert compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"].startswith(required)
    for name in ("api", "scheduler"):
        url = compose["services"][name]["environment"]["FOOTBALL_RESULTS_DATABASE_URL"]
        assert required in url
    assert "POSTGRES_PASSWORD:-" not in text


def test_demo_compose_override_runs_the_demo_without_admin_keys() -> None:
    compose = yaml.load(DEMO_COMPOSE.read_text(), Loader=ComposeLoader)
    target = "/app/config/demo-competitions.yaml"
    for name in ("api", "scheduler"):
        service = compose["services"][name]
        environment = service["environment"]
        assert environment["FOOTBALL_RESULTS_DEMO_MODE"] == "true"
        assert environment["FOOTBALL_RESULTS_CONFIG_FILE"] == target
        assert environment["FOOTBALL_RESULTS_LOG_FORMAT"] == "json"
        assert environment["FOOTBALL_RESULTS_API_KEYS"] == ""
        assert f"./config/demo-competitions.yaml:{target}:ro" in service["volumes"]
    api = compose["services"]["api"]["environment"]
    assert api["FOOTBALL_RESULTS_RATE_LIMIT_READ_PER_MINUTE"] == "${DEMO_READ_PER_MINUTE:-30}"
    assert (DEMO_COMPOSE.parent / "config/demo-competitions.yaml").is_file()


@pytest.mark.parametrize(
    ("dates", "message"),
    [
        ("", "set issued_at and expires_at"),
        ("issued_at: '2026-01-01T00:00:00Z', expires_at: '2026-01-01T00:00:00Z'", "later than"),
        ("issued_at: '2026-01-01T00:00:00Z', expires_at: '2027-01-02T00:00:00Z'", "365 days"),
    ],
)
def test_key_entries_need_an_expiry_within_a_year(dates: str, message: str) -> None:
    entry = f"{{name: app, hash: '{access.hash_key('k')}'{', ' + dates if dates else ''}}}"
    with pytest.raises(ValidationError, match=message):
        access.KeysFile.model_validate(yaml.safe_load(f"keys:\n  - {entry}\n"))


def test_oauth_subject_entries_have_no_expiry() -> None:
    entry = f"{{name: sso, subject: user-1, {EXPIRY}}}"
    with pytest.raises(ValidationError, match="apply only to key hashes"):
        access.KeysFile.model_validate(yaml.safe_load(f"keys:\n  - {entry}\n"))


def test_expired_keys_do_not_resolve() -> None:
    keys = access.file_keys_from(
        access.KeysFile.model_validate(
            yaml.safe_load(f"keys:\n  - {{name: app, hash: '{access.hash_key('k')}', {EXPIRY}}}\n")
        )
    )
    assert access.resolve("k", (), keys, now=NOW).name == "app"
    assert access.resolve("k", (), keys, now=NOW + timedelta(days=29)) is None


def test_keys_commands_issue_extend_list_and_revoke(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = tmp_path / "keys.yaml"
    assert cli(["keys", "--file", str(keys), "issue", "--name", "alice"]) == 0
    output = capsys.readouterr()
    key = output.out.strip()
    assert key.startswith(access.KEY_PREFIX)
    assert "alice" in output.err and key not in output.err
    assert keys.stat().st_mode & 0o777 == 0o644  # hashes only; the container user reads it
    (entry,) = access.load_keys(keys)
    assert access.resolve(key, (), (entry,)).name == "alice"
    assert entry.expires_at - entry.issued_at == timedelta(days=access.DEFAULT_KEY_DAYS)

    with pytest.raises(SystemExit):
        cli(["keys", "--file", str(keys), "issue", "--name", "alice"])
    assert "exists" in capsys.readouterr().err

    assert cli(["keys", "--file", str(keys), "extend", "--name", "alice", "--days", "200"]) == 0
    (extended,) = access.load_keys(keys)
    assert extended.digest == entry.digest and extended.issued_at == entry.issued_at
    assert extended.expires_at - extended.issued_at >= timedelta(days=200)
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli(["keys", "--file", str(keys), "extend", "--name", "alice", "--days", "366"])
    assert "between 1 and 365" in capsys.readouterr().err

    assert cli(["keys", "--file", str(keys), "list"]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert listing[0].split() == ["NAME", "ROLE", "KIND", "ISSUED", "EXPIRES", "STATUS"]
    assert listing[1].split()[:3] == ["alice", "client", "key"]
    assert listing[1].split()[-1] == "ok"

    assert cli(["keys", "--file", str(keys), "revoke", "--name", "alice"]) == 0
    assert access.load_keys(keys) == ()


def test_extend_refuses_more_than_a_year_after_issue_and_fixes_legacy_entries(
    tmp_path: Path,
) -> None:
    keys = tmp_path / "keys.yaml"
    issued = NOW - timedelta(days=300)
    keys.write_text(
        "# Partner keys\n"
        "keys:\n"
        f"  - {{name: old, hash: '{access.hash_key('a')}', "
        f"issued_at: '{access.format_time(issued)}', "
        f"expires_at: '{access.format_time(issued + timedelta(days=30))}'}}\n"
        f"  - {{name: legacy, hash: '{access.hash_key('b')}'}}\n"
    )
    with pytest.raises(ValueError, match="set issued_at and expires_at"):
        access.load_keys(keys)
    with pytest.raises(access.KeyCommandError, match="rotation"):
        access.extend_key(keys, "old", 90, NOW)
    access.extend_key(keys, "old", 65, NOW)

    legacy = access.extend_key(keys, "legacy", 30, NOW)
    assert legacy.issued_at == NOW and legacy.expires_at == NOW + timedelta(days=30)
    assert keys.read_text().startswith("# Partner keys\n")
    assert [key.name for key in access.load_keys(keys)] == ["old", "legacy"]


def test_keys_file_watcher_reloads_changes_and_keeps_keys_on_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    keys = tmp_path / "keys.yaml"
    access.issue_key(keys, "alice", "client", 30, NOW)
    watcher = access.KeysFileWatcher(keys, interval=5)
    assert [key.name for key in watcher.load(0)] == ["alice"]

    access.issue_key(keys, "bob", "client", 30, NOW)
    assert [key.name for key in watcher.poll(1)] == ["alice"]
    with caplog.at_level("INFO"):
        assert [key.name for key in watcher.poll(6)] == ["alice", "bob"]
    assert "bob" in caplog.text

    keys.write_text("keys: [oops")
    assert [key.name for key in watcher.poll(12)] == ["alice", "bob"]
    assert "previous keys stay in use" in caplog.text


def private_settings(**overrides: object) -> config_module.Settings:
    values = {
        "anonymous_access": False,
        "api_keys": "",
        "api_keys_file": Path("keys.yaml"),
        "public_base_url": "https://football.example.com",
        "demo_mode": False,
    }
    return config_module.Settings(**(values | overrides))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_keys": "env-key"}, "FOOTBALL_RESULTS_API_KEYS is set"),
        ({"api_keys_file": None}, "FOOTBALL_RESULTS_API_KEYS_FILE is not set"),
        ({"public_base_url": None}, "PUBLIC_BASE_URL is not set"),
        ({"public_base_url": "http://football.example.com"}, "is not https"),
        ({"demo_mode": True}, "DEMO_MODE is on"),
    ],
)
def test_private_instances_need_file_keys_and_https(
    overrides: dict[str, object], message: str
) -> None:
    assert config_module.private_instance_errors(private_settings()) == []
    assert (
        config_module.private_instance_errors(
            private_settings(public_base_url="http://localhost:8000")
        )
        == []
    )
    errors = config_module.private_instance_errors(private_settings(**overrides))
    assert len(errors) == 1 and message in errors[0]
    assert "env-key" not in errors[0]
