"""Point the app at a throwaway data directory.

``backend.core.config`` reads its YAML at import time, so this has to happen
before anything under ``backend`` is imported — hence the module-level code
rather than a fixture. pytest imports conftest first, which is what makes it
work.
"""

import os
import pathlib
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="kndb-test-")
_CFG = pathlib.Path(_TMP) / "kndb.yaml"
_CFG.write_text(f'data:\n  dir: "{_TMP.replace(chr(92), "/")}"\n', encoding="utf-8")
os.environ["KNDB_CONFIG"] = str(_CFG)

import pytest  # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    import app as app_module

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def as_user(client):
    """Act as a named user — the dev-mode identity header."""

    def _headers(name):
        return {"X-KNDB-User": name}

    return _headers
