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

"""Tests for cats/cfn/ec2_server/udpserver.py - UDP receipt token enforcement."""

import argparse
import subprocess
import sys
import uuid
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from cats.cfn.ec2_server.udpserver import extract_receipt_key, handle_datagram, main, parse_ports, put_s3_object

TOKEN = "0123456789abcdef0123456789abcdef"  # noqa: S105
BUCKET = "cats-responses-111111111111"
CLIENT_ADDRESS = ("10.0.1.20", 41234)


# =============================================================================
# extract_receipt_key
# =============================================================================


def test_extract_valid_token_returns_payload() -> None:
    # arrange
    message = f"{TOKEN}:my-correlation-id"
    expected = "my-correlation-id"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result == expected


def test_extract_wrong_token_returns_none() -> None:
    # arrange
    message = f"{'f' * 32}:my-correlation-id"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result is None


def test_extract_no_colon_returns_none() -> None:
    # arrange
    message = "my-correlation-id"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result is None


def test_extract_colon_in_payload_returns_none() -> None:
    # arrange - ':' is outside the receipt key charset
    message = f"{TOKEN}:payload:with:colons"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result is None


def test_extract_empty_payload_returns_none() -> None:
    # arrange - fails the 1-char minimum
    message = f"{TOKEN}:"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result is None


def test_extract_valid_uuid_payload_returns_payload() -> None:
    # arrange
    payload = str(uuid.uuid4())
    message = f"{TOKEN}:{payload}"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result == payload


def test_extract_max_length_payload_returns_payload() -> None:
    # arrange
    payload = "a" * 128
    message = f"{TOKEN}:{payload}"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result == payload


def test_extract_oversize_payload_returns_none() -> None:
    # arrange
    payload = "a" * 129
    message = f"{TOKEN}:{payload}"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result is None


def test_extract_out_of_charset_payload_returns_none() -> None:
    # arrange
    message = f"{TOKEN}:junk key!"

    # act
    result = extract_receipt_key(message, TOKEN)

    # assert
    assert result is None


# =============================================================================
# handle_datagram: parses and logs; the caller writes the returned key
# =============================================================================


def test_handle_valid_token_returns_payload_as_s3_key(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected_log = f"from [{CLIENT_ADDRESS}] received message: my-correlation-id\n"

    # act
    key = handle_datagram(f"{TOKEN}:my-correlation-id".encode(), CLIENT_ADDRESS, TOKEN)

    # assert
    assert (key, capsys.readouterr().out) == ("my-correlation-id", expected_log)


def test_handle_wrong_token_returns_none(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected_log = f"from [{CLIENT_ADDRESS}] dropped datagram: missing or mismatched receipt token\n"

    # act
    key = handle_datagram(f"{'f' * 32}:my-correlation-id".encode(), CLIENT_ADDRESS, TOKEN)

    # assert
    assert (key, capsys.readouterr().out) == (None, expected_log)


def test_handle_no_colon_returns_none(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected_log = f"from [{CLIENT_ADDRESS}] dropped datagram: missing or mismatched receipt token\n"

    # act
    key = handle_datagram(b"my-correlation-id", CLIENT_ADDRESS, TOKEN)

    # assert
    assert (key, capsys.readouterr().out) == (None, expected_log)


def test_handle_colon_in_payload_returns_none(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected_log = f"from [{CLIENT_ADDRESS}] dropped datagram: receipt key failed validation pattern\n"

    # act
    key = handle_datagram(f"{TOKEN}:payload:with:colons".encode(), CLIENT_ADDRESS, TOKEN)

    # assert
    assert (key, capsys.readouterr().out) == (None, expected_log)


def test_handle_non_utf8_datagram_returns_none_without_raising(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected_log = f"from [{CLIENT_ADDRESS}] dropped datagram: missing or mismatched receipt token\n"

    # act
    key = handle_datagram(b"\xff\xfe\xfd", CLIENT_ADDRESS, TOKEN)

    # assert
    assert (key, capsys.readouterr().out) == (None, expected_log)


def test_handle_invalid_key_returns_none_with_validation_message(capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    expected_log = f"from [{CLIENT_ADDRESS}] dropped datagram: receipt key failed validation pattern\n"

    # act
    key = handle_datagram(f"{TOKEN}:junk key!".encode(), CLIENT_ADDRESS, TOKEN)

    # assert
    assert (key, capsys.readouterr().out) == (None, expected_log)


# =============================================================================
# put_s3_object: one AWS CLI process per receipt; a failure is logged, never raised
# =============================================================================


@pytest.fixture
def mock_run(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.udpserver.subprocess.run")
    return mock


def test_put_passes_the_key_in_equals_form(mock_run: MagicMock) -> None:
    """The key pattern admits a leading '-'; `--key -abc` would be parsed as an option, `--key=-abc` is not."""
    # act
    put_s3_object("-abc", BUCKET)

    # assert
    mock_run.assert_called_once_with(["aws", "s3api", "put-object", "--bucket", BUCKET, "--key=-abc"], check=True, capture_output=True, text=True)


def test_put_successful_write_logs_nothing(mock_run: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    # act
    put_s3_object("my-correlation-id", BUCKET)

    # assert
    assert capsys.readouterr().out == ""


def test_put_cli_failure_is_logged_not_raised(mock_run: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    # arrange - a throttle, an expired instance credential, a bucket policy change: the listener must survive all of them
    mock_run.side_effect = subprocess.CalledProcessError(255, ["aws"], stderr="An error occurred (SlowDown) when calling the PutObject operation\n")
    expected_log = "failed to write receipt my-correlation-id: aws exited 255: An error occurred (SlowDown) when calling the PutObject operation\n"

    # act
    put_s3_object("my-correlation-id", BUCKET)

    # assert
    assert capsys.readouterr().out == expected_log


def test_put_missing_cli_is_logged_not_raised(mock_run: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    # arrange
    mock_run.side_effect = FileNotFoundError(2, "No such file or directory", "aws")
    expected_log = "failed to write receipt my-correlation-id: [Errno 2] No such file or directory: 'aws'\n"

    # act
    put_s3_object("my-correlation-id", BUCKET)

    # assert
    assert capsys.readouterr().out == expected_log


# =============================================================================
# parse_ports: the argument the systemd unit passes from CATS_UDP_PORTS
# =============================================================================


def test_parse_ports_splits_a_comma_separated_list() -> None:
    # act
    result = parse_ports("5000,5001")

    # assert
    assert result == [5000, 5001]


def test_parse_ports_accepts_a_single_port() -> None:
    # act
    result = parse_ports("5000")

    # assert
    assert result == [5000]


def test_parse_ports_rejects_a_space_separated_list() -> None:
    """systemd would have split a space-separated value into several arguments before it reached us; one arriving whole is not a port list."""
    # act & assert
    with pytest.raises(ValueError, match=r"invalid literal for int\(\) with base 10: '5000 5001'"):
        parse_ports("5000 5001")


# =============================================================================
# main: the unit's ExecStart arguments plus the token from the env file
# =============================================================================


@pytest.fixture
def mock_multi_port_server(mocker: MockerFixture) -> MagicMock:
    mock: MagicMock = mocker.patch("cats.cfn.ec2_server.udpserver.multi_port_server")
    return mock


def test_main_reads_the_receipt_token_from_the_environment_not_argv(mock_multi_port_server: MagicMock) -> None:
    """The token comes from the env file systemd exports, not argv, so it is not in `ps` output; tidiness, not secrecy
    (the token is a stack output and in the instance's user data)."""
    # act
    main([BUCKET, "5000,5001"], {"CATS_UDP_RECEIPT_TOKEN": TOKEN})

    # assert
    mock_multi_port_server.assert_called_once_with([5000, 5001], BUCKET, TOKEN)


def test_main_without_the_token_in_the_environment_exits_2_naming_the_variable(
    mock_multi_port_server: MagicMock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # arrange - argparse names the program after argv[0] (on the instance, the installed script /usr/local/bin/udpserver);
    # from 3.14 it prefers __main__'s module spec where one exists, so the expected prefix is taken from argparse itself
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/udpserver"])
    expected_prog = argparse.ArgumentParser().prog

    # act
    with pytest.raises(SystemExit) as exc_info:
        main([BUCKET, "5000"], {})

    # assert
    expected_last_line = (
        f"{expected_prog}: error: CATS_UDP_RECEIPT_TOKEN must be set in the environment: the '{{token}}:' prefix every datagram must carry"
    )
    assert (exc_info.value.code, capsys.readouterr().err.splitlines()[-1], mock_multi_port_server.called) == (2, expected_last_line, False)


def test_main_refuses_the_token_as_a_third_positional_argument(mock_multi_port_server: MagicMock) -> None:
    """The old ExecStart passed the token on the command line; a unit still doing so fails loudly rather than serving with the wrong bucket."""
    # act
    with pytest.raises(SystemExit) as exc_info:
        main([BUCKET, TOKEN, "5000"], {"CATS_UDP_RECEIPT_TOKEN": TOKEN})

    # assert
    assert (exc_info.value.code, mock_multi_port_server.called) == (2, False)
