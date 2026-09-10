# Public URL prefix with a stripped-path reverse proxy

`CB_PUBLIC_PATH_PREFIX` is optional and defaults to the empty string. Existing
root deployments keep their public URLs and backend routes. This feature changes
canonical public URLs, not the private console, MCP tool schemas, browser engine,
OAuth client registration policy, or dependency/container configuration.

## Operator configuration

For an externally hosted `/browser/mcp` endpoint, set:

```dotenv
CB_PUBLIC_ORIGIN="https://browser.example"
CB_PUBLIC_PATH_PREFIX="/browser"
```

Keep `CB_PUBLIC_ORIGIN` a pure scheme/host/optional-port origin. Do not put
`/browser` in it. Leave the separately configured `CB_CONTROL_ORIGIN` unchanged.
Prefixes must be empty or an absolute path of at most 128 ASCII characters with
one or more nonempty segments. Segment characters are letters, digits, `.`, `_`,
`~`, and `-`; whole `.` and `..` segments are forbidden. There is no trailing slash.
Absolute URLs, percent escapes (including encoded dots/slashes), whitespace,
backslashes, repeated slashes, query strings and fragments are rejected at startup.

The example derives these exact public identifiers:

| Purpose | Canonical URL |
| --- | --- |
| OAuth issuer | `https://browser.example/browser` |
| MCP resource and access-token audience | `https://browser.example/browser/mcp` |
| Authorization | `https://browser.example/browser/authorize` |
| Token exchange / refresh | `https://browser.example/browser/token` |
| Revocation | `https://browser.example/browser/revoke` |
| Resource metadata | `https://browser.example/.well-known/oauth-protected-resource/browser/mcp` |
| Authorization-server metadata | `https://browser.example/.well-known/oauth-authorization-server/browser` |

The well-known segment goes **before** the resource/issuer path, following
[RFC 9728 section 3](https://www.rfc-editor.org/rfc/rfc9728.html#section-3) and
[RFC 8414 section 3](https://www.rfc-editor.org/rfc/rfc8414.html#section-3).
The MCP 401 `WWW-Authenticate` challenge advertises the canonical resource-metadata
URL; discovery responses and the authorization callback `iss` use the identifiers
above. Clients should discover these values rather than append paths to the issuer.

## Required reverse-proxy contract

The backend still listens at its original **root** paths. The operator must arrange
exactly these three additional external mappings for `/browser`:

| External path | Backend path |
| --- | --- |
| `/browser/<rest>` | `/<rest>` (strip `/browser` exactly once) |
| `/.well-known/oauth-protected-resource/browser/mcp` | `/.well-known/oauth-protected-resource/mcp` |
| `/.well-known/oauth-authorization-server/browser` | `/.well-known/oauth-authorization-server` |

Preserve the method, query, body and streaming response. Forward a fixed `Host`
equal to the configured public origin's authority (including any nondefault port),
and preserve the browser's actual `Origin`. Host/Origin/CSRF checks use the pure
origin, **not** `public_base` or a caller-supplied `Forwarded`/`X-Forwarded-*` header.
Do not rewrite foreign/null Origins into the allowed origin. The application does
not infer its prefix from proxy headers or dynamically supplied hosts.

Do not publish the backend listener directly or forward all root traffic to this
application. Unrelated services, applications on other paths or ports, and the
private control route must retain their previous mappings. Tailscale Serve
and Funnel configuration belongs to the deployment operator; this code patch does
not edit it. Verify actual prefix stripping and fixed-Host behavior before switching
traffic; a local test proxy is not evidence about a particular Tailscale deployment.

In prefix mode, implicit trailing-slash redirects are disabled on both public
routers. Authenticated `/browser/mcp/` and OAuth slash variants return 404, not a
redirect to a root URL that might belong to a sibling service. Unauthorized MCP
variants can still receive the existing 401 challenge first. Unstripped/doubly
prefixed requests do not gain alternate application routes. Default empty-prefix
mode retains its existing slash behavior. No HEAD endpoint or broad route alias
was added.

## Forms, stored authorization and privacy

The OAuth form action and `cb_oauth` cookie creation/deletion path are
`/browser/authorize`. The existing HttpOnly, SameSite, production Secure,
same-origin form Referrer-Policy and callback CSP remain in force. The separate
private login, approval, CSRF and handback paths are not prefixed or weakened.
Cookie paths scope delivery; they are not a security boundary against an untrusted
sibling application on the same origin.

Changing origin or prefix changes the canonical resource. Existing access tokens,
pending authorization forms, authorization codes and refresh tokens with a different
stored resource are rejected. In particular, an old refresh token cannot be upgraded
to the new audience by supplying the new `resource` in a request. Clients must
reauthorize against the new endpoint. Reverting configuration can likewise require
reauthorization; do not assume tokens issued for the prefixed URL work at the root.

There is no automatic database migration, grant deletion, credential rotation or
profile reset. Ordinary one-time code/refresh consumption and expiry still apply,
including rejection after such a credential was consumed. Do not delete state to
work around an audience mismatch.

Optional HTTP diagnostics remain off by default. When explicitly enabled they
report the same allowlisted **backend** paths after prefix stripping, not raw
external URLs, headers, queries, tokens or bodies. See [HTTP diagnostics](HTTP_DIAGNOSTICS.md).

## Validation boundaries

`tests/test_public_prefix.py` and `tests/prefix_proxy.py` exercise a real loopback
HTTP reverse proxy with fixed Host and stripped paths, including root/prefixed
discovery, authorization, PKCE, refresh/revoke, MCP initialize/list/call/image,
identical 17-tool schemas, private-route isolation, bad Host/Origin/path/audience
rejection and stored old-resource rejection without grant migration.

`tests/test_public_prefix_browser.py` additionally uses real Chrome and an isolated
temporary profile to check native form POST, scoped HttpOnly cookies and their
deletion, cross-origin callback without password/cookie/referrer leakage, canonical
`iss`, PKCE exchange and code reuse rejection at both `/browser` and `/apps/browser`.
Set `CB_TEST_CHROMIUM` to run these browser tests. Local test-only HTTP callbacks do
not change production HTTPS validation. The proxy uses a fake browser worker for
MCP transport tests; existing browser/W3C tests separately cover actual browser actions.

These tests do not prove public TLS/Funnel routing, real ChatGPT registration or
image interpretation, Pi performance, or that prefix routing fixes a reported
ChatGPT connection timeout. Those are separate deployment and client validation
gates. A successful local request and zero observed backend requests during one
client attempt do not by themselves prove a particular public port is blocked.
