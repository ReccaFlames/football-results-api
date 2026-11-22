"""Named API keys with roles and per-key quotas (ADR 0005).

Keys come from two places: the optional YAML keys file (`FOOTBALL_RESULTS_API_KEYS_FILE`), which
stores only SHA-256 hashes, and the plain `FOOTBALL_RESULTS_API_KEYS` variable, whose keys are
admin keys named `env`. Supplied keys are compared in constant time with every configured key.
"""

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Role = Literal["client", "admin"]
ROLES: tuple[Role, ...] = ("client", "admin")
ENV_KEY_NAME = "env"
ANONYMOUS = "anonymous"
RESERVED_NAMES = frozenset({ENV_KEY_NAME, ANONYMOUS})
HASH_PREFIX = "sha256:"
KEY_BYTES = 32

KeyName = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")]
KeyHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class KeyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: KeyName
    hash: KeyHash
    role: Role = "client"
    read_per_minute: int | None = Field(default=None, ge=1)
    write_per_minute: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "KeyEntry":
        if self.name in RESERVED_NAMES:
            raise ValueError(f"name {self.name!r} is reserved")
        if self.write_per_minute is not None and self.role != "admin":
            raise ValueError(f"key {self.name!r}: write_per_minute is allowed only for admin keys")
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
        hashes = [item.hash for item in self.keys]
        if len(set(hashes)) != len(hashes):
            raise ValueError("two keys have the same hash")
        return self


@dataclass(frozen=True)
class FileKey:
    name: str
    digest: bytes
    role: Role
    read_per_minute: int | None
    write_per_minute: int | None


@dataclass(frozen=True)
class Identity:
    """Who sent a request with a valid key: the key name (`env` for environment keys)."""

    name: str
    role: Role
    read_per_minute: int | None = None
    write_per_minute: int | None = None
    # Rate-limit bucket; environment keys keep one bucket per key, as before named keys.
    bucket: str = ""


def hash_key(key: str) -> str:
    return HASH_PREFIX + hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def parse_keys_file(path: Path) -> KeysFile:
    try:
        data = yaml.safe_load(path.read_bytes())
    except OSError as error:
        raise ValueError(f"cannot read API keys file {path}: {error}") from error
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"cannot parse YAML in {path}: {error}") from error
    if data is None:
        data = {}
    try:
        return KeysFile.model_validate(data)
    except ValidationError as error:
        # Never echo input values: a plain key pasted into `hash` must not reach the logs.
        messages = "\n".join(
            f"  {'.'.join(str(part) for part in item['loc']) or '(file)'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        raise ValueError(f"invalid API keys file {path}:\n{messages}") from None


def load_keys(path: Path | None) -> tuple[FileKey, ...]:
    if path is None:
        return ()
    return tuple(
        FileKey(
            name=item.name,
            digest=bytes.fromhex(item.hash.removeprefix(HASH_PREFIX)),
            role=item.role,
            read_per_minute=item.read_per_minute,
            write_per_minute=item.write_per_minute,
        )
        for item in parse_keys_file(path).keys
    )


def env_keys(value: str) -> tuple[str, ...]:
    return tuple(key.strip() for key in value.split(",") if key.strip())


def has_admin_key(environment: Sequence[str], file_keys: Sequence[FileKey]) -> bool:
    return bool(environment) or any(item.role == "admin" for item in file_keys)


def resolve(
    supplied: str, environment: Sequence[str], file_keys: Sequence[FileKey]
) -> Identity | None:
    """The identity of ``supplied``, or None; every configured key is compared in constant time."""
    if not supplied:
        return None
    found: Identity | None = None
    for key in environment:
        if secrets.compare_digest(supplied.encode(), key.encode()) and found is None:
            found = Identity(ENV_KEY_NAME, "admin", bucket=_env_bucket(supplied))
    digest = _digest(supplied)
    for item in file_keys:
        if secrets.compare_digest(digest, item.digest) and found is None:
            found = Identity(
                item.name,
                item.role,
                item.read_per_minute,
                item.write_per_minute,
                bucket=f"name:{item.name}",
            )
    return found


def _env_bucket(key: str) -> str:
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def key_entry_yaml(name: str, role: Role, key: str) -> str:
    return f"  - name: {name}\n    hash: {hash_key(key)}\n    role: {role}\n"
