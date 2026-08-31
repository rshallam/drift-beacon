# Testing the Drift Beacon Home Assistant integration

The tests in this directory are focused unit tests for the custom integration in
`custom_components/drift_beacon`. They import Home Assistant's public classes and constants, but
they do not start a Home Assistant instance or require a running Drift Beacon server.

## Current setup

The repository currently has no `pyproject.toml`, Python dependency lockfile, `pytest.ini`, or
shared `conftest.py`. The test environment is therefore created on demand with
[uv](https://docs.astral.sh/uv/):

- `homeassistant` supplies the integration APIs and types used by the component.
- `pytest` is the test runner.
- `pytest-asyncio` runs tests marked with `@pytest.mark.asyncio`.
- `PYTHONPATH=.` makes the repository's `custom_components` package importable.

Tests use small in-memory collaborators such as `FakeHomeAssistant`, `FakeConfigEntry`, and
`FakeManager`, together with `unittest.mock`. Network connections, WebSocket messages, Home
Assistant's event bus, and coordinator RPCs are simulated locally.

## Running the tests

From the repository root, run the complete suite with:

```sh
uv run --with homeassistant --with pytest --with pytest-asyncio \
  env PYTHONPATH=. pytest -q tests/custom_components/drift_beacon
```

Some authentication tests are parameterized for both HTTP 401 and 403 responses.

Run one test module by replacing the final path:

```sh
uv run --with homeassistant --with pytest --with pytest-asyncio \
  env PYTHONPATH=. pytest -q tests/custom_components/drift_beacon/test_sensor.py
```

Run one test by its pytest node ID:

```sh
uv run --with homeassistant --with pytest --with pytest-asyncio \
  env PYTHONPATH=. pytest -q \
  tests/custom_components/drift_beacon/test_coordinator.py::test_snapshot_restores_availability_and_replaces_state
```

Remove `-q` for more runner output, or add `-vv` to show every collected case.

## Test organization

| File | Coverage |
| --- | --- |
| `test_config_flow.py` | Invalid credentials, reauthentication, and config-entry updates |
| `test_coordinator.py` | Connection lifecycle, retry behavior, identity snapshots, events, pin state, and RPC payloads |
| `test_sensor.py` | Workspace sensor discovery and pinned-activity sensor state |
| `test_switch.py` | Session/pin switch discovery, single pinned slot behavior, and RPC calls |

## Style checks

Ruff is also run without a repository-local Python environment:

```sh
uvx ruff check custom_components/drift_beacon tests/custom_components/drift_beacon
uvx ruff format --check custom_components/drift_beacon tests/custom_components/drift_beacon
```

To apply Ruff's formatter, omit `--check` from the second command.

## Adding a test

Keep new tests under `tests/custom_components/drift_beacon` and mirror the component module name
where practical. For async behavior, mark the test explicitly:

```python
@pytest.mark.asyncio
async def test_example() -> None:
    ...
```

Prefer deterministic fakes and mocks over real network or Home Assistant runtime state. If a
future test needs Home Assistant's full fixture-based test harness, add and document that setup
separately; the current suite does not provide fixtures such as `hass` or `MockConfigEntry`.
