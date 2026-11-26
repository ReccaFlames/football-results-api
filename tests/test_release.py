import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("set_demo_host", ROOT / "scripts/set_demo_host.py")
assert spec and spec.loader
set_demo_host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(set_demo_host)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    for path in (set_demo_host.SERVER_JSON, set_demo_host.CLIENT):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / path, tmp_path / path)
    return tmp_path


def test_setting_the_demo_host_replaces_every_placeholder(checkout: Path) -> None:
    assert len(set_demo_host.placeholders(checkout)) == 2

    set_demo_host.set_demo_host("Football.Example.com", checkout)

    assert set_demo_host.placeholders(checkout) == []
    server = (checkout / set_demo_host.SERVER_JSON).read_text()
    assert '"url": "https://football.example.com/mcp"' in server
    assert "https://{host}/mcp" in server
    client = (checkout / set_demo_host.CLIENT).read_text()
    assert 'DEMO_URL: str | None = "https://football.example.com"' in client


@pytest.mark.parametrize(
    "host", ["https://football.example.com", "football.example.com/x", "localhost", "a b.com"]
)
def test_only_a_bare_public_host_name_is_accepted(checkout: Path, host: str) -> None:
    with pytest.raises(ValueError, match="not a host name"):
        set_demo_host.set_demo_host(host, checkout)
    assert len(set_demo_host.placeholders(checkout)) == 2


def test_the_demo_host_is_set_only_once(checkout: Path) -> None:
    set_demo_host.set_demo_host("football.example.com", checkout)
    with pytest.raises(ValueError, match="already set"):
        set_demo_host.set_demo_host("other.example.com", checkout)
