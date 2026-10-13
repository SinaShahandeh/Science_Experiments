"""HTTP client for mhapi-service, shared by the `mhapi` CLI and a Python harness. Standard library only.

    from mhapi.client import call
    status, body = call("get_raw_opacity", {"label": "P3-A1"}, timeout=60)

Each command is `POST /v1/<command>` with its arguments as a JSON body; the response body is the one
JSON object the CLI prints. Errors from the service carry an `error` code (Model_Hardware_API_spec.md,
"Errors"). The service listens on http://127.0.0.1:8742 unless MHAPI_URL says otherwise.
"""
import json
import os
import socket
import urllib.error
import urllib.request

from . import DEFAULT_URL


class ServiceUnavailable(Exception):
    """The service isn't running or can't be reached (`service_unavailable`)."""


class ClientTimeout(Exception):
    """The caller stopped waiting (`client_timeout`); a started operation goes on in the service."""


# Never route localhost calls through a proxy from the environment or the system settings.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def base_url():
    return os.environ.get("MHAPI_URL", DEFAULT_URL).rstrip("/")


def call(command, args=None, timeout=60.0, url=None):
    """POST /v1/<command> and return (http_status, body). Raises ServiceUnavailable or ClientTimeout."""
    req = urllib.request.Request(f"{url or base_url()}/v1/{command}", data=json.dumps(args or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, _json(r.read(), r.status)
    except urllib.error.HTTPError as e:  # error responses carry the same JSON body
        return e.code, _json(e.read(), e.code)
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise ClientTimeout(f"no answer within {timeout:g} s") from None
        raise ServiceUnavailable(f"can't reach the service at {url or base_url()}: {e.reason}") from None
    except (socket.timeout, TimeoutError):
        raise ClientTimeout(f"no answer within {timeout:g} s") from None
    except (ConnectionError, OSError) as e:
        raise ServiceUnavailable(f"connection to the service failed: {e}") from None


def _json(raw, status):
    try:
        body = json.loads(raw)
    except ValueError:
        raise ServiceUnavailable(f"the service answered HTTP {status} without a JSON body") from None
    if not isinstance(body, dict):
        raise ServiceUnavailable(f"the service answered HTTP {status} with a non-object body")
    return body
