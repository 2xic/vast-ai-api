# /// script
# requires-python = ">=3.10"
# ///
import os
import subprocess
import tempfile

from vast_cli.api.remote import git_files


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
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} passed")
