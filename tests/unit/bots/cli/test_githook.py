"""bots.cli.githook, in process: the hook scripts, the name rules, the line rules (patterns and exact vault values)
and the patch reader. The real hooks run as subprocesses in tests/integration/test_gate_cli.py."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.factories import FAKE_KEYS, UNKNOWN_FORMAT_KEY
from yordamchi.bots.cli import githook
from yordamchi.bots.cli.githook import Hit

NO_HASHES: frozenset[str] = frozenset()
REPO_GITHOOKS = Path(__file__).resolve().parents[4] / "githooks"


def test_every_hook_script_hands_itself_to_the_module() -> None:
    names = {path.name for path in REPO_GITHOOKS.iterdir() if not path.name.startswith(".")}  # not Finder's
    assert names == {githook.PRE_PUSH, *githook.PASS_THROUGH_HOOKS}
    for name in sorted(names):
        script = (REPO_GITHOOKS / name).read_text(encoding="utf-8")
        assert script.startswith("#!/bin/sh\n"), name
        assert script.endswith('\nexec "$YORDAMCHI_PY" -I -m yordamchi.bots.cli.githook "$0" "$@"\n'), name
        assert (REPO_GITHOOKS / name).stat().st_mode & 0o111, name
    pass_through = {(REPO_GITHOOKS / name).read_bytes() for name in githook.PASS_THROUGH_HOOKS}
    assert len(pass_through) == 1  # one text: a fix to one of them reaches all of them


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        (".env", "env_file"),
        ("config/.env.production", "env_file"),
        (".envrc", "env_file"),
        (".env.example", None),
        ("app/.env.local.sample", None),
        (".env.template", None),
        ("certs/server.pem", "pem_file"),
        ("deploy/id_rsa", "ssh_key_file"),
        ("deploy/id_rsa.pub", "ssh_key_file"),  # SPEC §5.7 names `id_rsa*`
        ("keys/App.P12", "p12_file"),
        ("android/release.keystore", "keystore_file"),
        ("credentials.json", "credentials_file"),
        ("gcp/credentials-prod.json", "credentials_file"),
        ("src/env.py", None),
        ("docs/credentials.md", None),
    ],
)
def test_name_rules(path: str, rule: str | None) -> None:
    assert githook.name_rule(path) == rule


@pytest.mark.parametrize(("kind", "value"), sorted(FAKE_KEYS.items()))
def test_every_known_key_format_on_an_added_line_is_found(kind: str, value: str) -> None:
    assert githook.line_rule(f'API_TOKEN = "{value}"  # kalit', NO_HASHES) is not None


def test_an_exact_vault_value_is_found_by_its_hash_wherever_it_sits() -> None:
    hashes = frozenset({hashlib.sha256(UNKNOWN_FORMAT_KEY.encode()).hexdigest()})
    for line in (  # no keyword next to it, so only the hash can find it
        UNKNOWN_FORMAT_KEY,
        f"VALUE={UNKNOWN_FORMAT_KEY}",
        f'"x": "{UNKNOWN_FORMAT_KEY}",',
        f"postgres://app:{UNKNOWN_FORMAT_KEY}@db/app",
        f"x: {UNKNOWN_FORMAT_KEY}",
    ):
        assert githook.line_rule(line, hashes) == "vault_value", line
    assert githook.line_rule(f"PASSWORD={UNKNOWN_FORMAT_KEY}", hashes) is not None  # a pattern finds it first
    assert githook.line_rule("nothing to see here", hashes) is None
    assert githook.line_rule(UNKNOWN_FORMAT_KEY, NO_HASHES) is None  # no pattern knows this shape


PATCH = """\
deadbeef
diff --git a/src/app.py b/src/app.py
index 1..2 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,0 +11,2 @@ def f():
+print("ok")
+++ not a header, an added line that starts with ++
@@ -40 +42 @@
-old = 1
+token = "TOKEN"
"""


def test_the_patch_reader_names_the_file_and_line_never_the_secret() -> None:
    token = FAKE_KEYS["github_token"]
    hit = githook.scan_patch(PATCH.replace("TOKEN", token), NO_HASHES)
    assert hit == Hit("src/app.py", 42, "github_token")
    assert token not in repr(hit)
    assert githook.scan_patch(PATCH, NO_HASHES) is None


def test_binary_and_quoted_paths_are_still_checked_by_name() -> None:
    binary = (
        "diff --git a/keys/app.p12 b/keys/app.p12\nnew file mode 100644\n"
        "Binary files /dev/null and b/keys/app.p12 differ\n"
    )
    assert githook.scan_patch(binary, NO_HASHES) == Hit("keys/app.p12", 1, "p12_file")
    quoted = 'diff --git "a/my dir/\\tx.pem" "b/my dir/\\tx.pem"\n'
    assert githook.scan_patch(quoted, NO_HASHES) == Hit("my dir/\tx.pem", 1, "pem_file")
    spaced = "diff --git a/a b/c.env b/a b/c.env\n"  # a space in the name, renames off: both halves are equal
    assert githook.scan_patch(spaced, NO_HASHES) is None
    assert githook.scan_patch("diff --git a/a b/.env b/a b/.env\n", NO_HASHES) == Hit("a b/.env", 1, "env_file")


def test_the_hash_file_must_be_whole(tmp_path: Path) -> None:
    path = tmp_path / "secret-hashes.json"
    assert githook.load_hashes(None) is None
    assert githook.load_hashes(path) is None  # missing: the scan cannot run
    path.write_text("{not json", encoding="utf-8")
    assert githook.load_hashes(path) is None
    path.write_text(json.dumps({"sha256": [1, 2]}), encoding="utf-8")
    assert githook.load_hashes(path) is None
    path.write_text(json.dumps({"sha256": ["AB", "cd"]}), encoding="utf-8")
    assert githook.load_hashes(path) == frozenset({"ab", "cd"})
