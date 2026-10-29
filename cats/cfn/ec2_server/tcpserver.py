#!/usr/bin/python3
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

# Requires compatibility with Python 3.9.16
from __future__ import annotations

import argparse
import selectors
import socket
import types
from typing import Any, cast

sel = selectors.DefaultSelector()

RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


def accept_wrapper(sock: socket.socket) -> None:
    conn, addr = sock.accept()  # Should be ready to read
    print(f"Accepted connection from {conn} {addr}")
    conn.setblocking(False)
    data = types.SimpleNamespace(addr=addr, inb=b"", outb=b"")
    events = selectors.EVENT_READ | selectors.EVENT_WRITE
    sel.register(conn, events, data=data)


def close_connection(sock: socket.socket) -> None:
    sel.unregister(sock)
    sock.close()


# type is selectors._EventMask - not compatible with python 3.9
def service_connection(key: selectors.SelectorKey, mask: Any) -> None:  # noqa: ANN401
    if isinstance(key.fileobj, int):
        raise TypeError("key.fileobj must be socket")
    sock = cast(socket.socket, key.fileobj)
    data = key.data
    try:
        if mask & selectors.EVENT_READ:
            recv_data = sock.recv(1024)  # Should be ready to read
            if not recv_data:
                print(f"Closing connection to {data.addr}")
                close_connection(sock)
                return
            data.outb += recv_data
        if mask & selectors.EVENT_WRITE and data.outb:
            print(f"Received {data} on {sock}")
            sock.send(RESPONSE)  # Should be ready to write
            data.outb = b""
            sock.shutdown(socket.SHUT_WR)
            close_connection(sock)
    except OSError as e:
        # The peer reset or vanished mid-exchange. A TLS client rejecting the plaintext
        # reply closes with unread bytes, so its kernel answers RST and shutdown() fails
        # with ENOTCONN. One caller's exit must never take the listener down.
        print(f"Dropping connection to {data.addr}: {e}")
        close_connection(sock)


def main(ports: list[int]) -> None:
    host = ""
    for port in ports:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind((host, port))
        lsock.listen()
        print(f"Listening on {(host, port)}")
        lsock.setblocking(False)
        sel.register(lsock, selectors.EVENT_READ, data=None)

    try:
        while True:
            events = sel.select(timeout=None)
            for key, mask in events:
                if key.data is None:
                    sock = cast(socket.socket, key.fileobj)
                    accept_wrapper(sock)
                else:
                    service_connection(key, mask)
    except KeyboardInterrupt:
        print("Caught keyboard interrupt, exiting")
    finally:
        sel.close()


def parse_ports(value: str) -> list[int]:
    return [int(s) for s in value.split(",")]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a TCP server that listens on specified ports for incoming TCP connections")
    parser.add_argument(
        "ports",
        type=str,
        help="desired ports to listen for TCP connections, separated by commas",
    )

    args = parser.parse_args()
    main(parse_ports(args.ports))
