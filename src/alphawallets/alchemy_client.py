"""Web3 client wrapper for Alchemy RPC.

Thin layer over web3.py's HTTPProvider that:
- Centralizes Alchemy URL construction per chain
- Validates connectivity at instantiation
- Redacts the API key from any exception raised by RPC calls, so
  tracebacks in CI logs, error reporters, or terminal output don't
  leak credentials

The redaction is defense-in-depth. The API key still lives in
process memory and .env; this only prevents accidental exposure
through logged exceptions.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from web3 import Web3
from web3.providers.rpc import HTTPProvider

from alphawallets.config import Chain, get_alchemy_api_key, get_alchemy_rpc_url

_URL_REDACTED = "***REDACTED***"


def _redact(text: str, api_key: str) -> str:
    """Replace occurrences of api_key in text with a placeholder."""
    if not text or not api_key:
        return text
    return text.replace(api_key, _URL_REDACTED)


def _scrub_exception(e: BaseException, api_key: str) -> None:
    """Redact api_key from an exception's args and nested request/response URLs.

    Modifies the exception in place. Safe to call on any exception; only
    string args and known URL-bearing attributes are touched.
    """
    # Scrub args
    e.args = tuple(_redact(str(a), api_key) if isinstance(a, str) else a for a in e.args)
    # Scrub URL attributes on nested request/response objects (HTTPError carries these)
    for attr in ("request", "response"):
        obj = getattr(e, attr, None)
        if obj is not None:
            url = getattr(obj, "url", None)
            if isinstance(url, str) and api_key in url:
                # Some request objects have read-only url; skip silently
                with suppress(AttributeError):
                    obj.url = _redact(url, api_key)


class _RedactingHTTPProvider(HTTPProvider):
    """HTTPProvider that redacts the API key from exception messages.

    web3.py raises exceptions whose str() includes the full endpoint URL,
    which for Alchemy contains the API key as a path segment. Overriding
    make_request lets us catch every RPC error and re-raise with a
    scrubbed message and scrubbed nested URL attributes.
    """

    def __init__(self, endpoint_uri: str, api_key: str, **kwargs: Any) -> None:
        super().__init__(endpoint_uri, **kwargs)
        self._api_key = api_key

    def make_request(self, method: Any, params: Any) -> Any:
        try:
            return super().make_request(method, params)
        except Exception as e:
            _scrub_exception(e, self._api_key)
            raise


def make_web3(chain: Chain) -> Web3:
    """Return a connected Web3 instance for the given chain.

    Validates connectivity with `is_connected()` — one round-trip per call.
    Cheap in absolute terms (~10 CU on Alchemy) but fetchers should still
    create a single instance up front and reuse it across the block loop,
    not call this inside a hot path.

    All exceptions raised through the returned Web3's HTTP provider have
    the API key scrubbed from their args and nested request/response URLs
    before propagation.

    Raises ConnectionError if the RPC endpoint isn't reachable — validating
    connectivity here means the fetcher fails at startup, not mid-loop.
    """
    api_key = get_alchemy_api_key()
    url = get_alchemy_rpc_url(chain)
    provider = _RedactingHTTPProvider(url, api_key=api_key, request_kwargs={"timeout": 30})
    w3 = Web3(provider)

    try:
        if not w3.is_connected():
            raise ConnectionError(f"Cannot connect to Alchemy for {chain}")
    except Exception as e:
        _scrub_exception(e, api_key)
        raise

    return w3
