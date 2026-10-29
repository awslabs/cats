# CATS AccountUnderTest

The objects a test interacts with: the account fixture and its network handlers.
Session plumbing and probe internals are not part of the public API. Network targets are
reached as attributes (`account.<target>`), described in
[writing tests](../guides/writing-tests.md#the-accountundertest-object).

::: cats.resources.account_under_test
    options:
      show_source: false
      show_root_full_path: false
      filters:
        - "!^_"
      members:
        - AccountUnderTest

::: cats.resources.network_interact
    options:
      show_source: false
      show_root_full_path: false
      filters:
        - "!^_"
      members:
        - NetworkHandler
