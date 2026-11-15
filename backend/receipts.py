"""Signed integrity receipts (improvements.md 2.3).

A receipt is a statement FileGrid signs: "this file, with this SHA-256, is stored here, and
was found intact at this time". The signature is Ed25519. The signing key is derived from
the master key (HKDF), so there is no extra secret to generate, store or lose; the PUBLIC
key is published (`GET /receipts/public-key`) so a client can check a receipt it kept
without trusting our server at all.

Rotating FILEGRID_MASTER_KEY changes the signing key: receipts issued before the rotation
stay valid only against the old public key (record `key_id` to tell them apart).
"""

import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ALG = "Ed25519"
_SALT = b"filegrid-receipts-v1"
_INFO = b"ed25519-signing-key"


def _private_key(master_key: bytes) -> Ed25519PrivateKey:
    if len(master_key) != 32:
        raise ValueError("master key must be 32 bytes")
    seed = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(master_key)
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public_raw(master_key: bytes) -> bytes:
    return _private_key(master_key).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def key_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def public_key_info(master_key: bytes) -> dict:
    raw = _public_raw(master_key)
    return {"alg": ALG, "key_id": key_id(raw), "public_key": base64.b64encode(raw).decode()}


def canonical(payload: dict) -> bytes:
    """The exact bytes that are signed: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(master_key: bytes, payload: dict) -> dict:
    signature = _private_key(master_key).sign(canonical(payload))
    return {
        "payload": payload,
        "signature": base64.b64encode(signature).decode(),
        "alg": ALG,
        "key_id": key_id(_public_raw(master_key)),
    }


def verify(master_key: bytes, receipt: object) -> tuple[bool, str]:
    """(valid, reason). Never raises on malformed input."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("payload"), dict):
        return False, "malformed"
    if receipt.get("alg") != ALG:
        return False, "unsupported_algorithm"
    try:
        signature = base64.b64decode(str(receipt.get("signature", "")), validate=True)
    except ValueError:
        return False, "malformed_signature"
    public = _private_key(master_key).public_key()
    if receipt.get("key_id") not in (None, key_id(_public_raw(master_key))):
        return False, "unknown_key"
    try:
        public.verify(signature, canonical(receipt["payload"]))
    except InvalidSignature:
        return False, "signature_mismatch"
    return True, "ok"


def verify_with_public_key(public_key_b64: str, receipt: dict) -> bool:
    """What an outside party does: only the published public key, no secret."""
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(receipt["signature"]), canonical(receipt["payload"]))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
