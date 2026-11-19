"""Public player entry point; the opening exchange belongs to JoinSession."""

from .authentication import OfflineIdentity
from .login import JoinSession
from .protocol import Protocol


class Bot:
    def __init__(self, name: str, protocol: int = 2193):
        self.identity = OfflineIdentity(name)
        self.protocol = Protocol(protocol)
        self._session: JoinSession | None = None

    async def join_nethernet(self, url: str, *, timeout: float = 30) -> None:
        if self._session is not None:
            raise RuntimeError("player is already connected")
        self._session = await JoinSession.connect_nethernet(url, self.identity, self.protocol, timeout)

    @property
    def player_id(self) -> int:
        if self._session is None or self._session.player_id is None:
            raise RuntimeError("player has not joined")
        return self._session.player_id

    @property
    def packets(self):
        """Expose typed and raw packet I/O for external extensions."""
        if self._session is None:
            raise RuntimeError("player has not joined")
        return self._session.packets

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
