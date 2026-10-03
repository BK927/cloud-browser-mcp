# Personal MCP passkey front door

This optional, single-owner login service shares one passkey enrollment across
personal MCP deployments. It includes the WebAuthn portal, state store, fixed
backend adapters and security tests used in the recorded home-server deployment.
It is not a general multi-user identity provider.

Google Password Manager synced keys are allowed when `device_bound` is false;
setting it true restricts registration/authentication to single-device keys.
Both modes require user verification, exact origin/RP ID and valid signatures.
PINs and private keys never reach this server. Successful registration shows a
green completion panel; confirming login shows the signed-in key management view.

## Setup

Use Python 3.12+ and install `requirements.lock` in a dedicated virtual environment.
Copy `config.example.json` to a private path, replace every placeholder, and
restrict the config, backend env files and SQLite state directory to the owner.
Set `MCP_PASSKEY_CONFIG` to that private config. Use a fixed HTTPS origin without
a path or explicit port; the RP ID follows its hostname.

Run from this directory:

```sh
python -m uvicorn app:create_app --factory --host 127.0.0.1 --port 8090 --no-access-log
```

Proxy only the portal under `/auth` on the public HTTPS origin. Keep all backends
on loopback and do not publish state, private env files or enrollment links.
The included `deploy.py` is an operator helper for the existing six-backend
systemd/Tailscale profile. It expects each backend's private config to exist;
set `MCP_PASSKEY_ORIGIN` for a first installation. It preserves previously added
adapters and the enrolled-key database. Other layouts should install the service
and review their proxy configuration explicitly.

## Enrollment and activation

```sh
python app.py enroll --label owner-passkey
python app.py status
python activate.py
python probe.py --active
python probe-browser-routes.py
```

An SSH administrator issues a 15-minute, single-use registration URL. Open it on
the owner's device and complete the native credential prompt manually. Do not
put the URL in a ticket, document or log. A registered owner can issue additional
links from the portal. A synced copy of the same key needs no second enrollment.

Complete the separate login confirmation before activation: the tool requires a
real authentication record. It backs up routing, retains existing MCP and OAuth
metadata/token routes, and restores the preceding routes if a change fails.
`activate.py --rollback` restores the original backend login endpoints. The
example profile keeps the console on tailnet-only 9443 and includes optional
Steam 8443 and Browser 10000 aliases; never enable Funnel on the console port.

## Cloud Browser and other MCPs

For Cloud Browser, generate one random server-side `CB_PASSKEY_BRIDGE_SECRET`
of at least 32 characters and place the same value in both private environment
files. See [Cloud Browser integration](../../docs/PASSKEY_LOGIN.md). The front
door signs only a backend-issued nonce after fresh passkey authentication;
the proof expires after 60 seconds and distinguishes OAuth from console access.
Private session delivery is one-use and bound to the authenticated portal session.

The classic adapter expects the existing personal OAuth `transaction`/`access_key`
form. It validates the signed transaction with the backend before creating a
flow, then uses the existing `MCP_OAUTH_LOGIN_SECRET` only inside the server after
passkey verification. YouTube, DLsite, Steam and the personal Bilibili/TapTap/Reddit
stdio gateway profile use this adapter. Those upstream projects are not modified
or republished here. Each MCP retains its own OAuth tokens and scopes.

## Recovery and validation

Removing a synced key revokes its portal sessions and blocks that key on all its
devices. Existing ChatGPT grants and Browser console sessions must be revoked
separately. Web deletion of the last key is blocked. Use existing SSH administrator
access to issue a recovery enrollment link; preserve backend recovery credentials.
Linux sudo, SSH passphrases and website-account authentication remain separate.

Run `python -m pytest -q` in this directory with pytest installed. Tests use real
ES256 registrations/assertions and cover synced keys, UV/origin/RP binding, CSRF,
replay, one-use enrollment, fixed OAuth callbacks and console handoff. Test keys
are never inserted into a production database. Hostnames and example paths are
placeholders; keep operational data out of Git.
