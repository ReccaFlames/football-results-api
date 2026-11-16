from pathlib import Path

import pytest
import yaml

from app.config import ConfigFileWatcher, cli, load_config

DEFAULT_CONFIG = Path(__file__).parents[1] / "config/competitions.yaml"


def test_default_config_is_valid_and_preserves_competitions() -> None:
    config = load_config(DEFAULT_CONFIG)
    competitions = config.to_competitions()

    assert set(competitions) == {
        "ekstraklasa",
        "pierwsza-liga",
        "europa-league",
        "premier-league",
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
