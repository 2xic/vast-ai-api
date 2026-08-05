import argparse
import os
import sys

from vast_cli.api import AvailableInstancesFilter, InstanceOptions
from vast_cli.run import clean, launch, list_gpus, ps, reap


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

    clean_p = sub.add_parser("clean", help="destroy all managed (vrun:) instances")
    clean_p.add_argument("--force", action="store_true", help="skip confirmation")

    gpus_p = sub.add_parser("gpus", help="list rentable GPU models and cheapest price")
    gpus_p.add_argument("--price", type=float, default=1000.0, help="max $/hour")
    gpus_p.add_argument("--gpus", type=int, default=1, help="min gpu count")

    args = parser.parse_args()

    if args.command == "launch":
        filter = AvailableInstancesFilter(
            min_gpu=args.gpus,
            min_disk_space_gb=args.disk,
            max_dollar_price_hour=args.price,
            mbps_up=args.up,
            mbps_down=args.down,
            gpu_name=args.gpu,
        )
        options = InstanceOptions()
        options.disk_space = args.disk
        label = args.label or os.path.basename(os.path.normpath(args.src))
        try:
            launch(
                args.src,
                args.cmd,
                filter,
                options,
                label,
                setup=args.setup,
                grace=args.grace,
                max_age=args.max_age,
                drain=args.drain,
                paths=args.path,
            )
        except Exception as e:
            print(f"launch failed: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "reap":
        reap(default_max_age=args.max_age)
    elif args.command == "ps":
        ps()
    elif args.command == "clean":
        clean(force=args.force)
    elif args.command == "gpus":
        list_gpus(max_price=args.price, min_gpu=args.gpus)


if __name__ == "__main__":
    main()
