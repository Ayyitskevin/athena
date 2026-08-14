"""Fail-closed policy for Athena's supported deployment entrypoint.

This module validates facts Athena can actually observe: its declared network
mode, the socket address it is asked to bind, request Host authorities, local
configuration, and durable database state. It deliberately does not claim to
detect Internet reachability; a reverse proxy, tunnel, NAT rule, or container
port publication can expose even a loopback listener.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
import sqlite3
from typing import Iterable
from urllib.parse import urlsplit

from athena.core import oidc, passwords, tokens, users


LOCAL_MODE = "local"
TAILNET_MODE = "tailnet"
NETWORK_MODES = (LOCAL_MODE, TAILNET_MODE)
# Covers the worst-case URL-encoded login envelope for the bounded email and
# password fields, plus the JSON bootstrap envelope. This is a liveness floor,
# not a recommendation to lower the normal 1 MiB default.
MIN_SUPPORTED_REQUEST_BODY_BYTES = 16 * 1024
# Previous releases accepted unbounded password strings under this 1 MiB
# default request cap. A legacy hash cannot reveal its plaintext length, so
# normal upgrades retain that historical envelope until one successful login
# marks the credential bounded, or the operator rotates it.
LEGACY_LOGIN_BODY_BYTES = 1024 * 1024

# TestClient uses a synthetic server address rather than an IP literal. This
# value can only be injected into create_app() by tests; config.py rejects it.
TEST_MODE = "_test"
_TEST_SERVER = "testserver"

_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class Authority:
    """A normalized HTTP Host authority."""

    host: str
    port: int

    def render(self) -> str:
        if ":" in self.host:
            return f"[{self.host}]:{self.port}"
        return f"{self.host}:{self.port}"


def _port(value: str | int) -> int:
    if isinstance(value, str):
        if not value or not value.isascii() or not value.isdecimal():
            raise ValueError("authority port must be a decimal integer")
        parsed = int(value)
    else:
        parsed = value
    if not 1 <= parsed <= 65535:
        raise ValueError("authority port must be between 1 and 65535")
    return parsed


def _dns_name(value: str) -> str:
    lowered = value.lower()
    if (
        not lowered
        or len(lowered) > 253
        or lowered.endswith(".")
        or any(not _DNS_LABEL_RE.fullmatch(label) for label in lowered.split("."))
    ):
        raise ValueError("authority host must be an exact DNS name or IP address")
    # An all-numeric dotted value that is not valid IPv4 must not quietly become
    # a DNS name with surprising resolver semantics.
    if all(character.isdigit() or character == "." for character in lowered):
        raise ValueError("authority host contains an invalid IPv4 address")
    labels = lowered.split(".")
    if any(label.startswith("xn--") for label in labels):
        raise ValueError("internationalized Host authorities are not supported")
    if len(labels) <= 4 and all(
        label.isdecimal()
        or (
            label.startswith("0x")
            and len(label) > 2
            and all(character in _HEX_DIGITS for character in label[2:])
        )
        for label in labels
    ):
        raise ValueError("authority host contains a legacy IPv4 address")
    final_label = labels[-1]
    if (
        final_label.isdecimal()
        or final_label == "0x"
        or (
            final_label.startswith("0x")
            and all(character in _HEX_DIGITS for character in final_label[2:])
        )
    ):
        raise ValueError("authority host contains a browser-numeric final label")
    return lowered


def parse_authority(value: str, *, default_port: int | None = None) -> Authority:
    """Parse one exact DNS/IPv4/bracketed-IPv6 authority.

    Configuration values must include a port. Request Host values may omit it,
    in which case the caller supplies the request scheme's default port.
    """
    if (
        not value
        or not value.isascii()
        or any(character.isspace() for character in value)
        or any(character in value for character in (",", "/", "\\", "@", "#", "?"))
        or value.startswith("*")
    ):
        raise ValueError("authority must be one exact ASCII host and port")

    host: str
    raw_port: str | int
    if value.startswith("["):
        closing = value.find("]")
        if closing <= 1:
            raise ValueError("authority contains malformed IPv6 brackets")
        host_value = value[1:closing]
        suffix = value[closing + 1 :]
        if "]" in suffix or "%" in host_value:
            raise ValueError("authority contains malformed IPv6 brackets")
        if suffix:
            if not suffix.startswith(":") or suffix.count(":") != 1:
                raise ValueError("authority contains malformed IPv6 brackets")
            raw_port = suffix[1:]
        elif default_port is not None:
            raw_port = default_port
        else:
            raise ValueError("configured authority must include an explicit port")
        try:
            ipv6_address = ipaddress.IPv6Address(host_value)
        except ipaddress.AddressValueError as exc:
            raise ValueError("authority contains an invalid IPv6 address") from exc
        host = ipv6_address.compressed
    else:
        if value.count(":") > 1:
            raise ValueError("IPv6 authorities must use brackets")
        if ":" in value:
            host_value, raw_port = value.rsplit(":", 1)
        elif default_port is not None:
            host_value, raw_port = value, default_port
        else:
            raise ValueError("configured authority must include an explicit port")
        if not host_value:
            raise ValueError("authority host is empty")
        try:
            parsed_address = ipaddress.ip_address(host_value)
        except ValueError:
            host = _dns_name(host_value)
        else:
            if isinstance(parsed_address, ipaddress.IPv6Address):
                raise ValueError("IPv6 authorities must use brackets")
            host = str(parsed_address)

    return Authority(host=host, port=_port(raw_port))


def normalize_authorities(values: Iterable[str]) -> tuple[Authority, ...]:
    """Validate, normalize, and de-duplicate configured authorities."""
    normalized: list[Authority] = []
    seen: set[Authority] = set()
    for value in values:
        authority = parse_authority(value)
        if authority not in seen:
            normalized.append(authority)
            seen.add(authority)
    return tuple(normalized)


def _mapped_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def address_allowed(host: str, network_mode: str) -> bool:
    """Whether a numeric accepted-socket or bind address fits the mode."""
    if network_mode == TEST_MODE:
        return host == _TEST_SERVER
    if network_mode not in NETWORK_MODES:
        return False
    if "%" in host:
        return False
    try:
        address = _mapped_address(ipaddress.ip_address(host))
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if network_mode == LOCAL_MODE:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address in _TAILSCALE_V4
    return address in _TAILSCALE_V6


def server_address_matches(
    host: str,
    port: int,
    expected: tuple[str, int],
) -> bool:
    """Whether an ASGI server address is the exact preflighted listener."""
    if isinstance(port, bool) or port != expected[1]:
        return False
    try:
        observed_address = _mapped_address(ipaddress.ip_address(host))
        expected_address = _mapped_address(ipaddress.ip_address(expected[0]))
    except ValueError:
        return False
    return observed_address == expected_address


def validate_bind(host: str, port: int, network_mode: str) -> tuple[str, int]:
    """Return a normalized supported bind before Athena accepts traffic."""
    if network_mode not in NETWORK_MODES:
        raise ValueError(
            "ATHENA_NETWORK_MODE must be one of: " + ", ".join(NETWORK_MODES)
        )
    if "%" in host:
        raise ValueError("bind host must not contain an IPv6 scope identifier")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("bind host must be a numeric IP address") from exc
    normalized = str(_mapped_address(address))
    if not address_allowed(normalized, network_mode):
        raise ValueError(
            f"bind address is not permitted in {network_mode!r} network mode"
        )
    return normalized, _port(port)


def _authority_is_local(authority: Authority) -> bool:
    if authority.host == "localhost":
        return True
    try:
        address = _mapped_address(ipaddress.ip_address(authority.host))
    except ValueError:
        return False
    return address.is_loopback


def _numeric_authority_allowed(authority: Authority, network_mode: str) -> bool:
    """Constrain numeric authorities to addresses meaningful in the mode."""
    try:
        ipaddress.ip_address(authority.host)
    except ValueError:
        return True
    return address_allowed(authority.host, network_mode)


def normalize_runtime_authorities(
    values: Iterable[str], *, network_mode: str
) -> tuple[Authority, ...]:
    """Compile request-time authorities without claiming knowledge of the bind."""
    if network_mode not in (*NETWORK_MODES, TEST_MODE):
        raise ValueError("unsupported Athena network mode")
    authorities = normalize_authorities(values)
    if network_mode == LOCAL_MODE and any(
        not _authority_is_local(authority) for authority in authorities
    ):
        raise ValueError(
            "local network mode permits only localhost or loopback Host authorities"
        )
    if network_mode == TAILNET_MODE and any(
        not _numeric_authority_allowed(authority, network_mode)
        for authority in authorities
    ):
        raise ValueError(
            "tailnet network mode permits numeric Host authorities only for "
            "loopback or Tailscale addresses"
        )
    return authorities


def local_authorities(bind_host: str, port: int) -> tuple[str, ...]:
    """The natural exact Host values for one local listener."""
    validated_port = _port(port)
    numeric = Authority(bind_host, validated_port).render()
    if bind_host in {"127.0.0.1", "::1"}:
        return (f"localhost:{validated_port}", numeric)
    return (numeric,)


def _numeric_authority_matches_bind(authority: Authority, bind_host: str) -> bool:
    try:
        authority_address = _mapped_address(ipaddress.ip_address(authority.host))
        bind_address = _mapped_address(ipaddress.ip_address(bind_host))
    except ValueError:
        return True
    return authority_address == bind_address


def validate_authorities(
    values: Iterable[str],
    *,
    network_mode: str,
    bind_host: str,
    bind_port: int,
) -> tuple[Authority, ...]:
    """Validate the exact Host allowlist for one supported listener."""
    configured = tuple(values)
    if not configured:
        if network_mode == TAILNET_MODE:
            raise ValueError(
                "ATHENA_ALLOWED_AUTHORITIES is required in tailnet network mode"
            )
        configured = local_authorities(bind_host, bind_port)
    authorities = normalize_authorities(configured)
    if not authorities:
        raise ValueError("ATHENA_ALLOWED_AUTHORITIES must not be empty")
    if network_mode == LOCAL_MODE and any(
        not _authority_is_local(authority) for authority in authorities
    ):
        raise ValueError(
            "local network mode permits only localhost or loopback Host authorities"
        )
    if network_mode == TAILNET_MODE and any(
        not _numeric_authority_allowed(authority, network_mode)
        for authority in authorities
    ):
        raise ValueError(
            "tailnet network mode permits numeric Host authorities only for "
            "loopback or Tailscale addresses"
        )
    if any(
        not _numeric_authority_matches_bind(authority, bind_host)
        for authority in authorities
    ):
        raise ValueError(
            "numeric ATHENA_ALLOWED_AUTHORITIES entries must exactly match "
            "the listener address"
        )
    bind_accepts_localhost = bind_host in {"127.0.0.1", "::1"}
    if not bind_accepts_localhost and any(
        authority.host == "localhost" or authority.host.endswith(".localhost")
        for authority in authorities
    ):
        raise ValueError("localhost Host authorities require a loopback listener")
    if any(authority.port != bind_port for authority in authorities):
        raise ValueError(
            "every ATHENA_ALLOWED_AUTHORITIES entry must use the listener's exact port"
        )
    return authorities


def request_authority_allowed(
    headers: Iterable[tuple[bytes, bytes]],
    *,
    scheme: str,
    allowed: tuple[Authority, ...],
) -> bool:
    """Require exactly one well-formed Host header in the configured allowlist."""
    hosts = [value for name, value in headers if name.lower() == b"host"]
    if len(hosts) != 1:
        return False
    try:
        raw = hosts[0].decode("ascii")
        default_port = 443 if scheme == "https" else 80
        authority = parse_authority(raw, default_port=default_port)
    except (UnicodeDecodeError, ValueError):
        return False
    return authority in allowed


def validate_supported_oidc_recovery(
    *,
    issuer: str | None,
    redirect_url: str | None,
    allowed_authorities: tuple[Authority, ...],
) -> str | None:
    """Validate the observable OIDC facts used as an admin recovery path.

    Discovery reachability and IdP registration remain external operator facts.
    The supported direct-HTTP launcher can, however, prove that the issuer is a
    structurally usable HTTPS identifier and that the callback returns to its
    exact listener boundary.
    """
    if issuer is None and redirect_url is None:
        return None
    if not issuer or not redirect_url:
        raise ValueError("supported OIDC recovery requires issuer and redirect URL")
    if (
        any(not 0x21 <= ord(character) <= 0x7E for character in issuer)
        or any(not 0x21 <= ord(character) <= 0x7E for character in redirect_url)
        or "\\" in issuer
        or "\\" in redirect_url
    ):
        raise ValueError(
            "supported OIDC URLs must contain visible ASCII URL characters only"
        )

    try:
        issuer_parts = urlsplit(issuer)
        issuer_host = issuer_parts.hostname
        issuer_port = issuer_parts.port
    except ValueError as exc:
        raise ValueError("ATHENA_OIDC_ISSUER must be a valid HTTPS issuer URL") from exc
    if (
        issuer_parts.scheme != "https"
        or not issuer_parts.netloc
        or not issuer_host
        or issuer_parts.username is not None
        or issuer_parts.password is not None
        or issuer_parts.query
        or issuer_parts.fragment
    ):
        raise ValueError("ATHENA_OIDC_ISSUER must be a valid HTTPS issuer URL")
    try:
        parse_authority(
            issuer_parts.netloc,
            default_port=issuer_port or 443,
        )
    except ValueError as exc:
        raise ValueError("ATHENA_OIDC_ISSUER must be a valid HTTPS issuer URL") from exc

    try:
        redirect_parts = urlsplit(redirect_url)
        redirect_port = redirect_parts.port
    except ValueError as exc:
        raise ValueError(
            "ATHENA_OIDC_REDIRECT_URL must target the supported listener"
        ) from exc
    if (
        redirect_parts.scheme != "http"
        or not redirect_parts.netloc
        or redirect_parts.username is not None
        or redirect_parts.password is not None
        or redirect_parts.path != "/auth/callback"
        or redirect_parts.query
        or redirect_parts.fragment
    ):
        raise ValueError(
            "ATHENA_OIDC_REDIRECT_URL must be direct HTTP at /auth/callback"
        )
    try:
        callback_authority = parse_authority(
            redirect_parts.netloc,
            default_port=redirect_port or 80,
        )
    except ValueError as exc:
        raise ValueError(
            "ATHENA_OIDC_REDIRECT_URL must target the supported listener"
        ) from exc
    if callback_authority not in allowed_authorities:
        raise ValueError(
            "ATHENA_OIDC_REDIRECT_URL authority must exactly match "
            "ATHENA_ALLOWED_AUTHORITIES"
        )
    return issuer


def validate_static_configuration(
    *,
    db_path: Path,
    attach_dir: Path,
    network_mode: str,
    bind_host: str,
    bind_port: int,
    allowed_authorities: Iterable[str],
    bootstrap: bool,
    bootstrap_token: str,
    trust_actor_header: bool,
    cookie_secure: bool,
    oidc_issuer: str | None,
    oidc_redirect_url: str | None,
    max_request_body_bytes: int,
    token_rate_limit_per_minute: int,
    anon_rate_limit_per_minute: int,
    login_rate_limit_per_minute: int,
    login_account_rate_limit_per_minute: int,
) -> tuple[Authority, ...]:
    """Validate static deployment policy before SQLite or accepted traffic."""
    if not db_path.is_absolute():
        raise ValueError("ATHENA_DB must be an absolute path")
    if not attach_dir.is_absolute():
        raise ValueError("ATHENA_ATTACH_DIR must be an absolute path")
    if trust_actor_header:
        raise ValueError("ATHENA_TRUST_ACTOR_HEADER must be disabled for athena-serve")
    if cookie_secure:
        raise ValueError(
            "ATHENA_COOKIE_SECURE must be disabled for direct-HTTP athena-serve"
        )
    if bootstrap:
        if network_mode != LOCAL_MODE:
            raise ValueError("bootstrap mode is permitted only in local network mode")
        if not bootstrap_token:
            raise ValueError(
                "ATHENA_BOOTSTRAP_TOKEN must be configured for --bootstrap"
            )
    elif bootstrap_token:
        raise ValueError("ATHENA_BOOTSTRAP_TOKEN must be removed before normal startup")
    if 0 < max_request_body_bytes < MIN_SUPPORTED_REQUEST_BODY_BYTES:
        raise ValueError(
            "ATHENA_MAX_REQUEST_BODY_BYTES must be 0 (local only) or at least "
            f"{MIN_SUPPORTED_REQUEST_BODY_BYTES} for athena-serve"
        )
    if network_mode == TAILNET_MODE:
        required_limits = {
            "ATHENA_MAX_REQUEST_BODY_BYTES": max_request_body_bytes,
            "ATHENA_TOKEN_RATE_LIMIT_PER_MINUTE": token_rate_limit_per_minute,
            "ATHENA_ANON_RATE_LIMIT_PER_MINUTE": anon_rate_limit_per_minute,
            "ATHENA_LOGIN_RATE_LIMIT_PER_MINUTE": login_rate_limit_per_minute,
            "ATHENA_LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE": (
                login_account_rate_limit_per_minute
            ),
        }
        disabled = sorted(name for name, value in required_limits.items() if value <= 0)
        if disabled:
            raise ValueError(
                "tailnet network mode requires positive request limits: "
                + ", ".join(disabled)
            )
    authorities = validate_authorities(
        allowed_authorities,
        network_mode=network_mode,
        bind_host=bind_host,
        bind_port=bind_port,
    )
    validate_supported_oidc_recovery(
        issuer=oidc_issuer,
        redirect_url=oidc_redirect_url,
        allowed_authorities=authorities,
    )
    return authorities


def has_configured_active_admin_credential(
    conn: sqlite3.Connection,
    *,
    enabled_oidc_issuer: str | None,
    max_request_body_bytes: int = LEGACY_LOGIN_BODY_BYTES,
) -> bool:
    """Whether an active administrator has a configured durable login path."""
    active_admins = conn.execute(
        "SELECT id, email, password_hash FROM users "
        "WHERE role = ? AND paused_at IS NULL ORDER BY id",
        (users.ADMIN_ROLE,),
    ).fetchall()
    for user in active_admins:
        password_hash = user["password_hash"]
        legacy_password_envelope = (
            max_request_body_bytes == 0
            or max_request_body_bytes >= LEGACY_LOGIN_BODY_BYTES
        )
        if (
            users.is_browser_login_email(user["email"])
            and passwords.is_valid_hash(password_hash)
            and (passwords.is_bounded_hash(password_hash) or legacy_password_envelope)
        ):
            return True
        if tokens.has_live_scope_credential(
            conn,
            user_id=user["id"],
            scope=tokens.ADMIN_SCOPE,
        ):
            return True
        if enabled_oidc_issuer and any(
            identity["issuer"] == enabled_oidc_issuer
            and isinstance(identity["subject"], str)
            and bool(identity["subject"])
            for identity in oidc.list_identities(conn, user["id"])
        ):
            return True
    return False


def check_database_posture(
    conn: sqlite3.Connection,
    *,
    bootstrap: bool,
    enabled_oidc_issuer: str | None,
    max_request_body_bytes: int = LEGACY_LOGIN_BODY_BYTES,
) -> str:
    """Validate bootstrap or normal-start authority against durable state."""
    if bootstrap:
        if users.count_users(conn) != 0:
            raise ValueError("bootstrap startup requires a database with zero users")
        return "bootstrap database: ok (zero users)"

    active_admins = users.count_active_admins(conn)
    if active_admins == 0:
        raise ValueError("deployment requires at least one active administrator")
    if not has_configured_active_admin_credential(
        conn,
        enabled_oidc_issuer=enabled_oidc_issuer,
        max_request_body_bytes=max_request_body_bytes,
    ):
        raise ValueError(
            "deployment requires an active administrator with a password, "
            "live admin-scoped token, or linked configured OIDC identity"
        )
    return f"administrator recovery: ok ({active_admins} active)"
