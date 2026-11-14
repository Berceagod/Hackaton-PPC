"""Slice F: AES-256-GCM encryption of personal data before it reaches any storage."""

import asyncio
import hashlib
import json
import uuid

import pytest

from crypto_envelope import (
    MAGIC,
    SEGMENT,
    DecryptionError,
    decrypt_stream,
    encrypt_stream,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
FID = "6f1c2a8e-0000-4000-8000-000000000001"
# synthetic personal data (fake CNP / name / address), never real
PII = b"CNP 1960101123456 | Popescu Ion | Str. Exemplu 7, Ploiesti | "
DOC = PII * 3000  # ~180 KB: several 64 KB segments


async def _src(data: bytes, piece: int = 10_000):
    for i in range(0, len(data), piece):
        yield data[i : i + piece]


async def _collect(agen) -> bytes:
    out = bytearray()
    async for c in agen:
        out += c
    return bytes(out)


def _enc(data: bytes, key=KEY, fid=FID) -> bytes:
    return asyncio.run(_collect(encrypt_stream(_src(data), key, fid)))


def _dec(blob: bytes, key=KEY, fid=FID) -> bytes:
    return asyncio.run(_collect(decrypt_stream(_src(blob, 7_777), key, fid)))


# ---------------- the cipher itself ----------------

@pytest.mark.parametrize("size", [0, 1, SEGMENT - 1, SEGMENT, SEGMENT + 1, 3 * SEGMENT + 5])
def test_roundtrip_any_size(size):
    data = bytes((i * 7) % 251 for i in range(size))
    blob = _enc(data)
    assert blob.startswith(MAGIC)
    assert _dec(blob) == data


def test_ciphertext_contains_no_plaintext_and_differs_each_time():
    a, b = _enc(DOC), _enc(DOC)
    assert b"1960101123456" not in a and b"Popescu" not in a
    assert a != b  # random nonce per encryption


def test_encryption_streams_in_segments():
    pieces = asyncio.run(_collect_list(encrypt_stream(_src(DOC), KEY, FID)))
    assert len(pieces) > 2 and max(len(p) for p in pieces) <= SEGMENT + 16


async def _collect_list(agen):
    return [c async for c in agen]


@pytest.mark.parametrize("attack", ["flip", "truncate_segment", "truncate_bytes", "append",
                                    "swap", "drop_header"])
def test_any_tampering_is_detected(attack):
    blob = bytearray(_enc(DOC))
    seg = SEGMENT + 16
    head = len(MAGIC) + 8
    if attack == "flip":
        blob[head + 100] ^= 1
    elif attack == "truncate_segment":
        blob = blob[: head + seg]          # drop the last segments: the new last one is not final
    elif attack == "truncate_bytes":
        blob = blob[:-3]
    elif attack == "append":
        blob += b"\x00" * 20
    elif attack == "swap":
        blob[head:head + seg], blob[head + seg:head + 2 * seg] = (
            blob[head + seg:head + 2 * seg], blob[head:head + seg])
    elif attack == "drop_header":
        blob = blob[head:]
    with pytest.raises(DecryptionError):
        _dec(bytes(blob))


def test_wrong_key_or_wrong_file_cannot_decrypt():
    blob = _enc(DOC)
    with pytest.raises(DecryptionError):
        _dec(blob, key=OTHER_KEY)
    with pytest.raises(DecryptionError):
        _dec(blob, fid="6f1c2a8e-0000-4000-8000-000000000002")  # ciphertext moved to another file


# ---------------- through the API ----------------

def _upload(client, auth, data=DOC, category="connection_docs", name="cerere_racordare.pdf"):
    return client.post("/api/v1/ingest", headers=auth, files={"file": (name, data)},
                       data={"category": category, "metadata": json.dumps({"zone": "Banat"})})


def test_connection_doc_is_encrypted_at_rest_and_readable_through_filegrid(client, auth,
                                                                         ftp_storage, repo):
    r = _upload(client, auth)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["storage"] == "ftp" and body["sha256"] == hashlib.sha256(DOC).hexdigest()

    stored = next(iter(ftp_storage.objects.values()))
    assert stored.startswith(MAGIC) and b"1960101123456" not in stored and b"Popescu" not in stored
    rec = repo.files[body["file_id"]]
    assert rec.encrypted is True and rec.size_bytes == len(DOC)  # plaintext size

    d = client.get(f"/api/v1/files/{body['file_id']}/download", headers=auth)
    assert d.status_code == 200 and d.content == DOC
    assert d.headers["x-integrity-verified"] == "true"
    assert d.headers["x-file-sha256"] == hashlib.sha256(DOC).hexdigest()
    v = client.get(f"/api/v1/files/{body['file_id']}/verify", headers=auth).json()
    assert v["verified"] is True
    listed = client.get("/api/v1/files?category=connection_docs", headers=auth).json()
    assert listed[0]["encrypted"] is True


def test_tampered_ciphertext_is_refused_and_audited(client, auth, ftp_storage):
    fid = _upload(client, auth).json()["file_id"]
    key = next(iter(ftp_storage.objects))
    blob = bytearray(ftp_storage.objects[key])
    blob[len(MAGIC) + 8 + 50] ^= 0xFF
    ftp_storage.objects[key] = bytes(blob)
    d = client.get(f"/api/v1/files/{fid}/download", headers=auth)
    assert d.status_code == 409 and d.json()["reason"] == "decryption_failed"
    assert d.headers["x-integrity-verified"] == "false"
    events = [e["event"] for e in client.get(f"/api/v1/files/{fid}/audit", headers=auth).json()]
    assert "download_blocked" in events


def test_encryption_follows_the_policy_for_any_category_and_storage(client, auth, admin,
                                                                   storage, pg_storage):
    plain = _upload(client, auth, category="outage_records").json()["file_id"]
    for target, backend in (("s3", storage), ("postgres", pg_storage)):
        r = client.put("/api/v1/admin/policies", headers=admin, json={"outage_records": {
            "storage": target, "encryption": True, "deduplicate": False, "priority": "high"}})
        assert r.status_code == 200
        fid = _upload(client, auth, category="outage_records").json()["file_id"]
        obj = backend.objects[f"outage_records/{fid}"]
        assert obj.startswith(MAGIC) and b"Popescu" not in obj
        assert client.get(f"/api/v1/files/{fid}/download", headers=auth).content == DOC
    # a file stored before encryption was switched on stays readable
    assert storage.objects[f"outage_records/{plain}"] == DOC
    assert client.get(f"/api/v1/files/{plain}/download", headers=auth).content == DOC


def test_chunked_upload_never_leaves_plaintext_in_staging(client, auth, storage, ftp_storage,
                                                         repo):
    uid = str(uuid.uuid4())
    parts = [DOC[:70_000], DOC[70_000:140_000], DOC[140_000:]]
    staged_seen = []
    for i, part in enumerate(parts):
        r = client.post("/api/v1/upload/chunk", headers=auth, files={"file_chunk": ("b", part)},
                        data={"upload_id": uid, "chunk_index": str(i), "total_chunks": "3",
                              "filename": "aviz_tehnic.pdf", "category": "connection_docs",
                              "metadata": "{}",
                              "chunk_sha256": hashlib.sha256(part).hexdigest()})
        assert r.status_code == 200, r.text
        staged_seen += [v for k, v in storage.objects.items() if k.startswith("_staging/")]
    assert staged_seen and all(s.startswith(MAGIC) and b"Popescu" not in s for s in staged_seen)
    body = r.json()
    assert body["status"] == "complete" and body["storage"] == "ftp"
    final = ftp_storage.objects[f"connection_docs/{body['file_id']}"]
    assert final.startswith(MAGIC) and b"Popescu" not in final
    assert repo.files[body["file_id"]].encrypted is True
    d = client.get(f"/api/v1/files/{body['file_id']}/download", headers=auth)
    assert d.content == DOC


def test_encrypted_meter_readings_stay_idempotent(client, admin, pg_storage):
    client.put("/api/v1/admin/policies", headers=admin, json={"meter_readings": {
        "storage": "postgres", "encryption": True, "deduplicate": True, "priority": "normal"}})
    reading = json.dumps({"meter_id": "RO-BAN-000001", "timestamp": "2026-11-14T10:00:00.000Z",
                          "zone": "Banat"}).encode()
    sim = {"Authorization": "Bearer sim-token"}

    def send():
        return client.post("/api/v1/ingest", headers=sim, files={"file": ("r.json", reading)},
                           data={"category": "meter_readings"})

    first = send()
    assert first.status_code == 202, first.text
    assert send().status_code == 409
    stored = next(iter(pg_storage.objects.values()))
    assert stored.startswith(MAGIC) and b"RO-BAN-000001" not in stored
    d = client.get(f"/api/v1/files/{first.json()['file_id']}/download", headers=admin)
    assert d.status_code == 200 and d.content == reading


def test_master_key_must_be_32_bytes_of_hex(monkeypatch):
    from config import parse_master_key

    assert parse_master_key("ab" * 32) == bytes([0xAB]) * 32
    for bad in ("", "ab" * 31, "zz" * 32, "ab" * 33):
        with pytest.raises(RuntimeError):
            parse_master_key(bad)
