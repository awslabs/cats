# The `@cats` and `@cats_requires` decorators

`from cats import cats, cats_requires`. `@cats` attaches a standard (and, optionally, its
control ids and group) to a test, and carries the custom markers declared in
`reporting.custom_markers`. Its three keywords are frozen by the
[compatibility contract](../concepts/versioning.md#the-cats-decorator).

`@cats_requires` declares what the deployment must provide for the test to apply; the
test runs only when `test_selection.provides` in cats.yaml (or a `pytest_cats_provides`
hook) provides every requirement, and is recorded as `not_run` otherwise. See
[Test selection](../guides/configuration.md#test-selection).

::: cats.markers
    options:
      show_source: false
      show_root_heading: false
      show_root_full_path: false
      members:
        - cats
        - cats_requires

## Options

Every keyword other than these three is a custom marker and must be declared under
`reporting.custom_markers`; an undeclared keyword, including a misspelled CATS keyword, raises
`CatsUsageError` when the test module is imported, so a typo can never silently drop a standard
from the evidence.

| Option | Type | Description |
|--------|------|-------------|
| `standard_name` | `str \| list[str]` | Compliance standard identifier. Pass a list to attach the same test to several standards. Optional: a test may carry only custom markers. |
| `control_ids` | `list[str]` | Framework control IDs this standard attests to, carried as written into every report format. Overrides the [standards registry](../guides/standards-registry.md) per field. Needs `standard_name`. With `oscal_ar` in `report_formats` each ID must be an OSCAL token (`ac-2.1`, not `AC-2(1)`); the decorator refuses one that is not at import time. |
| `control_group` | `str` | Free-form group label emitted as an observation `control-group` prop. Overrides the registry. Needs `standard_name`. |
| `**kwargs` | `str \| bool \| list[str]` | Custom markers declared under `reporting.custom_markers` in cats.yaml. |

Skipping and slow-marking are pytest's job: use `@pytest.mark.skip(reason=...)` and your own
registered markers alongside `@cats`.
