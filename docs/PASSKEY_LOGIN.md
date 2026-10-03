# Shared passkey login

The optional service in [deploy/passkey-auth](../deploy/passkey-auth/README.md)
uses one registered owner passkey to authorize personal MCP connections.
Google Password Manager synced keys and device-bound keys are supported.
The default standalone Cloud Browser password login remains available when the
bridge is not configured.

## Boundaries

- WebAuthn requires the exact HTTPS origin/RP ID and user verification.
- The front door stores public keys. PINs and passkey private keys stay with the authenticator.
- Registration requires a short-lived, single-use administrator enrollment link.
- Each MCP keeps its own OAuth issuer, PKCE checks, scopes, access and refresh tokens.
- Cloud Browser accepts a 60-second HMAC proof bound to the backend nonce and
  either `oauth` or `control`. Its original Origin, cookie and nonce checks remain.
- Private-console session delivery requires the same authenticated front-door
  session, a single-use ticket and the tailnet-only control host.
- Browser network isolation, Chromium sandboxing and website-action approvals are unchanged.

The front door is part of the authentication trust boundary. Keep it on loopback,
protect its configuration and state with owner-only permissions, and never send
the bridge secret to a browser. Synced passkeys permit the same key on multiple
devices; protect the Google account and password manager accordingly. A Google
account login alone does not authenticate to the MCP server.

## Cloud Browser configuration

Generate a separate random `CB_PASSKEY_BRIDGE_SECRET` of at least 32 characters
on the server. Set the identical value in Cloud Browser's private `.env` and a
private environment file readable by the front-door service. Keep the existing
administrator password hash for local recovery. Do not commit either env file.

For the included profile, the passkey origin is HTTPS on port 443 and the private
control origin is the same hostname on port 9443. A deployment using the standalone
guide's 8443 console must deliberately select 9443 in both its Cloud Browser
configuration and private Serve routing before adopting this profile.

Configure adapters using [config.example.json](../deploy/passkey-auth/config.example.json).
Point the public `/browser/authorize` route to front-door `/login/browser`, the
private 9443 `/login` route to `/login/browser-control`, and the private
`/passkey/finish` route to `/finish-control`. Preserve MCP, token, revocation,
metadata and all existing proxy routes. Never enable Funnel on the console port.

Register a real passkey, complete a separate login confirmation, then activate
the profile. The activation tool refuses to switch routes without a real login
record and restores the preceding routes if a command fails. This profile also
supports existing personal OAuth backends with `transaction`/`access_key` forms;
it is not a general multi-user identity provider.

## UI and recovery

Successful registration shows a green completion panel and the registered key
name, and hides the registration button. A separate login confirmation switches
to the signed-in management view. These states are confirmed by the server.

A synced copy of the same Google passkey does not need another registration.
A new device may require a Google Password Manager PIN or another unlock step.
Removing one synced key blocks that key across its devices and revokes its
front-door sessions. Previously issued MCP OAuth grants and console sessions
must be revoked separately. The last key cannot be removed through the web UI.
Use existing SSH administrator access to issue a recovery enrollment link.

Existing connected ChatGPT apps keep their tokens; unconnected apps still need
their normal Connect flow. Website accounts, API keys, Linux sudo and SSH key
passphrases are outside this login.

## Validation

Real ES256 registration/assertion tests cover synced keys, user verification,
origin/RP binding, replay, CSRF and owner enrollment. Cloud Browser tests cover
proof nonce/audience/expiry binding, OAuth PKCE, revocation and private sessions.
The personal deployment was checked on 2026-10-03; no production keys, PINs,
passwords, enrollment links or personal hostnames are included here.
