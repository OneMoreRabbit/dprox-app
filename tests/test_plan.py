from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from dprox.config import PlanConfig
from dprox.plan import (
    AgentEntry,
    PlanCache,
    PlanError,
    load_plan_from_file,
    parse_plan_dict,
)

# Contract 0.11 states each group's org in `groups[]`, and dprox refuses a plan
# without it. These tests predate that section, so this supplies one covering
# every group name they use; the org value itself is irrelevant to them.
_TEST_GROUPS = [
    {"name": n, "org": "testorg"}
    for n in ("g", "g1", "g2", "g_admin", "g_engineering", "g_fresh", "g_late", "g_new")
]


def _with_groups(data: dict) -> dict:
    """Add a `groups[]` section to a pre-0.11 plan dict, unless it has one."""
    return data if "groups" in data else {**data, "groups": _TEST_GROUPS}

# --- parse_plan_dict ----------------------------------------------------------


def test_parse_real_compiled_plan_shape() -> None:
    """Mirror the real rbac-compile output shape from compiled_plan.yml."""
    data = {
        "meta": {"compiled_at": "2026-04-27T13:50:06Z"},
        "required_groups": ["arc_g0_engineering_global", "arc_g5_any_global"],
        # groups[] as contract 0.11 emits it: the tuple the name was built
        # from, stated outright. dprox reads `org` from here.
        "groups": [
            {
                "name": "arc_g0_engineering_global",
                "org": "arc",
                "grade": "g0",
                "vertical": "engineering",
                "scope": "global",
            },
            {
                "name": "arc_g5_any_global",
                "org": "arc",
                "grade": "g5",
                "vertical": "any",
                "scope": "global",
            },
        ],
        "agent_users": [
            {
                "name": "agent_oversight",
                "description": "Multi-org oversight",
                "groups": ["arc_g0_engineering_global", "arc_g5_any_global"],
            },
            {
                "name": "agent_arc_finance_global",
                "description": "ARC UK finance agent",
                "groups": ["arc_g5_any_global"],
            },
        ],
        "admin_users": [
            {
                "name": "beaver",
                "groups": ["arc_g0_engineering_global", "arc_g5_any_global"],
            },
        ],
        "directory_classifications": [
            {"path": "anything", "group": "arc_g0_engineering_global"},
        ],
    }

    result = parse_plan_dict(_with_groups(data))

    # Every caller resolves to the org groups[] states — not to anything
    # parsed out of the "arc_" prefix on the group names.
    for entry in result.values():
        assert entry.orgs == frozenset({"arc"})

    assert set(result) == {"agent_oversight", "agent_arc_finance_global", "beaver"}
    assert result["agent_oversight"].role == "agent"
    assert result["beaver"].role == "admin"
    assert result["agent_oversight"].groups == frozenset(
        {"arc_g0_engineering_global", "arc_g5_any_global"}
    )
    assert result["beaver"].description is None  # admin entry without description


def test_parse_returns_frozenset_groups() -> None:
    data = {
        "agent_users": [{"name": "agent_alice", "groups": ["g1", "g2"]}],
    }
    result = parse_plan_dict(_with_groups(data))
    assert isinstance(result["agent_alice"].groups, frozenset)


def test_parse_empty_groups_allowed() -> None:
    data = {"agent_users": [{"name": "agent_loner", "groups": []}]}
    result = parse_plan_dict(_with_groups(data))
    assert result["agent_loner"].groups == frozenset()


def test_parse_duplicate_in_agent_users_rejected() -> None:
    data = {
        "agent_users": [
            {"name": "agent_alice", "groups": ["g1"]},
            {"name": "agent_alice", "groups": ["g2"]},
        ],
    }
    with pytest.raises(PlanError, match="duplicate"):
        parse_plan_dict(_with_groups(data))


def test_parse_name_in_both_lists_rejected() -> None:
    data = {
        "agent_users": [{"name": "shared", "groups": ["g1"]}],
        "admin_users": [{"name": "shared", "groups": ["g2"]}],
    }
    with pytest.raises(PlanError, match="both agent_users and admin_users"):
        parse_plan_dict(_with_groups(data))


def test_parse_empty_plan_rejected() -> None:
    with pytest.raises(PlanError, match="no agent_users or admin_users"):
        parse_plan_dict({})


def test_parse_admin_only_plan_works() -> None:
    data = {"admin_users": [{"name": "ops", "groups": ["g_admin"]}]}
    result = parse_plan_dict(_with_groups(data))
    assert result["ops"].role == "admin"


def test_parse_missing_name_rejected() -> None:
    data = {"agent_users": [{"groups": ["g1"]}]}
    with pytest.raises(PlanError, match="validation failed"):
        parse_plan_dict(_with_groups(data))


# --- load_plan_from_file ------------------------------------------------------


def test_load_from_file_happy(plan_yaml: Path) -> None:
    result = load_plan_from_file(plan_yaml)
    assert "agent_alice" in result
    assert "admin_alice" in result
    assert result["agent_alice"].role == "agent"
    assert result["admin_alice"].role == "admin"


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="not found"):
        load_plan_from_file(tmp_path / "nope.yml")


def test_load_directory_path(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="not a file"):
        load_plan_from_file(tmp_path)


def test_load_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "empty.yml"
    target.write_text("", encoding="utf-8")
    with pytest.raises(PlanError, match="empty"):
        load_plan_from_file(target)


def test_load_non_mapping_root(tmp_path: Path) -> None:
    target = tmp_path / "list.yml"
    target.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(PlanError, match="must be a mapping"):
        load_plan_from_file(target)


def test_load_malformed_yaml(tmp_path: Path) -> None:
    target = tmp_path / "bad.yml"
    target.write_text("agent_users:\n  - {{{", encoding="utf-8")
    with pytest.raises(PlanError, match="YAML parse error"):
        load_plan_from_file(target)


def test_load_repo_example_file_parses() -> None:
    """Sanity check that the shipped example loads without errors."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "examples" / "compiled_plan.example.yml"
    assert example.exists()
    result = load_plan_from_file(example)
    assert "agent_alice" in result
    assert "admin_alice" in result


# --- PlanCache: basic behaviour -----------------------------------------------


def test_cache_initial_load(plan_cache: PlanCache) -> None:
    assert plan_cache.loaded is True
    agents, admins = plan_cache.counts()
    assert agents == 3
    assert admins == 1


def test_cache_lookup_known_cn(plan_cache: PlanCache) -> None:
    entry = plan_cache.lookup("agent_alice")
    assert isinstance(entry, AgentEntry)
    assert entry.role == "agent"
    assert entry.groups == frozenset({"g_engineering"})


def test_cache_lookup_admin(plan_cache: PlanCache) -> None:
    entry = plan_cache.lookup("admin_alice")
    assert entry is not None
    assert entry.role == "admin"


def test_cache_lookup_unknown_returns_none(plan_cache: PlanCache) -> None:
    assert plan_cache.lookup("agent_ghost") is None


# --- PlanCache: mtime-driven reload -------------------------------------------


def _rewrite_plan(path: Path, plan_dict: dict) -> None:
    """Rewrite plan and bump mtime forward so the cache notices."""
    path.write_text(yaml.safe_dump(_with_groups(plan_dict)), encoding="utf-8")
    # Bump mtime by 2s — tmp filesystems can have whole-second mtime resolution.
    new_time = time.time() + 2
    import os

    os.utime(path, (new_time, new_time))


def test_cache_reloads_on_mtime_change(plan_yaml: Path, baseline_config) -> None:
    cache = PlanCache(baseline_config.plan)
    cache.initial_load()
    assert cache.lookup("agent_charlie") is None

    _rewrite_plan(
        plan_yaml,
        {
            "agent_users": [
                {"name": "agent_alice", "groups": ["g_engineering"]},
                {"name": "agent_charlie", "groups": ["g_new"]},
            ]
        },
    )

    entry = cache.lookup("agent_alice")
    # After mtime-triggered reload, both old and new agents resolve.
    assert entry is not None
    new_entry = cache.lookup("agent_charlie")
    assert new_entry is not None
    assert new_entry.groups == frozenset({"g_new"})


def test_cache_no_reload_when_mtime_unchanged(
    plan_yaml: Path, baseline_config_dict: dict
) -> None:
    """Disable the unknown-CN reload path so we isolate the mtime-only behavior."""
    baseline_config_dict["plan"]["reload_on_unknown_cn"] = False
    from dprox.config import Config

    config = Config.model_validate(baseline_config_dict)
    cache = PlanCache(config.plan)
    cache.initial_load()

    # Modify the file in place but keep the original mtime — cache should not see the change.
    original_stat = plan_yaml.stat()
    plan_yaml.write_text(
        yaml.safe_dump(
            _with_groups({"agent_users": [{"name": "agent_only_here_now", "groups": ["g"]}]})
        ),
        encoding="utf-8",
    )
    import os

    os.utime(plan_yaml, (original_stat.st_atime, original_stat.st_mtime))

    # Old agents still resolve from cache; the new one is invisible.
    assert cache.lookup("agent_alice") is not None
    assert cache.lookup("agent_only_here_now") is None


# --- PlanCache: unknown-CN reload + rate limit --------------------------------


def test_cache_reloads_on_unknown_cn(plan_yaml: Path, baseline_config_dict: dict) -> None:
    """If reload_on_mtime_change is off, the unknown-CN path is the only reload trigger."""
    baseline_config_dict["plan"]["reload_on_mtime_change"] = False
    baseline_config_dict["plan"]["reload_on_unknown_cn"] = True
    baseline_config_dict["plan"]["reload_min_interval_seconds"] = 0
    from dprox.config import Config

    config = Config.model_validate(baseline_config_dict)
    cache = PlanCache(config.plan)
    cache.initial_load()

    # Add a new agent to disk, but mtime checks are off so it'd stay invisible
    # except for the unknown-CN reload path.
    _rewrite_plan(
        plan_yaml,
        {
            "agent_users": [
                {"name": "agent_alice", "groups": ["g_engineering"]},
                {"name": "agent_new", "groups": ["g_new"]},
            ]
        },
    )

    entry = cache.lookup("agent_new")
    assert entry is not None


def test_unknown_cn_reload_is_rate_limited(
    plan_yaml: Path, baseline_config_dict: dict
) -> None:
    """A flood of unknown CNs must not reload the plan on every miss."""
    baseline_config_dict["plan"]["reload_on_mtime_change"] = False
    baseline_config_dict["plan"]["reload_on_unknown_cn"] = True
    baseline_config_dict["plan"]["reload_min_interval_seconds"] = 60
    from dprox.config import Config

    config = Config.model_validate(baseline_config_dict)
    cache = PlanCache(config.plan)
    cache.initial_load()

    # First unknown lookup should trigger a reload.
    assert cache.lookup("agent_first_miss") is None

    # Now mutate the plan file — but the rate limit blocks any further reload
    # for the next 60s, so subsequent unknown CNs see stale cache.
    _rewrite_plan(
        plan_yaml,
        {
            "agent_users": [
                {"name": "agent_alice", "groups": ["g_engineering"]},
                {"name": "agent_late", "groups": ["g_late"]},
            ]
        },
    )

    # agent_late was added after the first reload finished and is invisible
    # because the second lookup is rate-limited from reloading.
    assert cache.lookup("agent_late") is None


def test_cache_reload_fails_closed_on_corrupt_file(
    plan_yaml: Path, baseline_config: PlanConfig | None
) -> None:
    """If reload finds a corrupt plan, lookup must raise PlanError (not serve stale)."""
    cache = PlanCache(baseline_config.plan)
    cache.initial_load()
    assert cache.lookup("agent_alice") is not None

    # Force a reload via mtime change, but write a corrupt file.
    plan_yaml.write_text("agent_users: this is not a list", encoding="utf-8")
    new_time = time.time() + 2
    import os

    os.utime(plan_yaml, (new_time, new_time))

    with pytest.raises(PlanError):
        cache.lookup("agent_alice")


def test_cache_disabled_reads_fresh_each_lookup(
    plan_yaml: Path, baseline_config_dict: dict
) -> None:
    baseline_config_dict["plan"]["cache_enabled"] = False
    from dprox.config import Config

    config = Config.model_validate(baseline_config_dict)
    cache = PlanCache(config.plan)
    cache.initial_load()

    # Mutate plan WITHOUT bumping mtime — cache_disabled should still see it.
    plan_yaml.write_text(
        yaml.safe_dump(
            _with_groups({"agent_users": [{"name": "agent_fresh", "groups": ["g_fresh"]}]})
        ),
        encoding="utf-8",
    )

    assert cache.lookup("agent_fresh") is not None


def test_initial_load_missing_file_raises(baseline_config_dict: dict, tmp_path: Path) -> None:
    baseline_config_dict["plan"]["compiled_plan_path"] = str(tmp_path / "nope.yml")
    from dprox.config import Config

    config = Config.model_validate(baseline_config_dict)
    cache = PlanCache(config.plan)
    with pytest.raises(PlanError, match="not found"):
        cache.initial_load()


# --- a group's org comes from groups[], never from its name -------------------
#
# Contract 0.11 states each group's org. dprox reads it from there: parsing
# "arc_g2_..." would make an access-control decision out of a naming
# convention (constitution principle 10). These cover the refusals, which are
# the security-relevant half.


def test_orgs_resolve_from_groups_section() -> None:
    data = {
        "groups": [
            {"name": "arc_public", "org": "arc"},
            {"name": "job_public", "org": "job"},
        ],
        "agent_users": [
            {"name": "agent_both", "groups": ["arc_public", "job_public"]},
            {"name": "agent_arc", "groups": ["arc_public"]},
        ],
    }
    out = parse_plan_dict(data)

    assert out["agent_both"].orgs == frozenset({"arc", "job"})
    assert out["agent_arc"].orgs == frozenset({"arc"})


def test_org_is_not_guessed_from_the_group_name() -> None:
    """A name that looks like one org but is declared another follows groups[].

    If dprox ever parsed the prefix it would answer "arc" here. It must
    answer what the plan says.
    """
    data = {
        "groups": [{"name": "arc_looks_like_arc", "org": "job"}],
        "agent_users": [{"name": "agent_x", "groups": ["arc_looks_like_arc"]}],
    }
    assert parse_plan_dict(data)["agent_x"].orgs == frozenset({"job"})


def test_plan_without_groups_section_is_refused() -> None:
    """A pre-0.11 plan is refused rather than served with unknown orgs."""
    data = {"agent_users": [{"name": "agent_x", "groups": ["g1"]}]}
    with pytest.raises(PlanError, match="groups.*0.11"):
        parse_plan_dict(data)


def test_group_absent_from_groups_section_is_refused() -> None:
    """An unresolvable group refuses the plan; it is never skipped.

    Skipping it would quietly shrink a caller's orgs, and guessing from the
    name is the thing the section exists to stop. Both are wrong, so refuse.
    """
    data = {
        "groups": [{"name": "arc_public", "org": "arc"}],
        "agent_users": [{"name": "agent_x", "groups": ["arc_public", "cpf_secret"]}],
    }
    with pytest.raises(PlanError, match="cpf_secret"):
        parse_plan_dict(data)


def test_group_with_two_orgs_is_refused() -> None:
    data = {
        "groups": [
            {"name": "arc_public", "org": "arc"},
            {"name": "arc_public", "org": "job"},
        ],
        "agent_users": [{"name": "agent_x", "groups": ["arc_public"]}],
    }
    with pytest.raises(PlanError, match="two orgs"):
        parse_plan_dict(data)
