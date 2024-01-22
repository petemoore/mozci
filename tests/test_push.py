# -*- coding: utf-8 -*-

from itertools import count

import pytest

from mozci import config
from mozci.data.sources import bugbug
from mozci.errors import (
    ChildPushNotFound,
    ParentPushNotFound,
    PushNotFound,
    SourcesNotFound,
)
from mozci.push import Push, PushStatus, Regressions, ToRetriggerOrBackfill
from mozci.task import GroupResult, GroupSummary, Status, Task, TestTask
from mozci.util.hgmo import HgRev
from mozci.util.taskcluster import (
    PRODUCTION_TASKCLUSTER_ROOT_URL,
    get_artifact_url,
    get_index_url,
)

SCHEDULES_EXTRACT = {
    "tasks": {
        "test-android-em-7.0-x86_64-lite-qr/debug-geckoview-junit-fis-e10s": 0.51,
        "test-linux1804-64-qr/opt-telemetry-tests-client-fis-e10s": 0.52,
    },
    "groups": {
        "toolkit/modules/tests/browser/browser.ini": 0.68,
        "devtools/client/framework/test/browser.ini": 0.99,
    },
    "config_groups": {
        "toolkit/modules/tests/browser/browser.ini": [
            "test-linux1804-64-qr/opt-*-swr-e10s"
        ],
        "devtools/client/framework/test/browser.ini": [
            "test-linux1804-64-qr/opt-*-e10s"
        ],
    },
    "reduced_tasks": {
        "test-android-em-7.0-x86_64-lite-qr/opt-geckoview-junit-fis-e10s": 0.88,
        "test-linux1804-64-qr/debug-reftest-swr-e10s-2": 0.83,
    },
    "reduced_tasks_higher": {},
    "known_tasks": [
        "test-windows10-64-2004-qr/debug-web-platform-tests-swr-e10s-9",
        "test-windows10-64-2004-qr/debug-mochitest-devtools-chrome-fis-e10s-1",
    ],
}

NUMBER_OF_DEFAULT_GROUPS = 5
NUMBER_OF_INTERMITTENT_GROUPS_IN_DEFAULT = 2
GROUP_SUMMARIES_DEFAULT = {
    group.name: group
    for group in [
        GroupSummary(
            f"group{i}",
            [
                Task.create(
                    id=j,
                    label=f"test-task{j}",
                    result="failed",
                    _results=[GroupResult(group=f"group{i}", ok=False, duration=42)],
                )
                for j in range(1, 4)
            ]
            + (
                [
                    Task.create(
                        id=4,
                        label="test-task1",
                        result="passed",
                        _results=[GroupResult(group=f"group{i}", ok=True, duration=42)],
                    )
                ]
                if i <= NUMBER_OF_INTERMITTENT_GROUPS_IN_DEFAULT
                else []
            ),
        )
        for i in range(1, NUMBER_OF_DEFAULT_GROUPS + 1)
    ]
}


def test_group_summaries_default_status():
    assert {
        **{
            f"group{i}": Status.INTERMITTENT
            for i in range(1, NUMBER_OF_INTERMITTENT_GROUPS_IN_DEFAULT + 1)
        },
        **{
            f"group{i}": Status.FAIL
            for i in range(
                NUMBER_OF_INTERMITTENT_GROUPS_IN_DEFAULT + 1,
                NUMBER_OF_DEFAULT_GROUPS + 1,
            )
        },
    } == {g.name: g.status for g in GROUP_SUMMARIES_DEFAULT.values()}


def make_tasks(group_id):
    return [
        TestTask(
            id=j,
            label=f"test-task{j}",
            result="failed",
            _results=[GroupResult(group=group_id, ok=False, duration=42)],
        )
        for j in range(1, 4)
    ]


@pytest.fixture
def create_changesets():
    """Return a set of changesets in automationrelevance format.

    Ordered from base -> head.
    """

    def node(i):
        i = str(i)
        pad = "0" * (40 - len(i))
        return pad + i

    def inner(num, extra=None, head=1):
        changesets = []
        for i in reversed(range(head, num + head)):
            c = {
                "node": node(i),
                "parents": [node(i + 1)],
                "pushhead": node(head),
            }
            if isinstance(extra, list):
                c.update(extra[num - i])
            elif isinstance(extra, dict):
                c.update(extra)

            changesets.append(c)

        return changesets

    return inner


def test_create_push(responses):
    def setup_responses(ctx):
        responses.reset()
        responses.add(
            responses.GET,
            HgRev.JSON_PUSHES_TEMPLATE.format(**ctx),
            json={
                "pushes": {
                    "123": {
                        "changesets": [
                            {"node": "123456", "desc": "Bug 567890 - Fix something bad"}
                        ],
                        "date": 1213174092,
                        "user": "user@example.org",
                    },
                },
            },
            status=200,
        )
        responses.add(
            responses.GET,
            HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(
                branch=ctx["branch"], rev="abcdef"
            ),
            json={"changesets": [{"node": "abcdef"}]},
            status=200,
        )
        responses.add(
            responses.GET,
            HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(
                branch=ctx["branch"], rev="123456"
            ),
            json={"changesets": [{"node": "123456"}]},
            status=200,
        )

    ctx = {
        "branch": "integration/autoland",
        "push_id_start": "122",
        "push_id_end": "123",
    }
    setup_responses(ctx)
    p1 = Push("abcdef")
    p2 = p1.create_push(123)
    assert p2.rev == "123456"
    assert p2.id == 123
    assert p2.date == 1213174092
    assert p2.branch in ctx["branch"]

    ctx["branch"] = "mozilla-central"
    setup_responses(ctx)
    p1 = Push("abcdef", branch=ctx["branch"])
    p2 = p1.create_push(123)
    assert p2.rev == "123456"
    assert p2.id == 123
    assert p2.date == 1213174092
    assert p2.branch in ctx["branch"]


def test_push_tasks_with_tier(responses):
    cache = config.cache
    rev = "abcdef"
    branch = "autoland"

    TASKS_KEY = "{}/{}/tasks".format(branch, rev)

    # Making sure there's nothing left in the cache
    if cache.get(TASKS_KEY):
        cache.forget(TASKS_KEY)
    assert cache.get(TASKS_KEY) is None

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/integration/autoland/json-automationrelevance/{rev}",
        json={"changesets": [{"node": rev, "pushdate": [1638349140]}]},
        status=200,
    )

    responses.add(
        responses.GET,
        "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/gecko.v2.autoland.revision.abcdef.taskgraph.decision",
        json={"taskId": 1},
        status=200,
    )

    responses.add(
        responses.GET,
        "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/1",
        json={"taskGroupId": "xyz789"},
        status=200,
    )

    responses.add(
        responses.GET,
        "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task-group/xyz789/list",
        json={
            "tasks": [
                {
                    "task": {
                        "extra": {
                            "treeherder": {"tier": 3},
                            "suite": "task",
                        },
                        "metadata": {
                            "name": "task-A",
                        },
                        "tags": {"name": "tag-A"},
                    },
                    "status": {
                        "taskId": "abc13",
                        "state": "unscheduled",
                    },
                },
                {
                    "task": {
                        "extra": {
                            "treeherder": {"tier": 1},
                            "suite": "task",
                        },
                        "metadata": {
                            "name": "task-B",
                        },
                        "tags": {"name": "tag-A"},
                    },
                    "status": {
                        "taskId": "abc123",
                        "state": "unscheduled",
                    },
                },
            ]
        },
        status=200,
    )

    responses.add(
        responses.GET,
        "https://treeherder.mozilla.org/api/project/autoland/note/push_notes/?revision=abcdef&format=json",
        json={},
        status=200,
    )

    push = Push(rev, branch)
    tasks = push.tasks
    print(len(tasks))
    assert len(tasks) == 1


def test_push_tasks_with_cached_uncompleted_tasks(monkeypatch, responses):
    rev = "abcdef"
    branch = "autoland"

    cached_tasks = [Task.create(id=1, label="test-task", state="running")]
    monkeypatch.setattr(config.cache, "get", lambda x: cached_tasks)

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/integration/autoland/json-automationrelevance/{rev}",
        json={"changesets": [{"node": rev, "pushdate": [1638349140]}]},
        status=200,
    )

    responses.add(
        responses.GET,
        "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/gecko.v2.autoland.revision.abcdef.taskgraph.decision",
        json={"taskId": 1},
        status=200,
    )

    responses.add(
        responses.GET,
        "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/1",
        json={"taskGroupId": "xyz789"},
        status=200,
    )

    responses.add(
        responses.GET,
        "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task-group/xyz789/list",
        json={
            "tasks": [
                {
                    "task": {
                        "extra": {
                            "treeherder": {"tier": 3},
                        },
                        "metadata": {
                            "name": "task-A",
                        },
                        "tags": {"name": "tag-A"},
                    },
                    "status": {
                        "taskId": "abc13",
                        "state": "unscheduled",
                    },
                },
                {
                    "task": {
                        "extra": {
                            "treeherder": {"tier": 1},
                        },
                        "metadata": {
                            "name": "task-B",
                        },
                        "tags": {"name": "tag-A"},
                    },
                    "status": {
                        "taskId": "abc123",
                        "state": "unscheduled",
                    },
                },
            ]
        },
        status=200,
    )

    responses.add(
        responses.GET,
        "https://treeherder.mozilla.org/api/project/autoland/note/push_notes/?revision=abcdef&format=json",
        json={},
        status=200,
    )

    push = Push(rev, branch)
    tasks = push.tasks
    assert len(tasks) == 1


def test_push_tasks_with_cached_completed_tasks(monkeypatch, responses):
    rev = "abcdef"
    branch = "autoland"

    cached_tasks = [
        Task.create(id=1, label="test-task", result="passed", state="completed")
    ]
    monkeypatch.setattr(config.cache, "get", lambda x: cached_tasks)

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/integration/autoland/json-automationrelevance/{rev}",
        json={"changesets": [{"node": rev, "pushdate": [1638349140]}]},
        status=200,
    )

    responses.add(
        responses.GET,
        "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/gecko.v2.autoland.revision.abcdef.taskgraph.decision",
        json={"taskId": 1},
        status=200,
    )

    push = Push(rev, branch)
    tasks = push.tasks
    assert len(tasks) == 1


def test_finalized_push_tasks_with_cache(monkeypatch, responses):
    rev = "abcdef"
    branch = "autoland"

    cached_tasks = [Task.create(id=1, label="test-task", result="passed")]
    monkeypatch.setattr(config.cache, "get", lambda x: cached_tasks)
    monkeypatch.setattr(Push, "is_finalized", True)

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/integration/autoland/json-automationrelevance/{rev}",
        json={"changesets": [{"node": rev, "pushdate": [1638349140]}]},
        status=200,
    )

    push = Push(rev, branch)
    tasks = push.tasks
    assert len(tasks) == 1
    assert tasks == cached_tasks


def test_push_does_not_exist(responses):
    # We hit hgmo when 'rev' is less than 40 characters.
    rev = "foobar"
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(
            branch="integration/autoland", rev="foobar"
        ),
        json={"error": f"unknown revision '{rev}'"},
        status=404,
    )

    with pytest.raises(PushNotFound):
        Push(rev)

    # Otherwise we need to hit hgmo some other way.
    rev = "a" * 40
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(
            branch="integration/autoland", rev=rev
        ),
        json={"error": f"unknown revision '{rev}'"},
        status=404,
    )
    p = Push(rev)
    with pytest.raises(PushNotFound):
        p.id


def test_push_bugs(responses):
    rev = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/integration/autoland/json-automationrelevance/{rev}",
        json={
            "changesets": [
                {"bugs": [{"no": "1624503"}]},
                {"bugs": [{"no": "1624503"}]},
            ]
        },
        status=200,
    )

    p = Push(rev)
    assert p.bugs == {"1624503"}


def test_push_bugs_different(responses):
    rev = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/integration/autoland/json-automationrelevance/{rev}",
        json={
            "changesets": [
                {"bugs": [{"no": "1617050"}]},
                {"bugs": [{"no": "1625220"}]},
                {"bugs": [{"no": "1625220"}]},
                {"bugs": [{"no": "1625220"}]},
                {"bugs": [{"no": "1595768"}]},
                {"bugs": [{"no": "1595768"}]},
            ]
        },
        status=200,
    )

    p = Push(rev)
    assert p.bugs == {"1617050", "1625220", "1595768"}


def test_push_bugs_multiple(responses):
    rev = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/integration/autoland/json-automationrelevance/{rev}",
        json={
            "changesets": [
                {"bugs": [{"no": "1617050"}, {"no": "123"}]},
                {"bugs": [{"no": "1617050"}]},
                {"bugs": [{"no": "456"}]},
            ]
        },
        status=200,
    )

    p = Push(rev)
    assert p.bugs == {"123", "456", "1617050"}


def test_push_parent_on_autoland(responses):
    ctx = {
        "branch": "integration/autoland",
        "push_id_start": "121",
        "push_id_end": "122",
    }
    responses.add(
        responses.GET,
        HgRev.JSON_PUSHES_TEMPLATE.format(**ctx),
        json={
            "pushes": {
                "122": {
                    "changesets": [{"node": "b" * 40}],
                    "date": 1213174092,
                    "user": "user@example.org",
                },
            },
        },
        status=200,
    )

    p1 = Push("a" * 40)
    p1._id = 123
    parent = p1.parent

    assert parent.id == 122
    assert parent.rev == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_push_parent_on_try(responses, create_changesets):
    changesets = create_changesets(
        4,
        [
            {"phase": "public"},
            {"phase": "public"},
            {"phase": "draft"},
            {"phase": "draft"},
        ],
    )

    from pprint import pprint

    pprint(changesets, indent=2)
    head = changesets[-1]["node"]
    ctx = {"branch": "try", "rev": head}

    # We'll query the initial pushes' changesets first.
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(**ctx),
        json={"changesets": changesets},
        status=200,
    )

    # Should find changesets[1] as the parent and then start searching for it.
    parent_rev = changesets[1]["node"]

    # First we'll search mozilla-central, but won't find parent_rev.
    ctx["rev"] = parent_rev
    ctx["branch"] = "mozilla-central"
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(**ctx),
        json={"error": f"unknown revision '{parent_rev}'"},
        status=404,
    )

    # Next we'll search mozilla-beta, we'll find parent_rev but it's not a push head.
    ctx["branch"] = "mozilla-beta"
    changesets = create_changesets(4, {"phase": "public"})
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(**ctx),
        json={"changesets": changesets},
        status=200,
    )

    # Finally we'll search mozilla-release, we find it and it's the push head!
    ctx["branch"] = "mozilla-release"
    changesets = create_changesets(2, {"phase": "public"}, head=3)
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(**ctx),
        json={"changesets": changesets},
        status=200,
    )

    # Now run it and assert.
    push = Push(head, branch="try")
    parent = push.parent
    assert parent.rev == parent_rev
    assert parent.branch == "mozilla-release"


def test_push_parent_on_try_fails_with_merge_commit(responses, create_changesets):
    ctx = {
        "branch": "try",
        "rev": "a" * 40,
    }

    # Finding parent fails on merge commits.
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(**ctx),
        json={"changesets": create_changesets(1, {"parents": ["b" * 40, "c" * 40]})},
        status=200,
    )

    push = Push(ctx["rev"], ctx["branch"])
    with pytest.raises(ParentPushNotFound):
        push.parent


def test_push_parent_on_try_fails_when_not_a_push_head(responses, create_changesets):
    changesets = create_changesets(3)
    head = changesets[-1]["node"]
    ctx = {
        "branch": "try",
        "rev": head,
    }
    responses.add(
        responses.GET,
        HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(**ctx),
        json={"changesets": changesets},
        status=200,
    )

    # We raise if rev is not found or a push head anywhere.
    ctx["rev"] = changesets[0]["parents"][0]
    for branch in (
        "mozilla-central",
        "mozilla-beta",
        "mozilla-release",
        "integration/autoland",
    ):
        ctx["branch"] = branch
        responses.add(
            responses.GET,
            HgRev.AUTOMATION_RELEVANCE_TEMPLATE.format(**ctx),
            json={"changesets": changesets},
            status=200,
        )

    push = Push(head, branch="try")
    with pytest.raises(ParentPushNotFound):
        push.parent


def test_push_child_raises(responses):
    rev = "a" * 40

    # Try and mozilla-unified are not supported.
    for branch in ("try", "mozilla-unified"):
        push = Push(rev, branch=branch)
        with pytest.raises(ChildPushNotFound):
            push.child

    # A push with no children raises.
    push = Push(rev, branch="integration/autoland")
    push._id = 100
    url = HgRev.JSON_PUSHES_TEMPLATE.format(
        branch=push.branch,
        push_id_start=push.id,
        push_id_end=push.id + 1,
    )
    responses.add(
        responses.GET,
        url,
        json={"lastpushid": push.id, "pushes": {}},
        status=200,
    )

    with pytest.raises(ChildPushNotFound):
        push.child


def test_generate_all_shadow_scheduler_tasks(responses):
    rev = "a" * 40
    shadow_schedulers = (
        (
            "bar",
            ["task-1", "task-3", "task-4"],
            "task",  # suite name to use
        ),  # names will be generated alphabetically
        ("foo", ["task-2", "task-4"], "task"),
    )

    push = Push(rev)
    responses.add(
        responses.GET,
        get_index_url(push.index + ".taskgraph.decision"),
        json={"taskId": 1},
        status=200,
    )

    id = count(2)
    responses.add(
        responses.GET,
        get_artifact_url(1, "public/task-graph.json"),
        json={
            next(id): {"label": f"source-test-shadow-scheduler-{s[0]}"}
            for s in shadow_schedulers
        },
        status=200,
    )

    id = count(2)
    for ss in shadow_schedulers:
        s_id = next(id)
        responses.add(
            responses.GET,
            get_index_url(f"{push.index}.source.shadow-scheduler-{ss[0]}"),
            json={"taskId": s_id},
            status=200,
        )

        responses.add(
            responses.GET,
            get_artifact_url(s_id, "public/shadow-scheduler/optimized-tasks.json"),
            stream=True,
            json={
                next(id): {
                    "label": task,
                    "task": {
                        "extra": {
                            "suite": ss[2],
                            "test-settings": {"runtime": {}},
                            "treeherder-platform": "",
                        },
                    },
                }
                for task in ss[1]
            },
            status=200,
        )

    # retrieve the data
    for i, (name, tasks) in enumerate(push.generate_all_shadow_scheduler_tasks()):
        print(i, name, tasks)
        assert name == shadow_schedulers[i][0]
        assert tasks == set(shadow_schedulers[i][1])


def test_generate_all_shadow_scheduler_config_groups(responses):
    rev = "a" * 40
    shadow_schedulers = (
        (
            "bar",
            [
                (
                    "test-linux1804-64/debug-xpcshell-spi-nw-1",
                    ["group1", "group5"],
                    "xpcshell",
                    "linux1804-64/debug",
                ),
                (
                    "test-linux1804-64/debug-xpcshell-spi-nw-2",
                    ["group2"],
                    "xpcshell",
                    "linux1804-64/debug",
                ),
                (
                    "test-windows7-32/opt-xpcshell-spi-nw-1",
                    ["group3"],
                    "xpcshell",
                    "windows7-32/opt",
                ),
            ],
            {
                ("test-linux1804-64/debug-*-spi-nw", "group2"),
                ("test-linux1804-64/debug-*-spi-nw", "group5"),
                ("test-linux1804-64/debug-*-spi-nw", "group1"),
                ("test-windows7-32/opt-*-spi-nw", "group3"),
            },
        ),
        (
            "foo",
            [
                (
                    "test-macosx1014-64/opt-xpcshell-e10s-1",
                    ["group4"],
                    "xpcshell",
                    "macosx1014-64/opt",
                ),
                (
                    "test-android-em-7-0-x86_64/debug-geckoview-xpcshell-spi-nw-1",
                    ["group3"],
                    "xpcshell",
                    "android-em-7-0-x86_64/debug",
                ),
            ],
            {
                ("test-android-em-7.0-x86_64/debug-geckoview-*-spi-nw", "group3"),
                ("test-macosx1014-64/opt-*-spi-nw", "group4"),
            },
        ),
    )

    push = Push(rev)
    responses.add(
        responses.GET,
        get_index_url(push.index + ".taskgraph.decision"),
        json={"taskId": 1},
        status=200,
    )

    id = count(2)
    responses.add(
        responses.GET,
        get_artifact_url(1, "public/task-graph.json"),
        json={
            next(id): {
                "label": f"source-test-shadow-scheduler-{s[0]}",
                "task": {
                    "extra": {
                        "suite": "shadow-scheduler",
                    }
                },
                "suite": "shadow-scheduler",
            }
            for s in shadow_schedulers
        },
        status=200,
    )

    id = count(2)
    for ss in shadow_schedulers:
        s_id = next(id)
        responses.add(
            responses.GET,
            "https://hg.mozilla.org/mozilla-central/raw-file/tip/taskcluster/ci/test/variants.yml",
            json={
                "socketprocess_networking": {"suffix": "spi-nw"},
                "no-fission": {"suffix": "nofis"},
                "webrender-sw": {"suffix": "swr"},
                "1proc": {"suffix": "1proc"},
                "msix": {"suffix": "msix"},
                "headless": {"suffix": "headless"},
                "fission": {"suffix": "fis"},
            },
            status=200,
        )

        responses.add(
            responses.GET,
            get_index_url(f"{push.index}.source.shadow-scheduler-{ss[0]}"),
            json={"taskId": s_id},
            status=200,
        )

        responses.add(
            responses.GET,
            get_artifact_url(s_id, "public/shadow-scheduler/optimized-tasks.json"),
            stream=True,
            json={
                next(id): {
                    "label": label,
                    "task": {
                        "extra": {
                            "suite": suite,
                            "test-settings": {
                                "runtime": {"socketprocess_networking": True}
                            },
                            "treeherder-platform": platform,
                        }
                    },
                    "attributes": {"test_manifests": groups},
                }
                for label, groups, suite, platform in ss[1]
            },
            status=200,
        )

    # retrieve the data
    for i, (name, config_groups) in enumerate(
        push.generate_all_shadow_scheduler_config_groups()
    ):
        print(i, name, config_groups)
        assert name == shadow_schedulers[i][0]
        assert config_groups == shadow_schedulers[i][2]


def test_iterate_children(responses):
    rev = "a" * 40
    branch = "integration/autoland"
    push = Push(rev, branch)

    push_id = 10
    depth = 5

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/{branch}/json-automationrelevance/{rev}",
        json={
            "changesets": [
                {"pushid": push_id},
            ]
        },
        status=200,
    )

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/{branch}/json-pushes?version=2&full=1&startID={push_id}&endID={push_id+depth+1}",
        json={
            "pushes": {
                push_id
                + i: {
                    "changesets": [
                        {
                            "node": chr(ord("a") + i) * 40,
                            "desc": "A nice description about Bug 1234567",
                        }
                    ],
                    "date": 1,
                }
                for i in range(1, depth + 2)
            }
        },
        status=200,
    )

    for other in push._iterate_children(depth):
        assert other.id == push_id
        push_id += 1


def test_iterate_parents(responses):
    rev = "a" * 40
    branch = "integration/autoland"
    push = Push(rev, branch)

    push_id = 10
    depth = 5

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/{branch}/json-automationrelevance/{rev}",
        json={
            "changesets": [
                {"pushid": push_id},
            ]
        },
        status=200,
    )

    responses.add(
        responses.GET,
        f"https://hg.mozilla.org/{branch}/json-pushes?version=2&full=1&startID={push_id-2-depth}&endID={push_id-1}",
        json={
            "pushes": {
                push_id
                - i: {
                    "changesets": [
                        {
                            "node": chr(ord("a") + i) * 40,
                            "desc": "A nice description about Bug 1234567",
                        }
                    ],
                    "date": 1,
                }
                for i in range(1, depth + 2)
            }
        },
        status=200,
    )

    for other in push._iterate_parents(depth):
        assert other.id == push_id
        push_id -= 1


def test_get_test_selection_data_from_cache(responses):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)

    task_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/index/v1/task/gecko.v2.{branch}.revision.{rev}.taskgraph.decision"
    responses.add(responses.GET, task_url, status=200, json={"taskId": "a" * 10})

    cache_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/queue/v1/task/aaaaaaaaaa/artifacts/public/bugbug-push-schedules.json"
    responses.add(responses.GET, cache_url, status=200, json=SCHEDULES_EXTRACT)

    data = push.get_test_selection_data()
    assert data == SCHEDULES_EXTRACT

    assert len(responses.calls) == 2
    assert [(call.request.method, call.request.url) for call in responses.calls] == [
        ("GET", task_url),
        ("GET", cache_url),
    ]


def test_get_test_selection_data_from_bugbug_handle_errors(responses, monkeypatch):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)

    task_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/index/v1/task/gecko.v2.{branch}.revision.{rev}.taskgraph.decision"
    responses.add(responses.GET, task_url, status=200, json={"taskId": "a" * 10})

    cache_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/queue/v1/task/aaaaaaaaaa/artifacts/public/bugbug-push-schedules.json"
    responses.add(responses.GET, cache_url, status=404)

    url = f"{bugbug.BUGBUG_BASE_URL}/push/{branch}/{rev}/schedules"
    responses.add(responses.GET, url, status=500)

    monkeypatch.setattr(bugbug, "DEFAULT_RETRY_TIMEOUT", 3)
    monkeypatch.setattr(bugbug, "DEFAULT_RETRY_INTERVAL", 1)
    with pytest.raises(SourcesNotFound) as e:
        push.get_test_selection_data()
    assert (
        e.value.msg
        == "No registered sources were able to fulfill 'push_test_selection_data'!"
    )

    assert len(responses.calls) == 5
    assert [(call.request.method, call.request.url) for call in responses.calls] == [
        ("GET", task_url),
        ("GET", cache_url),
        # We retry 3 times the call to the Bugbug HTTP service
        ("GET", url),
        ("GET", url),
        ("GET", url),
    ]


def test_get_test_selection_data_from_bugbug_handle_exceeded_timeout(
    responses, monkeypatch
):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)

    task_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/index/v1/task/gecko.v2.{branch}.revision.{rev}.taskgraph.decision"
    responses.add(responses.GET, task_url, status=200, json={"taskId": "a" * 10})

    cache_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/queue/v1/task/aaaaaaaaaa/artifacts/public/bugbug-push-schedules.json"
    responses.add(responses.GET, cache_url, status=404)

    url = f"{bugbug.BUGBUG_BASE_URL}/push/{branch}/{rev}/schedules"
    responses.add(responses.GET, url, status=202)

    monkeypatch.setattr(bugbug, "DEFAULT_RETRY_TIMEOUT", 3)
    monkeypatch.setattr(bugbug, "DEFAULT_RETRY_INTERVAL", 1)
    with pytest.raises(bugbug.BugbugTimeoutException) as e:
        push.get_test_selection_data()
    assert str(e.value) == "Timed out waiting for result from Bugbug HTTP Service"

    assert len(responses.calls) == 5
    assert [(call.request.method, call.request.url) for call in responses.calls] == [
        ("GET", task_url),
        ("GET", cache_url),
        # We retry 3 times the call to the Bugbug HTTP service
        ("GET", url),
        ("GET", url),
        ("GET", url),
    ]


def test_get_test_selection_data_from_bugbug(responses):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)

    task_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/index/v1/task/gecko.v2.{branch}.revision.{rev}.taskgraph.decision"
    responses.add(responses.GET, task_url, status=200, json={"taskId": "a" * 10})

    cache_url = f"{PRODUCTION_TASKCLUSTER_ROOT_URL}/api/queue/v1/task/aaaaaaaaaa/artifacts/public/bugbug-push-schedules.json"
    responses.add(responses.GET, cache_url, status=404)

    url = f"{bugbug.BUGBUG_BASE_URL}/push/{branch}/{rev}/schedules"
    responses.add(responses.GET, url, status=200, json=SCHEDULES_EXTRACT)

    data = push.get_test_selection_data()
    assert data == SCHEDULES_EXTRACT

    assert len(responses.calls) == 3
    assert [(call.request.method, call.request.url) for call in responses.calls] == [
        ("GET", task_url),
        ("GET", cache_url),
        ("GET", url),
    ]


@pytest.mark.parametrize(
    "classify_regressions_return_value, expected_result",
    [
        (Regressions(real={"group1": []}, intermittent={}, unknown={}), PushStatus.BAD),
        (
            Regressions(real={"group1": []}, intermittent={"group2": []}, unknown={}),
            PushStatus.BAD,
        ),
        (
            Regressions(real={"group1": []}, intermittent={}, unknown={"group2": []}),
            PushStatus.BAD,
        ),
        (Regressions(real={}, intermittent={}, unknown={}), PushStatus.GOOD),
        (
            Regressions(real={}, intermittent={"group1": []}, unknown={}),
            PushStatus.GOOD,
        ),
        (
            Regressions(real={}, intermittent={"group1": [], "group2": []}, unknown={}),
            PushStatus.GOOD,
        ),
        (
            Regressions(real={}, intermittent={}, unknown={"group1": []}),
            PushStatus.UNKNOWN,
        ),
        (
            Regressions(real={}, intermittent={"group1": []}, unknown={"group2": []}),
            PushStatus.UNKNOWN,
        ),
    ],
)
def test_classify(monkeypatch, classify_regressions_return_value, expected_result):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)

    def mock_return(self, *args, **kwargs):
        return classify_regressions_return_value, ToRetriggerOrBackfill(
            real_retrigger={},
            intermittent_retrigger={},
            backfill={},
        )

    monkeypatch.setattr(Push, "classify_regressions", mock_return)
    assert push.classify()[0] == expected_result


def generate_mocks(
    monkeypatch,
    push,
    get_test_selection_data_value,
    get_likely_regressions_value,
    get_possible_regressions_value,
    confirmed_failure_value,
    cross_config_values,
    classifications,
):
    monkeypatch.setattr(config.cache, "get", lambda x: None)
    monkeypatch.setattr(Push, "is_group_running", lambda *args: False)

    def mock_return_get_test_selection_data(*args, **kwargs):
        return get_test_selection_data_value

    monkeypatch.setattr(
        Push, "get_test_selection_data", mock_return_get_test_selection_data
    )

    def mock_return_get_likely_regressions(*args, **kwargs):
        return get_likely_regressions_value

    monkeypatch.setattr(
        Push, "get_likely_regressions", mock_return_get_likely_regressions
    )

    def mock_return_get_possible_regressions(*args, **kwargs):
        return get_possible_regressions_value

    monkeypatch.setattr(
        Push, "get_possible_regressions", mock_return_get_possible_regressions
    )

    push.group_summaries = {}
    for name in classifications.keys():
        push.group_summaries[name] = GROUP_SUMMARIES_DEFAULT[name]

    def mock_return_is_confirmed_failure(*args, **kwargs):
        return confirmed_failure_value

    for name, group in push.group_summaries.items():
        monkeypatch.setattr(
            group,
            "is_cross_config_failure",
            lambda x, name=name: cross_config_values[name],
        )
        monkeypatch.setattr(
            group,
            "is_config_consistent_failure",
            lambda x, name=name: cross_config_values[name],
        )
        monkeypatch.setattr(
            group,
            "classifications",
            classifications[name],
        )
        monkeypatch.setattr(
            group,
            "is_confirmed_failure",
            mock_return_is_confirmed_failure,
        )


@pytest.mark.parametrize(
    "test_selection_data, are_cross_config, classifications, to_retrigger",
    [
        (
            {"groups": {"group1": 0.7, "group2": 0.3}},
            {
                "group1": True,
                "group2": True,
                "group3": True,
                "group4": True,
                "group5": True,
            },
            {
                "group1": ["not classified"],
                "group2": ["not classified"],
                "group3": ["not classified"],
                "group4": ["not classified"],
                "group5": ["not classified"],
            },
            {},
        ),  # There are only cross-config failures with low confidence
        (
            {
                "groups": {
                    "group1": 0.85,
                    "group2": 0.85,
                    "group3": 0.85,
                    "group4": 0.85,
                    "group5": 0.85,
                }
            },
            {
                "group1": False,
                "group2": False,
                "group3": False,
                "group4": False,
                "group5": False,
            },
            {
                "group1": ["new failure not classified"],
                "group2": ["new failure not classified"],
                "group3": ["new failure not classified"],
                "group4": ["new failure not classified"],
                "group5": ["new failure not classified"],
            },
            {},
        ),  # There are only non cross-config failures with medium confidence
        (
            {
                "groups": {
                    "group1": 0.7,
                    "group2": 0.85,
                    "group3": 0.3,
                    "group4": 0.85,
                    "group5": 0.3,
                }
            },
            {
                "group1": None,
                "group2": True,
                "group3": None,
                "group4": True,
                "group5": None,
            },
            {
                "group1": ["not classified"],
                "group2": ["not classified"],
                "group3": ["not classified"],
                "group4": ["not classified"],
                "group5": ["not classified"],
            },
            {"intermittent_retrigger": {"group1", "group3", "group5"}},
        ),  # There are some failures, unknown if cross-config, and some low confidence groups but they don't match
    ],
)
def test_classify_almost_good_push(
    monkeypatch, test_selection_data, are_cross_config, classifications, to_retrigger
):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)
    generate_mocks(
        monkeypatch,
        push,
        test_selection_data,
        set(),
        set(),
        None,
        are_cross_config,
        classifications,
    )

    result = push.classify(
        unknown_from_regressions=False,
        consistent_failures_counts=None,
        consider_children_pushes_configs=False,
    )

    assert result[0] == PushStatus.UNKNOWN

    assert set(result[1].real) == set()
    assert set(result[1].intermittent) == set()
    assert set(result[1].unknown) == {"group1", "group2", "group3", "group4", "group5"}

    assert set(result[2].real_retrigger) == (
        to_retrigger["real_retrigger"] if "real_retrigger" in to_retrigger else set()
    )
    assert set(result[2].intermittent_retrigger) == (
        to_retrigger["intermittent_retrigger"]
        if "intermittent_retrigger" in to_retrigger
        else set()
    )
    assert set(result[2].backfill) == (
        to_retrigger["backfill"] if "backfill" in to_retrigger else set()
    )


def test_classify_good_push_only_intermittent_failures(monkeypatch):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)

    test_selection_data = {"groups": {"group1": 0.7, "group2": 0.3}}
    likely_regressions = {"group3", "group4"}
    are_cross_config = {name: False for name in GROUP_SUMMARIES_DEFAULT.keys()}
    classifications = {
        "group1": ["not classified"],
        "group2": ["not classified"],
        "group3": ["not classified"],
        "group4": ["not classified"],
        "group5": ["not classified"],
    }
    generate_mocks(
        monkeypatch,
        push,
        test_selection_data,
        likely_regressions,
        set(),
        None,
        are_cross_config,
        classifications,
    )

    result = push.classify(consider_children_pushes_configs=False)

    assert result[0] == PushStatus.GOOD

    assert set(result[1].real) == set()
    assert set(result[1].intermittent) == {
        "group1",
        "group2",
        "group3",
        "group4",
        "group5",
    }
    assert set(result[1].unknown) == set()

    assert set(result[2].real_retrigger) == set()
    assert set(result[2].intermittent_retrigger) == set()
    assert set(result[2].backfill) == set()


@pytest.mark.parametrize(
    "test_selection_data, likely_regressions, are_cross_config, classifications, to_retrigger",
    [
        (
            {"groups": {}},
            {"group1", "group2", "group3", "group4", "group5"},
            {
                "group1": True,
                "group2": True,
                "group3": True,
                "group4": True,
                "group5": True,
            },
            {
                "group1": ["not classified"],
                "group2": ["not classified"],
                "group3": ["not classified"],
                "group4": ["not classified"],
                "group5": ["not classified"],
            },
            {},
        ),  # There are only cross-config failures likely to regress
        # but they weren't selected by bugbug (no confidence)
        (
            {"groups": {}},
            {"group1", "group2", "group3", "group4", "group5"},
            {
                "group1": None,
                "group2": None,
                "group3": None,
                "group4": None,
                "group5": None,
            },
            {
                "group1": ["not classified"],
                "group2": ["not classified"],
                "group3": ["not classified"],
                "group4": ["not classified"],
                "group5": ["not classified"],
            },
            {
                "intermittent_retrigger": {
                    "group1",
                    "group2",
                    "group3",
                    "group4",
                    "group5",
                }
            },
        ),  # There are only failures likely to regress
        # but they weren't selected by bugbug (no confidence)
        # and it is unclear if they are cross-config
        (
            {
                "groups": {
                    "group1": 0.92,
                    "group2": 0.92,
                    "group3": 0.92,
                    "group4": 0.92,
                    "group5": 0.92,
                }
            },
            set(),
            {
                "group1": True,
                "group2": True,
                "group3": True,
                "group4": True,
                "group5": True,
            },
            {
                "group1": ["not classified"],
                "group2": ["not classified"],
                "group3": ["not classified"],
                "group4": ["not classified"],
                "group5": ["not classified"],
            },
            {},
        ),  # There are only cross-config failures that were selected
        # with high confidence by bugbug but weren't likely to regress
        (
            {
                "groups": {
                    "group1": 0.92,
                    "group2": 0.92,
                    "group3": 0.92,
                    "group4": 0.92,
                    "group5": 0.92,
                }
            },
            {"group1", "group2", "group3", "group4", "group5"},
            {
                "group1": False,
                "group2": False,
                "group3": False,
                "group4": False,
                "group5": False,
            },
            {
                "group1": ["new failure not classified"],
                "group2": ["new failure not classified"],
                "group3": ["new failure not classified"],
                "group4": ["new failure not classified"],
                "group5": ["new failure not classified"],
            },
            {},
        ),  # There are only groups that were selected with high confidence by
        # bugbug and also likely to regress but they aren't cross-config failures
        (
            {
                "groups": {
                    "group1": 0.92,
                    "group2": 0.92,
                    "group3": 0.92,
                    "group4": 0.92,
                    "group5": 0.92,
                }
            },
            {"group1", "group2", "group3", "group4", "group5"},
            {
                "group1": None,
                "group2": None,
                "group3": None,
                "group4": None,
                "group5": None,
            },
            {
                "group1": ["new failure not classified"],
                "group2": ["new failure not classified"],
                "group3": ["new failure not classified"],
                "group4": ["new failure not classified"],
                "group5": ["new failure not classified"],
            },
            {"real_retrigger": {"group1", "group2", "group3", "group4", "group5"}},
        ),  # There are only groups that were selected with high confidence by
        # bugbug and also likely to regress but it isn't clear yet if they are cross-config failures
    ],
)
def test_classify_almost_bad_push(
    monkeypatch,
    test_selection_data,
    likely_regressions,
    are_cross_config,
    classifications,
    to_retrigger,
):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)
    generate_mocks(
        monkeypatch,
        push,
        test_selection_data,
        likely_regressions,
        set(),
        None,
        are_cross_config,
        classifications,
    )

    result = push.classify(
        unknown_from_regressions=False,
        consistent_failures_counts=None,
        consider_children_pushes_configs=False,
    )

    assert result[0] == PushStatus.UNKNOWN

    assert set(result[1].real) == set()
    assert set(result[1].intermittent) == set()
    assert set(result[1].unknown) == {"group1", "group2", "group3", "group4", "group5"}

    assert set(result[2].real_retrigger) == (
        to_retrigger["real_retrigger"] if "real_retrigger" in to_retrigger else set()
    )
    assert set(result[2].intermittent_retrigger) == (
        to_retrigger["intermittent_retrigger"]
        if "intermittent_retrigger" in to_retrigger
        else set()
    )
    assert set(result[2].backfill) == (
        to_retrigger["backfill"] if "backfill" in to_retrigger else set()
    )


def test_classify_bad_push_some_real_failures(monkeypatch):
    rev = "a" * 40
    branch = "autoland"
    push = Push(rev, branch)

    test_selection_data = {"groups": {"group1": 0.99, "group2": 0.95, "group3": 0.91}}
    likely_regressions = {"group1", "group2", "group3"}
    are_cross_config = {
        "group1": True,
        "group2": False,
        "group3": True,
        "group4": False,
        "group5": True,
    }
    classifications = {
        "group1": ["not classified"],
        "group2": ["new failure not classified"],
        "group3": ["not classified"],
        "group4": ["not classified"],
        "group5": ["not classified"],
    }
    generate_mocks(
        monkeypatch,
        push,
        test_selection_data,
        likely_regressions,
        set(),
        None,
        are_cross_config,
        classifications,
    )

    result = push.classify(
        unknown_from_regressions=False, consider_children_pushes_configs=False
    )

    assert result[0] == PushStatus.BAD

    # group1 & group3 were both selected by bugbug with high confidence, likely to regress
    # and are cross config failures
    assert set(result[1].real) == {"group1", "group3"}
    # group4 isn't a cross config failure and was not selected by bugbug (no confidence)
    assert set(result[1].intermittent) == {"group4"}
    # group2 isn't a cross config failure but was selected with high confidence by bugbug
    # group5 is a cross config failure but was not selected by bugbug nor likely to regress
    assert set(result[1].unknown) == {"group2", "group5"}

    assert set(result[2].real_retrigger) == set()
    assert set(result[2].intermittent_retrigger) == set()
    assert set(result[2].backfill) == set()


@pytest.mark.parametrize(
    "test_selection_confidence, is_likely_regression, is_possible_regression, is_confirmed_failure, is_cross_config, classification, status, action",
    [
        # high confidence, likely regression, consistent, "new" -> BAD
        # (0.99, True, True, True, "new", PushStatus.BAD, None),
        # high confidence, likely regression, consistent, "not new" -> BAD
        (0.99, True, True, None, True, "not new", PushStatus.BAD, None),
        # no confidence, likely regression, consistent, "new" -> BAD
        (None, True, True, None, True, "new", PushStatus.BAD, None),
        # low confidence, likely regression, consistent, "new" -> BAD
        (0.01, True, True, None, True, "new", PushStatus.BAD, None),
        # high confidence, not likely nor possible regression, consistent, "new" -> UNKNOWN
        (0.99, False, False, None, True, "new", PushStatus.UNKNOWN, None),
        # high confidence, not likely nor possible regression, consistent, "not new" -> UNKNOWN
        (0.99, False, False, None, True, "not new", PushStatus.UNKNOWN, None),
        # high confidence, not likely nor possible regression, not consistent, "new" -> UNKNOWN
        (0.99, False, False, None, False, "new", PushStatus.UNKNOWN, None),
        # low confidence, not likely nor possible regression, consistent, "not new" -> UNKNOWN
        (0.01, False, False, None, True, "not new", PushStatus.UNKNOWN, None),
        # low confidence, not likely nor possible regression, consistent, "new" -> UNKNOWN
        (0.01, False, False, None, True, "new", PushStatus.UNKNOWN, None),
        # no confidence, not likely nor possible regression, consistent, "not new" -> UNKNOWN
        (None, False, False, None, True, "not new", PushStatus.UNKNOWN, None),
        # no confidence, not likely nor possible regression, consistent, "new" -> UNKNOWN
        (None, False, False, None, True, "new", PushStatus.UNKNOWN, None),
        # high confidence, likely regression, consistent, "new" -> UNKNOWN
        (0.99, True, True, None, False, "new", PushStatus.UNKNOWN, None),
        # low confidence, likely regression, consistent, "not new" -> UNKNOWN
        (0.01, True, True, None, True, "not new", PushStatus.UNKNOWN, None),
        # no confidence, likely regression, consistent, "not new" -> UNKNOWN
        (None, True, True, None, True, "not new", PushStatus.UNKNOWN, None),
        # no confidence, not likely nor possible regression, not consistent, "not new" -> GOOD
        (None, False, False, None, False, "not new", PushStatus.GOOD, None),
        # no confidence, likely regression, not consistent, "new" -> GOOD
        (None, True, True, None, False, "new", PushStatus.GOOD, None),
        # low confidence, likely regression, not consistent, "not new" -> GOOD
        (0.01, True, True, None, False, "not new", PushStatus.GOOD, None),
        # no confidence, likely regression, not consistent, "not new" -> GOOD
        (None, True, True, None, False, "not new", PushStatus.GOOD, None),
        # high confidence, not likely nor possible regression, not consistent, "not new" -> GOOD
        (0.99, False, False, None, False, "not new", PushStatus.GOOD, None),
        # high confidence, likely regression, not consistent, "not new" -> GOOD
        (0.99, True, True, None, False, "not new", PushStatus.GOOD, None),
        # low confidence, likely regression, not consistent, "new" -> GOOD
        (0.01, True, True, None, False, "new", PushStatus.GOOD, None),
        # low confidence, not likely nor possible regression, not consistent, "not new" -> GOOD
        (0.01, False, False, None, False, "not new", PushStatus.GOOD, None),
        # low confidence, not likely nor possible regression, not consistent, "new" -> GOOD
        (0.01, False, False, None, False, "new", PushStatus.GOOD, None),
        # no confidence, not likely nor possible regression, not consistent, "new" -> GOOD
        (None, False, False, None, False, "new", PushStatus.GOOD, None),
        # high confidence, likely regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if real
        (
            0.99,
            True,
            True,
            None,
            None,
            "not new",
            PushStatus.UNKNOWN,
            "real|intermittent",
        ),
        # high confidence, likely regression, unknown consistency, "new" -> UNKNOWN, retrigger to find if real
        (0.99, True, True, None, None, "new", PushStatus.UNKNOWN, "real"),
        # high confidence, not likely nor possible regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if intermittent
        (0.99, False, False, None, None, "not new", PushStatus.UNKNOWN, "intermittent"),
        # high confidence, not likely nor possible regression, unknown consistency, "new" -> UNKNOWN, retrigger won't help
        (0.99, False, False, None, None, "new", PushStatus.UNKNOWN, None),
        # low confidence, likely regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if intermittent
        (0.01, True, True, None, None, "not new", PushStatus.UNKNOWN, "intermittent"),
        # low confidence, likely regression, unknown consistency, "new" -> UNKNOWN, retrigger to find if real or intermittent
        (0.01, True, True, None, None, "new", PushStatus.UNKNOWN, "real|intermittent"),
        # low confidence, not likely nor possible regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if intermittent
        (0.01, False, False, None, None, "not new", PushStatus.UNKNOWN, "intermittent"),
        # low confidence, not likely nor possible regression, unknown consistency, "new" -> UNKNOWN, retrigger to find if intermittent
        (0.01, False, False, None, None, "new", PushStatus.UNKNOWN, "intermittent"),
        # no confidence, likely regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if intermittent
        (None, True, True, None, None, "not new", PushStatus.UNKNOWN, "intermittent"),
        # no confidence, likely regression, unknown consistency, "new" -> UNKNOWN, retrigger to find if intermittent
        (None, True, True, None, None, "new", PushStatus.UNKNOWN, "real|intermittent"),
        # no confidence, not likely nor possible regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if real or intermittent
        (None, False, False, None, None, "not new", PushStatus.UNKNOWN, "intermittent"),
        # no confidence, not likely nor possible regression, unknown consistency, "new" -> UNKNOWN, retrigger to find if intermittent
        (None, False, False, None, None, "new", PushStatus.UNKNOWN, "intermittent"),
        # high confidence, possible regression, consistent, "new" -> UNKNOWN, backfill to find if regression
        (0.99, False, True, None, True, "new", PushStatus.UNKNOWN, "backfill"),
        # high confidence, possible regression, consistent, "not new" -> UNKNOWN, backfill to find if regression
        (0.99, False, True, None, True, "not new", PushStatus.UNKNOWN, "backfill"),
        # high confidence, possible regression, not consistent, "new" -> UNKNOWN, nothing would change if we backfilled or retriggered
        (0.99, False, True, None, False, "new", PushStatus.UNKNOWN, None),
        # low confidence, possible regression, consistent, "not new" -> UNKNOWN, nothing would change if we backfilled
        (0.01, False, True, None, True, "not new", PushStatus.UNKNOWN, None),
        # low confidence, possible regression, consistent, "new" -> UNKNOWN, backfill to find if regression
        (0.01, False, True, None, True, "new", PushStatus.UNKNOWN, "backfill"),
        # no confidence, possible regression, consistent, "not new" -> UNKNOWN, nothing would change if we backfilled
        (None, False, True, None, True, "not new", PushStatus.UNKNOWN, None),
        # no confidence, possible regression, consistent, "new" -> UNKNOWN, backfill to find if regression
        (None, False, True, None, True, "new", PushStatus.UNKNOWN, "backfill"),
        # no confidence, possible regression, not consistent, "not new" -> GOOD
        (None, False, True, None, False, "not new", PushStatus.GOOD, None),
        # high confidence, possible regression, not consistent, "not new" -> GOOD
        (0.99, False, True, None, False, "not new", PushStatus.GOOD, None),
        # low confidence, possible regression, not consistent, "not new" -> GOOD
        (0.01, False, True, None, False, "not new", PushStatus.GOOD, None),
        # low confidence, possible regression, not consistent, "new" -> GOOD
        (0.01, False, True, None, False, "new", PushStatus.GOOD, None),
        # no confidence, possible regression, not consistent, "new" -> GOOD
        (None, False, True, None, False, "new", PushStatus.GOOD, None),
        # high confidence, possible regression, unknown consistency, "not new" -> UNKNOWN, backfill to find if regression and retrigger to find if intermittent
        (
            0.99,
            False,
            True,
            None,
            None,
            "not new",
            PushStatus.UNKNOWN,
            "backfill|intermittent",
        ),
        # high confidence, possible regression, unknown consistency, "new" -> UNKNOWN, backfill to find if regression
        (0.99, False, True, None, None, "new", PushStatus.UNKNOWN, "backfill"),
        # low confidence, possible regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if intermittent
        (0.01, False, True, None, None, "not new", PushStatus.UNKNOWN, "intermittent"),
        # low confidence, possible regression, unknown consistency, "new" -> UNKNOWN, backfill to find if regression and retrigger to find if intermittent
        (
            0.01,
            False,
            True,
            None,
            None,
            "new",
            PushStatus.UNKNOWN,
            "backfill|intermittent",
        ),
        # no confidence, possible regression, unknown consistency, "not new" -> UNKNOWN, retrigger to find if intermittent
        (None, False, True, None, None, "not new", PushStatus.UNKNOWN, "intermittent"),
        # no confidence, possible regression, unknown consistency, "new" -> UNKNOWN, backfill to find if regression and retrigger to find if intermittent
        (
            None,
            False,
            True,
            None,
            None,
            "new",
            PushStatus.UNKNOWN,
            "backfill|intermittent",
        ),
        # no confidence, possible regression, confirm failure is false, unknown consistency, "not new" -> GOOD; intermittent
        (None, False, True, False, None, "not new", PushStatus.GOOD, "intermittent"),
        # no confidence, possible regression, confirm failure is true, unknown consistency, "new" -> BAD; real
        (None, True, False, True, None, "new", PushStatus.BAD, "real"),
    ],
)
def test_classify_cases(
    monkeypatch,
    test_selection_confidence,
    is_likely_regression,
    is_possible_regression,
    is_confirmed_failure,
    is_cross_config,
    classification,
    status,
    action,
):
    push = Push("a" * 40, "autoland")

    generate_mocks(
        monkeypatch,
        push,
        {
            "groups": {"group1": test_selection_confidence}
            if test_selection_confidence
            else {}
        },
        {"group1"} if is_likely_regression else set(),
        {"group1"} if is_possible_regression else set(),
        is_confirmed_failure,
        {"group1": is_cross_config},
        {
            "group1": [
                "new failure not classified"
                if classification == "new"
                else "not classified"
            ]
        },
    )

    result = push.classify(
        unknown_from_regressions=False, consider_children_pushes_configs=False
    )

    assert result[0] == status
    if status == PushStatus.BAD:
        assert set(result[1].real) == {"group1"}
        assert set(result[1].intermittent) == set()
        assert set(result[1].unknown) == set()
    elif status == PushStatus.GOOD:
        assert set(result[1].real) == set()
        assert set(result[1].intermittent) == {"group1"}
        assert set(result[1].unknown) == set()
    elif status == PushStatus.UNKNOWN:
        assert set(result[1].real) == set()
        assert set(result[1].intermittent) == set()
        assert set(result[1].unknown) == {"group1"}

    if action is None:
        assert set(result[2].real_retrigger) == set()
        assert set(result[2].intermittent_retrigger) == set()
        assert set(result[2].backfill) == set()
    else:
        real_retrigger = (
            {"group1"} if "real" in action and is_confirmed_failure is None else set()
        )
        intermittent_retrigger = (
            {"group1"}
            if "intermittent" in action and is_confirmed_failure is None
            else set()
        )
        backfill = (
            {"group1"}
            if "backfill" in action and is_confirmed_failure is None
            else set()
        )

        assert set(result[2].real_retrigger) == real_retrigger
        assert set(result[2].intermittent_retrigger) == intermittent_retrigger
        assert set(result[2].backfill) == backfill
