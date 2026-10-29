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
import os
import re
import select
import socket
import subprocess  # nosec
import sys
from concurrent.futures import ThreadPoolExecutor

# Must stay identical to CORRELATION_ID_REGEX in cats/resources/network_interact.py
# (this stdlib-only script cannot import it); test_correlation_id_parity.py guards drift.
RECEIPT_KEY_REGEX = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Each receipt is one AWS CLI process (about a second). Writes run off the receive
# loop so a burst of datagrams drains the socket buffer instead of overflowing it,
# and a few run at once so a burst is written in seconds, not one per second.
S3_WRITERS = 4


def extract_receipt_key(message: str, receipt_token: str) -> str | None:
    """Return the S3 key from a '{token}:{payload}' message, split on the first ':' only, or None to drop."""
    token, delimiter, payload = message.partition(":")
    if delimiter == "":
        return None
    if token != receipt_token:
        return None
    if RECEIPT_KEY_REGEX.fullmatch(payload) is None:
        return None
    return payload


def multi_port_server(ports: list[int], bucketname: str, receipt_token: str) -> None:
    sockets = []

    for port in ports:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Security rationale: the listener accepts test traffic on explicitly configured, security-group-restricted ports.
        # S3 writes also require the per-deployment UDP receipt token, so binding all instance interfaces is intentional.
        # nosemgrep: python_bind-all-interfaces_rule-general-bindall-interfaces  # noqa: ERA001
        server_socket.bind(("0.0.0.0", port))  # noqa: S104  # nosec B104
        sockets.append(server_socket)
    print("listening for UDP on ports", ports)
    with ThreadPoolExecutor(max_workers=S3_WRITERS) as writers:
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [])
                for s in readable:
                    try:
                        (client_data, client_address) = s.recvfrom(1024)
                    except OSError as e:
                        # One bad datagram (an ICMP error queued on the socket, say) must not take the listener down.
                        print(f"recvfrom failed on {s.getsockname()}: {e}")
                        continue
                    key = handle_datagram(client_data, client_address, receipt_token)
                    if key is not None:
                        writers.submit(put_s3_object, key, bucketname)
        finally:
            for s in sockets:
                s.close()


def handle_datagram(client_data: bytes, client_address: object, receipt_token: str) -> str | None:
    """Return a valid datagram's payload as the S3 key to write, or log the drop to journald and return None."""
    message = client_data.decode("utf-8", errors="replace")
    key = extract_receipt_key(message, receipt_token)
    if key is None:
        token, delimiter, _ = message.partition(":")
        if delimiter == ":" and token == receipt_token:
            print(f"from [{client_address}] dropped datagram: receipt key failed validation pattern")
        else:
            print(f"from [{client_address}] dropped datagram: missing or mismatched receipt token")
        return None
    print(f"from [{client_address}] received message: {key}")
    return key


def put_s3_object(keyname: str, bucketname: str) -> None:
    """Write the receipt through the preinstalled AWS CLI; a failed write is logged, never fatal.

    The key is passed as `--key=VALUE`: the key pattern admits a leading '-', which the
    two-argument form would parse as an option.
    """
    try:
        subprocess.run(  # noqa: S603 # nosec
            ["aws", "s3api", "put-object", "--bucket", bucketname, f"--key={keyname}"],  # noqa: S607 # nosec
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"failed to write receipt {keyname}: aws exited {e.returncode}: {e.stderr.strip()}")
    except OSError as e:
        print(f"failed to write receipt {keyname}: {e}")


def parse_ports(value: str) -> list[int]:
    return [int(s) for s in value.split(",")]


def main(argv: list[str], environ: dict[str, str]) -> None:
    parser = argparse.ArgumentParser(description="Run a UDP server that writes each token-prefixed message to an S3 bucket")
    parser.add_argument("bucketname", type=str, help="S3 bucket name to write delivered messages to as keys")
    parser.add_argument("ports", type=str, help="comma-separated string of ints for desired port to listen for UDP messages")
    args = parser.parse_args(argv)
    receipt_token = environ.get("CATS_UDP_RECEIPT_TOKEN", "")
    if receipt_token == "":
        parser.error("CATS_UDP_RECEIPT_TOKEN must be set in the environment: the '{token}:' prefix every datagram must carry")
    multi_port_server(parse_ports(args.ports), args.bucketname, receipt_token)


if __name__ == "__main__":
    main(sys.argv[1:], dict(os.environ))
