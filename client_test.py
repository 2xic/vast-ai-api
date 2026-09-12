# /// script
# requires-python = ">=3.10"
# ///
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import urllib.parse

os.environ.setdefault("VAST_API_KEY", "test")


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m


class _Sess:
    def mount(self, *a, **k):
        pass


_stub("requests", Session=_Sess)
_stub("requests.adapters", HTTPAdapter=lambda *a, **k: None)
_stub("urllib3.util", Retry=lambda *a, **k: None)
_stub("dotenv", load_dotenv=lambda *a, **k: None)

import vast_cli.__main__ as main_mod
from vast_cli import api
from vast_cli.api import client, remote, run

CASES = []


@contextlib.contextmanager
def _fake_clock():
    real_sleep, real_time = client.time.sleep, client.time.time
    now = [0.0]
    client.time.sleep = lambda s: now.__setitem__(0, now[0] + s)
    client.time.time = lambda: now[0]
    try:
        yield now
    finally:
        client.time.sleep, client.time.time = real_sleep, real_time


@contextlib.contextmanager
def _instant_retries():
    real = remote.time.sleep
    remote.time.sleep = lambda s: None
    try:
        yield
    finally:
        remote.time.sleep = real


@contextlib.contextmanager
def _patched(mod, **attrs):
    saved = {k: getattr(mod, k) for k in attrs}
    for k, v in attrs.items():
        setattr(mod, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)


def case(fn):
    CASES.append(fn)
    return fn


def _raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        return str(e)
    raise AssertionError(f"{fn.__name__} must raise {exc.__name__}")


_FILTER_4090 = client.AvailableInstancesFilter(gpu_name="RTX 4090")


def _raw_offer(n):
    return {
        "id": n,
        "machine_id": n,
        "gpu_name": "RTX 4090",
        "num_gpus": 1,
        "dph_total": 0.4,
        "score": 1,
        "disk_space": 50,
        "gpu_ram": 24,
        "verified": True,
    }


def _market(rows, cap=64):
    asked = []

    def fake(method, url):
        if "gpu_names" in url:
            return {"success": True, "gpu_names": sorted({r["gpu_name"] for r in rows})}
        q = json.loads(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0])
        asked.append(q)
        assert "offset" not in q, "the bundles API rejects offset outright"
        hits = [r for r in rows if r["gpu_name"] == q["gpu_name"]["eq"]]
        if q.get("verified"):
            hits = [r for r in hits if r["verified"]]
        hits.sort(key=lambda r: r["dph_total"], reverse=q["order"][0][1] == "desc")
        return {"offers": hits[: min(q["limit"], cap)]}

    return fake, asked


@case
def a_search_never_asks_for_more_than_the_server_returns():
    fake, asked = _market([_raw_offer(1)])
    with _patched(client, _request=fake):
        list(client.get_available_instances(_FILTER_4090, limit=5000))
    assert asked[0]["limit"] == 64, f"the server caps a page at 64: {asked[0]['limit']}"


@case
def every_gpu_model_is_priced_by_its_own_query():
    rows = [_raw_offer(1), {**_raw_offer(2), "gpu_name": "GTX 1080", "dph_total": 0.02}]
    fake, asked = _market(rows)
    with _patched(client, _request=fake):
        got = run.list_gpus()
    assert got == [("GTX 1080", 0.02), ("RTX 4090", 0.4)], f"one row per model: {got}"
    assert len(asked) == 2, f"one query per model, not one for the market: {len(asked)}"


@case
def the_cheapest_offer_of_a_model_is_the_one_reported():
    rows = [_raw_offer(1), {**_raw_offer(2), "dph_total": 0.19}]
    fake, asked = _market(rows)
    with _patched(client, _request=fake):
        got = run.list_gpus()
    assert got == [("RTX 4090", 0.19)], f"the server must sort by price for us: {got}"
    assert asked[0]["order"] == [["dph_total", "asc"]], f"wrong order: {asked[0]}"
    assert asked[0]["limit"] == 1, f"only the cheapest row is needed: {asked[0]}"


@case
def a_model_with_no_verified_host_is_hidden_until_you_ask():
    rows = [{**_raw_offer(1), "gpu_name": "GTX 1080", "verified": False}]
    fake, _ = _market(rows)
    with _patched(client, _request=fake):
        assert run.list_gpus() == [], "an unverified-only model must not be listed"
        got = run.list_gpus(verified=False)
    assert got == [("GTX 1080", 0.4)], f"--any-host must surface it: {got}"


@case
def launch_still_takes_the_highest_scoring_offer_first():
    fake, asked = _market([_raw_offer(1)])
    with _patched(client, _request=fake):
        client.pick_offer(_FILTER_4090)
    assert asked[0]["order"] == [["score", "desc"]], f"launch order changed: {asked[0]}"


@case
def malformed_instances_are_skipped_not_fatal():
    body = {
        "instances": [
            {
                "id": 1,
                "ssh_host": "h",
                "ssh_port": 22,
                "public_ipaddr": "1.2.3.4",
                "actual_status": "running",
                "label": "vrun:a",
                "start_date": 5,
                "ports": {},
            },
            {"id": 2, "label": "vrun:b"},
            "not-a-dict",
            {"label": "vrun:c"},
            {"id": 3, "ports": {"22/tcp": "garbage"}, "label": "vrun:d"},
        ]
    }
    with _patched(client, _request=lambda *a, **k: body):
        rows = list(client.get_running_instances())
    ids = sorted(r["id"] for r in rows)
    assert ids == [1, 2, 3], f"expected well-formed ids kept, got {ids}"
    row2 = next(r for r in rows if r["id"] == 2)
    assert row2["ssh_host"] is None, "missing ssh fields should be None, not crash"
    row3 = next(r for r in rows if r["id"] == 3)
    assert row3["open_ports"] == [], f"malformed ports leaked: {row3['open_ports']}"
    assert [r["ssh_direct"] for r in rows] == [None] * 3, "no row exposes 22/tcp here"


@case
def missing_instances_key_still_raises():
    with _patched(client, _request=lambda *a, **k: {"success": False, "msg": "boom"}):
        try:
            list(client.get_running_instances())
        except RuntimeError:
            return
    raise AssertionError("total API failure should still raise")


NODE = {
    "id": 7,
    "ssh_host": "proxy",
    "ssh_port": 100,
    "ssh_direct": {"host": "1.2.3.4", "port": 40125},
    "status": "running",
    "label": "vrun:web",
}


def _only_node(**over):
    run.get_running_instances = lambda: iter([{**NODE, **over}])


@case
def ssh_prefers_direct_and_falls_back_to_the_proxy():
    _only_node()
    argv = run.ssh_argv("web")
    assert argv[-1] == "root@1.2.3.4", f"direct address not used: {argv}"
    assert argv[argv.index("-p") + 1] == "40125", f"direct port not used: {argv}"
    assert run.ssh_argv("web", direct=False)[-1] == "root@proxy", "--proxy ignored"
    _only_node(ssh_direct=None)
    assert run.ssh_argv("web")[-1] == "root@proxy", "no direct address should fall back"
    _only_node(ssh_direct=None, ssh_host=None, ssh_port=None)
    try:
        run.ssh_argv("web")
    except RuntimeError:
        return
    raise AssertionError("a node with no ssh address must not build a command")


@case
def everything_after_the_label_goes_to_ssh_verbatim():
    _only_node()
    execed = []
    main_mod.os.execvp = lambda f, a: execed.append(a)
    extra = ["-N", "-L", "8081:localhost:8081", "-o", "ExitOnForwardFailure=yes"]
    sys.argv = ["vast", "ssh", "web", *extra]
    main_mod.main()
    assert execed[0][-len(extra) :] == extra, f"passthrough mangled: {execed[0]}"
    assert execed[0][-len(extra) - 1] == "root@1.2.3.4", "host must come first"

    sys.argv = ["vast", "ssh", "web", "uv", "run", "serve.py"]
    main_mod.main()
    assert execed[1][-3:] == ["uv", "run", "serve.py"], f"command lost: {execed[1]}"

    sys.argv = ["vast", "ssh", "--proxy", "web", "-v"]
    main_mod.main()
    assert execed[2][-2:] == ["root@proxy", "-v"], (
        f"our flags not honoured: {execed[2]}"
    )

    sys.argv = ["vast", "ssh", "web", "--print"]
    main_mod.main()
    assert execed[3][-1] == "--print", "after the label, even our flags go to ssh"


def _launch_filter(argv):
    seen = []
    with _patched(
        main_mod.run,
        local_pubkey=lambda: "k",
        key_on_account=lambda k: True,
        launch=lambda *a, **k: seen.append(a[2]),
    ):
        sys.argv = argv
        main_mod.main()
    return seen[0]


@case
def launch_and_gpus_agree_on_which_hosts_count():
    base = ["vast", "launch", "/tmp", "--cmd", "x"]
    assert _launch_filter(base).verified, "launch must vet hosts by default"
    assert not _launch_filter([*base, "--any-host"]).verified, (
        "a price gpus --any-host quotes must be one launch can rent at"
    )


@case
def the_label_is_the_first_non_flag_word():
    split = main_mod._split_ssh
    assert split(["ssh", "web", "-N"]) == (["ssh", "web"], ["-N"]), (
        "label then ssh args"
    )
    assert split(["ssh", "--print", "web"]) == (["ssh", "--print", "web"], []), "ours"
    assert split(["ssh", "-h"]) == (["ssh", "-h"], []), "no label means nothing to ssh"
    assert split(["ssh"]) == (["ssh"], []), "a bare ssh keeps the default target"
    assert split(["ps"]) == (["ps"], []), "other subcommands are untouched"
    assert split(["exec", "web", "-la"]) == (["exec", "web", "-la"], []), "exec intact"


@case
def print_before_the_label_shows_the_command_without_connecting():
    _only_node()
    execed = []
    main_mod.os.execvp = lambda f, a: execed.append(a)
    sys.argv = ["vast", "ssh", "--print", "web", "-L", "8081:localhost:8081"]
    main_mod.main()
    assert not execed, "--print must not connect"


@case
def real_ssh_accepts_what_we_build():
    if shutil.which("ssh") is None:
        raise AssertionError("no ssh binary to validate against")
    inst = {"id": 7, "ssh_host": "h", "ssh_port": 100, "ssh_direct": None}
    argv = remote.ssh_argv(
        inst, ["-L", "8081:localhost:8081", "-p", "2222", "uv", "run", "serve.py"]
    )
    done = subprocess.run([argv[0], "-G", *argv[1:]], capture_output=True, check=False)
    assert done.returncode == 0, f"ssh rejected our argv: {done.stderr}"
    out = done.stdout.decode()
    assert "\nport 100\n" in out, f"our resolved port must win over a later -p: {out}"


@case
def a_receiver_that_dies_mid_push_does_not_hang():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "big.bin"), "wb") as f:
        f.write(os.urandom(8 << 20))

    def hung(*a):
        raise AssertionError("push hung: tar is blocked on a pipe nobody reads")

    real_base = remote.base
    remote.base = lambda inst: ["sh", "-c", "exit 1"]
    signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    try:
        with _instant_retries():
            remote._stream({"id": 7}, ["-C", d, "."], "/root/proj")
    except RuntimeError:
        return
    finally:
        signal.alarm(0)
        remote.base = real_base
        shutil.rmtree(d, ignore_errors=True)
    raise AssertionError("a receiver that exits must fail the push, not pass it")


@case
def ssh_resolves_by_label_only():
    rows = [
        {"id": 11, "ssh_host": "a", "ssh_port": 1, "status": "running", "label": None},
        {"id": 22, "ssh_host": "b", "ssh_port": 2, "status": "running", "label": "s"},
        {"id": 33, "ssh_host": "c", "ssh_port": 3, "status": "running", "label": "dup"},
        {"id": 44, "ssh_host": "d", "ssh_port": 4, "status": "running", "label": "dup"},
    ]
    run.get_running_instances = lambda: iter(rows)
    assert run.resolve("s")["id"] == 22, "a plain label should resolve"
    for bad in (None, "dup", "11", "vrun:s", "nope"):
        try:
            run.resolve(bad)
        except RuntimeError:
            continue
        raise AssertionError(f"{bad!r} should not resolve")


@case
def ssh_ignores_instances_that_are_not_running():
    rows = [
        {"id": 1, "ssh_host": "a", "ssh_port": 1, "status": "exited", "label": "old"},
        {"id": 2, "ssh_host": "b", "ssh_port": 2, "status": "running", "label": "new"},
    ]
    run.get_running_instances = lambda: iter(rows)
    assert run.resolve()["id"] == 2, "a stopped node must not force a choice"
    assert run.resolve("new")["id"] == 2, "the running node resolves by label"
    try:
        run.resolve("old")
    except RuntimeError as e:
        assert "exited" in str(e), f"the error should name the state: {e}"
        return
    raise AssertionError("a stopped node must not resolve")


@case
def jupyter_nodes_get_the_offset_proxy_port():
    body = {
        "instances": [
            {
                "id": 1,
                "ssh_host": "h",
                "ssh_port": 100,
                "image_runtype": "jupyter_direc ssh_direc ssh_proxy",
                "label": "vrun:j",
            },
            {"id": 2, "ssh_host": "h", "ssh_port": 100, "image_runtype": "ssh_proxy"},
            {"id": 3, "ssh_host": "h", "ssh_port": None},
        ]
    }
    with _patched(client, _request=lambda *a, **k: body):
        ports = {r["id"]: r["ssh_port"] for r in client.get_running_instances()}
    assert ports[1] == 101, f"jupyter runtype needs ssh_port+1, got {ports[1]}"
    assert ports[2] == 100, f"plain ssh runtype must not shift, got {ports[2]}"
    assert ports[3] is None, f"a node with no port must stay None, got {ports[3]}"


@case
def reap_survives_one_bad_instance():
    good = {"id": 1, "label": "vrun:a", "start_date": 999_999_999_999}
    bad = {"id": 2, "label": "vrun:b", "start_date": 999_999_999_999}
    run.get_running_instances = lambda: iter([good, bad])
    run.destroy_with_retries = lambda *a, **k: True

    def fake_probe(inst):
        if inst["id"] == 2:
            raise RuntimeError("probe blew up")

    run._probe = fake_probe
    results = run.reap(default_max_age=86_400, max_restarts=3)
    ids = [r["id"] for r in results]
    assert ids == [1], f"bad instance should be skipped, good kept: {ids}"


@case
def reap_raises_only_when_all_fail():
    a = {"id": 1, "label": "vrun:a", "start_date": 0}
    b = {"id": 2, "label": "vrun:b", "start_date": 0}
    run.get_running_instances = lambda: iter([a, b])

    def boom(inst):
        raise RuntimeError("everything is broken")

    run._probe = boom
    try:
        run.reap(default_max_age=86_400, max_restarts=3)
    except RuntimeError:
        return
    raise AssertionError("all-fail tick should raise (environment broken)")


@case
def a_stuck_docker_pull_fails_instead_of_waiting_out_the_timeout():
    stuck = {
        "id": 9,
        "ssh_host": None,
        "ssh_port": None,
        "status": "loading",
        "status_msg": 'Error response from daemon: Get "https://reg/v2/": EOF',
    }
    client.get_running_instances = lambda: iter([stuck])
    with _fake_clock():
        try:
            client.wait_until_ready(9, timeout=600, error_grace=3)
        except client.InstanceError as e:
            assert "EOF" in str(e), f"the daemon error must reach the caller: {e}"
            return
        except TimeoutError as e:
            raise AssertionError(
                "a node in error must not burn the full timeout"
            ) from e
    raise AssertionError("a node in error must raise")


@case
def an_empty_status_msg_is_not_treated_as_an_error():
    seq = [
        {
            "id": 9,
            "ssh_host": None,
            "ssh_port": None,
            "status": "loading",
            "status_msg": "",
        },
        {
            "id": 9,
            "ssh_host": "h",
            "ssh_port": 1,
            "status": "running",
            "status_msg": "",
        },
    ]
    client.get_running_instances = lambda: iter([seq.pop(0)])
    with _fake_clock():
        inst = client.wait_until_ready(9, timeout=600, error_grace=1)
    assert inst["ssh_host"] == "h", f"a loading node must still be waited for: {inst}"


@case
def a_node_that_stops_reporting_progress_is_dropped():
    quiet = {
        "id": 9,
        "ssh_host": None,
        "ssh_port": None,
        "status": "loading",
        "status_msg": "pulling image",
    }
    client.get_running_instances = lambda: iter([quiet])
    with _fake_clock() as now:
        try:
            client.wait_until_ready(9, timeout=10_000, stall_s=300)
        except client.InstanceError as e:
            assert "stuck" in str(e), f"a stalled node must say so: {e}"
            assert now[0] < 400, f"a stall must fire near stall_s, not late: {now[0]}"
            return
    raise AssertionError("a node frozen on one message must not wait out the timeout")


@case
def progress_messages_keep_the_node_alive():
    msgs = ["pull 10%", "pull 50%", "pull 90%"]
    seq = [
        {
            "id": 9,
            "ssh_host": None,
            "ssh_port": None,
            "status": "loading",
            "status_msg": m,
        }
        for m in msgs
    ] + [
        {"id": 9, "ssh_host": "h", "ssh_port": 1, "status": "running", "status_msg": ""}
    ]
    client.get_running_instances = lambda: iter([seq.pop(0)])
    with _fake_clock():
        inst = client.wait_until_ready(9, timeout=10_000, stall_s=15)
    assert inst["ssh_host"] == "h", f"changing messages mean progress: {inst}"


@case
def an_instance_that_vanishes_does_not_hang_until_timeout():
    client.get_running_instances = lambda: iter([])
    with _fake_clock():
        try:
            client.wait_until_ready(9, timeout=1800, error_grace=3)
        except client.InstanceError as e:
            assert "listed" in str(e), f"a vanished node must say so: {e}"
            return
        except TimeoutError as e:
            raise AssertionError("a vanished node must not wait out 1800s") from e
    raise AssertionError("a vanished node must raise")


class _HtmlErrorPage:
    status_code = 502
    text = "<html><body>502 Bad Gateway</body></html>"

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


@case
def an_html_error_page_names_the_status_not_the_json_parser():
    sess = types.SimpleNamespace(request=lambda *a, **k: _HtmlErrorPage())
    with _patched(client, _session=sess):
        msg = _raises(RuntimeError, client._request, "PUT", "https://x/?api_key=secret")
    assert "502" in msg and "Bad Gateway" in msg, f"the user must see why: {msg}"
    assert "secret" not in msg, f"the api key LEAKED into the error: {msg}"


def _launch_that_fails(boom=None):
    destroyed = []

    def default_boom(*a, **k):
        raise RuntimeError("setup failed (rc=1)")

    with (
        _patched(
            run,
            _find_existing=lambda label: [],
            local_pubkey=lambda: "k",
            key_on_account=lambda k: True,
            _provision_reachable=lambda *a, **k: (5, {"id": 5, "ssh": "ssh://x"}),
            _push_and_start=boom or default_boom,
            destroy_with_retries=destroyed.append,
        ),
        contextlib.suppress(RuntimeError, KeyboardInterrupt),
    ):
        run.launch("/tmp", "c", None, None, "job")
    return destroyed


@case
def a_broken_launch_does_not_leak_a_paid_node():
    left = _launch_that_fails()
    assert left == [5], f"a failed launch must destroy the node: {left}"


@case
def a_ctrl_c_mid_launch_still_destroys_the_node():
    def interrupt(*a, **k):
        raise KeyboardInterrupt

    left = _launch_that_fails(interrupt)
    assert left == [5], f"ctrl-c must not leak a paid node: {left}"


@case
def a_held_node_is_still_reaped_once_it_ages_out():
    p = {"job": {"launched_at": 0}, "has_pgid": False, "hold": True}
    action = run._decide_running(p, run.LAUNCH_GRACE_S + 100, 3)[0]
    assert action == "destroy", f"a hold must not buy a node forever: {action}"


def _tty(yes):
    return types.SimpleNamespace(
        stderr=types.SimpleNamespace(
            isatty=lambda: yes, write=lambda s: None, flush=lambda: None
        )
    )


def _tree():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "sub"))
    for path, n in (("a.bin", 300), ("sub/b.bin", 700)):
        with open(os.path.join(root, path), "wb") as f:
            f.write(b"x" * n)
    return root


@case
def the_push_bar_counts_exactly_the_bytes_tar_reports():
    root = _tree()
    tar = subprocess.Popen(
        ["tar", "czf", "-", "-v", "-C", root, "."],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    seen = []
    with _patched(remote, sys=_tty(True)):
        remote._track(
            tar.stderr, remote._sizes(root, None), lambda d, t: seen.append(d)
        )
    assert seen[-1] == 1000, f"tar's names must match the size map, got {seen}"
    assert seen == sorted(seen), f"progress must never go backwards: {seen}"


@case
def a_push_to_a_log_file_pays_for_no_progress_bar():
    got = []
    with _patched(
        remote,
        sys=_tty(False),
        _stream=lambda i, s, d, sizes=None: got.append(sizes),
    ):
        remote.put({"id": 7}, _tree(), "/root/proj")
    assert got == [None], f"a non-tty push must not turn on tar -v: {got}"


@case
def a_single_file_push_still_knows_its_size():
    got = []
    path = os.path.join(_tree(), "a.bin")
    with _patched(
        remote,
        sys=_tty(True),
        _stream=lambda i, s, d, sizes=None: got.append(sizes),
    ):
        remote.put({"id": 7}, path, "/root/proj")
    assert got == [{"a.bin": 300}], f"a lone file must size itself: {got}"


@case
def a_dropped_push_is_retried_not_fatal():
    calls = []

    def flaky(inst, tar_src, dest, sizes=None):
        calls.append(dest)
        return (0, 0) if len(calls) > 1 else (2, 255)

    with _patched(remote, _stream_once=flaky), _instant_retries():
        remote._stream({"id": 7}, ["-C", "/tmp", "."], "/root/proj")
    assert len(calls) == 2, f"one drop must be retried, not fatal: {calls}"


@case
def a_push_that_never_lands_still_fails():
    calls = []

    def always_drops(inst, tar_src, dest, sizes=None):
        calls.append(dest)
        return (2, 255)

    with _patched(remote, _stream_once=always_drops), _instant_retries():
        try:
            remote._stream({"id": 7}, ["-C", "/tmp", "."], "/root/proj")
        except RuntimeError as e:
            assert "ssh=255" in str(e), f"the real rc must survive retries: {e}"
            assert len(calls) == 5, f"must stop after 5 attempts: {calls}"
            return
    raise AssertionError("a push that never lands must raise")


@case
def a_local_tar_failure_is_not_retried():
    calls = []

    def bad_tar(inst, tar_src, dest, sizes=None):
        calls.append(dest)
        return (2, 0)

    with _patched(remote, _stream_once=bad_tar), _instant_retries():
        try:
            remote._stream({"id": 7}, ["-C", "/tmp", "."], "/root/proj")
        except RuntimeError as e:
            assert "tar=2" in str(e), f"must name the local failure: {e}"
            assert len(calls) == 1, f"a local failure must not re-upload: {calls}"
            return
    raise AssertionError("a local tar failure must raise")


@case
def every_public_name_still_resolves():
    missing = [n for n in api.__all__ if not hasattr(api, n)]
    assert not missing, f"lazy export map is out of date: {missing}"


@case
def a_direct_ip_is_used_only_once_it_is_proven():
    inst = {
        "id": 9,
        "ssh_host": "ssh8.vast.ai",
        "ssh_port": 11778,
        "ssh_direct": {"host": "1.2.3.4", "port": 40022},
    }
    argv = remote.base(inst)
    assert "root@ssh8.vast.ai" in argv, (
        f"an unproven direct ip must not be used: {argv}"
    )
    with _patched(remote, _probe=lambda *a, **k: (0, "")):
        remote.wait(inst, timeout=1, poll=0)
    argv = remote.base(inst)
    assert "root@1.2.3.4" in argv, f"a proven direct ip must be used: {argv}"
    assert "40022" in argv, f"must use the direct port: {argv}"


@case
def commands_fall_back_to_the_proxy_without_a_direct_ip():
    inst = {"id": 9, "ssh_host": "ssh8.vast.ai", "ssh_port": 11778, "ssh_direct": None}
    argv = remote.base(inst)
    assert "root@ssh8.vast.ai" in argv, f"must still reach the node: {argv}"
    assert "11778" in argv, f"must use the proxy port: {argv}"


@case
def the_proxy_flag_is_not_swallowed_by_an_open_direct_session():
    inst = {
        "id": 9,
        "ssh_host": "ssh8.vast.ai",
        "ssh_port": 11778,
        "ssh_direct": {"host": "1.2.3.4", "port": 40022},
    }

    def sock(argv):
        return next(a for a in argv if a.startswith("ControlPath="))

    direct = remote.ssh_argv(inst, direct=True)
    proxy = remote.ssh_argv(inst, direct=False)
    assert "root@ssh8.vast.ai" in proxy, f"--proxy must reach the relay: {proxy}"
    assert sock(direct) != sock(proxy), "--proxy must not ride the direct session"


@case
def repeated_commands_reuse_one_ssh_connection():
    argv = remote.base({"id": 7, "ssh_host": "h", "ssh_port": 1, "ssh_direct": None})
    assert "ControlMaster=auto" in argv, f"each push must not re-handshake: {argv}"
    paths = [a for a in argv if a.startswith("ControlPath=")]
    assert paths, f"no socket: {argv}"


@case
def two_nodes_never_share_a_control_socket():
    def sock(inst_id):
        argv = remote.base(
            {"id": inst_id, "ssh_host": "h", "ssh_port": 1, "ssh_direct": None}
        )
        return next(a for a in argv if a.startswith("ControlPath="))

    assert sock(1) != sock(2), "same address, different node, must not share a socket"


@case
def a_blocked_direct_ip_falls_back_to_the_proxy():
    inst = {
        "id": 9,
        "ssh_host": "ssh8.vast.ai",
        "ssh_port": 11778,
        "ssh_direct": {"host": "1.2.3.4", "port": 40022},
    }
    seen = []

    def only_proxy_answers(inst, host, port, timeout=30):
        seen.append(host)
        return (0, "") if host == "ssh8.vast.ai" else (255, "refused")

    with _patched(remote, _probe=only_proxy_answers):
        remote.wait(inst, timeout=1, poll=0)
    assert inst["ssh_addr"] == ("ssh8.vast.ai", 11778), f"wrong pick: {inst}"
    assert "root@ssh8.vast.ai" in remote.base(inst), "later commands must follow"


@case
def a_reachable_direct_ip_is_preferred():
    inst = {
        "id": 9,
        "ssh_host": "ssh8.vast.ai",
        "ssh_port": 11778,
        "ssh_direct": {"host": "1.2.3.4", "port": 40022},
    }
    with _patched(remote, _probe=lambda *a, **k: (0, "")):
        remote.wait(inst, timeout=1, poll=0)
    assert inst["ssh_addr"] == ("1.2.3.4", 40022), f"must not relay: {inst}"


@case
def a_node_that_answers_on_neither_address_still_fails():
    inst = {
        "id": 9,
        "ssh_host": "ssh8.vast.ai",
        "ssh_port": 11778,
        "ssh_direct": {"host": "1.2.3.4", "port": 40022},
    }
    with _patched(remote, _probe=lambda *a, **k: (255, "refused")):
        try:
            remote.wait(inst, timeout=0, poll=0)
        except RuntimeError as e:
            assert "1.2.3.4:40022" in str(e), f"must name both addresses: {e}"
            assert "ssh8.vast.ai:11778" in str(e), f"must name both addresses: {e}"
            return
    raise AssertionError("an unreachable node must raise")


@case
def a_broken_machine_is_not_rented_again():
    offers = [
        {"id": 1, "machine_id": 100, "num_gpus": 1, "gpu": "A100", "price": 1},
        {"id": 2, "machine_id": 200, "num_gpus": 1, "gpu": "A100", "price": 1},
    ]
    rented = []

    def always_broken(inst_id, log=None):
        raise client.InstanceError("daemon error")

    patches = dict(
        get_available_instances=lambda f: iter(offers),
        create_instance=lambda oid, o, label=None: (rented.append(oid), oid)[1],
        attach_ssh_key=lambda *a, **k: None,
        destroy_with_retries=lambda *a, **k: True,
        wait_until_ready=always_broken,
    )
    with _patched(run, **patches), contextlib.suppress(client.InstanceError):
        run._provision_reachable(None, None, "l", lambda m: None, "k", True, attempts=2)
    assert rented == [1, 2], f"attempt 2 must skip the failed machine: {rented}"


@case
def exhausting_the_offers_reports_why_the_nodes_failed():
    offers = [{"id": 1, "machine_id": 100, "num_gpus": 1, "gpu": "A100", "price": 1}]

    def always_broken(inst_id, log=None):
        raise client.InstanceError("daemon said EOF")

    patches = dict(
        get_available_instances=lambda f: iter(offers),
        create_instance=lambda oid, o, label=None: oid,
        attach_ssh_key=lambda *a, **k: None,
        destroy_with_retries=lambda *a, **k: True,
        wait_until_ready=always_broken,
    )
    with _patched(run, **patches):
        try:
            run._provision_reachable(
                None, None, "l", lambda m: None, "k", True, attempts=3
            )
        except client.InstanceError as e:
            assert "EOF" in str(e), f"the real cause must survive: {e}"
            return
        except RuntimeError as e:
            raise AssertionError(f"pool exhaustion must not hide the cause: {e}") from e
    raise AssertionError("an all-bad pool must raise")


@case
def a_node_that_flaps_between_error_and_ok_is_still_dropped():
    seq = [
        {
            "id": 9,
            "ssh_host": None,
            "ssh_port": None,
            "status": "loading",
            "status_msg": "pulling" if i % 2 else "Error: transient",
        }
        for i in range(60)
    ]
    client.get_running_instances = lambda: iter([seq.pop(0)])
    with _fake_clock():
        try:
            client.wait_until_ready(9, timeout=10_000, error_grace=6, stall_s=10_000)
        except client.InstanceError as e:
            assert "transient" in str(e), f"the flapping error must surface: {e}"
            return
    raise AssertionError("a node flapping in and out of error must not run forever")


@case
def a_message_that_changes_after_an_error_resets_the_stall_clock():
    seq = [
        {
            "id": 9,
            "ssh_host": None,
            "ssh_port": None,
            "status": "loading",
            "status_msg": "pulling",
        },
        {
            "id": 9,
            "ssh_host": None,
            "ssh_port": None,
            "status": "loading",
            "status_msg": "Error: transient",
        },
        {
            "id": 9,
            "ssh_host": None,
            "ssh_port": None,
            "status": "loading",
            "status_msg": "pulling",
        },
        {
            "id": 9,
            "ssh_host": "h",
            "ssh_port": 1,
            "status": "running",
            "status_msg": "",
        },
    ]
    client.get_running_instances = lambda: iter([seq.pop(0)])
    with _fake_clock():
        inst = client.wait_until_ready(9, timeout=10_000, error_grace=6, stall_s=15)
    assert inst["ssh_host"] == "h", f"an error poll must not freeze the clock: {inst}"


def main():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
