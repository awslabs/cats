# CATS Configuration

The `cats.yaml` schema, as Pydantic models. Only the models below are stable
configuration surface; loader internals are not part of the public API.

::: cats.config
    options:
      show_source: false
      show_root_full_path: false
      filters:
        - "!^_"
        - "!^settings_customise_sources$"
        - "!^model_config$"
      members:
        - CatsConfig
        - CatsDeploymentSettings
        - NetworkSettings
        - AccountSettings
        - CatsAccountUnderTest
        - CatsNetworkUnderTest
        - CatsAdditionalTestRole
        - CatsReportingSettings
        - CatsSecurityHubSettings
        - CatsTestSelectionSettings
        - TestSelectionExclusion
        - SecurityHubSeverity
        - ReportFormat
        - EnabledNetworkHandler
        - GatewayEndpoints
