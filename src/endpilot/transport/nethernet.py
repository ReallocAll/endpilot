"""NetherNet HTTP signaling for a Bedrock server."""

from cryptography.hazmat.primitives.asymmetric import ec
from nethernet import InvalidIdentity, PlayerSigner, connect_http


async def connect(
    url: str, *, private_key: ec.EllipticCurvePrivateKey | None = None,
    server_key_sha256: str | None = None,
):
    key = private_key or ec.generate_private_key(ec.SECP384R1())

    def verify_pin(server_identity):
        if server_identity.key_digest != server_key_sha256:
            raise InvalidIdentity("NetherNet server identity does not match server_key_sha256")

    return await connect_http(
        url, identity_signer=PlayerSigner(key, domain="self"),
        on_server_identity=verify_pin if server_key_sha256 is not None else None,
    )
