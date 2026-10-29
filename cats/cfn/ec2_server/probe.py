# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Python 3.9 syntax, stdlib only, embedded verbatim: see README.md in this directory before editing.
from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import traceback
from typing import Any

TIMEOUT_SECONDS = 2
HTTP_TIMEOUT = 10


class ResolutionFailure(Exception):
    pass


class NoHttpReply(OSError):
    pass


RESULT_BY_EXCEPTION: dict[type[BaseException], str] = {
    ResolutionFailure: "DNS_FAILURE",
    ssl.SSLError: "TLS_FAILURE",
    ConnectionRefusedError: "REFUSED",
    ConnectionResetError: "REFUSED",
    socket.timeout: "TIMEOUT",
    TimeoutError: "TIMEOUT",
    OSError: "FAILURE",
    http.client.HTTPException: "FAILURE",
}

DNS_ERRNOS = {socket.EAI_NONAME, socket.EAI_AGAIN, socket.EAI_FAIL, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}


def classify(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror) and exc.errno in DNS_ERRNOS:
        return "DNS_FAILURE"
    for cls in type(exc).__mro__:
        if cls in RESULT_BY_EXCEPTION:
            return RESULT_BY_EXCEPTION[cls]
    return "ERROR"


def _resolve(url: str, port: int, socktype: int) -> list[tuple[Any, ...]]:
    try:
        return socket.getaddrinfo(url, port, type=socktype)
    except socket.gaierror:
        raise
    except OSError as e:
        raise ResolutionFailure(str(e)) from None


def _attempt_http(url: str, port: int, scheme: str) -> int:
    _attempt_tcp(url, port)
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = cls(url, port, timeout=HTTP_TIMEOUT)
    try:
        if scheme == "http":
            conn.connect()
        try:
            conn.request("GET", "/")
            return conn.getresponse().status
        except (TimeoutError, socket.timeout) as e:
            raise NoHttpReply(f"connected; no HTTP reply in {HTTP_TIMEOUT}s: {e}") from None
    finally:
        conn.close()


def _attempt_tcp(url: str, port: int) -> None:
    addresses = _resolve(url, port, socket.SOCK_STREAM)
    for i, (family, socktype, proto, _, address) in enumerate(addresses):
        try:
            with socket.socket(family, socktype, proto) as s:
                s.settimeout(TIMEOUT_SECONDS)
                s.connect(address)
            return
        except OSError:
            if i == len(addresses) - 1:
                raise


def _attempt_udp(url: str, port: int, message: str) -> None:
    family, _, _, _, address = _resolve(url, port, socket.SOCK_DGRAM)[0]
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.sendto(message.encode(), address)


def _failure(exc: BaseException) -> dict[str, object]:
    result = classify(exc)
    if result == "ERROR":
        traceback.print_exc()
    return {"result": result, "reason": str(exc), "http_status": None}


def probe(protocol: str, url: str, port: int, message: str | None = None) -> dict[str, object]:
    http_status: int | None = None
    try:
        if protocol in ("http", "https"):
            http_status = _attempt_http(url, port, protocol)
        elif protocol == "tcp":
            _attempt_tcp(url, port)
        elif protocol == "udp":
            if message is None:
                raise ValueError("udp requires a message")
            _attempt_udp(url, port, message)
        else:
            raise ValueError(f"Unsupported protocol: {protocol!r}")
    except Exception as e:
        return _failure(e)
    return {"result": "SENT" if protocol == "udp" else "SUCCESS", "reason": None, "http_status": http_status}


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, object]:
    return probe(event["PROTOCOL"], event["TARGET_URL"], event["TARGET_PORT"], event.get("TARGET_MESSAGE"))


def main(env: dict[str, str]) -> dict[str, object]:
    try:
        return probe(env["SSM_Protocol"], env["SSM_TargetUrl"], int(env["SSM_TargetPort"]), env.get("SSM_TargetMessage") or None)
    except Exception as e:
        return _failure(e)


if __name__ == "__main__":
    print(json.dumps(main(dict(os.environ))))
