# WPE single-tab preview

This is an opt-in development backend, not a replacement for the deployed
Chromium service. One work owns one Cog page and one stable `tab_id`. It never
asks WPE to create another window. The default engine remains Chromium.

## Implemented behavior

The base WPE MCP registry has eight tools: `browser_open`,
`browser_list_tabs`, `browser_navigate`, `browser_observe`, `browser_act`,
`browser_close`, `browser_status`, and `browser_configure`. With private manual
control enabled, it also exposes `browser_auth_request` and `browser_handoff`.

`browser_open` creates the page; later calls must use `new_tab=false` to reuse
it. A second-tab request fails without replacing the page. Navigation permits
only an explicit HTTP(S) `goto`. Observation returns bounded main-document
text and visible control metadata. Embedded frames are **not** read. Clicking
is limited to a freshly observed node, requires confirmation, and rejects form
submission, non-HTTP links, and links declared to open another window. Other
automated input, downloads, uploads, and page-provided tools are unavailable.

Visual observation can return a viewport PNG only when the main document is
unfilled and contains no detected sensitive input, iframe, canvas, video, or
SVG. A changed page during capture fails closed. This is a conservative
heuristic, **not a proof** that arbitrary website pixels contain no secrets.
The AI must treat all page text and control labels as untrusted website content.

With a separate private administrator password and managed display, a human
can take over the same page through the existing loopback noVNC bridge. While
private control is active, MCP observation and actions are paused. Returning
control requires a fresh non-protected snapshot; a failure keeps the private
bridge available and automation paused. Authentication is reported as
`unverified` unless an operator-configured site rule identifies success or
failure. Login credentials are never requested through MCP.

## Local isolated trial on the Raspberry Pi

Use a separate checkout and data directory. Do not point the preview at the
production Cloud Browser profile or state. Required settings include:

```text
CB_ENGINE=wpe
CB_DEVELOPMENT=true
CB_MAX_SESSIONS=1
CB_WEBMCP_ENABLED=false
CB_NETWORK_ISOLATED=false
CB_BIND_HOST=127.0.0.1
CB_PUBLIC_ORIGIN=http://127.0.0.1:18080
CB_CONTROL_ORIGIN=http://127.0.0.1:18081
CB_PUBLIC_PORT=18080
CB_CONTROL_PORT=18081
CB_DATA_DIR=<separate private directory>
```

For a private human-control trial, also set `CB_MANAGED_DISPLAY=true`,
`CB_MANUAL_CONTROL_ENABLED=true`, an independent Argon2id
`CB_ADMIN_PASSWORD_HASH`, and unused X/VNC/bridge ports. Keep their listeners
on loopback. The application refuses WPE outside development mode, multiple
sessions, non-loopback public/control addresses, and an unauthenticated manual
console.

The loopback VNC socket itself has no password; the private HTTP control
console supplies administrator authentication. Other local processes with
access to the Pi's loopback interface are therefore inside the trial's trust
boundary. Do not forward the VNC or WebSocket ports to a network interface.

## Verification and remaining gates

The local fixture exercises one-page reuse, observed click, PNG capture,
private-display RFB keyboard/mouse input through a dummy login form, the
WebSocket bridge handshake, protected login-screen blocking, and return from
private control. A separate smoke test visits `example.com` and `python.org`
on the actual Pi and clicks a public Python.org link. Dynamic pages may return
`SCREEN_CHANGED` instead of an image; that is an intended fail-closed result.

These checks do **not** prove that an arbitrary real login succeeds. No real
account credentials or human login were used. Nor do they establish network
egress confinement against redirects, DNS rebinding, page subresources, or
WebDriver-driven navigation. WPE WebDriver JavaScript executes in page context,
not Chromium's isolated world. The visual privacy filter also cannot reliably
classify all CSS-generated or image-embedded secrets. Until those boundaries
are addressed and tested, do not expose WPE over the Tailscale tunnel, register
it as a ChatGPT connector, or switch the production backend to WPE.

Run local unit tests with `pytest tests/test_wpe_preview.py`. The real-engine
smoke scripts require the Raspberry Pi's WPE/Cog/Weston executables and an
unprivileged user; they do not touch the production service.
