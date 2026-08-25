# /// script
# requires-python = ">=3.10"
# ///
import os
import shutil
import subprocess
import sys
import types

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
from vast_cli.api import client, remote, run

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


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
    client._request = lambda *a, **k: body
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
    client._request = lambda *a, **k: {"success": False, "msg": "boom"}
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
    client._request = lambda *a, **k: body
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
        return None

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
