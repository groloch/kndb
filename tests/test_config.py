"""kndb.yaml is the application's settings file, not just its plumbing.

Each case boots a subprocess with its own ``KNDB_CONFIG``: ``backend.core.config``
reads its YAML once, at import, so a second configuration cannot be reached from
a process that has already imported it.
"""

import json
import pathlib
import subprocess
import sys
import textwrap

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(tmp_path, yaml_text: str, probe: str):
    """Import the backend under ``yaml_text`` and return the probe's payload,
    or ``("exit", <message>)`` when startup refused the configuration."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = tmp_path / "kndb.yaml"
    cfg.write_text(f'data:\n  dir: "{str(data).replace(chr(92), "/")}"\n' + yaml_text,
                   encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(probe)], cwd=ROOT,
        capture_output=True, text=True,
        env={"KNDB_CONFIG": str(cfg), "PATH": "", "SYSTEMROOT": "C:\\Windows"},
    )
    if proc.returncode != 0:
        return ("exit", proc.stderr)
    payload = [l for l in proc.stdout.splitlines() if l.startswith("@@")]
    assert payload, proc.stdout + proc.stderr
    return json.loads(payload[0][2:])


PROBE = """
    import json
    from backend.core import config
    from backend.data import notes, projects
    print("@@" + json.dumps({
        "default_user": projects.DEFAULT_USER,
        "roles": sorted(projects.ROLES),
        "default_role": projects.DEFAULT_ROLE,
        "write": list(config.WRITE_ROLES),
        "edit_others": list(notes._EDIT_ANY),
        "team_caps": projects.TEAM_CAPS,
        "colors": list(projects.PALETTE),
        "upload": config.UPLOAD_MAX_BYTES,
    }))
"""


def test_defaults_apply_when_the_file_says_nothing(tmp_path):
    out = _run(tmp_path, "", PROBE)
    assert out["default_user"] == "me"
    assert out["roles"] == ["contributor", "maintainer", "owner", "spectator"]
    assert out["write"] == ["owner", "maintainer", "contributor"]
    assert out["upload"] == 300 * 1024 * 1024


def test_the_file_drives_permissions_and_limits(tmp_path):
    """The point of the exercise: none of this is edited in Python."""
    out = _run(tmp_path, textwrap.dedent("""
        permissions:
          default_user: alice
          grants:
            write: [owner, maintainer]
            edit_others: [owner]
        limits:
          upload_mb: 5
    """), PROBE)
    assert out["default_user"] == "alice"
    assert out["write"] == ["owner", "maintainer"]   # contributors read-only now
    assert out["edit_others"] == ["owner"]
    assert out["upload"] == 5 * 1024 * 1024


def test_a_mapping_is_merged_key_by_key(tmp_path):
    """Overriding one capability must not blank the others, or a one-line edit
    would silently turn three switches off."""
    out = _run(tmp_path, textwrap.dedent("""
        projects:
          team_capabilities:
            allow_quiz: true
    """), PROBE)
    assert out["team_caps"] == {"allow_quiz": True, "multi_notes": True,
                                "show_blame": True, "auto_import": False}


def test_a_list_replaces_the_default_wholesale(tmp_path):
    out = _run(tmp_path, 'projects:\n  member_colors: ["#111111", "#222222"]\n',
               PROBE)
    assert out["colors"] == ["#111111", "#222222"]


@pytest.mark.parametrize("yaml_text, expected", [
    ("permissions:\n  grants:\n    write: [owner, editor]\n", "permissions.roles"),
    ("permissions:\n  default_role: reviewer\n", "default_role"),
    ("permissions:\n  owner_role: chief\n", "owner_role"),
    ("projects:\n  team_capabilities:\n    allow_quizz: true\n", "unknown capabilities"),
    ("projects:\n  member_colors: 3\n", "list of strings"),
    ("limits:\n  upload_mb: plenty\n", "limits.upload_mb must be a number"),
])
def test_nonsense_is_refused_at_startup(tmp_path, yaml_text, expected):
    """A role nobody holds or a capability nobody reads is a silent lockout —
    it has to be a startup error, not a setting that quietly does nothing."""
    kind, err = _run(tmp_path, yaml_text, PROBE)
    assert kind == "exit"
    assert expected in err
