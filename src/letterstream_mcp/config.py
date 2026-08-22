"""Configuration loading: CLI overrides environment, environment overrides file.

This module owns two rules that the rest of the package depends on:

1. No credential ever appears as a literal in this repository. ``api_id`` and
   ``api_key`` come from a CLI flag, an environment variable, or a config file
   the operator wrote. If none of those supply them, :func:`load_config` raises
   :class:`~letterstream_mcp.errors.CredentialsError` with a message naming
   every place it looked. There is no fallback value.

2. Live mode is off unless the operator turns it on in a config file or in the
   environment. :func:`load_config` accepts no CLI override for it and the MCP
   tool layer exposes no parameter for it. See :attr:`Config.live`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, CredentialsError

DEFAULT_BASE_URL = "https://www.letterstream.com/apis/index.php"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_PROOF_TTL_SECONDS = 86_400
DEFAULT_STATE_DIRNAME = "state"

ENV_CONFIG_PATH = "LETTERSTREAM_MCP_CONFIG"
ENV_API_ID = "LETTERSTREAM_API_ID"
ENV_API_KEY = "LETTERSTREAM_API_KEY"
ENV_LIVE = "LETTERSTREAM_LIVE"
ENV_STATE_DIR = "LETTERSTREAM_STATE_DIR"

#: Config file locations searched in order when no path is given explicitly.
#: Paths are expanded at call time so tests and operators can redirect HOME.
CONFIG_SEARCH_PATHS = (
    Path("config.toml"),
    Path("~/.config/letterstream-mcp/config.toml"),
)

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"", "0", "false", "no", "off"})

VALID_MAIL_TYPES = (
    "firstclass",
    "firstclass_hse",
    "certified",
    "certnoerr",
    "postcard",
    "flat",
    "propostcard",
)


@dataclass(frozen=True)
class MailDefaults:
    """Optional per-submission defaults. Any field may be ``None``."""

    mail_type: str | None = None
    coversheet: str | None = None
    duplex: str | None = None
    ink: str | None = None
    return_envelope: str | None = None


@dataclass(frozen=True)
class Config:
    """Resolved configuration.

    ``api_key`` is held in memory only. Nothing in this package writes it to
    disk, includes it in a log line, or returns it from an MCP tool; see
    :meth:`redacted` for what is safe to show.
    """

    api_id: str
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    #: When False, no HTTP request is made to LetterStream by any operation.
    live: bool = False
    proof_ttl_seconds: int = DEFAULT_PROOF_TTL_SECONDS
    max_authorize_cost_usd: float | None = None

    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIRNAME))
    defaults: MailDefaults = field(default_factory=MailDefaults)

    #: Human-readable note about where credentials came from, for diagnostics.
    credential_source: str = "unknown"
    #: Config file actually read, or None if configuration came from elsewhere.
    config_path: Path | None = None

    def redacted(self) -> dict[str, Any]:
        """A dict safe to return from a tool call or print to a terminal.

        Includes whether an api_key is present and its length, never its value
        and never any prefix of it.
        """
        return {
            "api_id": self.api_id,
            "api_key_present": bool(self.api_key),
            "api_key_length": len(self.api_key),
            "base_url": self.base_url,
            "timeout_seconds": self.timeout_seconds,
            "live": self.live,
            "proof_ttl_seconds": self.proof_ttl_seconds,
            "max_authorize_cost_usd": self.max_authorize_cost_usd,
            "state_dir": str(self.state_dir),
            "credential_source": self.credential_source,
            "config_path": str(self.config_path) if self.config_path else None,
            "defaults": {
                "mail_type": self.defaults.mail_type,
                "coversheet": self.defaults.coversheet,
                "duplex": self.defaults.duplex,
                "ink": self.defaults.ink,
                "return_envelope": self.defaults.return_envelope,
            },
        }


def _blank_to_none(value: Any) -> Any:
    """Treat an empty or whitespace-only string as absent.

    config.example.toml ships every value blank, so a blank must mean "not
    set" rather than "set to the empty string".
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _as_bool(value: Any, *, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise ConfigError(
        f"{where} must be true or false (got {value!r}). "
        "Use true/false in config.toml, or true/false/1/0 in the environment."
    )


def _as_float(value: Any, *, where: str) -> float | None:
    value = _blank_to_none(value)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} must be a number (got {value!r}).") from exc


def _as_int(value: Any, *, where: str) -> int | None:
    number = _as_float(value, where=where)
    return None if number is None else int(number)


def find_config_file(explicit: Path | str | None = None) -> Path | None:
    """Locate a config file, or return None when there is none to read.

    Order: explicit path, then ``$LETTERSTREAM_MCP_CONFIG``, then each entry of
    :data:`CONFIG_SEARCH_PATHS`. An explicit path that does not exist is an
    error rather than a silent fall-through, because silently ignoring the file
    the operator named is how the wrong account gets used.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path

    from_env = os.environ.get(ENV_CONFIG_PATH)
    if from_env:
        path = Path(from_env).expanduser()
        if not path.is_file():
            raise ConfigError(
                f"{ENV_CONFIG_PATH} points at {path}, which does not exist."
            )
        return path

    for candidate in CONFIG_SEARCH_PATHS:
        path = candidate.expanduser()
        if path.is_file():
            return path
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def _missing_credentials_message(config_path: Path | None) -> str:
    looked_in = [
        "the --api-id/--api-key command line flags",
        f"the {ENV_API_ID} and {ENV_API_KEY} environment variables",
    ]
    if config_path is not None:
        looked_in.append(f"[credentials] in {config_path}")
    else:
        searched = ", ".join(str(p) for p in CONFIG_SEARCH_PATHS)
        looked_in.append(f"a config.toml (searched: {searched})")
    return (
        "LetterStream API credentials are not configured.\n"
        "Looked in: " + "; ".join(looked_in) + ".\n"
        "To fix this: copy config.example.toml to config.toml and fill in\n"
        "api_id and api_key from your own LetterStream account (My Account ->\n"
        f"API Information), or export {ENV_API_ID} and {ENV_API_KEY}.\n"
        "This project ships no credentials and has no default account."
    )


def load_config(
    *,
    config_path: Path | str | None = None,
    api_id: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    state_dir: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Resolve configuration from CLI arguments, environment, and file.

    Precedence for every value is: the keyword arguments to this function
    (which the CLI populates from its flags), then the environment, then the
    config file, then the documented default.

    ``live`` is the deliberate exception: it has no keyword argument here. It
    can only be turned on by ``LETTERSTREAM_LIVE`` or by ``[safety] live`` in
    the config file.

    Raises:
        ConfigError: the config file is unreadable or holds an unusable value.
        CredentialsError: no api_id/api_key could be found anywhere.
    """
    env = os.environ if env is None else env

    path = find_config_file(config_path)
    raw: dict[str, Any] = _read_toml(path) if path is not None else {}

    creds = raw.get("credentials") or {}
    api_section = raw.get("api") or {}
    safety = raw.get("safety") or {}
    storage = raw.get("storage") or {}
    defaults_section = raw.get("defaults") or {}

    resolved_id = _blank_to_none(api_id)
    resolved_key = _blank_to_none(api_key)
    source_parts: list[str] = []

    if resolved_id is None:
        resolved_id = _blank_to_none(env.get(ENV_API_ID))
        if resolved_id is not None:
            source_parts.append(f"api_id from ${ENV_API_ID}")
    else:
        source_parts.append("api_id from --api-id")
    if resolved_id is None:
        resolved_id = _blank_to_none(creds.get("api_id"))
        if resolved_id is not None:
            source_parts.append(f"api_id from {path}")

    if resolved_key is None:
        resolved_key = _blank_to_none(env.get(ENV_API_KEY))
        if resolved_key is not None:
            source_parts.append(f"api_key from ${ENV_API_KEY}")
    else:
        source_parts.append("api_key from --api-key")
    if resolved_key is None:
        resolved_key = _blank_to_none(creds.get("api_key"))
        if resolved_key is not None:
            source_parts.append(f"api_key from {path}")

    if not resolved_id or not resolved_key:
        raise CredentialsError(_missing_credentials_message(path))

    resolved_id = str(resolved_id).strip()
    resolved_key = str(resolved_key).strip()

    resolved_base = (
        _blank_to_none(base_url)
        or _blank_to_none(env.get("LETTERSTREAM_BASE_URL"))
        or _blank_to_none(api_section.get("base_url"))
        or DEFAULT_BASE_URL
    )
    if not str(resolved_base).lower().startswith(("http://", "https://")):
        raise ConfigError(
            f"[api] base_url must be an http(s) URL (got {resolved_base!r})."
        )

    resolved_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _as_float(
            env.get("LETTERSTREAM_TIMEOUT", api_section.get("timeout_seconds")),
            where="[api] timeout_seconds",
        )
    )
    if resolved_timeout is None:
        resolved_timeout = DEFAULT_TIMEOUT_SECONDS
    if resolved_timeout <= 0:
        raise ConfigError("[api] timeout_seconds must be greater than zero.")

    live_raw = env[ENV_LIVE] if ENV_LIVE in env else safety.get("live", False)
    live = _as_bool(live_raw, where=f"[safety] live / ${ENV_LIVE}")

    ttl = _as_int(safety.get("proof_ttl_seconds"), where="[safety] proof_ttl_seconds")
    if ttl is None:
        ttl = DEFAULT_PROOF_TTL_SECONDS
    if ttl <= 0:
        raise ConfigError("[safety] proof_ttl_seconds must be greater than zero.")

    ceiling = _as_float(
        safety.get("max_authorize_cost_usd"), where="[safety] max_authorize_cost_usd"
    )
    if ceiling is not None and ceiling < 0:
        raise ConfigError("[safety] max_authorize_cost_usd must not be negative.")

    resolved_state = (
        _blank_to_none(state_dir)
        or _blank_to_none(env.get(ENV_STATE_DIR))
        or _blank_to_none(storage.get("state_dir"))
        or DEFAULT_STATE_DIRNAME
    )

    mail_type = _blank_to_none(defaults_section.get("mail_type"))
    if mail_type is not None and mail_type not in VALID_MAIL_TYPES:
        raise ConfigError(
            f"[defaults] mail_type must be one of {', '.join(VALID_MAIL_TYPES)} "
            f"(got {mail_type!r})."
        )

    return Config(
        api_id=resolved_id,
        api_key=resolved_key,
        base_url=str(resolved_base),
        timeout_seconds=float(resolved_timeout),
        live=live,
        proof_ttl_seconds=ttl,
        max_authorize_cost_usd=ceiling,
        state_dir=Path(str(resolved_state)).expanduser(),
        defaults=MailDefaults(
            mail_type=mail_type,
            coversheet=_blank_to_none(defaults_section.get("coversheet")),
            duplex=_blank_to_none(defaults_section.get("duplex")),
            ink=_blank_to_none(defaults_section.get("ink")),
            return_envelope=_blank_to_none(defaults_section.get("return_envelope")),
        ),
        credential_source="; ".join(source_parts) if source_parts else "unknown",
        config_path=path,
    )
