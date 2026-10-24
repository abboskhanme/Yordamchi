"""core/proc: PATH order, child environment, credential refusal (SPEC §2.5 steps 2–4; STACK §4.4) and `run`."""

import json
import os
import stat
from pathlib import Path

import pytest

from tests.factories import make_paths
from yordamchi.core import proc


def test_search_path_follows_the_spec_order_and_dedupes(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    login = f"{paths.local_bin}:/usr/local/bin:/Users/x/.cargo/bin::/usr/bin/"
    entries = proc.build_search_path(paths, login).split(":")
    assert entries[:3] == [str(paths.shims), str(paths.local_bin), str(paths.bin)]
    assert entries[3:5] == ["/usr/local/bin", "/Users/x/.cargo/bin"]
    assert entries[5:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert len(entries) == len(set(entries))


def test_search_path_without_a_login_shell_still_has_the_system_dirs(tmp_path: Path) -> None:
    entries = proc.build_search_path(make_paths(tmp_path), None).split(":")
    assert entries[-4:] == list(proc.SYSTEM_PATH)


def test_clean_env_scrubs_claude_nesting_credentials_and_private_names() -> None:
    base = {
        "HOME": "/Users/owner",
        "LANG": "en_US.UTF-8",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "ANTHROPIC_API_KEY": "sk-ant-api03-x",
        "ANTHROPIC_MODEL": "opus",
        "CLAUDE_CONFIG_DIR": "/tmp/other",
        "YORDAMCHI_ENV_FILE": "/data/dev.env",
        "PATH": "/old",
    }
    extra = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "YORDAMCHI_SOCK": "/data/run/dev.sock"}
    env = proc.clean_env(path="/shims:/usr/bin", extra=extra, base=base)
    assert env == {
        "HOME": "/Users/owner",
        "LANG": "en_US.UTF-8",
        "PATH": "/shims:/usr/bin",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "YORDAMCHI_SOCK": "/data/run/dev.sock",
    }


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"])
def test_clean_env_refuses_to_pass_a_credential(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        proc.clean_env(path="/usr/bin", extra={name: "x"}, base={})


def test_scrub_environ_removes_claude_code_variables_only() -> None:
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_SSE_PORT": "1234", "CLAUDE_HOME": "keep", "HOME": "/h"}
    assert proc.scrub_environ(env) == ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT")
    assert env == {"CLAUDE_HOME": "keep", "HOME": "/h"}


def test_refusal_lists_every_source_by_name_never_by_value() -> None:
    environ = {
        "ANTHROPIC_API_KEY": "sk-ant-api03-secret",
        "ANTHROPIC_BASE_URL": "https://proxy",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CONFIG_DIR": "/Users/owner/.claude",
        "HOME": "/Users/owner",
    }
    settings = {"settings.json": {"apiKeyHelper": "/bin/key", "env": {"ANTHROPIC_AUTH_TOKEN": "t", "FOO": "bar"}}}
    found = proc.refused_credentials(environ, settings)
    assert found == (
        "env:ANTHROPIC_API_KEY",
        "env:ANTHROPIC_BASE_URL",
        "env:CLAUDE_CODE_OAUTH_TOKEN",
        "env:CLAUDE_CODE_USE_BEDROCK",
        "env:CLAUDE_CONFIG_DIR",
        "settings.json:apiKeyHelper",
        "settings.json:env.ANTHROPIC_AUTH_TOKEN",
    )
    assert not any("secret" in item for item in found)


def test_refusal_must_run_before_the_scrub() -> None:
    """Some refused names start with CLAUDE_CODE_; scrubbing first would hide them (SPEC §2.5 steps 2–3)."""
    environ = {"CLAUDE_CODE_OAUTH_TOKEN": "x"}
    assert proc.refused_credentials(environ) == ("env:CLAUDE_CODE_OAUTH_TOKEN",)
    proc.scrub_environ(environ)
    assert proc.refused_credentials(environ) == ()


def test_a_clean_setup_is_accepted(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"BASH_DEFAULT_TIMEOUT_MS": "1"}, "model": "opus"}), encoding="utf-8")
    missing = tmp_path / "settings.local.json"
    assert proc.check_credentials([settings, missing], environ={"HOME": "/h"}) == ()


def test_an_unreadable_settings_file_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "settings.json"
    broken.write_text("{not json", encoding="utf-8")
    wrong_shape = tmp_path / "settings.local.json"
    wrong_shape.write_text('{"env": ["ANTHROPIC_API_KEY"]}', encoding="utf-8")
    assert proc.check_credentials([broken, wrong_shape], environ={}) == (
        "settings.json:unreadable",
        "settings.local.json:env",
    )


def test_find_binaries_separates_fatal_from_degraded(tmp_path: Path) -> None:
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n", encoding="utf-8")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    report = proc.find_binaries(str(tmp_path))
    assert report.found == {"claude": str(claude)}
    assert report.missing_required == ("git",)
    assert report.missing_optional == ("ffmpeg", "gh", "node")
    assert report.fatal


async def test_run_returns_output_and_exit_code() -> None:
    result = await proc.run(["/bin/echo", "salom"], env={"PATH": "/bin"}, timeout=5)
    assert (result.returncode, result.stdout) == (0, b"salom\n")


async def test_run_kills_the_process_group_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = f'echo $$ > "{pid_file}"; exec /bin/sleep 30'
    with pytest.raises(TimeoutError):
        await proc.run(["/bin/sh", "-c", script], env={"PATH": "/bin"}, timeout=0.5, kill_grace_s=0.2)
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_login_shell_path_takes_the_last_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = tmp_path / "zsh"
    shell.write_text('#!/bin/sh\necho "profile noise"\nprintf %s "/opt/x/bin:/usr/bin"\n', encoding="utf-8")
    shell.chmod(0o700)
    monkeypatch.setattr(proc, "LOGIN_SHELL", str(shell))
    assert await proc.login_shell_path(env={"PATH": "/bin"}) == "/opt/x/bin:/usr/bin"
    monkeypatch.setattr(proc, "LOGIN_SHELL", str(tmp_path / "missing"))
    assert await proc.login_shell_path(env={"PATH": "/bin"}) is None
