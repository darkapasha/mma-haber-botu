"""Paylaşılan asenkron HTTP istemcisi (curl_cffi, Chrome TLS/HTTP2 parmak izi taklidi)."""
from __future__ import annotations

import asyncio
import logging

from curl_cffi.requests import AsyncSession, Response
from curl_cffi.requests.exceptions import RequestException

log = logging.getLogger(__name__)

IMPERSONATE = "chrome120"
# User-Agent bilerek verilmiyor: impersonate, TLS parmak iziyle uyumlu Chrome başlıklarını kendisi ekler.
DEFAULT_HEADERS = {"Accept-Language": "en-US,en;q=0.9"}
RETRY_STATUSES = {429, 500, 502, 504}

# Diğer modüller curl_cffi'ye doğrudan bağımlı olmasın diye dışarı açılır
NetworkError = RequestException

_session: AsyncSession | None = None


def session() -> AsyncSession:
    global _session
    if _session is None:
        _session = AsyncSession(
            impersonate=IMPERSONATE,
            headers=DEFAULT_HEADERS,
            timeout=20,
            allow_redirects=True,
            max_clients=20,
        )
    return _session


async def get(url: str, *, params: dict | None = None, headers: dict | None = None,
              retries: int = 2) -> Response:
    """GET isteği; geçici hatalarda artan beklemeyle yeniden dener."""
    for attempt in range(retries + 1):
        try:
            resp = await session().get(url, params=params, headers=headers)
        except RequestException as exc:
            if attempt >= retries:
                raise
            log.debug("HTTP hata (%s), yeniden deneniyor: %s", exc, url)
        else:
            if resp.status_code not in RETRY_STATUSES or attempt >= retries:
                return resp
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


async def close() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None