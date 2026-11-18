import argparse
import logging
import os
import shlex
import signal
import sys

from vast_cli.api import AvailableInstancesFilter, InstanceOptions, client, run


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
        state += f"({r['exit_code']})" + (" HOLD" if r["hold"] else "")
    return state


def _print_ps(rows):
    states = [_state_text(r) for r in rows]
    lw = max([len("LABEL"), *(len(r["label"]) for r in rows)])
    sw = max([len("STATE"), *(len(s) for s in states)])
    cw = max([len("CC"), *(len(str(r.get("country", "-"))) for r in rows)])
    print(
        f"{'ID':>10}  {'MACHINE':>8}  {'CC':<{cw}} {'LABEL':<{lw}} {'STATE':<{sw}} "
        f"{'AGE':<10} {'LEFT':<10} GPU%"
    )
    for r, state in zip(rows, states, strict=True):
        print(
            f"{r['id']:>10}  {r['machine_id']:>8}  {r.get('country', '-')!s:<{cw}} "
            f"{r['label']:<{lw}} {state:<{sw}} "
            f"{_fmt_dur(r['age_s']):<10} {_fmt_dur(r['left_s']):<10} "
            f"{r['gpu']}"
        )


def _add_any_host(p):
    p.add_argument(
        "--any-host",
        action="store_true",
        help="include unverified hosts (cheaper, slightly lower reliability score)",
    )


def _machine_ids(text):
    out = []
    for word in text.replace(",", " ").split():
        if not word.isdigit():
            raise argparse.ArgumentTypeError(f"not a machine id: {word!r}")
        out.append(int(word))
    return tuple(out)


def _add_skip_machines(p):
    p.add_argument(
        "--skip-machines",
        type=_machine_ids,
        default=(),
        metavar="ID[,ID...]",
        help="never rent these machine ids",
    )


def _add_offer_filters(p):
    p.add_argument(
        "--countries",
        type=client.countries,
        default=(),
        metavar="CC[,CC...]",
        help="only rent in these countries, e.g. 'no,se', or a group: "
        + ", ".join(sorted(k.lower() for k in client.COUNTRY_GROUPS)),
    )
    p.add_argument(
        "--direct-port",
        action="store_true",
        help="only rent nodes with a direct ssh port, skipping the vast ssh proxy",
    )
    p.add_argument(
        "--min-reliability",
        type=float,
        default=0.0,
        metavar="FRAC",
        help="minimum host reliability score, e.g. 0.98",
    )


def _add_bundle_flag(p):
    p.add_argument(
        "--bundle",
        default=None,
        metavar="TARBALL",
        help="a 'vast bundle' tarball to push instead of walking git",
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
        help="provision a node, run a command, stream it, take a fresh node on request",
    )
    launch_p.add_argument(
        "src", nargs="?", help="local file or dir to push to /root/proj"
    )
    _add_bundle_flag(launch_p)
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
    launch_p.add_argument(
        "--gpu-ram", type=float, default=0, metavar="GB", help="minimum memory per GPU"
    )
    launch_p.add_argument("--disk", type=int, default=10)
    launch_p.add_argument(
        "--image",
        default=None,
        metavar="REPO:TAG",
        help="docker image, e.g. 'pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime'",
    )
    launch_p.add_argument("--price", type=float, default=10.0)
    launch_p.add_argument("--up", type=float, default=client.MIN_MBPS)
    launch_p.add_argument("--down", type=float, default=client.MIN_MBPS)
    launch_p.add_argument("--label", default=None)
    launch_p.add_argument(
        "--grace", type=int, default=900, help="seconds a failed node lingers for debug"
    )
    launch_p.add_argument(
        "--max-age",
        type=int,
        default=86400,
        help="hard cap for a job with no EXIT_CODE marker",
    )
    launch_p.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="LOCAL[:REMOTE]",
        help="extra file/dir to push (repeatable); REMOTE relative to the project dir",
    )
    launch_p.add_argument(
        "--node-key",
        action="store_true",
        help="attach the ssh key to this node only, not to your vast account",
    )
    _add_any_host(launch_p)
    _add_skip_machines(launch_p)
    _add_offer_filters(launch_p)

    b = sub.add_parser(
        "bundle",
        help="pack tracked files and --path extras into one tarball",
    )
    b.add_argument("src", nargs="?", default=".", help="local git dir to pack")
    b.add_argument(
        "-o", "--out", default="-", help="output tarball ('-' for stdout, the default)"
    )
    b.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="LOCAL[:REMOTE]",
        help="extra file/dir to pack (repeatable); REMOTE relative to the project dir",
    )

    rr = sub.add_parser(
        "rerun",
        help="re-push files and restart the job on an existing node (edit-run loop)",
    )
    rr.add_argument("label", help="label of the running node to rerun on")
    rr.add_argument("src", nargs="?", default=".", help="local dir to re-push")
    _add_bundle_flag(rr)
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
    gpus_p.add_argument(
        "--gpu-ram", type=float, default=0, metavar="GB", help="minimum memory per GPU"
    )
    _add_any_host(gpus_p)

    argv, ssh_args = _split_ssh(sys.argv[1:])
    args = parser.parse_args(argv)
    args.ssh_args = ssh_args
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    try:
        _dispatch(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"{args.command} failed: {e}", file=sys.stderr)
        sys.exit(1)


def _do_bundle(args):
    n = run.bundle(args.src, args.out, args.path)
    print(f"[bundle] {n} files -> {args.out}", file=sys.stderr)


def _do_launch(args):
    if not args.bundle and not args.src:
        sys.exit("launch needs a src dir or --bundle")
    options = InstanceOptions()
    options.disk_space = args.disk
    if args.image:
        options.docker_image = args.image
    filters = AvailableInstancesFilter(
        min_gpu=args.gpus,
        min_disk_space_gb=args.disk,
        max_dollar_price_hour=args.price,
        mbps_up=args.up,
        mbps_down=args.down,
        gpu_name=args.gpu,
        min_gpu_ram_gb=args.gpu_ram,
        verified=not args.any_host,
        skip_machines=args.skip_machines,
        countries=args.countries,
        direct_port=args.direct_port,
        min_reliability=args.min_reliability,
        min_cuda=options.cuda_version(),
    )
    label = args.label or os.path.basename(os.path.normpath(args.src or "."))
    if not label:
        sys.exit("could not derive a label; pass --label")
    attach = False
    pubkey = run.local_pubkey()
    if not run.key_on_account(pubkey):
        print(f"this ssh key is NOT on your vast account:\n  {pubkey}")
        where = "this node only" if args.node_key else "your vast account"
        if not _confirm(f"add it to {where}? [y/N] "):
            sys.exit("aborted: no ssh key added, the node would be unreachable")
        attach = True
    return run.launch(
        args.src,
        args.cmd,
        filters,
        options,
        label,
        setup=args.setup,
        grace=args.grace,
        max_age=args.max_age,
        paths=args.path,
        attach_missing_key=attach,
        node_key_only=args.node_key,
        bundle=args.bundle,
    )


def _dispatch(args):
    if args.command == "bundle":
        _do_bundle(args)
    elif args.command == "launch":
        sys.exit(_do_launch(args))
    elif args.command == "rerun":
        sys.exit(
            run.rerun(
                args.label,
                src=args.src,
                setup=args.setup,
                paths=args.path,
                cmd=args.cmd,
                grace=args.grace,
                max_age=args.max_age,
                bundle=args.bundle,
            )
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
        run.reap(default_max_age=args.max_age)
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
        for name, price in run.list_gpus(
            max_price=args.price,
            min_gpu=args.gpus,
            verified=not args.any_host,
            min_gpu_ram_gb=args.gpu_ram,
        ):
            print(f"{name:20} from ${price:.3f}/h")


if __name__ == "__main__":
    main()
