# CITADEL

> **Type:** standalone service dashboard with an optional OpenClaw plugin.
>
> The FastAPI dashboard and scanner run on bare metal. The release ZIP is an
> OpenClaw plugin package, not a generic bare-metal installer.

[![OpenClaw plugin](https://github.com/safrano9999/CITADEL/actions/workflows/openclaw-plugin-release.yml/badge.svg)](https://github.com/safrano9999/CITADEL/actions/workflows/openclaw-plugin-release.yml)

![CITADEL dashboard](CITADEL.png)

CITADEL discovers listening TCP services, identifies HTTP endpoints, and builds
one dashboard for local, subnet, Tailscale, and Cloudflare routes. Providers are
reconciled independently, so local discovery remains useful even when a remote
provider is disabled or unavailable.

## Features

- Discovers listeners with `ss` and probes HTTPS before HTTP.
- Recognizes HTML services, generic HTTP services, and OpenAI-compatible
  `/v1/models` endpoints.
- Produces deterministic service metadata in `services.json`.
- Presents discovered services in a FastAPI dashboard.
- Supports localhost, subnet, Tailscale Serve, and Cloudflare providers.
- Rebuilds all Tailscale Serve routes as same-port HTTPS; preserves unrelated Cloudflare resources.
- Supports port allowlists, blocklists, Cloudflare hostnames, and Access email
  policies.
- Exposes the same route data through the `/citadel` OpenClaw command.

## Supported deployment modes

| Mode | Status | What is provided |
|---|---|---|
| Bare metal | **Supported** | Scanner, FastAPI dashboard, configuration scripts, and a user-systemd installer |
| OpenClaw | **Supported** | Optional release ZIP with the `/citadel` command and scan integration |
| Hermes | **Not provided** | This repository contains no Hermes plugin, hook, or manifest |

CITADEL can discover a running Hermes service like any other listener. That is
service discovery, not a native Hermes integration.

## Releases

The [latest release](https://github.com/safrano9999/CITADEL/releases/latest)
contains:

- [`citadel-latest.zip`](https://github.com/safrano9999/CITADEL/releases/download/latest/citadel-latest.zip)
  · [SHA-256](https://github.com/safrano9999/CITADEL/releases/download/latest/citadel-latest.zip.sha256)

This ZIP is assembled and validated as an **OpenClaw plugin package**. For a
bare-metal installation, clone the source repository instead.

## Bare-metal installation

Requirements:

- System Python 3 and distribution-packaged Python dependencies (no virtual environment)
- `curl`
- `ss` from `iproute2`
- `flock` from `util-linux`
- Tailscale CLI only when the Tailscale provider is enabled

Install dependencies through the operating system's package manager. On Fedora:

```bash
sudo dnf install python3-fastapi 'python3-uvicorn+standard' python3-jinja2 python3-dotenv
```

On Fedora CoreOS/uCore, use persistent package layering instead of `dnf`:

```bash
sudo rpm-ostree install --idempotent --allow-inactive \
  python3-fastapi 'python3-uvicorn+standard' python3-jinja2 python3-dotenv
```

Activate the resulting deployment before switching the service. Package-only
additions can use `--apply-live` when no conflicting pending changes exist;
otherwise plan a reboot. Do not force replacements or overwrite RPM-managed
Python modules with `sudo pip`. Other distributions should install the packages
corresponding to `requirements.txt` through their own package manager.

Clone the source and configure it:

```bash
git clone https://github.com/safrano9999/CITADEL.git
cd CITADEL
./config.sh --no-container
```

Run one scan, then start the dashboard:

```bash
./scan.sh
/usr/bin/python3 -s webui.py
```

The default example binds the dashboard to `127.0.0.1:11000`.

### User systemd service

`set_daemon.sh` checks the system dependencies, writes and links the unit, then
enables and restarts `citadel.service`. It defaults explicitly to
`/usr/bin/python3`, regardless of an activated environment in `PATH`, and uses
`-s` to ignore per-user Python packages. `PYTHON_BIN` can select a different
system interpreter when required.

```bash
./set_daemon.sh
systemctl --user status citadel.service
```

The installer also attempts to enable user lingering when it is available.
`CITADEL_WEBUI_TRANSPORT=tcp` is the default, including an unset or empty
value. Explicitly select `unix` to serve the same WebUI over a Unix socket,
on either the host or inside a container. `CITADEL_WEBUI_SOCKET` selects its
absolute path; empty uses `$XDG_RUNTIME_DIR/citadel/citadel.sock`, or
`/run/citadel/citadel.sock` without a runtime directory. The web server creates
the socket; Podman can share its directory using a bind mount. A browser still
needs a TCP/HTTP(S) proxy in front of a socket-only WebUI.

The WebUI transport does not enable, disable, or otherwise control Caddyfile
generation. Neither transport requires Caddy, and neither starts it.

For an existing HTTPS reverse proxy, point its upstream at the dashboard's HTTP
port. `CITADEL_HIDE_HTTP_WEBUI_DUPE=1` (default) hides only the duplicate HTTP
tile when the same WebUI is simultaneously reachable through a scanned HTTPS
frontend. There is no HTTPS-port setting, JSON mapping, or Caddy dependency.
Discovery compares the WebUI's per-process `X-Citadel-Instance` response header,
so another container's Citadel cannot hide this instance by sharing its title.
Only successful, non-redirected responses count. The marker is not an auth token
and is not persisted. This is a local discovery check, not public TLS verification.
The rule is recomputed each scan; if HTTPS disappears, is filtered out, or fails,
HTTP appears again. Setting the option to `0` keeps both tiles. The HTTP backend
remains available for route providers; no blacklist or firewall rule is changed.
A containerized proxy must be able to reach the host bind address; its own
`127.0.0.1` is not the host's loopback.

## Containerfile and container definitions

Render the image recipe without building or starting anything:

```bash
./setup.sh --render-containerfile
```

This copies `Containerfile.example` to `Containerfile`. A matching file is left
unchanged; move a custom or outdated Containerfile aside before regenerating.
Ordinary `./setup.sh` retains its configuration/service-rendering workflow.

Use `./config.sh` to configure a container, or regenerate only Compose and
Quadlet from existing configuration with `./config.sh --render-container`.
The `#container-command: image` directive in `container.example` makes both
use the image's `/usr/bin/python3 -s webui.py` command, so runtime TCP/Unix
selection works without a Uvicorn command override. Regenerate older container
definitions to remove that override. Neither rendering command builds, pushes,
or starts an image; runtime secrets and scan state are not copied into the image.

## OpenClaw installation

Download and verify the public release package:

```bash
curl -fL \
  -o citadel-latest.zip \
  https://github.com/safrano9999/CITADEL/releases/download/latest/citadel-latest.zip
curl -fL \
  -o citadel-latest.zip.sha256 \
  https://github.com/safrano9999/CITADEL/releases/download/latest/citadel-latest.zip.sha256
sha256sum -c citadel-latest.zip.sha256
openclaw plugins install ./citadel-latest.zip \
  --force \
  --dangerously-force-unsafe-install
openclaw gateway restart
```

The plugin can use its packaged scanner and state, or it can point to an
existing bare-metal CITADEL checkout:

```json
{
  "plugins": {
    "entries": {
      "citadel": {
        "enabled": true,
        "config": {
          "servicesPath": "/opt/citadel/services.json",
          "scanScript": "/opt/citadel/scan.sh"
        }
      }
    }
  }
}
```

Available commands:

```text
/citadel
/citadel localhost
/citadel subnet
/citadel tailscale
/citadel cloudflare
/citadel other
/citadel scan
```

The plugin does not launch the FastAPI dashboard. It reads CITADEL state,
renders provider buttons, and can run the configured scanner.

## Configuration

`config.sh --no-container` renders local configuration from
`config.conf_example` and `env.example`.

| Setting | Example default | Purpose |
|---|---:|---|
| `CITADEL_WEBUI_TRANSPORT` | `tcp` | `tcp` or explicit `unix`, on host and in containers |
| `CITADEL_WEBUI_SOCKET` | empty | Optional absolute socket path for Unix transport |
| `FASTAPI_HOST` | `127.0.0.1` | Dashboard bind address |
| `CITADEL_WEBUI_PORT` | `11000` | Dashboard port |
| `CITADEL_HIDE_HTTP_WEBUI_DUPE` | `1` | Hide this WebUI's HTTP tile only while the same instance also responds successfully over HTTPS |
| `CADDYFILE_START` | empty | First generated HTTPS frontend port; empty/0 disables export |
| `CADDYFILE_STEPS` | `1` | Exported port increment; empty also means 1 |
| `CITADEL_TOKEN` | generated | Optional token protecting Cloudflare edits in the dashboard |
| `CITADEL_SUBNET_IP` | empty | Address used for subnet routes and the Cloudflare origin |
| `CITADEL_HTTPS_ONLY` | `0` | When enabled, route only services that already speak HTTPS on localhost; HTTP services remain visible |
| `CITADEL_PERSISTENT` | `0` | Reset/rebuild Tailscale Serve routes per scan; 1 retains routes and enables policy/state persistence |
| `CITADEL_LOGO_PERSISTENT` | `1` | Retain logos independently; service metadata is always rescanned |
| `CITADEL_USER_AGENT` | `Mozilla/5.0 (compatible; CITADEL/1.0)` | HTTP probe user agent |
| `CITADEL_CLOUDFLARE_DOMAIN` | empty | DNS suffix used for generated hostnames |
| `CITADEL_CLOUDFLARE_WWW443` | `0` | Add `www.<domain>` for Cloudflare port 443 |
| `CITADEL_CLOUDFLARE_DOMAIN443` | `0` | Add the bare domain for Cloudflare port 443 |
| `CITADEL_CLOUDFLARE_ACCOUNT_ID` | empty | Existing Cloudflare account ID |
| `CITADEL_CLOUDFLARE_ZONE_ID` | empty | Existing Cloudflare zone ID |
| `CITADEL_CLOUDFLARE_TUNNEL_ID` | empty | Existing named Tunnel ID |
| `CLOUDFLARE_API_TOKEN` | empty | Scoped Cloudflare API token |
| `CLOUDFLARE_EMAIL` | empty | Default Access email allowlist |
| `TUNNEL_TOKEN` | empty | Token consumed by the separately managed connector |

Non-secret service settings belong in `config.conf`. Secrets belong in `.env`,
which is ignored by Git.

During interactive configuration, `CITADEL_TOKEN` offers three choices: no
token, enter a token, or generate one with `openssl rand -hex 32` (the default).
An empty or `blank` value disables the prompt in the dashboard. When configured,
the token is required to enter Cloudflare edit mode and to save Cloudflare
rules. Five invalid attempts within five minutes lock that client out for
15 minutes.

An optional `config.ini` selects a custom CA:

```ini
[CITADEL]
ca_cert = /path/to/certs/ca.pem
```

### Port policy

`ports.filter.json` is created during the first scan. Start from
`ports.filter.json.example` when a policy should be prepared in advance:

```json
{
  "whitelist": [],
  "blacklist": [4000, "5000-5010"],
  "cloudflare": {
    "11000": {
      "subdomains": ["citadel"],
      "whitelist": true,
      "emails": ["operator@example.net"]
    }
  }
}
```

A non-empty whitelist takes precedence. Otherwise the blacklist is applied.

### Persistent Fedora container state

The merged Fedora container setup asks for `CITADEL_PERSISTENT`, default `0`.
When explicitly enabled, the generated container mounts the instance-specific
named volume `<container>-citadel` at `/named_volumes/CITADEL`.

The volume stores only mutable runtime state. Provider code and configuration
remain in the installed plugin directory, so image updates are never hidden by
the volume. Links generated during container initialization migrate existing
files once where present and are safe to recreate. The initially absent
`tailscale.json` uses a direct link so its first atomic write creates valid JSON
instead of an empty placeholder:

```text
ports.filter.json
extensions/enabled/cloudflare/routes.json
tailscale.json
```

Without persistence, CITADEL reads and writes these paths directly below its
own plugin directory. With persistence, atomic replacements resolve the link
target and stay inside the named volume.

Logos are independent: `CITADEL_LOGO_PERSISTENT=1` retains `icons/` between
scans and adds a separate `<container>-citadel-logos` volume in generated
container configurations. Setting it to `0` refreshes icons during each scan
and omits that volume. Listener discovery, titles and routes are rebuilt on
every scan; logo reuse never skips probing a service.

## Extensions: URL providers and file exporters

Provider activation is directory based:

```text
extensions/enabled/<provider>/
extensions/disabled/<provider>/
```

`extension.json` describes an extension; directory placement controls whether
it participates. URL providers supply dashboard links. An extension with
`"kind": "export"` generates files instead: it never appears in the provider
dropdown or supplies tile URLs. Export status is recorded in `status.json`,
separately from URL providers' `routes.json`.

### Caddyfile export

`extensions/enabled/caddy/` is an optional, file-only export extension. Set:

```dotenv
CADDYFILE_START=4000
CADDYFILE_STEPS=1
```

An unset, empty, or zero start disables generation; no output directory is
created. Empty steps use 1. The extension reads the current service scan and
writes `caddyfile/Caddyfile`, independently of the WebUI's `tcp`/`unix`
transport. It never starts/reloads Caddy, changes Quadlets, opens ports, calls
Podman, or issues certificates. No running Caddy is required for generation.

Configure the addresses visible **from Caddy** in
`extensions/enabled/caddy/config.json`, for example:

```json
{
  "backend": "fedora44-ai-safrano9999-ucore",
  "hosts": ["ucore.tailbab54f.ts.net"]
}
```

The default `127.0.0.1` backend and `localhost` frontend are for a same-host
deployment. A Caddy in a separate container needs a backend name reachable
from its Podman network; its own localhost is not the source container.
Use the **Caddy host's** real Tailscale DNS name for original Tailscale
certificates, not a different container's Tailscale name. For `.ts.net`, Caddy
obtains the official certificate from that node's Tailscale daemon at the TLS
handshake; give the consuming Caddy access to the daemon socket and certificate
permissions. Both Caddys on a node can use the same daemon and certificate.
The generated file does not embed certs,
keys, self-signed workarounds, or disable HTTPS upstream verification.

Backend ports receive sequential slots beginning at START, incremented by
STEPS. `caddyfile/ports.json` remembers only this numeric mapping: newly
discovered ports append, disappeared ports keep their slots, and titles,
icons, and process identities are not used to recognize services. Preserve
this directory across container recreation. Changing START/STEPS requires
explicitly moving the old mapping aside; it never silently renumbers.
The generated Caddyfile contains only currently discovered services, including
HTTP backends (its frontends are HTTPS). An empty scan produces an empty
import while retaining allocated slots. Malformed scan/config/state does not
overwrite the last successful Caddyfile. Unchanged output is not rewritten.

Optionally bind-mount the **directory**, not the individual generated file,
read-only into Caddy, e.g. in its Quadlet:

```ini
Volume=/absolute/host/path/citadel/caddyfile:/etc/caddy/ucore:ro,z
PublishPort=4000-4099:4000-4099
```

For a containerized generator, bind the same host directory read-write to
its `citadel/caddyfile` path. This also persists the mapping. The published
range is an operator choice, not managed by Citadel; use nonoverlapping
ranges for multiple exporters. Check Unix ownership and SELinux labels for
the shared directory. Import `/etc/caddy/ucore/Caddyfile` in Caddy and validate
then reload it after changes. An atomic replacement is visible through the
directory mount. Disabling export stops writing; it does **not** delete an
existing mounted file or remove Caddy routes. Remove the import separately
when decommissioning it.

The sequence is discovery → Caddyfile generation → consuming Caddy validates
and reloads. A restart is not normally necessary. Tailscale and Cloudflare can
process the same initial discovery without waiting for Caddy. Newly published
Caddy ports appear in the next host scan; they must not be advertised as live
merely because a file was generated. Keep the exporter on the source instance,
not on a host scanning its own generated frontends (which would feed them back
into the export).

`./scan.sh --provider caddy` scans and runs only this export extension.

### Live ucore refresh through an init bind mount

`image/init/citadel-refresh.py` shallow-clones `f24sales/citadel` main into
`/opt/safrano9999/CITADEL`. Install it as `00-refresh.py` in the existing
bind-mounted init directory, alongside these instance-specific inputs:

- `instance.conf`: service environment, including `CADDYFILE_START=4000`,
  `CADDYFILE_STEPS=1`, `CITADEL_PERSISTENT=0`, and `CITADEL_LOGO_PERSISTENT=1`.
- `export-config.json`: the consuming Caddy's hostnames and the source
  container's reachable Podman-network name as `backend`.
- `service.conf`: a systemd drop-in with `After=fedora45-ai-init-hooks.service`
  in `[Unit]` and the absolute container-side `EnvironmentFile=.../instance.conf`
  in `[Service]`.
- `caddyfile/`: persistent generated output, shared read-only with Caddy.

Install the ordering drop-in for both Citadel units **before boot**; the hook
cannot reorder an already started WebUI. On recreation, provide that drop-in
through the image or a bind mount too. The refresh preserves instance secrets,
policy, logos, known disabled extension selections and runtime volume links,
but replaces the code tree completely. Previous trees remain as backups.
Missing Python dependencies are installed as Fedora packages, never in a venv.

For an explicit live refresh, run `python3 -s /path/to/00-refresh.py --live`
inside the container. It refuses an active scan, validates the clone first,
and restarts only Citadel's WebUI. It does not scan, reset routes, restart
Caddy, recreate the container or build an image. Run the subsequent scan and
Caddy validation/reload separately.

### Localhost and subnet

- `localhost` maps services to `127.0.0.1:<port>`.
- `subnet` maps services to `CITADEL_SUBNET_IP:<port>`.

### Tailscale

CITADEL owns the node's Tailscale Serve configuration when the Tailscale
extension is in `extensions/enabled` and its manifest is enabled. There is no
separate environment switch. It checks the daemon and makes one bounded,
noninteractive start attempt when necessary, then checks again. Missing CLI,
login requirements or failed startup produce an error instead of retry loops
or fabricated URLs.

With `CITADEL_PERSISTENT=0` (default), after validating discovery and the running
node, every Tailscale scan runs
`tailscale serve reset` and rebuilds the currently discovered ports 1:1 using
**HTTPS only**. This deliberately replaces manual/foreign Serve routes too,
including their HTTP handlers and Funnel configuration. Existing connections
may be interrupted during a rebuild. Do not share this node's Serve configuration
with another route manager.

With `CITADEL_PERSISTENT=1`, existing matching HTTPS routes are reused instead
of resetting the node. Conflicting routes on discovered ports are replaced.
Unobserved ports stay configured but are not advertised as current dashboard
services. Neither mode reuses stale service discovery or needs an ownership
ledger.

Both HTTP on `127.0.0.1:4096` and HTTPS on `127.0.0.1:2000` receive HTTPS on
`<node>.ts.net:4096` and `<node>.ts.net:2000`, respectively. There is no public
HTTP fallback, second port range, ownership ledger, or service identity matching.
An internal HTTP backend is still allowed. HTTPS backends remain encrypted on
loopback; Serve's local `https+insecure` target accepts their local certificates.
The **public** certificate is the official node certificate managed and renewed
by Tailscale Serve. Citadel does not generate or copy certificate keys.

**Restart constraint on TUN-mode nodes:** bind backends to explicit non-Tailscale
addresses, not `0.0.0.0` or `[::]`, when Serve uses the same port. Peer HTTPS can
work while a wildcard backend is running, but after it stops, tailscaled can
claim the Tailscale-IP listener and prevent that backend from restarting with
`EADDRINUSE`. This also applies to wildcard Podman-published ports. A successful
peer probe or index health result does not prove restart safety.
[Tailscale's local listener implementation](https://github.com/tailscale/tailscale/blob/v1.98.8/ipn/ipnlocal/serve.go#L104-L113)
explains the separate host-level listener. Userspace networking avoids those
listeners but changes normal tailnet networking; choose that mode deliberately.
Citadel does not silently change daemon modes or rebind unrelated services.

The port filter and `CITADEL_HTTPS_ONLY` still decide which local services enter
the scan; the latter filters locally HTTP-speaking backends, not the public
Tailscale scheme. A failed reset stops reconstruction; failed route applications
are reported without a dashboard URL. Adding or removing a service never
renumbers the other ports.

`./scan.sh --provider tailscale` performs discovery and reconciles only
Tailscale, without calling Cloudflare or subnet providers.

### HTTPS and Caddy

Caddy is optional. Quadlets and Caddy own the published host ports; Citadel's
host scan consumes those listeners 1:1. The optional Caddy export extension
can generate a separate container's frontend mapping, but never changes
container networking or requires a Caddy installation.

By default the dashboard serves HTTP on `127.0.0.1:11000`. If Caddy exposes the
same instance over HTTPS, `CITADEL_HIDE_HTTP_WEBUI_DUPE=1` automatically hides its
duplicate HTTP tile while both endpoints work. Without working HTTPS, the HTTP
dashboard stays visible and usable.

For a Tailscale hostname, Caddy obtains the original node certificate from
the local Tailscale daemon. Two Caddys serving the same node hostname should
use the same daemon, with access to its socket and certificate permissions;
do not substitute a locally generated certificate for that hostname.
Certificates for localhost or IP addresses are separate.

Allow the scanning user to manage Tailscale without running every scan as root:

```bash
sudo tailscale set --operator="$USER"
./scan.sh
```

Release selected Serve ports with:

```bash
./unroute.sh 11000
```

With no port arguments, `unroute.sh` uses `CITADEL_WEBUI_PORT`. It never runs a
global Tailscale Serve reset.

### Cloudflare

Cloudflare runs when its extension is in `extensions/enabled`, its manifest is
enabled, and `CLOUDFLARE_API_TOKEN` is configured. There is no separate activation
variable. The token is verified and account, zone, Tunnel, domain and Access
settings are validated before route changes. Missing tokens skip the provider;
invalid tokens or incomplete required configuration are reported. It manages:

- DNS records for discovered services;
- ingress entries on an existing named Tunnel;
- optional Cloudflare Access email policies.

It preserves unrelated DNS records, Access resources, and Tunnel ingress
rules. See [CITADEL_CLOUDFLARE.md](CITADEL_CLOUDFLARE.md) for the required API
permissions and provider-specific setup.

For port 443, the local Caddy site block must include both `<domain>:443` and
`www.<domain>:443` when both hostname flags are enabled. Reload or restart Caddy
after changing the site block; CITADEL manages the Cloudflare DNS and Tunnel routes.

## Operations

Run or repeat discovery:

```bash
./scan.sh
```

The scan updates:

```text
ss.json
services.json
cache/<port>.json
extensions/providers_state.json
extensions/enabled/<provider>/routes.json
tailscale.json
last_scan.txt
```

The dashboard does not execute scans. Cloudflare edits made in the dashboard
are saved immediately and applied by the next `./scan.sh` CLI run.

Inspect the user service:

```bash
systemctl --user status citadel.service
journalctl --user -u citadel.service
```

## Security and storage

- Keep the dashboard bound to `127.0.0.1` unless remote exposure is deliberate.
- Treat `.env`, Cloudflare tokens, Tunnel tokens, and private CA material as
  secrets.
- Give the Cloudflare token only the account- and zone-scoped permissions
  described in `CITADEL_CLOUDFLARE.md`.
- The scanner records listener metadata and service titles. Protect the
  repository directory if that inventory is sensitive.
- Downloaded service icons are limited to 1 MiB, passive image formats, and
  the same host and port as the discovered service.
- Dashboard templates use automatic HTML escaping.
- Runtime JSON is written through durable temporary files and atomically
  replaced so readers never observe a partial update.
- Runtime state, caches, generated routes, local configuration, and release
  archives are excluded by `.gitignore`.
- Without Fedora container persistence, back up `ports.filter.json` and
  provider state when custom routes must survive a fresh checkout.

## Development and checks

Install dependencies, then run the same source checks used by the release
workflow:

```bash
/usr/bin/python3 -s -m unittest discover -s tests
node --check index.js
bash -n scan.sh
bash -n unroute.sh
```

The release workflow builds `citadel-latest.zip`, writes its SHA-256 file, and
publishes both only for a version tag.

## Health: index view and external reachability

`/healthz` is a separate page with ✅ / ❌ / ⏭️ indicators. The existing
service overview is unchanged. `/api/health?extensions=tailscale,cloudflare`
returns the same **index state** as JSON (`schema_version: 1`). Health only reads
existing index files, provider routes and caches. It never starts a scan,
executes a provider, changes routing, or writes to the index. `last_index_at`
is read from the existing `last_scan.txt` timestamp; it is shown without claiming
that old data describes the current deployment. No implicit age limit is applied.

An extension absent or unconfigured in the index is `SKIP`. A configured but
unavailable extension, failed provider or broken route/cache is `FAIL`. If no
extension can be tested, the result is `NOT_TESTED`, never a successful gate.
The endpoint defaults to showing all enabled extensions; its `extensions`
parameter restricts the selection. The CLI requires an explicit selection.

For file exporters, health verifies that generated artifacts exist under the
Citadel directory and match the last successful export's SHA-256. The Caddy
export also permits `caddyfile/` itself to link to a shared output directory;
the generated file must remain inside that directory. No dropdown
URLs are required. This verifies the exported files, **not** whether an
external Caddy has imported them, reloaded, or can reach its backends. The
standalone checker does not turn file exporters into HTTP probe jobs.

### Standalone checker (also suitable for n8n SSH nodes)

Download the standard-library-only checker from the Citadel instance being tested:

```bash
CITADEL_URL='https://your-container.your-tailnet.ts.net:10002'
curl --fail --show-error --silent "$CITADEL_URL/healthz/check.py" \
  --output citadel-health-check.py.new &&
  mv citadel-health-check.py.new citadel-health-check.py
python3 citadel-health-check.py --url "$CITADEL_URL" \
  --extensions tailscale cloudflare
```

Only the supplied extensions are checked. Do not pass `localhost` from another
machine: those addresses refer to the checker machine. The CLI first requires
HTTP 200 from Citadel's dashboard, then reads its selected index entries and
checks their URLs from the caller's network. It does not call `systemctl` on the
caller, which would say nothing about Citadel inside a remote container.

Tailscale requires HTTP 200. Cloudflare requires HTTP 200 **and an identifiable
Cloudflare Access login form** on its HTTPS `*.cloudflareaccess.com` login route.
An arbitrary successful page, a challenge page or a denial page is insufficient.
No credentials are submitted and the application behind Access is **NOT_TESTED**.
The checker never scans, logs in, retries, or exposes URL query tokens in results.

Output is a single JSON object with `status`, `gates.citadel_self`,
`gates.citadel_links`, `extensions`, `results`, and `last_index_at`.
Exit code 0 means PASS, 1 means a failed/incomplete check, 2 means invalid CLI
arguments. If Citadel itself is unreachable, extension checks remain NOT_TESTED.
Default per-request socket timeout: 5 seconds, up to 8 concurrent route checks.
A workflow should also set its overall command timeout. Download failure must
stop the gate rather than executing an older cached checker.
