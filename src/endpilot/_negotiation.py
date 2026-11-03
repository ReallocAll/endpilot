"""Connection negotiation and readiness prerequisites for a single Session."""

import bedrock_protocol as bp

from .types import BotState


class _Negotiation:
    """Own negotiation phases without performing transport I/O or publishing events."""

    def __init__(self, p, options):
        self.p = p
        self.options = options
        self.settings = False
        self.handshake = False
        self.login = False
        self.start_game = False
        self.spawn = False
        self.initialized = False

    @property
    def pinned_raknet(self):
        return self.options.transport == "raknet" and bool(self.options.server_key_sha256)

    def blocks_gameplay(self, packet_id):
        return self.pinned_raknet and not self.login and packet_id not in (143, 3, 2, 5)

    def require_settings(self, state):
        if self.settings or state != BotState.negotiating:
            raise RuntimeError("unexpected NetworkSettings")

    def require_handshake(self):
        if not self.settings or self.handshake or self.login:
            raise RuntimeError("unexpected server handshake")

    def accept_login_success(self):
        if not self.settings:
            raise RuntimeError("LoginSuccess before settings")
        if self.pinned_raknet and not self.handshake:
            raise RuntimeError("LoginSuccess before pinned server handshake")
        if self.login:
            return False
        self.login = True
        return True

    def accept_spawn(self):
        if self.pinned_raknet and not self.login:
            return False
        self.spawn = True
        return True

    def can_initialize(self, chunk_radius):
        return (
            not self.initialized
            and self.login
            and (not self.pinned_raknet or self.handshake)
            and self.start_game
            and self.spawn
            and chunk_radius > 0
        )

    def resource_pack_response(self, packet_id):
        p = self.p
        if self.options.protocol == 2193:
            cls = p.ResourcePackClientResponsePacket
            response = bp.Variant(
                3 if packet_id == 7 else 2,
                cls.ResourcePackStackFinished() if packet_id == 7 else cls.DownloadingFinished(),
            )
            return cls(response=response)
        response = (
            p.ResourcePackResponse.RESOURCE_PACK_STACK_FINISHED
            if packet_id == 7
            else p.ResourcePackResponse.DOWNLOADING_FINISHED
        )
        return p.ResourcePackClientResponsePacket(response=response, downloading_packs=[])
