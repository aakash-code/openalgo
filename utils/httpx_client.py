"""
Shared httpx client module with connection pooling support for all broker APIs
with automatic protocol negotiation (HTTP/2 when available, HTTP/1.1 fallback)
"""

from typing import Optional

import httpx

from utils.logging import get_logger

# Set up logging
logger = get_logger(__name__)

# Global httpx client for connection pooling
_httpx_client = None


def get_httpx_client() -> httpx.Client:
    """
    Returns an HTTP client with automatic protocol negotiation.
    The client will use HTTP/2 when the server supports it,
    otherwise automatically falls back to HTTP/1.1.

    Returns:
        httpx.Client: A configured HTTP client with protocol auto-negotiation
    """
    global _httpx_client

    if _httpx_client is None:
        _httpx_client = _create_http_client()
        logger.info(
            "Created shared HTTP client (protocol negotiated per _create_http_client)"
        )
    return _httpx_client


# Methods safe to replay: no server-side state change, so a retry cannot place a
# duplicate order or cancel something twice. POST/PUT/DELETE are NEVER retried.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# A pooled keepalive connection the peer closed while idle fails on first read.
# The pool discards the dead connection as part of raising, so one immediate
# replay lands on a fresh connection. Without this, a single dropped connection
# turned into a failed chart backfill — precisely when a feed stall had just
# made that backfill necessary.
_RETRYABLE_ERRORS = (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError)


def _request_with_retry(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """Send a request, replaying idempotent ones once on a transport-level failure."""
    try:
        return client.request(method, url, **kwargs)
    except _RETRYABLE_ERRORS as e:
        if method.upper() not in _IDEMPOTENT_METHODS:
            raise
        logger.warning(f"{type(e).__name__} on {method} {url[:80]} — retrying once: {e}")
        return client.request(method, url, **kwargs)


def request(method: str, url: str, **kwargs) -> httpx.Response:
    """
    Make an HTTP request using the shared client with automatic protocol negotiation.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: URL to request
        **kwargs: Additional arguments to pass to the request

    Returns:
        httpx.Response: The HTTP response

    Raises:
        httpx.HTTPError: If the request fails
    """
    import time

    from flask import g

    client = get_httpx_client()

    # Track actual broker API call time for latency monitoring
    broker_api_start = time.time()
    response = _request_with_retry(client, method, url, **kwargs)
    broker_api_end = time.time()

    # Store broker API time in Flask's g object for latency tracking
    if hasattr(g, "latency_tracker"):
        broker_api_time_ms = (broker_api_end - broker_api_start) * 1000
        g.broker_api_time = broker_api_time_ms
        logger.debug(f"Broker API call took {broker_api_time_ms:.2f}ms")

    # Log the actual HTTP version used (info level for visibility)
    if response.http_version:
        logger.info(f"Request used {response.http_version} - URL: {url[:50]}...")

    return response


# Shortcut methods for common HTTP methods
def get(url: str, **kwargs) -> httpx.Response:
    """
    Send a GET request.

    Args:
        url (str): The URL to send the GET request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    """
    Send a POST request.

    Args:
        url (str): The URL to send the POST request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("POST", url, **kwargs)


def put(url: str, **kwargs) -> httpx.Response:
    """
    Send a PUT request.

    Args:
        url (str): The URL to send the PUT request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("PUT", url, **kwargs)


def delete(url: str, **kwargs) -> httpx.Response:
    """
    Send a DELETE request.

    Args:
        url (str): The URL to send the DELETE request to.
        **kwargs: Additional arguments passed to the underlying request method.

    Returns:
        httpx.Response: The HTTP response from the server.
    """
    return request("DELETE", url, **kwargs)


def _create_http_client() -> httpx.Client:
    """
    Create a new HTTP client with automatic protocol negotiation and latency tracking.
    Enables both HTTP/2 and HTTP/1.1, letting httpx choose the best protocol.

    Returns:
        httpx.Client: A configured HTTP client with protocol auto-negotiation and timing hooks
    """
    import os
    import time

    from flask import g

    # Event hooks for tracking broker API timing
    def log_request(request):
        """Hook called before request is sent"""
        request.extensions["start_time"] = time.time()
        logger.debug(f"Starting request to {request.url}")

    def log_response(response):
        """Hook called after response is received"""
        try:
            start_time = response.request.extensions.get("start_time")
            if start_time:
                duration_ms = (time.time() - start_time) * 1000

                # Store broker API time in Flask's g object for latency tracking
                try:
                    from flask import has_request_context

                    if has_request_context() and hasattr(g, "latency_tracker"):
                        g.broker_api_time = duration_ms
                        logger.debug(f"Broker API call took {duration_ms:.2f}ms")
                except (RuntimeError, AttributeError):
                    # Not in Flask request context or g not available
                    pass

                logger.debug(f"Request completed in {duration_ms:.2f}ms")
        except Exception as e:
            logger.exception(f"Error in response hook: {e}")

    try:
        # Detect if running in standalone mode (Docker/production) vs integrated mode (local dev)
        # In standalone mode, disable HTTP/2 to avoid protocol negotiation issues
        app_mode = os.environ.get("APP_MODE", "integrated").strip().strip("'\"")
        is_standalone = app_mode == "standalone"

        # HTTP/2 is OFF by default in every mode (standalone already disabled it
        # for "protocol negotiation issues" — the same root cause).
        #
        # httpcore's *sync* HTTP/2 connection multiplexes every concurrent
        # request onto ONE TCP connection and drives a shared h2 state machine.
        # Under eventlet-patched (non-blocking) sockets with many greenlets in
        # flight — e.g. a chart backfilling 100 symbols at once — reads race and
        # surface as `httpx.ReadError: [Errno 35] Resource temporarily
        # unavailable` from httpcore/_sync/http2.py::_receive_event. Observed
        # 249 times in one session on 2026-07-31, all on /historical-candle,
        # which is exactly the call that repairs a chart after a feed stall.
        #
        # HTTP/1.1 over the existing 40-connection keepalive pool gives real
        # parallelism instead of head-of-line blocking on a single stream, and
        # removes the single connection whose GOAWAY previously took out both
        # /history and the tick feed at once.
        http2_enabled = os.environ.get("HTTPX_HTTP2", "0").strip() == "1"

        client = httpx.Client(
            http2=http2_enabled,  # Disable HTTP/2 in standalone mode, enable in integrated mode
            http1=True,  # Always enable HTTP/1.1 for compatibility
            timeout=120.0,  # Increased timeout for large historical data requests
            limits=httpx.Limits(
                max_keepalive_connections=40,  # Increased from 20 for multi-strategy environments
                max_connections=100,  # Increased from 50 for 10+ concurrent strategies
                keepalive_expiry=30.0,  # Reduced from 120s to recycle stale connections faster
            ),
            # Add verify parameter to handle SSL/TLS issues in standalone mode
            verify=True,  # Can be set to False for debugging SSL issues (not recommended for production)
            # Add event hooks for latency tracking
            event_hooks={"request": [log_request], "response": [log_response]},
        )

        logger.info(
            f"HTTP client ready (mode={'standalone' if is_standalone else 'integrated'}, "
            f"http2={'enabled' if http2_enabled else 'disabled'})"
        )

        return client

    except Exception as e:
        logger.exception(f"Failed to create HTTP client: {e}")
        raise


def cleanup_httpx_client() -> None:
    """
    Closes the global httpx client and releases its resources.

    Should be called when the application is shutting down to prevent
    resource leaks.

    Returns:
        None
    """
    global _httpx_client

    if _httpx_client is not None:
        _httpx_client.close()
        _httpx_client = None
        logger.info("Closed HTTP client")
