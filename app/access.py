"""Named API keys with roles, per-key quotas, and expiry (ADR 0005, ADR 0008).

Keys come from two places: the optional YAML keys file (`FOOTBALL_RESULTS_API_KEYS_FILE`), which
stores only SHA-256 hashes, and the plain `FOOTBALL_RESULTS_API_KEYS` variable, whose keys are
admin keys named `env`. Supplied keys are compared in constant time with every configured key.
A keys-file entry may name an OAuth token `subject` instead of a key hash (ADR 0006).

Every key hash has an `issued_at` and an `expires_at` at most `MAX_KEY_LIFETIME` later; an
expired key resolves like a wrong one. `KeysFileWatcher` reloads the file when it changes, and
`issue_key`, `extend_key`, and `revoke_key` edit it for `python -m app.config keys`.
"""

import hashlib
import logging
import os
import secrets
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

Role = Literal["client", "admin"]
ROLES: tuple[Role, ...] = ("client", "admin")
ENV_KEY_NAME = "env"
ANONYMOUS = "anonymous"
# Metrics label of a valid OAuth token whose subject is not in the keys file.
OAUTH_NAME = "oauth"
RESERVED_NAMES = frozenset({ENV_KEY_NAME, ANONYMOUS, OAUTH_NAME})
HASH_PREFIX = "sha256:"
KEY_BYTES = 32
# Prefix of generated keys, so a leaked key is easy to find in logs or a repository (ADR 0008).
KEY_PREFIX = "frk_"
DEFAULT_KEY_DAYS = 30
MAX_KEY_LIFETIME = timedelta(days=365)
EXPIRY_WARNING = timedelta(days=14)
# Operations that need no key even on a private instance (ADR 0008): monitoring, the landing
# page's competition list, and access requests (ADR 0010).
PUBLIC_OPERATIONS = frozenset(
    {"get_health", "list_competitions", "create_access_request", "get_access_request"}
)
# How often the API checks the keys file for changes.
RELOAD_INTERVAL_SECONDS = 5.0
# Validation context of the `keys` commands: they may leave older entries without expiry.
ALLOW_MISSING_EXPIRY = "allow_missing_expiry"

KeyName = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")]
KeyHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Subject = Annotated[str, Field(min_length=1, max_length=255)]


def as_utc(value: datetime) -> datetime:
    """A naive date-time in the keys file is UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class KeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: KeyName
    hash: KeyHash | None = None
    subject: Subject | None = None
    role: Role = "client"
    read_per_minute: int | None = Field(default=None, ge=1)
    write_per_minute: int | None = Field(default=None, ge=1)
    issued_at: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else as_utc(value)

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo) -> "KeyEntry":
        if self.name in RESERVED_NAMES:
            raise ValueError(f"name {self.name!r} is reserved")
        if (self.hash is None) == (self.subject is None):
            raise ValueError(f"key {self.name!r}: set exactly one of hash or subject")
        if self.write_per_minute is not None and self.role != "admin":
            raise ValueError(f"key {self.name!r}: write_per_minute is allowed only for admin keys")
        if self.subject is not None:
            if self.issued_at is not None or self.expires_at is not None:
                raise ValueError(
                    f"entry {self.name!r}: issued_at and expires_at apply only to key hashes; "
                    "OAuth tokens expire at the issuer"
                )
            return self
        if self.issued_at is None or self.expires_at is None:
            if (info.context or {}).get(ALLOW_MISSING_EXPIRY):
                return self
            raise ValueError(
                f"key {self.name!r}: set issued_at and expires_at (every key expires, ADR 0008); "
                f"run `python -m app.config keys extend --name {self.name}` or issue a new key"
            )
        if self.expires_at <= self.issued_at:
            raise ValueError(f"key {self.name!r}: expires_at must be later than issued_at")
        if self.expires_at - self.issued_at > MAX_KEY_LIFETIME:
            raise ValueError(
                f"key {self.name!r}: expires_at is more than {MAX_KEY_LIFETIME.days} days after "
                "issued_at; issue a new key instead (rotation)"
            )
        return self


class KeysFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: list[KeyEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "KeysFile":
        names = [item.name for item in self.keys]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate key names: {', '.join(duplicates)}")
        hashes = [item.hash for item in self.keys if item.hash is not None]
        if len(set(hashes)) != len(hashes):
            raise ValueError("two keys have the same hash")
        subjects = [item.subject for item in self.keys if item.subject is not None]
        if len(set(subjects)) != len(subjects):
            raise ValueError("two entries have the same subject")
        return self


@dataclass(frozen=True)
class FileKey:
    """A keys-file entry: an API key (``digest``) or an OAuth token subject (``subject``)."""

    name: str
    digest: bytes | None
    role: Role
    read_per_minute: int | None
    write_per_minute: int | None
    subject: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None

    def identity(self) -> "Identity":
        return Identity(
            self.name,
            self.role,
            self.read_per_minute,
            self.write_per_minute,
            bucket=f"name:{self.name}",
            expires_at=self.expires_at,
        )

    def expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True)
class Identity:
    """Who sent a request with a valid key or token: the entry name (`env` for environment keys,
    `oauth` for token subjects that are not in the keys file)."""

    name: str
    role: Role
    read_per_minute: int | None = None
    write_per_minute: int | None = None
    # Rate-limit bucket; environment keys keep one bucket per key, as before named keys.
    bucket: str = ""
    # When the key stops working (keys-file keys only).
    expires_at: datetime | None = None


def hash_key(key: str) -> str:
    return HASH_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)


def generate_claim_secret() -> str:
    """The one-time secret of an access request (ADR 0010); only its hash is stored."""
    return secrets.token_urlsafe(KEY_BYTES)


def claim_secret_matches(supplied: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_key(supplied).encode(), stored_hash.encode())


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_bytes())
    except OSError as error:
        raise ValueError(f"cannot read API keys file {path}: {error}") from error
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"cannot parse YAML in {path}: {error}") from error


def _validate(path: Path, data: Any, *, allow_missing_expiry: bool = False) -> KeysFile:
    try:
        return KeysFile.model_validate(
            {} if data is None else data,
            context={ALLOW_MISSING_EXPIRY: allow_missing_expiry},
        )
    except ValidationError as error:
        # Never echo input values: a plain key pasted into `hash` must not reach the logs.
        messages = "\n".join(
            f"  {'.'.join(str(part) for part in item['loc']) or '(file)'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise ValueError(f"invalid API keys file {path}:\n{messages}") from None


def parse_keys_file(path: Path) -> KeysFile:
    return _validate(path, _read_yaml(path))


def file_keys_from(keys_file: KeysFile) -> tuple[FileKey, ...]:
    return tuple(
        FileKey(
            name=item.name,
            digest=bytes.fromhex(item.hash.removeprefix(HASH_PREFIX)) if item.hash else None,
            role=item.role,
            read_per_minute=item.read_per_minute,
            write_per_minute=item.write_per_minute,
            subject=item.subject,
            issued_at=item.issued_at,
            expires_at=item.expires_at,
        )
        for item in keys_file.keys
    )


def load_keys(path: Path | None) -> tuple[FileKey, ...]:
    if path is None:
        return ()
    return file_keys_from(parse_keys_file(path))


def env_keys(value: str) -> tuple[str, ...]:
    return tuple(key.strip() for key in value.split(",") if key.strip())


def has_admin_key(environment: Sequence[str], file_keys: Sequence[FileKey]) -> bool:
    return bool(environment) or any(item.role == "admin" for item in file_keys)


def resolve(
    supplied: str,
    environment: Sequence[str],
    file_keys: Sequence[FileKey],
    now: datetime | None = None,
) -> Identity | None:
    """The identity of ``supplied``, or None (also for an expired key); every configured key is
    compared in constant time."""
    if not supplied:
        return None
    found: Identity | None = None
    for key in environment:
        if secrets.compare_digest(supplied.encode(), key.encode()) and found is None:
            found = Identity(ENV_KEY_NAME, "admin", bucket=_env_bucket(supplied))
    digest = _digest(supplied)
    matched: FileKey | None = None
    for item in file_keys:
        if item.digest is None:
            continue
        if secrets.compare_digest(digest, item.digest) and matched is None:
            matched = item
    if found is None and matched is not None and not matched.expired(now or datetime.now(UTC)):
        found = matched.identity()
    return found


def resolve_subject(subject: str, file_keys: Sequence[FileKey]) -> Identity:
    """The identity of a valid token's subject: its keys-file entry, else an `oauth` client
    with the default keyed quotas and a bucket of its own."""
    for item in file_keys:
        if item.subject == subject:
            return item.identity()
    return Identity(
        OAUTH_NAME, "client", bucket="sub:" + hashlib.sha256(subject.encode()).hexdigest()[:16]
    )


def _env_bucket(key: str) -> str:
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def expiry_warnings(file_keys: Sequence[FileKey], now: datetime) -> list[str]:
    """Keys that have expired or expire within `EXPIRY_WARNING`, by name."""
    warnings = []
    for item in file_keys:
        if item.expires_at is None:
            continue
        if item.expired(now):
            warnings.append(f"API key {item.name!r} expired at {format_time(item.expires_at)}")
        elif item.expires_at - now <= EXPIRY_WARNING:
            days = (item.expires_at - now).days
            warnings.append(
                f"API key {item.name!r} expires at {format_time(item.expires_at)} "
                f"(in {days} days); extend it with `keys extend --name {item.name}`"
            )
    return warnings


def key_entry_yaml(name: str, role: Role, key: str, now: datetime, days: int) -> str:
    return (
        f"  - name: {name}\n    hash: {hash_key(key)}\n    role: {role}\n"
        f"    issued_at: '{format_time(now)}'\n"
        f"    expires_at: '{format_time(now + timedelta(days=days))}'\n"
    )


def changed_names(old: Sequence[FileKey], new: Sequence[FileKey]) -> dict[str, list[str]]:
    """Entry names added, removed, or changed between two loads of the keys file."""
    before = {item.name: item for item in old}
    after = {item.name: item for item in new}
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(
            name for name in before.keys() & after.keys() if before[name] != after[name]
        ),
    }


def describe_changes(changes: dict[str, list[str]]) -> str:
    parts = [f"{kind} {', '.join(names)}" for kind, names in changes.items() if names]
    return "; ".join(parts) or "no changes"


class KeysFileWatcher:
    """Reloads the keys file when its modification time, size, or inode changes.

    An invalid file is logged and ignored, so the last valid keys stay in use.
    """

    def __init__(self, path: Path, interval: float = RELOAD_INTERVAL_SECONDS) -> None:
        self.path = path
        self.interval = interval
        self.keys: tuple[FileKey, ...] = ()
        self._signature: tuple[int, int, int] | None = None
        self._checked_at: float | None = None

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size, stat.st_ino)

    def load(self, now: float) -> tuple[FileKey, ...]:
        """The first load: raises ``ValueError`` on an invalid file."""
        signature = self._stat()
        self.keys = load_keys(self.path)
        self._signature, self._checked_at = signature, now
        return self.keys

    def poll(self, now: float) -> tuple[FileKey, ...]:
        """The current keys, reloaded when the file changed since the last check."""
        if self._checked_at is not None and now - self._checked_at < self.interval:
            return self.keys
        self._checked_at = now
        signature = self._stat()
        if signature == self._signature:
            return self.keys
        self._signature = signature
        try:
            keys = load_keys(self.path)
        except ValueError as error:
            logger.error("API keys file not reloaded; the previous keys stay in use: %s", error)
            return self.keys
        logger.info("API keys file reloaded: %s", describe_changes(changed_names(self.keys, keys)))
        self.keys = keys
        return keys


# Editing the keys file (`python -m app.config keys ...`).


class KeyCommandError(ValueError):
    pass


def _raw_entries(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Leading comment lines and the raw entries; a missing file has neither."""
    if not path.exists():
        return [], []
    header = []
    for line in path.read_text().splitlines():
        if not line.startswith("#"):
            break
        header.append(line)
    data = _read_yaml(path)
    if data is None:
        return header, []
    if not isinstance(data, dict) or not isinstance(data.get("keys", []), list):
        raise KeyCommandError(f"invalid API keys file {path}: expected a `keys:` list")
    return header, [dict(item) if isinstance(item, dict) else item for item in data.get("keys", [])]


def _write_entries(path: Path, header: list[str], entries: list[dict[str, Any]]) -> KeysFile:
    keys_file = _validate(path, {"keys": entries}, allow_missing_expiry=True)
    text = yaml.safe_dump({"keys": entries}, sort_keys=False, default_flow_style=False)
    content = "".join(f"{line}\n" for line in header) + text
    # Hashes only, so readable by the container user (UID 10001); an existing mode is kept.
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a new file and rename it, so the API never reads a half-written file.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w") as file:
            file.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return keys_file


def _find(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    raise KeyCommandError(f"no entry named {name!r}")


def _check_days(days: int) -> None:
    if not 1 <= days <= MAX_KEY_LIFETIME.days:
        raise KeyCommandError(f"--days must be between 1 and {MAX_KEY_LIFETIME.days}")


def issue_key(
    path: Path,
    name: str,
    role: Role,
    days: int,
    now: datetime,
    *,
    read_per_minute: int | None = None,
    write_per_minute: int | None = None,
) -> tuple[str, KeyEntry]:
    """Add a new key entry; returns the plain key (shown once) and the stored entry."""
    _check_days(days)
    header, entries = _raw_entries(path)
    if any(isinstance(entry, dict) and entry.get("name") == name for entry in entries):
        raise KeyCommandError(
            f"an entry named {name!r} exists; choose another name (for example {name}-2) or "
            "revoke it first"
        )
    key = generate_key()
    entry: dict[str, Any] = {"name": name, "hash": hash_key(key), "role": role}
    if read_per_minute is not None:
        entry["read_per_minute"] = read_per_minute
    if write_per_minute is not None:
        entry["write_per_minute"] = write_per_minute
    entry["issued_at"] = format_time(now)
    entry["expires_at"] = format_time(now + timedelta(days=days))
    keys_file = _write_entries(path, header, [*entries, entry])
    return key, next(item for item in keys_file.keys if item.name == name)


def extend_key(path: Path, name: str, days: int, now: datetime) -> KeyEntry:
    """Set a key's expiry to ``days`` from now, at most `MAX_KEY_LIFETIME` after its issue."""
    _check_days(days)
    header, entries = _raw_entries(path)
    entry = _find(entries, name)
    if "subject" in entry:
        raise KeyCommandError(f"entry {name!r} is an OAuth subject; it has no expiry")
    issued = entry.get("issued_at")
    if issued is None:
        # An entry from before mandatory expiry starts its lifetime now.
        issued_at = now
        entry["issued_at"] = format_time(now)
    else:
        issued_at = as_utc(issued if isinstance(issued, datetime) else _parse(issued))
    expires_at = now + timedelta(days=days)
    latest = issued_at + MAX_KEY_LIFETIME
    if expires_at > latest:
        raise KeyCommandError(
            f"key {name!r} was issued at {format_time(issued_at)} and may not be valid after "
            f"{format_time(latest)}; issue a new key instead (rotation)"
        )
    entry["expires_at"] = format_time(expires_at)
    keys_file = _write_entries(path, header, entries)
    return next(item for item in keys_file.keys if item.name == name)


def revoke_key(path: Path, name: str) -> None:
    header, entries = _raw_entries(path)
    entry = _find(entries, name)
    _write_entries(path, header, [item for item in entries if item is not entry])


def list_entries(path: Path) -> list[KeyEntry]:
    return _validate(path, _read_yaml(path), allow_missing_expiry=True).keys


def _parse(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as error:
        raise KeyCommandError(f"invalid date-time {value!r}") from error
