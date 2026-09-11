# /// script
# requires-python = ">=3.10"
# ///
import os
import subprocess
import sys
import tempfile

_HOME = tempfile.mkdtemp()
for _leak in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
    os.environ.pop(_leak, None)
os.environ.update(
    HOME=_HOME,
    XDG_CONFIG_HOME=_HOME,
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_SYSTEM=os.devnull,
    GIT_CEILING_DIRECTORIES=tempfile.gettempdir(),
)

from vast_cli.api.remote import git_files  # noqa: E402


def _write(root, path, content=""):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)


def _repo():
    root = tempfile.mkdtemp()
    subprocess.run(["git", "-C", root, "init", "-q"], check=True)
    _write(root, ".gitignore", "*.env\nignored/\n__pycache__/\n")
    _write(root, "train.py", "print(1)")
    _write(root, "pkg/mod.py", "x = 1")
    _write(root, "untracked.txt", "note")
    _write(root, "secret.env", "TOKEN=abc")
    _write(root, "ignored/big.bin", "data")
    _write(root, "__pycache__/mod.pyc", "junk")
    subprocess.run(["git", "-C", root, "add", "train.py", "pkg/mod.py"], check=True)
    return root


def test_not_a_repo_returns_none():
    root = tempfile.mkdtemp()
    assert git_files(root) is None


def test_file_selection_needs_no_http_stack():
    assert "requests" not in sys.modules, "picking files must not need the api client"


def test_empty_repo_pushes_nothing_rather_than_everything():
    root = tempfile.mkdtemp()
    subprocess.run(["git", "-C", root, "init", "-q"], check=True)
    assert git_files(root) == [], "an empty repo must not look like a missing repo"


def test_paths_with_spaces_survive():
    root = tempfile.mkdtemp()
    subprocess.run(["git", "-C", root, "init", "-q"], check=True)
    _write(root, "my notes/a b.txt", "x")
    assert git_files(root) == ["my notes/a b.txt"], "a space must not split a path"


def test_nested_gitignore_is_respected():
    root = _repo()
    _write(root, "pkg/.gitignore", "*.log\n")
    _write(root, "pkg/debug.log", "noise")
    files = set(git_files(root))
    assert "pkg/debug.log" not in files, "nested ignore rule LEAKED"
    assert "pkg/.gitignore" in files, "the nested ignore file itself must ship"


def test_gitignore_respected():
    files = set(git_files(_repo()))
    assert "train.py" in files, "tracked file missing"
    assert "pkg/mod.py" in files, "tracked nested file missing"
    assert "untracked.txt" in files, "untracked-but-not-ignored file missing"
    assert "secret.env" not in files, "ignored *.env LEAKED"
    assert "ignored/big.bin" not in files, "ignored dir LEAKED"
    assert "__pycache__/mod.pyc" not in files, "ignored cache LEAKED"
    assert not any(f.startswith(".git/") for f in files), ".git internals LEAKED"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
