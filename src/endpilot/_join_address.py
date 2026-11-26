"""Parse the endpoint shared by the public address-based join and the CLI."""

from dataclasses import dataclass
from ipaddress import IPv6Address
import re
from urllib.parse import urlsplit


_DEFAULT_PORTS = {"https": 443, "http": 80, "raknet": 19132}
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True, slots=True)
class _JoinAddress:
    value: str
    scheme: str | None
    host: str
    port: int

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def _parse_join_address(value: str) -> _JoinAddress:
    if not isinstance(value, str) or not value or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "\\?#" for char in value
    ):
        raise ValueError("invalid server address")

    explicit = "://" in value
    try:
        url = urlsplit(value if explicit else f"//{value}")
        scheme = url.scheme if explicit else None
        host, port = url.hostname, url.port
        if not host or url.username is not None or (explicit and scheme not in _DEFAULT_PORTS):
            raise ValueError
        if url.path and (not explicit or (scheme == "raknet" and url.path != "/")):
            raise ValueError
        if url.netloc.startswith("["):
            if "%" in host:
                raise ValueError
            _, _, suffix = url.netloc.partition("]")
            if suffix and not suffix.startswith(":"):
                raise ValueError
            host = str(IPv6Address(host))
        else:
            host = host.encode("idna").decode("ascii")
            name = host.removesuffix(".")
            if len(name) > 253 or any(_DNS_LABEL.fullmatch(label) is None for label in name.split(".")):
                raise ValueError
        if port == 0 or url.netloc.endswith(":"):
            raise ValueError
        if port is None:
            if scheme is None:
                raise ValueError
            port = _DEFAULT_PORTS[scheme]
    except ValueError as error:
        raise ValueError("invalid server address") from error
    return _JoinAddress(value, scheme, host, port)
