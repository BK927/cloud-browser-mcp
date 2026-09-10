# Self-Hosted Browser MCP for ChatGPT — Secure Remote Chromium with Human Approval

[English](README.md) | [한국어](README.ko.md)

A single-user, self-hosted [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
server designed to let web ChatGPT operate a dedicated Chromium browser on your
own server. It returns real MCP image content, keeps website login and approval in a
private operator console, and does not run an LLM or require a model API key.

Unlike browser-extension MCPs, this project does not import your everyday local
Chrome profile, cookies, or history. It creates isolated server-side profiles and
places browser traffic behind a validating egress proxy.

> [!IMPORTANT]
> This is an early-development, personal deployment: package version `0.1.0`
> with external contract `0.4` draft. The current contract exposes 17 tools and
> requires the server-issued `lease_id` on session calls. Refresh your client's
> tool schema after upgrading. Public SaaS, multi-user hosting, and app-directory
> submission are outside the project scope.

> [!CAUTION]
> Project code and documentation are MIT-licensed, but the default DrissionPage
> engine has separate personal-learning and lawful non-commercial usage terms.
> Read [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before deployment.

## Why use it?

- **Self-hosted remote Chromium:** keep the browser runtime and profiles on a
  machine you control.
- **ChatGPT-oriented HTTP MCP:** authenticated Streamable HTTP with OAuth 2.1-style
  PKCE flows for a personal ChatGPT connection.
- **Human approval:** login, sensitive actions, and manual control stay in a
  separate private console.
- **Multimodal observation:** receive rendered DOM data, stable node IDs, and
  privacy-checked screenshots as MCP image content.
- **Defence in depth:** isolated profiles, browser sandboxing, bounded resources,
  destination validation, and a public-only egress proxy.
- **ARM64 and AMD64:** Docker and Debian 13 native installation paths use the
  same browser and MCP contract.

## Deployment support

| Environment | Status | Notes |
| --- | --- | --- |
| Debian 13 home server | Primary deployment | Use Docker Compose; the native systemd installer is an advanced alternative. |
| Raspberry Pi 4 Model B (2 GB RAM) | Tested hardware only | It was used for the recorded benchmark/integration test series. It is **not** a recommendation or minimum requirement. |
| Windows or macOS | Local self-test; Docker Desktop unverified | `self-test` can use a separate local Chromium. The Docker Desktop deployment path has not been measured or integration-tested. |
| GCP Compute Engine | Manual and unverified | A persistent Debian VM can run the same Docker Compose stack, subject to your own network and cost review. |
| GCP Cloud Run | Unsupported | The stack needs capabilities, sandbox policy, persistent profiles, and public/private listeners that Cloud Run does not provide. |
| Cloudflare Workers | Unsupported | Workers cannot run this Chromium/DrissionPage service. |
| Cloudflare Containers | Unsupported | The current three-service stack requires iptables/`NET_ADMIN` and durable profile storage; it is not deployable there as-is. |
| Cloudflare Tunnel | Possible ingress concept, unverified | Tunnel can proxy a server running elsewhere; it is not a Workers or Containers deployment. |

Cloud-hosted browser traffic may encounter provider-specific blocks, CAPTCHAs,
or datacenter-IP restrictions. The project does not bypass those controls.
The protocol, OAuth, browser, and image contracts have automated and local
coverage; a live end-to-end web ChatGPT account connection remains an
operator-run validation step rather than a project-wide compatibility guarantee.

## Quick local validation

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required for development.

```sh
git clone https://github.com/BK927/cloud-browser-mcp.git
cd cloud-browser-mcp
uv sync --extra browser --extra dev --frozen
uv run cloud-browser self-test
uv run cloud-browser doctor
```

`self-test` creates a temporary profile and local ports, then checks the full
authenticated MCP → worker → Chromium → image path. It does not modify your
production `.env`, cookies, accounts, or public routing. If discovery fails, use
`self-test --chromium /absolute/path/to/chromium`.

## Home-server quick start

The documented production target is one Debian 13 arm64 or amd64 server with a
security-updated Docker Engine 28+ and Compose. The bundled seccomp policy is
based on Docker Engine 29.8.0 and must be reviewed when Docker or the kernel is
upgraded.

1. Prepare the configuration and administrator password hash:

   ```sh
   cp .env.example .env
   uv sync --extra dev --frozen
   uv run cloud-browser hash-password
   ```

   Put only the resulting Argon2id hash in `.env`. Configure the exact public
   origin, private control origin, OAuth callback, and client ID. Never commit
   the real `.env` file.

2. Build and start the isolated three-service stack:

   ```sh
   docker compose up --build -d
   docker compose ps
   docker compose exec browser cloud-browser doctor
   ```

3. Keep both upstreams on loopback:

   - Public MCP and OAuth: `127.0.0.1:8000`
   - Private operator console: `127.0.0.1:8001`

4. Publish them through different access boundaries. The supported reference
   path uses Tailscale Funnel for public MCP and tailnet-only Serve for control:

   ```sh
   tailscale funnel --bg --https=443 http://127.0.0.1:8000
   tailscale serve --bg --https=8443 http://127.0.0.1:8001
   ```

   Never enable Funnel for the private control port. Review existing Tailscale
   routes before changing them.

5. Register `https://YOUR-HOST/mcp` in ChatGPT developer mode with the exact
   OAuth callback shown by ChatGPT, then verify the first MCP image response.

See [Docker deployment](docs/DEPLOYMENT.md),
[ChatGPT setup](docs/CHATGPT_SETUP.md), and
[validation](docs/VALIDATION.md) for the complete checklist. For Docker-free
Debian installation, use the [native systemd guide](docs/NATIVE_INSTALL.md).

## Architecture

```text
web ChatGPT ── public HTTPS / OAuth ── MCP :8000
                                             │
operator ── private tailnet console ── control :8001
                                             │
                 serialized worker ── DrissionPage ── Chromium
                                                        │
                                         validated public egress proxy
```

Docker runs `browser`, `egress`, and `ingress` services. The browser stays on an
internal network; ingress forwards only the two fixed loopback ports. Raw CDP,
VNC, and noVNC ports are not published. Chromium, Xvfb, VNC, and its bridge are
started only when a session or manual-control flow needs them.

## GCP Compute Engine

Compute Engine is the documented GCP option that best matches the current architecture. Use
a persistent Debian 13 VM, install a supported Docker Engine and Tailscale, then
follow the same Compose and public/private routing procedure as a home server.
Keep ports 8000/8001 off the public firewall, preserve the `browser_data` volume,
and size memory and disk for Chromium rather than using a generic microservice
default.

This path has not been integration-tested by the project. Treat it as a manual
operator deployment, validate Chromium sandboxing and cgroup limits on the
selected VM, and expect some websites to restrict cloud-datacenter egress.

Cloud Run is not a substitute: the browser service requires `NET_ADMIN`, a
custom seccomp profile, persistent login profiles, and a separate private
control listener.

## Cloudflare: runtime versus ingress

Neither Cloudflare Workers nor Cloudflare Containers can run the current stack.
Cloudflare Browser Rendering is a different Puppeteer-based service and does not
preserve this project's DrissionPage adapter, private manual-control console, or
profile lifecycle.

A Cloudflare Tunnel could be evaluated only as an HTTPS front door to a Docker
or native server that remains elsewhere. If you test it, keep public MCP/OAuth
and private control on separate access policies, preserve OAuth discovery and
callback paths, and do not describe the result as “running on Workers.” The
project's validated reference topology remains Tailscale Funnel + Serve.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `browser_open` | Open a session/tab, reuse a tab, and optionally navigate. |
| `browser_list_tabs` | List tabs in the owned session. |
| `browser_navigate` | Navigate by URL or history and refresh GET documents. |
| `browser_observe` | Return DOM-first observations, JSON nodes, images, and balanced pagination. |
| `browser_act` | Click, type, press keys, select, check, scroll, use coordinates, or upload an approved staged file. |
| `browser_auth_request` | Begin a protected user-login flow. |
| `browser_handoff` | Start private manual control and return immediately. |
| `browser_close` | Close a tab or session. |
| `browser_status` | Inspect resources, tabs, control state, and authentication progress. |
| `browser_configure` | Adjust viewport, image quality, output size, and waits within operator limits. |
| `browser_list_page_tools` | List native WebMCP tools exposed by the current document. |
| `browser_call_page_tool` | Invoke one page-provided tool after required approval. |
| `browser_wait` | Wait for URL, element, dialog, or download conditions. |
| `browser_dialog` | Inspect and answer a dialog with approval when required. |
| `browser_logs` | Return bounded, secret-free diagnostic metadata. |
| `browser_artifacts` | List, export, retrieve, or clean isolated task artifacts. |
| `browser_clipboard` | Use a task-only text buffer, separate from the OS clipboard. |

## Safety model

- The default `strict` policy asks for private approval before each browser
  mutation. `balanced` permits a bounded set of ordinary low-risk edits, links,
  and searches; submission, purchase, deletion, permission changes, sensitive
  input, and uncertain effects remain gated.
- An approval token is not itself approval. The server verifies a human decision,
  document revision, target, action, and transmitted data, then consumes the
  approval once.
- DOM, screenshot, and title collection stop for the entire session during login
  or manual control. Automation does not silently resume after control expires.
- Known sensitive input screens reject images. Inspectable ordinary frames may be
  shown; sensitive or uninspectable regions are masked and coordinate actions in
  masked regions are rejected.
- `RESULT_UNCERTAIN` is never retried automatically.
- The server does not import a user's normal Chrome profile or claim to bypass
  CAPTCHA, passkeys, security keys, or website anti-automation controls.

Read [SECURITY.md](SECURITY.md), the [external contract](docs/CONTRACT.md), and
the [approval policy](docs/APPROVAL_POLICY.md) before exposing the service.

## Development and verification

```sh
uv sync --extra browser --extra dev --frozen
uv run pytest -q -m 'not browser'
uv run ruff check src tests scripts
CB_TEST_CHROMIUM=/usr/bin/chromium uv run pytest -q -m browser
```

Real-browser tests always use new temporary Chromium profiles. CI separates
contract tests, Chromium tests, multi-architecture image builds, Docker runtime,
and native runtime checks. Passing local tests does not by itself certify a new
Docker host, cloud VM, public tunnel, or ChatGPT account connection.

## FAQ

### Does it control my normal desktop Chrome profile?

No. It launches dedicated Chromium profiles owned by this service.

### Does the server need an OpenAI or other model API key?

No. The AI client runs elsewhere; this repository provides the browser MCP
runtime only.

### Is a Raspberry Pi 4 Model B with 2 GB RAM required?

No. That exact device was benchmark and integration-test hardware only. It is
neither a recommendation nor a minimum requirement.

### Can I deploy it to Cloud Run or Cloudflare Workers?

No. Those runtimes do not match the service's isolation, capability, storage,
and private-control requirements. A persistent GCP Compute Engine VM is a manual,
unverified option; Cloudflare Tunnel is only a possible ingress to a server
running elsewhere.

## License

Original project code and documentation are available under the
[MIT License](LICENSE). Third-party components retain their own terms; review
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) before use.
