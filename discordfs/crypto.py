"""Compression and AES-256 encryption via pyzipper."""

from __future__ import annotations

import io

import pyzipper

# Internal filename inside the encrypted zip — never the real filename
_INNER_NAME = "data.bin"


def encrypt_data(data: bytes, password: str) -> bytes:
    """Compress with deflate and encrypt with AES-256 into a zip archive.

    Returns the raw bytes of the encrypted zip.
    """
    buf = io.BytesIO()
    with pyzipper.AESZipFile(
        buf,
        "w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr(_INNER_NAME, data)
    return buf.getvalue()


def decrypt_data(encrypted: bytes, password: str) -> bytes:
    """Decrypt and decompress an AES-256 encrypted zip archive.

    Returns the original file bytes.
    """
    buf = io.BytesIO(encrypted)
    with pyzipper.AESZipFile(buf, "r") as zf:
        zf.setpassword(password.encode("utf-8"))
        return zf.read(_INNER_NAME)
