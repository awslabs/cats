# Helpers

Every name below is exported from `cats`; import it as `from cats import ...`. The module
paths shown by the renderer are internal and may move.

## Results and helpers

::: cats.constants
    options:
      show_source: false
      show_root_heading: false
      show_root_full_path: false
      members:
        - CatsNetworkResponse
        - CatsNetworkResult
        - ShellResult
        - get_account_fixture_names
        - get_accounts_by_tag

## Reporting

::: cats.reporting
    options:
      show_source: false
      show_root_heading: false
      show_root_full_path: false
      members:
        - CatsReporter

## Standards registry and the other hooks

A conftest may implement three CATS hooks: `pytest_cats_standards` (the registry below),
`pytest_cats_test_pack` (see [Test packs](#test-packs)) and `pytest_cats_provides(cats_config)`,
which returns the requirements the deployment provides for tests marked `@cats_requires`
(see [Test selection](../guides/configuration.md#test-selection)).

::: cats.hookspecs
    options:
      show_source: false
      show_root_heading: false
      show_root_full_path: false
      members:
        - StandardMetadata
        - CatsHookSpec

## Test packs

::: cats.user_agent
    options:
      show_source: false
      show_root_heading: false
      show_root_full_path: false
      members:
        - CatsTestPack

## Type alias and version

- `GetAccountFixture` is `Callable[[str], AccountUnderTest]`: the type of the
  `get_account_fixture` fixture, which resolves an account fixture by name and records the
  account in the test's evidence. See [multi-account fixtures](../guides/writing-tests.md#multi-account-fixtures).
- `__version__` is the installed CATS release as a string, the value `cats --version` prints
  and the `cats:version` stack tag carries.
