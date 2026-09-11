import argparse
import logging
import os
import shlex
import sys

from vast_cli.api import AvailableInstancesFilter, InstanceOptions, run


def _fmt_dur(s):
    if s is None:
        return "-"
    if s <= 0:
        return "expired"
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m" if m else f"{s}s"


def _confirm(prompt):
    return input(prompt).strip().lower() in ("y", "yes")


def _state_text(r):
    state = r["state"]
    if state == "exited":
        state += f"({r['done']})" + (" HOLD" if r["hold"] else "")
    return state


def _print_ps(rows):
    states = [_state_text(r) for r in rows]
    lw = max([len("LABEL"), *(len(r["label"]) for r in rows)])
    sw = max([len("STATE"), *(len(s) for s in states)])
    print(
        f"{'ID':>10}  {'LABEL':<{lw}} {'STATE':<{sw}} "
        f"{'AGE':<10} {'LEFT':<10} {'GPU%':<8} RESTARTS"
    )
    for r, state in zip(rows, states, strict=True):
        print(
            f"{r['id']:>10}  {r['label']:<{lw}} {state:<{sw}} "
            f"{_fmt_dur(r['age_s']):<10} {_fmt_dur(r['left_s']):<10} "
            f"{r['gpu']:<8} {r['restarts']}"
        )


def _split_ssh(argv):
    if not argv or argv[0] != "ssh":
        return argv, []
    for n, a in enumerate(argv[1:], 1):
        if not a.startswith("-"):
            return argv[: n + 1], argv[n + 1 :]
    return argv, []


def main():
    parser = argparse.ArgumentParser(prog="vast")
    sub = parser.add_subparsers(dest="command", required=True)

    launch_p = sub.add_parser(
        "launch",
        help="provision a node, push a dir, start a command detached, record it, exit",
    )
    launch_p.add_argument("src", help="local file or dir to push to /root/proj")
    launch_p.add_argument(
        "--cmd", required=True, help="command to run in /root/proj, e.g. 'uv run x.py'"
    )
    launch_p.add_argument(
        "--setup", default=None, help="command run once first (install uv/nix/apt)"
    )
    launch_p.add_argument("--gpus", type=int, default=1)
    launch_p.add_argument(
        "--gpu", default=None, metavar="NAME", help="exact GPU model, e.g. 'H100 SXM'"
    )
    launch_p.add_argument("--disk", type=int, default=10)
    launch_p.add_argument(
        "--image",
        default=None,
        metavar="REPO:TAG",
        help="docker image, e.g. 'pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime'",
    )
    launch_p.add_argument("--price", type=float, default=10.0)
    launch_p.add_argument("--up", type=float, default=10.0)
    launch_p.add_argument("--down", type=float, default=10.0)
    launch_p.add_argument("--label", default=None)
    launch_p.add_argument(
        "--grace", type=int, default=900, help="seconds a failed node lingers for debug"
    )
    launch_p.add_argument(
        "--max-age",
        type=int,
        default=86400,
        help="hard cap for a job with no DONE marker",
    )
    launch_p.add_argument(
        "--drain",
        type=int,
        default=1800,
        help="seconds a running job gets to clean up after SIGTERM before destroy",
    )
    launch_p.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="LOCAL[:REMOTE]",
        help="extra file/dir to push (repeatable); REMOTE relative to the project dir",
    )

    rr = sub.add_parser(
        "rerun",
        help="re-push files and restart the job on an existing node (edit-run loop)",
    )
    rr.add_argument("label", help="label of the running node to rerun on")
    rr.add_argument("src", nargs="?", default=".", help="local dir to re-push")
    rr.add_argument("--cmd", default=None, help="override the command (default: reuse)")
    rr.add_argument("--setup", default=None, help="command run once before the job")
    rr.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="LOCAL[:REMOTE]",
        help="extra file/dir to push (repeatable)",
    )
    rr.add_argument(
        "--grace", type=int, default=None, help="override grace (default: reuse)"
    )
    rr.add_argument(
        "--max-age", type=int, default=None, help="override max-age (default: reuse)"
    )
    rr.add_argument(
        "--drain", type=int, default=None, help="override drain (default: reuse)"
    )

    ex = sub.add_parser(
        "exec",
        help="run a shell command on a labelled node (no cmd = interactive shell)",
    )
    ex.add_argument("label", help="label of the node to run on")
    ex.add_argument(
        "cmd", nargs=argparse.REMAINDER, help="command (default: interactive)"
    )

    sh = sub.add_parser(
        "ssh",
        help="ssh to a running instance; everything after the label goes to ssh",
    )
    sh.add_argument(
        "target",
        nargs="?",
        default=None,
        help="node label (default: the only running instance)",
    )
    sh.add_argument(
        "--proxy", action="store_true", help="use the vast ssh proxy, not the direct ip"
    )
    sh.add_argument(
        "--print", dest="print_only", action="store_true", help="print the command only"
    )

    r = sub.add_parser(
        "reap", help="cron on your always-on host: reconcile jobs, warn-then-destroy"
    )
    r.add_argument(
        "--max-age",
        type=int,
        default=86400,
        help="destroy an unreachable managed node older than this",
    )
    r.add_argument(
        "--max-restarts",
        type=int,
        default=3,
        help="relaunch a crashed job (no DONE, dead pgid) up to this many times",
    )

    sub.add_parser(
        "ps",
        help="list managed jobs and their state (from the vast API + node markers)",
    )

    ma = sub.add_parser(
        "max-age", help="change the max-age deadline on a running node (no restart)"
    )
    ma.add_argument("label", help="label of the node")
    ma.add_argument("seconds", type=int, help="new max-age in seconds from launch time")

    clean_p = sub.add_parser("clean", help="destroy all managed (vrun:) instances")
    clean_p.add_argument("--force", action="store_true", help="skip confirmation")

    d = sub.add_parser("destroy", help="destroy a single node by label (immediate)")
    d.add_argument("label", help="label of the node to destroy")

    gpus_p = sub.add_parser("gpus", help="list rentable GPU models and cheapest price")
    gpus_p.add_argument("--price", type=float, default=1000.0, help="max $/hour")
    gpus_p.add_argument("--gpus", type=int, default=1, help="min gpu count")

    argv, ssh_args = _split_ssh(sys.argv[1:])
    args = parser.parse_args(argv)
    args.ssh_args = ssh_args
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        _dispatch(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"{args.command} failed: {e}", file=sys.stderr)
        sys.exit(1)


def _dispatch(args):
    if args.command == "launch":
        filters = AvailableInstancesFilter(
            min_gpu=args.gpus,
            min_disk_space_gb=args.disk,
            max_dollar_price_hour=args.price,
            mbps_up=args.up,
            mbps_down=args.down,
            gpu_name=args.gpu,
        )
        options = InstanceOptions()
        options.disk_space = args.disk
        if args.image:
            options.docker_image = args.image
        label = args.label or os.path.basename(os.path.normpath(args.src))
        attach = False
        pubkey = run.local_pubkey()
        if not run.key_on_account(pubkey):
            print(f"this ssh key is NOT on your vast account:\n  {pubkey}")
            if not _confirm("attach it so the node is reachable? [y/N] "):
                sys.exit("aborted: no ssh key attached, the node would be unreachable")
            attach = True
        run.launch(
            args.src,
            args.cmd,
            filters,
            options,
            label,
            setup=args.setup,
            grace=args.grace,
            max_age=args.max_age,
            drain=args.drain,
            paths=args.path,
            attach_missing_key=attach,
        )
    elif args.command == "rerun":
        run.rerun(
            args.label,
            src=args.src,
            setup=args.setup,
            paths=args.path,
            cmd=args.cmd,
            grace=args.grace,
            max_age=args.max_age,
            drain=args.drain,
        )
    elif args.command == "exec":
        sys.exit(run.exec_on(args.label, cmd=" ".join(args.cmd) or None))
    elif args.command == "ssh":
        argv = run.ssh_argv(args.target, args.ssh_args, direct=not args.proxy)
        if args.print_only:
            print(shlex.join(argv))
            return
        os.execvp(argv[0], argv)
    elif args.command == "reap":
        run.reap(default_max_age=args.max_age, max_restarts=args.max_restarts)
    elif args.command == "ps":
        _print_ps(run.ps())
    elif args.command == "max-age":
        r = run.set_max_age(args.label, args.seconds)
        print(
            f"[max-age] {r['label']}: max_age={r['max_age_s']}s, "
            f"~{_fmt_dur(r['left_s'])} left"
        )
    elif args.command == "clean":
        targets = run.list_managed()
        if not targets:
            print("[clean] no managed instances")
            return
        for inst in targets:
            label = (inst.get("label") or "")[len(run.LABEL_PREFIX) :]
            print(f"[clean] {inst['id']}  {label}")
        if not args.force and not _confirm(
            f"destroy these {len(targets)} instance(s)? [y/N] "
        ):
            print("[clean] aborted")
            return
        run.clean(targets)
    elif args.command == "destroy":
        inst_id = run.destroy(args.label)
        print(f"[destroy] {args.label} -> instance {inst_id}")
    elif args.command == "gpus":
        for name, price in run.list_gpus(max_price=args.price, min_gpu=args.gpus):
            print(f"{name:20} from ${price:.3f}/h")


if __name__ == "__main__":
    main()
