# Standards registry

The **standards registry** maps the names on your tests to catalog control IDs in the OSCAL assessment results. The names can come from a workbook, a CSV, a database or a hand-maintained YAML.

If you label tests directly with catalog control IDs (`@cats(standard_name="SC-7")`) and that's all you need, you do not need a registry. CATS will emit findings against `SC-7` verbatim. The registry exists for the case where tests carry friendlier names (`@cats(standard_name="ACME-G-DCP-1")`) and you want the OSCAL output to resolve those into catalog IDs like `SC-7`, `AC-3`, etc.

## How CATS picks up your registry

Implement the `pytest_cats_standards` hook in your consuming project's `conftest.py`. CATS calls it once per test session on the main process and passes the returned mapping into the OSCAL writer.

Every implementer counts: a shared organization-level `conftest.py`, an installed plugin and a project's own `tests/conftest.py` are merged into one registry, resolved after collection and before the first test runs. Two implementers may declare the same standard only with the same value; if they disagree, the session stops before any test runs with an error naming both files and both values, so one registry owns each standard and no call order decides which mapping the evidence carries.

Put the implementing `conftest.py` where pytest loads it before collection starts: the root `conftest.py`, a `test*/` directory's `conftest.py` (`tests/conftest.py`), or a directory you pass to pytest. Under pytest-xdist only the workers collect, so the main process that writes the reports never loads a conftest pytest first finds during collection, such as `tests/deep/conftest.py` on `pytest tests`. Its registry does not reach the reports, the OSCAL `target-id` falls back to the standard name, and each worker warns `... implements pytest_cats_standards but was loaded during collection ...`. A serial run collects on the main process, so there the location does not matter.

```python
# conftest.py
from cats import StandardMetadata


def pytest_cats_standards() -> dict[str, StandardMetadata]:
    return {
        "ACME-G-DCP-1": StandardMetadata(
            control_ids=["SC-7", "AC-3"],
            control_group="Governance",
        ),
    }
```

If you don't need a registry, just don't implement the hook. CATS will still emit valid OSCAL — the `standard_name` is used verbatim as the `target-id`.

Because the hook is called once per session, on the main process, after collection and before the first test, it is safe to do expensive work (parse a spreadsheet, hit a database) inside it without impacting test execution time. You only pay the cost once.

## What the hook returns

The hook is not covered by semver (see [Not covered](../concepts/versioning.md#not-covered)); the `StandardMetadata` class it returns is.

The hook returns `Mapping[str, StandardMetadata]`:

- **Keys** are your standard names — the same strings you pass to `@cats(standard_name=...)`. You own this namespace. It can be whatever makes sense for your project (internal tickets, framework control IDs, free-text names).
- **Values** are `StandardMetadata` with two fields:

| Field | Type | Purpose | Effect when empty |
|-------|------|---------|-------------------|
| `control_ids` | `list[str]` | Framework control IDs this standard attests to, carried as written into `report.json`, JUnit and Security Hub `RelatedRequirements`. With `oscal_ar` in `report_formats`, one OSCAL finding is emitted per ID and the list is space-joined into `observation.remarks` so OSCAL consumers that scan free-text for catalog IDs pick them up; each ID must then be an OSCAL token (letters, digits, `.`, `-` and `_`, starting with a letter or `_`), so the catalog's own `AC-2(1)` is written `ac-2.1`. An ID that is not a token, with `oscal_ar` configured, stops the session before any test runs, naming the standard and the ID. | Falls back to `[standard_name]` — one finding with the standard name as the target-id. |
| `control_group` | `str \| None` | Free-form group label (e.g. `"Governance"`, `"Identity"`). Emitted as `observation.props[name="control-group"]`. | No `control-group` prop on the observation. |

### Resolution order

When CATS builds each observation, it resolves metadata in this order per field:

1. Values on the `@cats()` decorator — `@cats(control_ids=[...], control_group="...")` — win per field.
2. Values from the registry fill any gaps.
3. If both are empty for `control_ids`, the `standard_name` itself is used as a single control ID. This keeps the writer producing valid output even with no registry at all.

Decorator values override registry values per field, not per standard. So `@cats(standard_name="X", control_ids=["SC-7"])` with a registry entry `{"X": StandardMetadata(control_ids=["AC-3"], control_group="Governance")}` produces findings for `SC-7` (decorator wins) and a control-group of `"Governance"` (registry fills the gap).

## Single-framework constraint

The registry returns **one framework's control IDs per test run**. If your source spans multiple frameworks (NIST 800-53, PCI-DSS, CIS), pick one at registry-build time — typically via an environment variable — so the OSCAL output is a coherent assessment against a single catalog. The [workbook example](#excel-workbook) below shows one way.

## Stability expectations

Your `standard_name` keys and their resolved `control_ids` should be stable between runs if you want stable report diffing. Churning a standard name or reshuffling its control mappings from run to run will make every dashboard and compliance delta noisy. Pin the workbook / CSV / source-of-truth in version control and treat changes there as changes to your compliance posture.

## Worked examples

Four shapes, each around ten lines of conftest. CATS does not care about the source shape — the hook author transforms whatever they have into a dict of `StandardMetadata`.

### Excel workbook

Many teams already keep their control mapping in a spreadsheet: one sheet per framework,
one row per internal control name. Parse the workbook once, return the full dict, and
select the framework via an environment variable. Requires
[openpyxl](https://openpyxl.readthedocs.io/) (`uv add --dev openpyxl`) — CATS does not
depend on it.

```python
# conftest.py
import os
from pathlib import Path

from openpyxl import load_workbook

from cats import StandardMetadata

_WORKBOOK = Path(os.environ.get("COMPLIANCE_WORKBOOK", "compliance-workbook.xlsx"))
_FRAMEWORK = os.environ.get("OSCAL_FRAMEWORK", "NIST 800-53 Rev5")


def oscal_token(control_id: str) -> str:
    """The catalog's AC-2(1) as the OSCAL token ac-2.1."""
    return control_id.strip().lower().replace("(", ".").rstrip(")")


def pytest_cats_standards() -> dict[str, StandardMetadata]:
    sheet = load_workbook(_WORKBOOK, read_only=True)[_FRAMEWORK]
    rows = sheet.iter_rows(min_row=2, values_only=True)  # columns: standard name, control IDs, group
    return {
        str(row[0]): StandardMetadata(
            control_ids=[oscal_token(control) for control in str(row[1]).split(",")],
            control_group=str(row[2]) if row[2] is not None else None,
        )
        for row in rows
        if row[0] is not None
    }
```

### CSV file

One row per standard. Columns for control IDs and group. Parse with stdlib `csv`.

```python
# conftest.py
import csv
from pathlib import Path

from cats import StandardMetadata

_CSV = Path("standards.csv")  # columns: standard_name, control_ids, control_group


def pytest_cats_standards() -> dict[str, StandardMetadata]:
    registry: dict[str, StandardMetadata] = {}
    with _CSV.open() as f:
        for row in csv.DictReader(f):
            registry[row["standard_name"]] = StandardMetadata(
                control_ids=[cid.strip().lower().replace("(", ".").rstrip(")") for cid in row["control_ids"].split(";") if cid.strip()],
                control_group=row.get("control_group") or None,
            )
    return registry
```

Example `standards.csv`:

```csv
standard_name,control_ids,control_group
ACME-G-DCP-1,SC-7;AC-3,Governance
ACME-G-IAM-2,AC-2;AC-3,Identity
```

### In-memory (no data source)

Smallest possible conftest — useful when you don't have a catalog and just want CATS to produce correct OSCAL output.

```python
# conftest.py
from cats import StandardMetadata


def pytest_cats_standards() -> dict[str, StandardMetadata]:
    return {
        "no-ssh-port": StandardMetadata(control_ids=["AC-4", "SC-7"], control_group="Network"),
        "cloudtrail-enabled": StandardMetadata(control_ids=["AU-2", "AU-12"], control_group="Audit"),
    }
```

### Hybrid — registry plus per-test override

Registry supplies defaults. Individual tests override with the decorator when they differ from the common mapping.

```python
# conftest.py
from cats import StandardMetadata


def pytest_cats_standards() -> dict[str, StandardMetadata]:
    return {
        "ACME-G-DCP-1": StandardMetadata(control_ids=["SC-7"], control_group="Governance"),
    }
```

```python
# tests/test_dcp.py
from cats import cats


@cats(standard_name="ACME-G-DCP-1")
def test_default_mapping_from_registry() -> None:
    """Uses SC-7 from the registry."""


@cats(standard_name="ACME-G-DCP-1", control_ids=["SC-7", "AC-3"])
def test_override_from_decorator() -> None:
    """Decorator wins: findings for SC-7 and AC-3, not just SC-7."""
```
