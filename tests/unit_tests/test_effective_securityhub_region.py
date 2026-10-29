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

"""Tests for CatsConfig.effective_securityhub_region: the one Region deploy and test time agree on."""

from cats.config import CatsConfig, CatsDeploymentSettings, CatsReportingSettings, CatsSecurityHubSettings, ReportFormat


def _config(report_formats: list[ReportFormat], region: str | None) -> CatsConfig:
    return CatsConfig(
        cats_deployment_settings=CatsDeploymentSettings(aws_region="us-east-1"),
        reporting=CatsReportingSettings(report_formats=report_formats, securityhub=CatsSecurityHubSettings(aws_region=region)),
    )


def test_format_not_configured_returns_none() -> None:
    """A securityhub block without the format listed is inert: no grant is rendered, no import runs."""
    # arrange
    config = _config([ReportFormat.CATS_JSON], region="eu-west-1")

    # act
    result = config.effective_securityhub_region()

    # assert
    assert result is None


def test_configured_region_wins() -> None:
    # arrange
    config = _config([ReportFormat.CATS_JSON, ReportFormat.SECURITYHUB], region="eu-west-1")

    # act
    result = config.effective_securityhub_region()

    # assert
    expected = "eu-west-1"
    assert expected == result


def test_unset_region_falls_back_to_the_deploy_region() -> None:
    # arrange
    config = _config([ReportFormat.SECURITYHUB], region=None)

    # act
    result = config.effective_securityhub_region()

    # assert
    expected = "us-east-1"
    assert expected == result


def test_reporting_disabled_returns_none() -> None:
    """enabled: false is the switch that disables every writer, so the deploy-time import grant follows it."""
    # arrange
    config = CatsConfig(
        cats_deployment_settings=CatsDeploymentSettings(aws_region="us-east-1"),
        reporting=CatsReportingSettings(
            enabled=False, report_formats=[ReportFormat.SECURITYHUB], securityhub=CatsSecurityHubSettings(aws_region="eu-west-1")
        ),
    )

    # act
    result = config.effective_securityhub_region()

    # assert
    assert result is None
