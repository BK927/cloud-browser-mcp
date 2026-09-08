#!/usr/bin/env python3
"""Operator-only benchmark launcher in the native API's namespace and cgroup.

This matches `docker exec --user app` measurement placement. It never changes
service settings, restarts a service, or prints environment/credentials.
"""

import argparse
import os
import pwd
import subprocess
from pathlib import Path

from sample_memory import group_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--script", type=Path, default=Path(__file__).with_name("benchmark.py"))
    args, rest = parser.parse_known_args()
    if os.geteuid() != 0:
        parser.error("Use sudo directly; never put passwords in arguments")
    account = pwd.getpwnam("cb-api")
    proc = Path(f"/proc/{args.pid}")
    if proc.stat().st_uid != account.pw_uid:
        raise RuntimeError("PID is not the dedicated cb-api process")
    argv = (proc / "cmdline").read_bytes().decode().strip("\0").split("\0")
    if argv[1:] != ["-I", "-m", "cloud_browser.cli", "serve"]:
        raise RuntimeError("PID is not the native API command")
    python = Path(argv[0])
    if not python.is_relative_to("/opt/cloud-browser/releases") or python.name != "python":
        raise RuntimeError("Unexpected native release interpreter")
    group = group_for(args.pid)
    if group.name != "cloud-browser.service":
        raise RuntimeError("Unexpected native API cgroup")
    environment = {}
    for field in (proc / "environ").read_bytes().split(b"\0"):
        key, separator, value = field.partition(b"=")
        if separator and (key.startswith(b"CB_") or key in (b"PATH", b"LANG")):
            environment[key.decode()] = value.decode()
    network = os.open(proc / "ns/net", os.O_RDONLY)

    def enter():
        os.setns(network, 0)
        (group / "cgroup.procs").write_text(str(os.getpid()))
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)

    try:
        # stdin avoids chmod/copying administrator source into the private data
        # directory. The same byte-identical script is used for Docker variants.
        result = subprocess.run(
            [str(python), "-I", "-", *rest],
            input=args.script.read_bytes(),
            env=environment,
            cwd="/opt/cloud-browser",
            preexec_fn=enter,
            pass_fds=(network,),
            check=False,
        )
        raise SystemExit(result.returncode)
    finally:
        os.close(network)


if __name__ == "__main__":
    main()
