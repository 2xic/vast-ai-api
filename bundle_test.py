# /// script
# requires-python = ">=3.10"
# ///
import contextlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import types


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


class _Sess:
    def __init__(self, *a, **k):
        self.headers = {}

    def mount(self, *a, **k):
        pass


_stub("requests", Session=_Sess)
_stub("requests.adapters", HTTPAdapter=lambda *a, **k: None)
_stub("urllib3.util", Retry=lambda *a, **k: None)
_stub("dotenv", load_dotenv=lambda *a, **k: None)
os.environ.setdefault("VAST_API_KEY", "test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vast_cli import __main__ as cli  # noqa: E402
from vast_cli.api import remote, run  # noqa: E402

run_git_files = remote.git_files

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _git(d, *args):
    subprocess.run(["git", "-C", d, *args], check=True, capture_output=True)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _repo(d):
    os.makedirs(d, exist_ok=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")


def _members(tarball):
    with tarfile.open(tarball) as t:
        return sorted(n[2:] for n in t.getnames())


def _body(tarball, name):
    with tarfile.open(tarball) as t:
        return t.extractfile(f"./{name}").read().decode()


ARGPARSE_USAGE_ERROR = 2


def _bundle(src, out, paths=()):
    return run.bundle(src, out, paths)


@test
def gitignored_files_are_not_packed():
    with tempfile.TemporaryDirectory() as d:
        _repo(d)
        _write(f"{d}/.gitignore", "secret.env\nignored/\n")
        _write(f"{d}/train.py", "x")
        _write(f"{d}/secret.env", "KEY=1")
        _write(f"{d}/ignored/junk.txt", "junk")
        _git(d, "add", "-A")
        out = f"{d}/../j.tgz"
        _bundle(d, out)
        got = set(_members(out))
        assert got == set(run_git_files(d)), got
        assert "secret.env" not in got, got
        assert not any(m.startswith("ignored/") for m in got), got
        assert "train.py" in got, got


@test
def path_overlay_wins_and_leaves_the_repo_untouched():
    with tempfile.TemporaryDirectory() as d:
        repo, ov = f"{d}/repo", f"{d}/ov"
        _repo(repo)
        _write(f"{repo}/scheduler/a.py", "tracked")
        _write(f"{repo}/scheduler/shared.py", "tracked")
        _git(repo, "add", "-A")
        _write(f"{ov}/shared.py", "OVERLAY")
        _write(f"{ov}/local.sh", "gitignored")
        out = f"{d}/j.tgz"
        _bundle(repo, out, [f"{ov}:scheduler"])
        assert _body(out, "scheduler/shared.py") == "OVERLAY"
        assert _body(out, "scheduler/a.py") == "tracked"
        assert _body(out, "scheduler/local.sh") == "gitignored"
        with open(f"{repo}/scheduler/shared.py") as f:
            assert f.read() == "tracked"


@test
def remote_is_always_a_directory():
    with tempfile.TemporaryDirectory() as d:
        repo = f"{d}/repo"
        _repo(repo)
        _write(f"{repo}/train.py", "x")
        _git(repo, "add", "-A")
        _write(f"{d}/lib/src/m.py", "m")
        _write(f"{d}/lib/setup.py", "s")
        _write(f"{d}/run.yaml", "y")
        out = f"{d}/j.tgz"
        _bundle(
            repo,
            out,
            [
                f"{d}/run.yaml",
                f"{d}/lib/src:shared-library/src",
                f"{d}/lib/setup.py:shared-library",
            ],
        )
        got = set(_members(out))
        assert "run.yaml" in got, got
        assert "shared-library/src/m.py" in got, got
        assert "shared-library/setup.py" in got, got


@test
def a_parent_relative_path_lands_under_the_project_dir():
    with tempfile.TemporaryDirectory() as d:
        repo = f"{d}/a/b/repo"
        _repo(repo)
        _write(f"{repo}/train.py", "x")
        _git(repo, "add", "-A")
        _write(f"{d}/a/infra/deploy.sh", "d")
        out = f"{d}/j.tgz"
        _bundle(repo, out, [f"{repo}/../../infra:infra"])
        assert "infra/deploy.sh" in _members(out)


@test
def symlinks_stay_symlinks():
    with tempfile.TemporaryDirectory() as d:
        _repo(d)
        _write(f"{d}/keep.py", "x")
        os.symlink("keep.py", f"{d}/link.py")
        _git(d, "add", "-A")
        out = f"{d}/../j.tgz"
        _bundle(d, out)
        with tarfile.open(out) as t:
            assert t.getmember("./link.py").issym()


@test
def an_escaping_remote_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        _repo(d)
        _write(f"{d}/train.py", "x")
        _git(d, "add", "-A")
        _write(f"{d}/cfg.yaml", "y")
        for bad in ["/etc", "../../escape"]:
            try:
                _bundle(d, f"{d}/../j.tgz", [f"{d}/cfg.yaml:{bad}"])
            except RuntimeError:
                continue
            raise AssertionError(f"{bad} was not rejected")


@test
def a_non_git_dir_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        _write(f"{d}/train.py", "x")
        try:
            _bundle(d, f"{d}/../j.tgz")
        except RuntimeError as e:
            assert "not a git repository" in str(e), e
            return
        raise AssertionError("not rejected")


@test
def bundle_can_write_to_stdout():
    with tempfile.TemporaryDirectory() as d:
        _repo(d)
        _write(f"{d}/train.py", "x")
        _git(d, "add", "-A")
        out = f"{d}/../j.tgz"
        with open(out, "wb") as fh:
            subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--emit", d],
                stdout=fh,
                check=True,
            )
        assert set(_members(out)) == {"train.py"}, _members(out)


def _bundled(d):
    _repo(d)
    _write(f"{d}/train.py", "x")
    _git(d, "add", "-A")
    out = f"{d}/../j.tgz"
    run.bundle(d, out)
    return out


@test
def an_omitted_any_host_still_means_verified():
    with tempfile.TemporaryDirectory() as d:
        out = _bundled(d)
        paths = [x for spec in ("a", "b", "c", "d") for x in ("--path", spec)]
        with _captured_launch() as seen:
            _run_main(["launch", "--bundle", out, "--cmd", "uv run x.py", *paths])
    assert seen["filters"].verified is True, seen["filters"]
    assert seen["paths"] == ["a", "b", "c", "d"], seen["paths"]


def _run_main(argv):
    sys.argv = ["vast", *argv]
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as e:
            return e.code, err.getvalue()
    return 0, err.getvalue()


@test
def launch_still_requires_cmd_and_a_bundle_replaces_src():
    code, err = _run_main(["launch", ".", "--label", "x"])
    assert code == ARGPARSE_USAGE_ERROR and "--cmd" in err, (code, err)

    code, err = _run_main(["launch", "--cmd", "uv run x.py"])
    assert code == "launch needs a src dir or --bundle", (code, err)

    with tempfile.TemporaryDirectory() as d:
        out = _bundled(d)
        with _captured_launch() as seen:
            _run_main(["launch", "--bundle", out, "--cmd", "uv run x.py"])
    assert (seen["src"], seen["bundle"]) == (None, out), seen


@contextlib.contextmanager
def _captured_launch():
    seen = {}

    def fake(src, cmd, filters, options, label, **kw):
        seen.update(
            src=src, cmd=cmd, filters=filters, options=options, label=label, **kw
        )

    orig = (run.launch, run.local_pubkey, run.key_on_account)
    run.launch, run.local_pubkey, run.key_on_account = fake, lambda: "k", lambda k: True
    try:
        yield seen
    finally:
        run.launch, run.local_pubkey, run.key_on_account = orig


DEFAULT_DISK = 10
DEFAULT_PRICE = 10.0
DEFAULT_GRACE = 900
DEFAULT_MAX_AGE = 86400
DEFAULT_DRAIN = 1800


@test
def a_plain_launch_resolves_exactly_todays_values():
    with _captured_launch() as seen:
        _run_main(["launch", ".", "--cmd", "uv run x.py"])
    f, o = seen["filters"], seen["options"]
    assert (f.min_gpu, f.min_disk_space_gb) == (1, DEFAULT_DISK), f
    assert (f.max_dollar_price_hour, f.mbps_up, f.mbps_down) == (
        DEFAULT_PRICE,
        DEFAULT_PRICE,
        DEFAULT_PRICE,
    ), f
    assert f.gpu_name is None and f.verified is True, f
    assert o.disk_space == DEFAULT_DISK, o
    assert seen["bundle"] is None and seen["setup"] is None
    assert (seen["grace"], seen["max_age"], seen["drain"]) == (
        DEFAULT_GRACE,
        DEFAULT_MAX_AGE,
        DEFAULT_DRAIN,
    ), seen


@contextlib.contextmanager
def _fake_ssh(codes):
    seen = {"cmds": [], "sleeps": []}
    codes = list(codes)
    orig = (remote.subprocess.call, remote.time.sleep)

    def call(argv, stdin=None):
        seen["cmds"].append(argv[-1])
        seen["payload"] = stdin.read()
        return codes.pop(0)

    remote.subprocess.call = call
    remote.time.sleep = seen["sleeps"].append
    try:
        yield seen
    finally:
        remote.subprocess.call, remote.time.sleep = orig


def _raises(fn):
    try:
        fn()
    except RuntimeError as e:
        return str(e)
    raise AssertionError("no RuntimeError")


NODE = {"id": 7, "ssh_addr": ("h", 22)}
EXPECTED_BACKOFF = [5, 10, 20, 40]
MAX_ATTEMPTS = len(EXPECTED_BACKOFF) + 1


@test
def put_bundle_retries_a_dropped_upload_then_succeeds():
    with tempfile.TemporaryDirectory() as d:
        out = _bundled(d)
        codes = [255, 0]
        with _fake_ssh(codes) as seen:
            remote.put_bundle(NODE, out, "/root/proj")
    assert len(seen["cmds"]) == len(codes), seen["cmds"]
    assert seen["sleeps"] == [5], seen["sleeps"]


@test
def put_bundle_reopens_the_tarball_on_every_attempt():
    with tempfile.TemporaryDirectory() as d:
        out = _bundled(d)
        with open(out, "rb") as fh:
            whole = fh.read()
        with _fake_ssh([255, 0]) as seen:
            remote.put_bundle(NODE, out, "/root/proj")
    assert seen["payload"] == whole, "a retry must re-send the whole tarball"


@test
def put_bundle_gives_up_after_five_attempts():
    with tempfile.TemporaryDirectory() as d:
        out = _bundled(d)
        with _fake_ssh([1] * MAX_ATTEMPTS) as seen:
            msg = _raises(lambda: remote.put_bundle(NODE, out, "/root/proj"))
    assert msg == f"bundle upload failed after {MAX_ATTEMPTS} attempts (rc=1)", msg
    assert len(seen["cmds"]) == MAX_ATTEMPTS, seen["cmds"]
    assert seen["sleeps"] == EXPECTED_BACKOFF, seen["sleeps"]


@test
def put_bundle_rejects_a_missing_tarball():
    msg = _raises(lambda: remote.put_bundle(NODE, "/nope/x.tgz", "/root/proj"))
    assert "not found" in msg, msg


def main():
    if sys.argv[1:2] == ["--emit"]:
        run.bundle(sys.argv[2], "-")
        return 0
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
