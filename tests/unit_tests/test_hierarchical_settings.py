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

"""Tests for hierarchical settings resolution in CatsConfig."""

from cats.config import CatsConfig


def test_target_inherits_from_account_inherits_from_global() -> None:
    """Target gets account value, account gets global value when not set."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_network_settings": {"tcp_listen_ports": [8080]},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
                "account_network_under_test": {
                    "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]},
                },
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    target = config.accounts_under_test["prod"].account_network_under_test["private"]
    assert target.network_settings.tcp_listen_ports == [8080]


def test_account_overrides_global() -> None:
    """Account-level setting overrides global."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_network_settings": {"tcp_listen_ports": [8080]},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
                "network_settings": {"tcp_listen_ports": [443]},
                "account_network_under_test": {
                    "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]},
                },
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    account = config.accounts_under_test["prod"]
    target = account.account_network_under_test["private"]
    assert account.network_settings.tcp_listen_ports == [443]
    assert target.network_settings.tcp_listen_ports == [443]


def test_target_overrides_account() -> None:
    """Target-level setting overrides account."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_network_settings": {"tcp_listen_ports": [8080]},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
                "network_settings": {"tcp_listen_ports": [443]},
                "account_network_under_test": {
                    "private": {
                        "vpc_id": "vpc-123",
                        "subnet_ids": ["subnet-1"],
                        "network_settings": {"tcp_listen_ports": [8443]},
                    },
                },
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    target = config.accounts_under_test["prod"].account_network_under_test["private"]
    assert target.network_settings.tcp_listen_ports == [8443]


def test_empty_list_overrides_nonempty_parent() -> None:
    """Empty list [] explicitly opts out, not inherited."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_network_settings": {"tcp_listen_ports": [8080]},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
                "network_settings": {"tcp_listen_ports": []},
                "account_network_under_test": {
                    "private": {"vpc_id": "vpc-123", "subnet_ids": ["subnet-1"]},
                },
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    account = config.accounts_under_test["prod"]
    target = account.account_network_under_test["private"]
    assert account.network_settings.tcp_listen_ports == []
    assert target.network_settings.tcp_listen_ports == []


def test_account_settings_inherit_from_global() -> None:
    """Account settings inherit from global_account_settings."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_account_settings": {"fixture_scope": "module"},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    assert config.accounts_under_test["prod"].account_settings.fixture_scope == "module"


def test_account_settings_override_global() -> None:
    """Account-level account_settings overrides global."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_account_settings": {"fixture_scope": "session"},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
                "account_settings": {"fixture_scope": "function"},
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    assert config.accounts_under_test["prod"].account_settings.fixture_scope == "function"


def test_allow_shell_default_false() -> None:
    """Account defaults to allow_shell=False when nothing is set anywhere."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    assert config.accounts_under_test["prod"].account_settings.allow_shell is False


def test_allow_shell_account_inherits_global() -> None:
    """Global allow_shell: true is inherited to the account."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_account_settings": {"allow_shell": True},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    assert config.accounts_under_test["prod"].account_settings.allow_shell is True


def test_allow_shell_account_overrides_global() -> None:
    """Account-level allow_shell: false overrides global true."""
    # arrange
    config_dict = {
        "cats_deployment_settings": {"parent_account_id": "123456789012"},
        "global_account_settings": {"allow_shell": True},
        "accounts_under_test": {
            "prod": {
                "account_id": "111111111111",
                "account_settings": {"allow_shell": False},
            }
        },
    }

    # act
    config = CatsConfig.model_validate(config_dict)

    # assert
    assert config.accounts_under_test["prod"].account_settings.allow_shell is False
