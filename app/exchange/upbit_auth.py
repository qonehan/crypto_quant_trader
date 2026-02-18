"""Upbit REST API JWT 인증 헬퍼 (HS256 + SHA512 query_hash)."""
from __future__ import annotations

import hashlib
import uuid

import jwt  # PyJWT


def make_auth_header(
    access_key: str,
    secret_key: str,
    query_params: dict | None = None,
) -> dict[str, str]:
    """JWT HS256 인증 헤더를 생성합니다.

    query_params가 있으면 SHA512 query_hash를 payload에 포함합니다.
    Upbit 공식 인증 스펙 준수.
    """
    payload: dict = {
        "access_key": access_key,
        "nonce": str(uuid.uuid4()),
    }

    if query_params:
        query_string = "&".join(f"{k}={v}" for k, v in query_params.items())
        m = hashlib.sha512()
        m.update(query_string.encode())
        payload["query_hash"] = m.hexdigest()
        payload["query_hash_alg"] = "SHA512"

    token = jwt.encode(payload, secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}
