# /// script
# requires-python = ">=3.10"
# ///
import os
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


@case
def missing_instances_key_still_raises():
    client._request = lambda *a, **k: {"success": False, "msg": "boom"}
    try:
        list(client.get_running_instances())
    except RuntimeError:
        return
    raise AssertionError("total API failure should still raise")


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
