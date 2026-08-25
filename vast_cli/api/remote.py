import os
import shlex
import subprocess
import time

SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
]


def git_files(local_dir):
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                local_dir,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        ).stdout
    except Exception:
        return None
    return [f for f in out.decode().split("\0") if f]


def addr(inst, direct=False):
    d = inst.get("ssh_direct") if direct else None
    if d:
        return d["host"], d["port"]
    return inst.get("ssh_host"), inst.get("ssh_port")


def base(inst):
    return ["ssh", *SSH_OPTS, "-p", str(inst["ssh_port"]), f"root@{inst['ssh_host']}"]


def ssh_argv(inst, ssh_args=(), direct=False):
    host, port = addr(inst, direct)
    if not host or not port:
        raise RuntimeError(f"instance {inst['id']} has no ssh address yet")
    return ["ssh", *SSH_OPTS, "-p", str(port), f"root@{host}", *ssh_args]


def shell(inst, cmd, tty=False):
    cmd_ssh, *rest = base(inst)
    opts = ["-t"] if tty else []
    return subprocess.call([cmd_ssh, *opts, *rest, cmd])


def run(inst, cmd, timeout=None):
    try:
        p = subprocess.run(
            [*base(inst), cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return p.returncode, p.stdout, p.stderr


def wait(inst, timeout=180, poll=5, log=None):
    end = time.time() + timeout
    while True:
        rc, _, err = run(inst, "true", timeout=30)
        if rc == 0:
            return
        err = err.strip().splitlines()[-1] if err.strip() else "no stderr"
        if time.time() > end:
            raise RuntimeError(
                f"ssh not ready on {inst['ssh_host']}:{inst['ssh_port']} "
                f"within {timeout}s (rc={rc}): {err}"
            )
        if log:
            log(f"ssh not up (rc={rc}): {err}")
        time.sleep(poll)


def _stream(inst, tar_src, dest):
    unpack = f"mkdir -p {shlex.quote(dest)} && tar xzf - -C {shlex.quote(dest)}"
    tar = subprocess.Popen(["tar", "czf", "-", *tar_src], stdout=subprocess.PIPE)
    rc = subprocess.call([*base(inst), unpack], stdin=tar.stdout)
    tar.wait()
    if rc or tar.returncode:
        raise RuntimeError(f"upload failed (tar={tar.returncode} ssh={rc})")


def put(inst, local, dest, files=None):
    local = os.path.expanduser(local)
    if not os.path.exists(local):
        raise RuntimeError(f"put: {local} not found")
    if files is not None:
        if not files:
            return
        src = ["-C", local, "--", *files]
    elif os.path.isdir(local):
        src = ["-C", local, "."]
    else:
        src = ["-C", os.path.dirname(local) or ".", os.path.basename(local)]
    _stream(inst, src, dest)
