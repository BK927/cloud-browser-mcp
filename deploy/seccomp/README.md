# Chromium namespace policy

`docker-29.8.0.json` is an unmodified copy of the default profile actually
vendored in Docker Engine 29.8.0 (Moby profiles/seccomp v0.2.3):
https://github.com/moby/moby/blob/docker-v29.8.0/vendor/github.com/moby/profiles/seccomp/default.json

Upstream package: https://github.com/moby/profiles/tree/seccomp/v0.2.3
Baseline SHA-256: `de1f5327ca42b80be02daba8d39c0d087a530dc3c16f7028170fe068c9d66e61`.

It and the derived `chromium.json` retain Apache-2.0, not this project's MIT
license. See the accompanying upstream `LICENSE` and `NOTICE`.

`chromium.json` was modified by the Cloud Browser MCP contributors on 2026-09-08.
It adds exactly six argument-equality allow rules, only on amd64 and arm64:

| Call | Exact flags | Purpose |
|---|---|---|
| clone | 0x10000011 | NEWUSER + SIGCHLD |
| clone | 0x30000011 | NEWUSER + NEWPID + SIGCHLD |
| clone | 0x50000011 | NEWUSER + NEWNET + SIGCHLD |
| clone | 0x70000011 | NEWUSER + NEWPID + NEWNET + SIGCHLD |
| clone | 0x20000011 | NEWPID + SIGCHLD inside the new user namespace |
| unshare | 0x10000000 | NEWUSER for Chromium's credentials sandbox |

These correspond to Chromium's `NamespaceSandbox::LaunchProcessWithOptions`,
`ForkInNewPidNamespace` and `Credentials::MoveToNewUserNS`. All other policy
entries remain unchanged. In particular, there is no new permission for mount,
setns, arbitrary unshare, or clone3. Chromium's own seccomp-BPF sandbox remains
enabled. No host SYS_ADMIN capability is granted. Kernel capability checks still
apply; new namespace root is not host root.

The additions expose kernel namespace creation to the container and must be
reviewed with kernel/runtime security updates. This pinned policy does **not**
automatically acquire hardening introduced in later Docker default policies.
Keep the host patched and explicitly review upstream policy drift on upgrades.
Do not use this profile for other workloads or replace it with `unconfined`.

An earlier Pi arm64 canary on 2026-09-08 (Docker 26.1.5) used these same six
additions on the older 26.1.5 baseline. Baseline failed with namespace EPERM;
the candidate returned an actual blank DOM. A separate operator-only
`chrome://sandbox` diagnostic reported Namespace layer 1, PID/network namespaces,
seccomp-BPF and TSYNC enabled. Yama ptrace protection was No on that host.
The final 29.8.0 profile was subsequently tested on the same Pi with Engine
29.8.0: default policy failed with EPERM (1.46s); blank DOM passed (6.60s), and
sandbox diagnostic passed (6.31s) with the same enabled namespace/seccomp fields.
Final profile SHA-256:
`50797a4acbdc5b4146763f9ce8a787b3f5e78a16877a8db819ce4c2ea6b62100`.
This is not full deployment or amd64 container validation. Repeat on upgrades.

The 26.1.5 candidate is not shipped: it lacks later default restrictions such as
socket(AF_ALG). The production profile preserves all 29.8.0 rules, including
AF_ALG/AF_VSOCK blocking. The upstream socketcall/32-bit LSM caveat still applies;
do not interpret this profile as a replacement for a patched kernel and LSM.

Example isolated diagnostic (no profiles, credentials, ports or host mounts):

```sh
docker run --rm --network none --user 1001:1001 \
  --memory 512m --memory-swap 512m --pids-limit 128 --cpus 1 --shm-size 128m \
  --security-opt seccomp=./deploy/seccomp/chromium.json \
  --entrypoint /usr/bin/chromium YOUR_LOCAL_BROWSER_IMAGE \
  --headless --disable-gpu --no-first-run --user-data-dir=/tmp/cb-sandbox-canary \
  --allow-chrome-scheme-url --dump-dom chrome://sandbox
```

Apply an operator timeout and inspect the returned sandbox table, not exit status
alone. The internal-scheme diagnostic flag is never passed by the MCP launcher.
Host-wide user namespace/LSM restrictions require diagnosis; do not disable them
globally to make this container start.
