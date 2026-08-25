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
from vast_cli.api import client, run

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


@case
def ssh_prefers_direct_and_expands_forwards():
    rows = [
        {
            "id": 7,
            "ssh_host": "proxy",
            "ssh_port": 100,
            "ssh_direct": {"host": "1.2.3.4", "port": 40125},
            "status": "running",
            "label": "vrun:web",
        }
    ]
    run.get_running_instances = lambda: iter(rows)
    argv = run.ssh_argv("web", forwards=["8081", "9000:80"])
    assert "root@1.2.3.4" in argv, f"direct address not used: {argv}"
    assert argv[argv.index("-p") + 1] == "40125", f"direct port not used: {argv}"
    fwd = [argv[n + 1] for n, a in enumerate(argv) if a == "-L"]
    assert fwd == ["8081:localhost:8081", "9000:localhost:80"], f"bad forwards: {fwd}"
    proxied = run.ssh_argv("web", direct=False)
    assert "root@proxy" in proxied, f"--proxy ignored: {proxied}"


@case
def ssh_falls_back_to_proxy_and_refuses_addressless_nodes():
    proxy_only = {
        "id": 8,
        "ssh_host": "proxy",
        "ssh_port": 100,
        "ssh_direct": None,
        "status": "running",
        "label": "vrun:web",
    }
    run.get_running_instances = lambda: iter([proxy_only])
    argv = run.ssh_argv("web")
    assert "root@proxy" in argv, f"no direct address should fall back: {argv}"
    pending = {**proxy_only, "ssh_host": None, "ssh_port": None}
    run.get_running_instances = lambda: iter([pending])
    try:
        run.ssh_argv("web")
    except RuntimeError:
        return
    raise AssertionError("a node with no ssh address must not build a command")


@case
def ssh_rejects_bad_forward_specs():
    for spec in ("8081:", "", "http", "80:eighty", ":80"):
        try:
            run._forward(spec)
        except RuntimeError:
            continue
        raise AssertionError(f"{spec!r} should be rejected")
    assert run._forward("1259:localhost:22") == "1259:localhost:22", "3-part spec"


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
def real_ssh_accepts_the_command_we_build():
    if shutil.which("ssh") is None:
        raise AssertionError("no ssh binary to validate against")
    rows = [
        {
            "id": 7,
            "ssh_host": "proxy",
            "ssh_port": 100,
            "ssh_direct": {"host": "1.2.3.4", "port": 40125},
            "status": "running",
            "label": "vrun:web",
        }
    ]
    run.get_running_instances = lambda: iter(rows)
    argv = run.ssh_argv("web", forwards=["8081", "9000:80", "1259:localhost:1259"])
    probe = [argv[0], "-G", *argv[1:]]
    done = subprocess.run(probe, capture_output=True, check=False)
    assert done.returncode == 0, f"ssh rejected our argv: {done.stderr}"


@case
def cli_execs_ssh_and_print_does_not():
    rows = [
        {
            "id": 7,
            "ssh_host": "proxy",
            "ssh_port": 100,
            "ssh_direct": {"host": "1.2.3.4", "port": 40125},
            "status": "running",
            "label": "vrun:web",
        }
    ]
    run.get_running_instances = lambda: iter(rows)
    execed = []
    main_mod.os.execvp = lambda f, a: execed.append((f, a))
    sys.argv = ["vast", "ssh", "web", "-L", "8081", "--print"]
    main_mod.main()
    assert not execed, "--print must not connect"
    sys.argv = ["vast", "ssh", "web", "-L", "8081"]
    main_mod.main()
    assert len(execed) == 1, "ssh should exec exactly once"
    prog, argv = execed[0]
    assert prog == "ssh" and argv[0] == "ssh", f"bad exec target: {execed[0]}"
    assert "8081:localhost:8081" in argv, f"forward lost on the cli path: {argv}"


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
