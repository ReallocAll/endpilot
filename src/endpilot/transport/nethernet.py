"""NetherNet HTTP signaling for a Bedrock server."""

from cryptography.hazmat.primitives.asymmetric import ec
from nethernet import IdentitySigner, connect_http


async def connect(url: str):
    key = ec.generate_private_key(ec.SECP384R1())
    return await connect_http(url, identity_signer=IdentitySigner(key, domain="self"))
