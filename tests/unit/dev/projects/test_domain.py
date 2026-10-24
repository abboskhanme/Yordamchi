"""projects.domain: which folders are projects, their flags and descriptions, deploy-branch detection from CI workflows
(fail closed), the registry view with the owner's settings, exact name lookup, the owner's edits and `loyihalar`
(SPEC §3.2.6, §4.2, §5.7; UC-10, UC-13 #1, UC-18)."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import pytest

from tests.factories import WORKFLOW_FIXTURES, load_workflow, make_project_record
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    ProjectAliasInvalid,
    ProjectAliasTaken,
    ProjectAmbiguous,
    ProjectDescriptionInvalid,
    ProjectNotFound,
)
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.projects import domain
from yordamchi.dev.projects.domain import (
    FAIL_CLOSED,
    NO_DEPLOY,
    UNPARSEABLE,
    DeployBranches,
    DescriptionSource,
    DocText,
    FolderFacts,
    GitFacts,
    GitKind,
    Project,
    ProjectPrefs,
    SkipReason,
    WorkflowFile,
    WorkflowStat,
)

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
HOME = PurePosixPath("/Users/owner")
WUNDERKIND = (
    "WunderkindAIAgent",
    "WunderkindEmployeesBot",
    "WunderkindLC",
    "WunderkindLMS",
    "WunderkindOlympics",
    "WunderkindTestPortal",
)


def _view(
    name: str,
    prefs: ProjectPrefs | None = None,
    *,
    description: str = "",
    description_source: DescriptionSource = DescriptionSource.NONE,
    is_worktree: bool = False,
    is_archive_name: bool = False,
    missing_since: datetime | None = None,
) -> Project:
    built = make_project_record(
        name,
        at=NOW,
        description=description,
        description_source=description_source,
        is_worktree=is_worktree,
        is_archive_name=is_archive_name,
        missing_since=missing_since,
    )
    return domain.project_view(built, prefs or ProjectPrefs())


def _registry(*names: str, prefs: ProjectPrefs | None = None) -> list[Project]:
    return [_view(name, prefs) for name in names]


# --- folders --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "is_dir", "is_symlink", "reason"),
    [
        ("BonviHR", True, False, None),
        ("MegaPlus ERP", True, False, None),
        ("BONVI_SERVER.md", False, False, SkipReason.NOT_A_FOLDER),
        ("WunderkindLC.zip", False, False, SkipReason.NOT_A_FOLDER),
        (".DS_Store", False, False, SkipReason.HIDDEN),
        (".git", True, False, SkipReason.HIDDEN),
        ("Elsewhere", True, True, SkipReason.SYMLINK),
        ("x" * 65, True, False, SkipReason.NAME_TOO_LONG),
    ],
)
def test_only_visible_real_folders_are_projects(
    name: str, is_dir: bool, is_symlink: bool, reason: SkipReason | None
) -> None:
    assert domain.skip_reason(name, is_dir=is_dir, is_symlink=is_symlink) is reason


def test_an_excluded_folder_is_skipped_whatever_its_case() -> None:
    for name in ("OvozYoz", "ovozyoz"):
        assert domain.skip_reason(name, is_dir=True, is_symlink=False, excluded=["OvozYoz"]) is SkipReason.EXCLUDED
    assert domain.skip_reason("OvozYozWeb", is_dir=True, is_symlink=False, excluded=["OvozYoz"]) is None
    assert domain.skip_reason("OvozYoz", is_dir=True, is_symlink=False) is None  # only the setting excludes


def test_archive_like_names_are_flagged() -> None:
    for name in (
        "BonviCall Clone Archive",
        "Old Archives",
        "site backup",
        "App old",
        "App_old",
        "App copy",
        "App copy 2",
    ):
        assert domain.is_archive_name(name), name
    for name in ("BonviCall", "HRMentor", "Bold", "Golden", "zinnur-v2", "Copywriter"):
        assert not domain.is_archive_name(name), name


def test_worktrees_are_known_by_their_git_file_or_their_name() -> None:
    assert domain.is_worktree("HRMentor-wt-agent-api", GitKind.REPO)
    assert domain.is_worktree("Feature", GitKind.WORKTREE)
    assert not domain.is_worktree("HRMentor", GitKind.REPO)


def test_the_root_setting_expands_the_owners_home() -> None:
    assert domain.projects_root("~/Documents/Projects", home=HOME) == HOME / "Documents" / "Projects"
    assert domain.projects_root(" ~ ", home=HOME) == HOME
    assert domain.projects_root("/Volumes/Work/Projects", home=HOME) == PurePosixPath("/Volumes/Work/Projects")
    assert domain.projects_root("Code", home=HOME) == HOME / "Code"


def test_an_id_is_the_folder_name_exactly_in_nfc() -> None:
    assert domain.project_id("Café") == "Café"
    assert domain.project_id("HRMentor-wt-agent-api") == "HRMentor-wt-agent-api"


# --- descriptions ---------------------------------------------------------------------------------------------------
README = """---
title: x
---
# BonviHR

[![CI](https://example.com/badge.svg)](https://example.com) ![logo](logo.png)
<p align="center"><img src="x.png"></p>

```bash
make dev
```

| col | col |
|-----|-----|

- a list item is not a description

HR tizimi: **xodimlar**, ta'tillar va [oylik](https://example.com/pay) hisob-kitobi. `FastAPI` + React.

Second paragraph.
"""


def test_the_description_is_the_first_prose_paragraph() -> None:
    assert domain.summarize(README) == "HR tizimi: xodimlar, ta'tillar va oylik hisob-kitobi. FastAPI + React."


def test_a_heading_is_never_prose_and_a_title_counts_only_when_it_says_more_than_the_name() -> None:
    assert domain.summarize("# Yordamchi — project rules\n\n## Read first\n") is None
    assert domain.summarize("# BonviHR\n\nTODO\n") is None
    assert domain.summarize("") is None
    assert domain.title("# Yordamchi — project rules\n", name="Yordamchi") is None
    assert domain.title("# BonviCall — Technical specification (release 1)\n", name="BonviCall") is None
    assert domain.title("# PrivateBusiness — Claude uchun kontekst\n", name="PrivateBusiness") is None
    assert domain.title("# ZIN-NUR v2 — QAT'IY TEXNIK SHARTNOMA (SPEC)\n", name="zinnur-v2") is None
    bikes = "# Bolalar velosipedlari — AI sotuv tizimi: reja va hisob-kitob\n\nSana: 2026-09-27 · Holat: faqat reja\n"
    assert domain.title(bikes, name="BS") == "Bolalar velosipedlari — AI sotuv tizimi: reja va hisob-kitob"


def test_a_setext_heading_and_a_github_alert_are_not_descriptions() -> None:
    text = "Title Here Now\n==============\n\n> [!NOTE]\n> read this first please\n\nThe real text is here.\n"
    assert domain.summarize(text) == "The real text is here."


@pytest.mark.parametrize(
    "metadata",
    [
        "Author: plan-architect · Date: 2026-09-04 · Status: **build from this**",
        "Status: **binding for build agents** · 2026-10-20 · plan-architect. Inputs: `BRIEF.md`,\n`STACK.md`, v1.1.",
        "> **HOLAT:** Domain va Application qatlamlari YOZILGAN va kompilyatsiyadan o'tgan.\n> Ular haqiqat manbai.",
        "**Version:** 1.0 · **Date:** 2026-09-10 · **Status:** approved for implementation",
        "Inputs (authority order): `docs/BRIEF.md` > `docs/REQUIREMENTS.md` > `docs/UX.md` and the rest",
        "[Docs](docs/README.md) · [API](https://example.com/api) · [Changelog](CHANGELOG.md)",
    ],
)
def test_metadata_and_link_rows_are_never_descriptions(metadata: str) -> None:
    """T26 eval: BonviCall's description was "Author: plan-architect · Date: 2026-09-04 · Status: build from this",
    BonviGo's and BonviImzo's their "Status:" lines, zinnur-v2's its "HOLAT:" block."""
    assert domain.summarize(f"# X — SPEC\n\n{metadata}\n\nCall records for the sales team, kept for good.\n") == (
        "Call records for the sales team, kept for good."
    )


def test_a_paragraph_under_its_heading_line_is_prose_and_a_pointer_sentence_is_dropped() -> None:
    """T26 eval: PrivateBusiness's CLAUDE.md puts "## Bu nima" right above its text, and BonviHR's README ends its one
    line with "To'liq reja va bosqichlar: PLAN.md"."""
    claude_md = "# PrivateBusiness — Claude uchun kontekst\n\n## Bu nima\nLinkedIn orqali qurilayotgan B2B biznes.\n"
    assert domain.summarize(claude_md) == "LinkedIn orqali qurilayotgan B2B biznes."
    readme = "# Bonvi HR\n\nBonvi uchun HR boshqaruv tizimi.\nTo'liq reja va bosqichlar: [PLAN.md](./PLAN.md)\n"
    assert domain.summarize(readme) == "Bonvi uchun HR boshqaruv tizimi."


def test_a_paragraph_ending_in_a_colon_goes_on_into_its_list() -> None:
    """T26 eval: Yordamchi's description stopped at "… as launchd agents:"."""
    readme = (
        "# Yordamchi\n\nTwo owner-only Telegram bots that run on the owner's MacBook:\n\n"
        "- the **dev bot** turns messages into work, one session per project, and\n"
        "  asks only about important things;\n"
        "- the **personal bot** answers anything.\n\nA supervisor watches both.\n"
    )
    assert domain.summarize(readme) == (
        "Two owner-only Telegram bots that run on the owner's MacBook: the dev bot turns messages into work, one "
        "session per project, and asks only about important things; the personal bot answers anything."
    )


def test_a_spec_or_a_plan_describes_only_through_its_goal() -> None:
    spec = (
        "# BonviCall — Technical specification\n\nStyle and layering rules are NOT in this file.\n\n"
        "## 1. Goal\n\nSalespeople's calls are recorded from their Android phones.\n\n## 2. Architecture\n\nText.\n"
    )
    assert domain.summarize(spec, goal_only=True) == "Salespeople's calls are recorded from their Android phones."
    assert domain.summarize("# X\n\nThe wire contract is code-first.\n\n## 2. Data\n\nRows.\n", goal_only=True) is None
    plan = (
        "# BONVI HR — Tizim Rejasi\n\n> **Maqsad:** HR jarayonini bitta platformaga yig'ish.\n> **Ijrochi:** 1 dev.\n"
    )
    assert domain.summarize(plan, goal_only=True) == "HR jarayonini bitta platformaga yig'ish."


def test_a_description_is_two_or_three_sentences_within_300_characters() -> None:
    long = " ".join(f"Sentence number {index} says one more thing about the project." for index in range(12))
    text = domain.summarize(f"# X\n\n{long}\n")
    assert text is not None
    assert len(text) <= domain.DESCRIPTION_MAX == 300
    assert text.endswith(".")
    assert text.count(".") >= 2


def test_long_text_is_cut_at_a_sentence_or_a_word() -> None:
    sentence = "Birinchi gap juda uzun emas. " + "so'z " * 60
    assert domain.shorten(sentence, 50) == "Birinchi gap juda uzun emas."
    words = "alpha " * 40
    cut = domain.shorten(words.strip(), 30)
    assert cut.endswith("…")
    assert len(cut) <= 30
    assert domain.shorten("short", 30) == "short"


def test_the_owner_beats_readme_beats_claude_md_beats_spec() -> None:
    docs = (
        DocText(DescriptionSource.README, "# X\n"),  # says nothing: skipped
        DocText(DescriptionSource.CLAUDE_MD, "# X rules\n\nTwo owner-only bots that run on the Mac.\n"),
        DocText(DescriptionSource.SPEC, "Spec text that comes last.\n"),
    )
    assert domain.describe(docs, name="X", owner=None) == (
        "Two owner-only bots that run on the Mac.",
        DescriptionSource.CLAUDE_MD,
    )
    assert domain.describe(docs, name="X", owner="Owner's words") == ("Owner's words", DescriptionSource.OWNER)
    assert domain.describe((), name="X", owner=None) == ("", DescriptionSource.NONE)


def test_a_thin_description_is_topped_up_from_the_next_source() -> None:
    """T26 eval: BonviHR's README says only "HR boshqaruv tizimi"; its PLAN.md says what the system gathers."""
    docs = (
        DocText(DescriptionSource.README, "# Bonvi HR\n\nBonvi uchun HR boshqaruv tizimi.\n"),
        DocText(DescriptionSource.PLAN, "# Reja\n\n> **Maqsad:** Excel + Jobster + Verifix'dagi HR ishini yig'ish.\n"),
    )
    assert domain.describe(docs, name="BonviHR", owner=None) == (
        "Bonvi uchun HR boshqaruv tizimi. Excel + Jobster + Verifix'dagi HR ishini yig'ish.",
        DescriptionSource.README,
    )


def test_with_no_prose_anywhere_a_title_or_else_nothing_describes_the_project() -> None:
    plan = "# Bolalar velosipedlari — AI sotuv tizimi\n\nSana: 2026-09-27\n\n## 1. Xulosa\n\n- MVP 8 kunda.\n"
    assert domain.describe((DocText(DescriptionSource.PLAN, plan),), name="BS", owner=None) == (
        "Bolalar velosipedlari — AI sotuv tizimi",
        DescriptionSource.PLAN,
    )
    spec = "# ZIN-NUR v2 — QAT'IY TEXNIK SHARTNOMA (SPEC)\n\n> **HOLAT:** yozilgan.\n\n## 0. TEXNOLOGIYALAR\n"
    assert domain.describe((DocText(DescriptionSource.SPEC, spec),), name="zinnur-v2", owner=None) == (
        "",
        DescriptionSource.NONE,
    )


# --- deploy branches: the fixtures (tests/fixtures/workflows/) ------------------------------------------------------
FIXTURE_CASES = [
    ("deploy_on_push_main.yml", ("main",), (), False, False),
    ("ci_only.yml", (), (), False, False),
    ("on_quoted_key.yml", ("production",), (), False, False),
    ("unparseable.yml", (), (), True, True),
    ("deploy_job_if_main.yml", ("main",), (), False, False),
    ("release_on_tags.yml", (), ("v*",), False, False),
    ("branches_globs.yml", ("prod", "release/**"), (), False, False),
    ("branches_ignore.yml", (), (), True, False),
    ("workflow_run_deploy.yml", ("main",), (), False, False),
    ("reusable_flyctl.yml", (), (), False, False),
    ("step_if_deploy.yml", ("production",), (), False, False),
    ("on_list.yml", (), (), True, True),
    ("docker_build_no_push.yml", (), (), False, False),
    ("negated_if.yml", (), (), True, True),
    ("odd_on.yml", (), (), True, True),
    ("pr_merge_deploy.yml", ("main",), (), False, False),
]


@pytest.mark.parametrize(("fixture", "branches", "tags", "every_branch", "every_tag"), FIXTURE_CASES)
def test_deploy_branches_of_each_fixture(
    fixture: str, branches: tuple[str, ...], tags: tuple[str, ...], every_branch: bool, every_tag: bool
) -> None:
    found = domain.deploy_branches([load_workflow(fixture)])
    assert (found.branches, found.tags, found.every_branch, found.every_tag) == (
        branches,
        tags,
        every_branch,
        every_tag,
    )
    assert found.workflows == ((fixture,) if found.deploys else ())


def test_every_fixture_has_a_case() -> None:
    on_disk = {path.name for path in WORKFLOW_FIXTURES.glob("*.yml")}
    cased = {case[0] for case in FIXTURE_CASES} | {"reusable_caller.yml"}  # the caller is tested with its callee
    assert on_disk == cased


def test_the_unquoted_on_key_arrives_as_true_and_still_counts() -> None:
    workflow = load_workflow("deploy_on_push_main.yml")
    assert isinstance(workflow.document, dict)
    assert True in workflow.document
    assert "on" not in workflow.document


def test_a_push_to_main_asks_and_a_feature_branch_does_not() -> None:
    """UC-13 AC: in a repo whose CI deploys on push to main, main asks once; a feature branch does not."""
    found = domain.deploy_branches([load_workflow("ci_only.yml"), load_workflow("deploy_on_push_main.yml")])
    assert found.covers_branch("main")
    assert not found.covers_branch("feature/login")
    assert found.workflows == ("deploy_on_push_main.yml",)


def test_an_unparseable_workflow_makes_every_push_deploy() -> None:
    found = domain.deploy_branches([load_workflow("ci_only.yml"), load_workflow("unparseable.yml")])
    assert found.every_branch
    assert found.every_tag
    assert found.covers_branch("any/branch")
    assert found.covers_tag("v9")
    assert found.unparseable == ("unparseable.yml",)
    assert domain.deploy_branches([WorkflowFile("x.yml", UNPARSEABLE)]) == DeployBranches(
        every_branch=True, every_tag=True, workflows=("x.yml",), unparseable=("x.yml",)
    )


def test_a_reusable_deploy_workflow_counts_through_its_caller() -> None:
    pair = [load_workflow("reusable_caller.yml"), load_workflow("reusable_flyctl.yml")]
    found = domain.deploy_branches(pair)
    assert found.branches == ("main",)
    assert found.workflows == ("reusable_caller.yml",)
    alone = domain.deploy_branches([load_workflow("reusable_caller.yml")])  # the callee is not there: fail closed
    assert alone.branches == ("main",)


def test_reusable_workflows_from_another_repo_or_a_cycle_count_as_deploying() -> None:
    external = {
        True: {"push": {"branches": ["dev"]}},
        "jobs": {"call": {"uses": "org/ci/.github/workflows/lint.yml@v1"}},
    }
    assert domain.deploy_branches([WorkflowFile("a.yml", external)]).branches == ("dev",)
    loop_a = {True: {"push": {"branches": ["main"]}}, "jobs": {"j": {"uses": "./.github/workflows/b.yml"}}}
    loop_b = {True: "workflow_call", "jobs": {"j": {"uses": "./.github/workflows/a.yml"}}}
    found = domain.deploy_branches([WorkflowFile("a.yml", loop_a), WorkflowFile("b.yml", loop_b)])
    assert found.branches == ("main",)


def test_a_deploy_name_counts_only_when_no_job_says_which_one_deploys() -> None:
    opaque = {True: "push", "jobs": {"build": {"steps": [{"run": "./bin/go"}]}}}
    assert domain.deploy_branches([WorkflowFile("deploy.yml", opaque)]).every_branch
    assert domain.deploy_branches([WorkflowFile("ci.yml", {**opaque, "name": "Release"})]).every_branch
    assert not domain.deploy_branches([WorkflowFile("ci.yml", opaque)]).deploys
    precise = {
        True: "push",
        "jobs": {
            "test": {"steps": [{"run": "make test"}]},
            "ship": {"if": "github.ref == 'refs/heads/main'", "environment": "production", "steps": []},
        },
    }
    found = domain.deploy_branches([WorkflowFile("deploy.yml", precise)])
    assert (found.every_branch, found.branches) == (False, ("main",))


def test_files_that_never_run_add_nothing() -> None:
    assert domain.deploy_branches([WorkflowFile("empty.yml", None)]) == NO_DEPLOY
    no_on = {"jobs": {"deploy": {"steps": [{"run": "./deploy.sh"}]}}}
    assert domain.deploy_branches([WorkflowFile("no_on.yml", no_on)]) == NO_DEPLOY
    scheduled = {True: {"schedule": [{"cron": "0 3 * * *"}]}, "jobs": {"deploy": {"steps": []}}}
    assert domain.deploy_branches([WorkflowFile("nightly.yml", scheduled)]) == NO_DEPLOY


def test_a_job_list_that_cannot_be_read_deploys_on_every_triggered_push() -> None:
    odd = {True: {"push": {"branches": ["main"]}}, "jobs": ["not", "a", "mapping"]}
    assert domain.deploy_branches([WorkflowFile("odd.yml", odd)]).branches == ("main",)
    not_a_mapping = ["just", "a", "list"]
    assert domain.deploy_branches([WorkflowFile("list.yml", not_a_mapping)]).every_branch


def test_a_deploy_secret_marks_a_step_and_a_checkout_key_does_not() -> None:
    def workflow(step: Mapping[str, object]) -> WorkflowFile:
        return WorkflowFile("w.yml", {True: "push", "jobs": {"j": {"steps": [dict(step)]}}})

    secret = {"run": "./run.sh", "env": {"KEY": "${{ secrets.VPS_SSH_KEY }}"}}
    assert domain.deploy_branches([workflow(secret)]).every_branch
    token = {"run": "./run.sh", "env": {"TOKEN": "${{ secrets.GITHUB_TOKEN }}"}}
    assert not domain.deploy_branches([workflow(token)]).deploys
    checkout = {"uses": "actions/checkout@v4", "with": {"ssh-key": "${{ secrets.DEPLOY_KEY }}"}}
    assert not domain.deploy_branches([workflow(checkout)]).deploys
    pushed_image = {"uses": "docker/build-push-action@v6", "with": {"push": "${{ github.event_name != 'pr' }}"}}
    assert domain.deploy_branches([workflow(pushed_image)]).every_branch


def test_a_tag_only_trigger_with_a_branch_condition_deploys_nothing() -> None:
    doc = {
        True: {"push": {"tags": ["v*"]}},
        "jobs": {"deploy": {"if": "github.ref == 'refs/heads/main'", "steps": []}},
    }
    assert domain.deploy_branches([WorkflowFile("t.yml", doc)]) == NO_DEPLOY


@pytest.mark.parametrize(
    ("condition", "branches", "tags"),
    [
        (None, None, None),
        (True, None, None),
        (False, frozenset(), frozenset()),
        ("${{ false }}", frozenset(), frozenset()),
        ("github.ref == 'refs/heads/main'", {"main"}, frozenset()),
        ("'refs/heads/main' == github.ref", {"main"}, frozenset()),
        ("github.ref == 'refs/tags/v1'", frozenset(), {"v1"}),
        ("github.ref_name == 'prod'", {"prod"}, {"prod"}),
        ("github.ref == 'refs/heads/a' || github.ref == 'refs/heads/b'", {"a", "b"}, frozenset()),
        ("github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'", None, None),
        ("!cancelled() && github.ref == 'refs/heads/main'", {"main"}, frozenset()),
        ("!(github.ref == 'refs/heads/main')", None, None),
        ("github.ref != 'refs/heads/main'", None, None),
        ("github.ref == 'refs/pull/1/merge'", frozenset(), frozenset()),
        ("github.event.workflow_run.head_branch == 'main'", {"main"}, {"main"}),
        ("success()", None, None),
    ],
)
def test_conditions_restrict_only_when_they_name_refs(
    condition: object, branches: set[str] | None, tags: set[str] | None
) -> None:
    found = domain.condition_filter(condition)
    assert found.branches == (None if branches is None else frozenset(branches))
    assert found.tags == (None if tags is None else frozenset(tags))


@pytest.mark.parametrize(
    ("pattern", "name", "matches"),
    [
        ("main", "main", True),
        ("main", "Main", True),  # case-insensitive: matching more only over-asks
        ("main", "main2", False),
        ("release/*", "release/v1", True),
        ("release/*", "release/v1/hotfix", False),
        ("release/**", "release/v1/hotfix", True),
        ("v*", "v1.2.0", True),
        ("v[0-9]", "v7", True),
        ("v[0-9]", "vx", False),
        ("ma+in", "maaain", True),
        ("colou?r", "color", True),
        ("*?", "anything", True),
        ("+broken", "whatever", True),  # cannot be read: matches all (fail closed)
        ("[unclosed", "whatever", True),
        ("a\\*b", "a*b", True),
        ("a\\*b", "axb", False),
    ],
)
def test_github_filter_globs(pattern: str, name: str, matches: bool) -> None:
    assert domain.glob_matches(pattern, name) is matches


def test_a_literal_name_from_a_condition_is_escaped() -> None:
    doc = {True: "push", "jobs": {"deploy": {"if": "github.ref == 'refs/heads/a+b'", "steps": []}}}
    found = domain.deploy_branches([WorkflowFile("x.yml", doc)])
    assert found.covers_branch("a+b")
    assert not found.covers_branch("aab")


def test_deploy_branches_round_trip_and_a_bad_row_fails_closed() -> None:
    found = domain.deploy_branches([load_workflow("branches_globs.yml"), load_workflow("release_on_tags.yml")])
    assert DeployBranches.from_json(found.to_json()) == found
    broken: list[JsonValue] = [None, [], "x", {"every_branch": "yes"}, {**found.to_json(), "tags": "v*"}]
    for value in broken:
        assert DeployBranches.from_json(value) == FAIL_CLOSED


def test_union_keeps_everything_either_side_deploys() -> None:
    worktree = DeployBranches(branches=("main",), workflows=("a.yml",))
    committed = DeployBranches(branches=("prod",), tags=("v*",), workflows=("b.yml",))
    both = worktree.union(committed)
    assert (both.branches, both.tags, both.workflows) == (("main", "prod"), ("v*",), ("a.yml", "b.yml"))
    assert worktree.union(FAIL_CLOSED).every_branch
    assert worktree.union(FAIL_CLOSED).branches == ()


def test_the_cache_key_changes_with_head_workflows_and_readability() -> None:
    stats = (WorkflowStat("deploy.yml", 120, 1),)
    base = domain.deploy_key("a1", stats)
    assert domain.deploy_key("a1", stats) == base
    assert domain.deploy_key("b2", stats) != base
    assert domain.deploy_key("a1", (WorkflowStat("deploy.yml", 121, 1),)) != base
    assert domain.deploy_key("a1", ()) != base
    assert domain.deploy_key("a1", None) != domain.deploy_key("a1", ())
    assert len(base) == 64


# --- platform Git integrations (T17b) -------------------------------------------------------------------------------
@pytest.mark.parametrize("config", domain.PLATFORM_FILES)
def test_a_platform_config_makes_the_default_branch_deploy(config: str) -> None:
    """Vercel, Netlify, Render, Railway and App Engine deploy the production branch on push with no workflow file."""
    found = domain.platform_deploy({config, "README.md"}, default_branch="main")
    assert (found.branches, found.every_branch, found.workflows) == (("main",), False, (config,))
    assert found.covers_branch("main")
    assert not found.covers_branch("feature/cart")
    assert not found.covers_tag("v1.0")


def test_no_platform_config_deploys_nothing() -> None:
    assert domain.platform_deploy({"README.md", "package.json", "fly.toml"}, default_branch="main") == NO_DEPLOY
    assert domain.platform_deploy((), default_branch=None) == NO_DEPLOY


@pytest.mark.parametrize("branch", [None, "", "  "])
def test_a_platform_config_with_an_unknown_default_branch_fails_closed(branch: str | None) -> None:
    found = domain.platform_deploy({".vercel/project.json"}, default_branch=branch)
    assert found.every_branch
    assert found.covers_branch("anything")


def test_a_platform_default_branch_is_matched_literally_and_joins_the_workflows() -> None:
    odd = domain.platform_deploy({"netlify.toml"}, default_branch="release[1]*")
    assert odd.covers_branch("release[1]*")
    assert not odd.covers_branch("release1x")
    both = domain.deploy_branches([load_workflow("release_on_tags.yml")]).union(
        domain.platform_deploy({"vercel.json", "railway.toml"}, default_branch="master")
    )
    assert (both.branches, both.tags) == (("master",), ("v*",))
    assert both.workflows == ("railway.toml", "release_on_tags.yml", "vercel.json")


def test_the_cache_key_changes_with_platform_configs_and_the_default_branch() -> None:
    stats = (WorkflowStat("deploy.yml", 120, 1),)
    base = domain.deploy_key("a1", stats, platform=(), default_branch="main")
    assert domain.deploy_key("a1", stats) == domain.deploy_key("a1", stats, platform=(), default_branch=None)
    assert domain.deploy_key("a1", stats, platform=("vercel.json",), default_branch="main") != base
    assert domain.deploy_key("a1", stats, platform=(), default_branch="master") != base
    assert domain.deploy_key("a1", stats, platform=("a", "b"), default_branch="main") == domain.deploy_key(
        "a1", stats, platform=("b", "a"), default_branch="main"
    )


# --- the registry view ----------------------------------------------------------------------------------------------
def test_an_archive_name_is_archived_until_the_owner_unarchives_it() -> None:
    name = "BonviCall Clone Archive"
    archived = _view(name, is_archive_name=True)
    assert (archived.archived, archived.exact_name_only, archived.routable) == (True, True, False)
    back = _view(name, ProjectPrefs(unarchived=[name.lower()]), is_archive_name=True)
    assert (back.archived, back.exact_name_only, back.routable) == (False, False, True)


def test_the_owners_archive_list_wins_over_unarchived() -> None:
    prefs = ProjectPrefs(archived=["BonviGo"], unarchived=["BonviGo"])
    assert _view("BonviGo", prefs).archived


def test_a_worktree_answers_only_to_its_exact_name_but_is_not_archived() -> None:
    worktree = _view("HRMentor-wt-agent-api", is_worktree=True)
    assert (worktree.archived, worktree.exact_name_only, worktree.routable) == (False, True, False)
    assert _view("HRMentor-wt-agent-api", ProjectPrefs(unarchived=["HRMentor-wt-agent-api"]), is_worktree=True).routable


def test_a_missing_folder_is_not_routable() -> None:
    gone = _view("BonviAGI", missing_since=NOW)
    assert gone.missing
    assert not gone.routable


def test_settings_supply_aliases_descriptions_and_the_executor() -> None:
    prefs = ProjectPrefs(
        aliases={"hr": "BonviHR", "HR tizim": "bonvihr", "mentor": "HRMentor"},
        descriptions={"bonvihr": "  Kadrlar   tizimi "},
        executor_per_project={"BonviHR": "codex", "HRMentor": "gpt"},
    )
    project = _view("BonviHR", prefs, description="from README", description_source=DescriptionSource.README)
    assert project.aliases == ("HR tizim", "hr")
    assert (project.description, project.description_source) == ("Kadrlar tizimi", DescriptionSource.OWNER)
    assert project.executor == "codex"
    assert _view("HRMentor", prefs).executor == "claude"  # an unknown executor falls back to the default
    assert _view("Other", ProjectPrefs(executor_default="nope")).executor == "claude"


def _folder(name: str, *, git: GitKind = GitKind.REPO, docs: tuple[DocText, ...] = ()) -> FolderFacts:
    return FolderFacts(name=name, path=PurePosixPath("/p") / name, git=git, docs=docs)


def test_a_new_record_takes_its_facts_from_the_scan() -> None:
    docs = (DocText(DescriptionSource.README, "# X\n\nA project that does things well.\n"),)
    git = GitFacts(
        head="abc", head_at=NOW - timedelta(days=1), remote_url="git@github.com:o/x.git", default_branch="main"
    )
    record = domain.record_for(
        _folder("HRMentor-wt-agent-api", git=GitKind.WORKTREE, docs=docs),
        git=git,
        deploy=NO_DEPLOY,
        deploy_checked_sha="k",
        previous=None,
        prefs=ProjectPrefs(),
        now=NOW,
    )
    assert record.id == "HRMentor-wt-agent-api"
    assert record.path == "/p/HRMentor-wt-agent-api"
    assert (record.description, record.description_source) == ("A project that does things well.", "readme")
    assert (record.is_worktree, record.is_archive_name, record.executor) == (True, False, "claude")
    assert (record.git_remote_url, record.default_branch, record.deploy_checked_sha) == (git.remote_url, "main", "k")
    assert (record.discovered_at, record.last_seen_at, record.last_activity_at) == (NOW, NOW, git.head_at)


def test_a_refresh_keeps_history_and_git_facts_when_git_failed() -> None:
    earlier = NOW - timedelta(days=30)
    previous = make_project_record("BonviHR", at=earlier, missing_since=earlier)
    previous = domain.record_for(
        _folder("BonviHR"),
        git=GitFacts(head="a", head_at=NOW - timedelta(hours=2), remote_url="u", default_branch="dev"),
        deploy=NO_DEPLOY,
        deploy_checked_sha="k",
        previous=previous,
        prefs=ProjectPrefs(),
        now=earlier,
    )
    later = domain.record_for(
        _folder("BonviHR"),
        git=None,
        deploy=NO_DEPLOY,
        deploy_checked_sha=None,
        previous=previous,
        prefs=ProjectPrefs(),
        now=NOW,
    )
    assert (later.discovered_at, later.last_seen_at, later.missing_since) == (earlier, NOW, None)
    assert (later.git_remote_url, later.default_branch) == ("u", "dev")
    assert later.last_activity_at == NOW - timedelta(hours=2)


def test_vanished_lists_unseen_projects_not_yet_missing() -> None:
    known = {
        "A": make_project_record("A", at=NOW),
        "B": make_project_record("B", at=NOW),
        "C": make_project_record("C", at=NOW, missing_since=NOW),
    }
    assert domain.vanished(known, {"A"}) == ("B",)


# --- names (look-alikes stay distinct) ------------------------------------------------------------------------------
def test_look_alike_names_never_merge() -> None:
    registry = _registry("HRMentor", "HRMentor-wt-agent-api", *WUNDERKIND)
    assert [project.id for project in domain.find_projects("hrmentor", registry)] == ["HRMentor"]
    assert [project.id for project in domain.find_projects("HRMentor-wt-agent-api", registry)] == [
        "HRMentor-wt-agent-api"
    ]
    assert domain.find_projects("Wunderkind", registry) == ()
    for name in WUNDERKIND:
        assert [project.id for project in domain.find_projects(name.upper(), registry)] == [name]


def test_a_name_beats_an_alias_and_an_alias_finds_its_project() -> None:
    prefs = ProjectPrefs(aliases={"lc": "WunderkindLC", "  Markaz ": "WunderkindLC"})
    registry = _registry("WunderkindLC", "WunderkindLMS", prefs=prefs)
    assert [project.id for project in domain.find_projects("LC", registry)] == ["WunderkindLC"]
    assert [project.id for project in domain.find_projects("markaz", registry)] == ["WunderkindLC"]


def test_resolving_a_name_fails_plainly() -> None:
    registry = _registry("BonviHR", "BonviGo")
    assert domain.resolve_project(" bonvigo ", registry).id == "BonviGo"
    with pytest.raises(ProjectNotFound) as missing:
        domain.resolve_project("Bonvi", registry)
    assert missing.value.params == {"name": "Bonvi"}
    twins = [_view("App"), _view("app")]  # possible on a case-sensitive volume
    with pytest.raises(ProjectAmbiguous) as ambiguous:
        domain.resolve_project("APP", twins)
    assert ambiguous.value.params == {"name": "APP", "candidates": "App, app"}


# --- the owner's edits ----------------------------------------------------------------------------------------------
def test_adding_an_alias() -> None:
    registry = _registry("BonviHR", "HRMentor", prefs=ProjectPrefs(aliases={"mentor": "HRMentor"}))
    bonvi = registry[0]
    assert domain.with_alias({"mentor": "HRMentor"}, registry, project=bonvi, alias="  hr  ") == {
        "mentor": "HRMentor",
        "hr": "BonviHR",
    }
    assert domain.with_alias({"hr": "BonviHR"}, registry, project=bonvi, alias="HR") is None  # already there
    assert domain.with_alias({}, registry, project=bonvi, alias="bonvihr") is None  # its own name
    with pytest.raises(ProjectAliasTaken) as taken:
        domain.with_alias({"mentor": "HRMentor"}, registry, project=bonvi, alias="Mentor")
    assert taken.value.params == {"alias": "Mentor", "project": "HRMentor"}
    with pytest.raises(ProjectAliasTaken):
        domain.with_alias({}, registry, project=bonvi, alias="hrmentor")  # another project's folder name
    for bad in ("x", "y" * 41, "two\nlines"):
        with pytest.raises(ProjectAliasInvalid):
            domain.with_alias({}, registry, project=bonvi, alias=bad)


def test_archiving_and_unarchiving() -> None:
    plain = _view("BonviGo")
    change = domain.with_archived(ProjectPrefs(unarchived=["bonvigo"]), plain, archived=True)
    assert change == domain.ArchiveChange(archived=["BonviGo"], unarchived=[])
    assert domain.with_archived(ProjectPrefs(archived=["BonviGo"]), plain, archived=True) == domain.ArchiveChange()
    back = domain.with_archived(ProjectPrefs(archived=["x", "BonviGo"]), plain, archived=False)
    assert back == domain.ArchiveChange(archived=["x"], unarchived=None)

    by_name = _view("BonviCall Clone Archive", is_archive_name=True)
    assert domain.with_archived(ProjectPrefs(), by_name, archived=False) == domain.ArchiveChange(
        archived=None, unarchived=["BonviCall Clone Archive"]
    )


def test_describing_a_project() -> None:
    project = _view("BonviHR")
    assert domain.with_description({"bonvihr": "old", "X": "y"}, project, "  Kadrlar   tizimi ") == {
        "X": "y",
        "BonviHR": "Kadrlar tizimi",
    }
    for bad in ("", "   ", "z" * 301, "two\nlines"):
        with pytest.raises(ProjectDescriptionInvalid):
            domain.with_description({}, project, bad)


# --- "loyihalar" ----------------------------------------------------------------------------------------------------
def test_the_listing_shows_active_projects_then_archived_and_missing() -> None:
    prefs = ProjectPrefs(aliases={"hr": "BonviHR"})
    projects = [
        _view("HRMentor-wt-agent-api", is_worktree=True),
        _view("BonviHR", prefs, description="Kadrlar tizimi"),
        _view("BonviCall Clone Archive", is_archive_name=True),
        _view("BonviAGI", missing_since=NOW),
        _view("AgentsRoom"),
    ]
    text = domain.listing(projects, root="~/Documents/Projects")
    assert text.splitlines() == [
        S.PROJECTS_SUMMARY(active=3, archived=1),
        "• AgentsRoom",
        "• BonviHR — Kadrlar tizimi (yana: hr)",
        "• HRMentor-wt-agent-api (worktree)",
        S.PROJECTS_ARCHIVED_LIST(projects="BonviCall Clone Archive"),
        S.PROJECTS_MISSING_LIST(projects="BonviAGI"),
    ]
    assert domain.listing([], root="~/P") == S.PROJECTS_NONE(root="~/P")
