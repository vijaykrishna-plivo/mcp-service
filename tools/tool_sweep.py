#!/usr/bin/env python3
"""Strong end-to-end test suite for the whole ACI tool catalog.

Three phases, run over EVERY tool in apps/*/functions.json:

  1. STATIC  — validate each tool's schema is well-formed and LLM-callable:
               * ACI object-node rules (type/properties/required/visible/additionalProperties)
               * visible ⊆ properties, required ⊆ properties (per object)
               * the visible-filtered schema is a valid JSON Schema (Draft7 meta-schema)
               This catches tools an LLM literally could not call, independent of the network.

  2. INPUT   — build a *valid* request for each tool with a recursive instance generator
               (nested required objects, arrays, enums, formats, oneOf/anyOf), then
               self-check it against the same visible schema ACI validates against. A tool
               that still gets "Invalid function input" from ACI is a real schema problem,
               not a harness limitation.

  3. WIRING  — fire every tool through ACI (concurrently) and classify whether the request
               reached the correct provider. With fake credentials the strongest *provable*
               outcome is REACHED (provider answered, e.g. 401/404) or WORKS_200 (real 200).
               A genuine 200 for every tool needs real per-provider creds (see --owner).

Usage:  python tool_sweep.py <API_KEY> [--owner tooltest] [--apps app1,app2] [--workers 10]
Writes /tmp/tool_sweep_results.json and prints a summary.
"""
import json, glob, os, sys, copy, argparse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

try:
    import jsonschema
    from jsonschema import Draft7Validator
except Exception:
    jsonschema = None

ap = argparse.ArgumentParser()
ap.add_argument("key")
ap.add_argument("--owner", default="tooltest")
ap.add_argument("--apps", default=None, help="comma-separated subset (default: all)")
ap.add_argument("--workers", type=int, default=10)
ap.add_argument("--url", default="http://localhost:8200")
args = ap.parse_args()

KEY, U, OWNER = args.key, args.url, args.owner
APPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")
# per-connection custom fields for templated server_urls (zendesk {subdomain}, zoho {region}, ...)
CC = {"subdomain": "acmetest", "instance_url": "https://acmetest.my.salesforce.com",
      "region": "com", "site": "acmetest", "auth_id": "MATESTAUTHID0000000000",
      "account_name": "acmetest", "organizations": "acmetest", "store_name": "acmetest"}


def api(method, path, body=None, timeout=45):
    req = urllib.request.Request(U + path, data=json.dumps(body).encode() if body else None,
                                 method=method, headers={"X-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or "null")
        except Exception: return e.code, None
    except Exception as e:
        return -1, {"error": f"client:{e}"}


# ---------- visible-schema filter (mirrors ACI processor.filter_visible_properties) ----------
def filter_visible(schema):
    def f(s):
        if not isinstance(s, dict) or s.get("type") != "object":
            return s
        vis = s.get("visible", [])
        out = {k: v for k, v in s.items() if k != "visible"}
        props = s.get("properties")
        if props is not None:
            out["properties"] = {k: f(v) for k, v in props.items() if k in vis}
            if out.get("required") is not None:
                out["required"] = [k for k in out["required"] if k in vis]
        return out
    return f(copy.deepcopy(schema))


# ---------- recursive valid-instance generator over the VISIBLE schema ----------
def instance_for(s):
    if not isinstance(s, dict):
        return "test"
    if "const" in s: return s["const"]
    if "default" in s: return s["default"]
    if s.get("enum"): return s["enum"][0]
    t = s.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    # object FIRST — many schemas are {type:object, properties:{...}, oneOf:[{required:[a]},...]}
    # where oneOf only discriminates which required subset applies; the real props live here.
    if t == "object" or "properties" in s:
        props = dict(s.get("properties", {}))
        req = list(s.get("required", []))
        for uk in ("oneOf", "anyOf"):  # satisfy a discriminated union by filling its first branch
            if s.get(uk):
                br = s[uk][0] if isinstance(s[uk][0], dict) else {}
                props.update(br.get("properties", {}))
                req += [r for r in br.get("required", []) if r not in req]
                break
        return {k: instance_for(props.get(k, {"type": "string"})) for k in req}
    for k in ("oneOf", "anyOf"):  # non-object unions (scalar choices)
        if s.get(k):
            return instance_for(s[k][0])
    if s.get("allOf"):
        return instance_for(s["allOf"][0])
    if t == "array":
        items = s.get("items", {})
        return [instance_for(items)] if (s.get("minItems", 0) >= 1 or items) else []
    if t in ("integer", "number"):
        return s.get("minimum", 1) if isinstance(s.get("minimum"), (int, float)) else 1
    if t == "boolean":
        return True
    fmt = s.get("format", "")
    return {"date-time": "2026-01-01T00:00:00Z", "date": "2026-01-01", "email": "test@example.com",
            "uri": "https://example.com", "url": "https://example.com",
            "uuid": "00000000-0000-0000-0000-000000000000"}.get(fmt, "test")


def build_input(fn):
    """Build a complete, valid request from the visible schema (ACI validates against it)."""
    vis = filter_visible(fn["parameters"])
    inp = {}
    props = vis.get("properties", {})
    top_req = vis.get("required", [])
    for loc, grp in props.items():
        if isinstance(grp, dict) and grp.get("type") == "object" and grp.get("properties") is not None:
            built = instance_for(grp)
            if loc in top_req or built:  # required group, or has required children
                inp[loc] = built
        elif loc in top_req:  # flat leaf required at top level
            inp[loc] = instance_for(grp)
    return inp, vis


# ---------- static schema validation ----------
def static_check(fn):
    errs = []
    def walk(node, p):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            for k in ("properties", "required", "visible", "additionalProperties"):
                if k not in node:
                    errs.append(f"{p}: object missing '{k}'")
            props = node.get("properties", {})
            for v in node.get("visible", []) or []:
                if v not in props:
                    errs.append(f"{p}: visible '{v}' not in properties")
            for r in node.get("required", []) or []:
                if r not in props:
                    errs.append(f"{p}: required '{r}' not in properties")
            for ck, cv in props.items():
                walk(cv, f"{p}.{ck}")
    walk(fn["parameters"], fn["name"])
    if jsonschema:
        try:
            Draft7Validator.check_schema(filter_visible(fn["parameters"]))
        except Exception as e:
            errs.append(f"invalid JSON Schema: {str(e).splitlines()[0][:80]}")
    return errs


# ---------- live classification ----------
NET = ["getaddrinfo", "name or service", "nodename", "all connection attempts", "connection refused",
       "failed to resolve", "temporary failure in name", "name not known", "[errno", "ssl:",
       "connecterror", "connect call failed", "no address associated", "timed out", "read timeout"]
INPUTM = ["invalid function input", "is a required property", "is not valid under", "does not match",
          "is not of type", "additional properties are not allowed", "validation error for"]
SETUP = ["linked account", "not allowed for this agent", "subscription plan", "quota",
         "no implementation", "app configuration for app", "not enabled"]


def classify(est, body):
    if est == -1:
        return "CLIENT_ERR", str(body)[:140]
    if est == 200 and isinstance(body, dict):
        if body.get("success") is True:
            return "WORKS_200", "real 200 from provider"
        err = str(body.get("error") or "")
        low = err.lower()
        if any(m in low for m in NET): return "URL_NETWORK", err[:160]
        if any(m in low for m in INPUTM): return "INPUT", err[:160]
        if any(m in low for m in SETUP): return "SETUP", err[:160]
        if "{" in err and "}" in err and any("{" + k + "}" in err for k in CC): return "TEMPLATE", err[:160]
        return "REACHED", err[:160]
    if est == 400: return "INPUT", str((body or {}).get("error", body))[:160]
    if est == 404 and isinstance(body, dict) and "linked account" in str(body.get("error", "")).lower():
        return "SETUP", str(body.get("error"))[:160]
    if est in (401, 403, 429): return "SETUP", str((body or {}).get("error", body))[:160]
    return "OTHER", f"est={est} body={str(body)[:120]}"


# ================= run =================
want = set(args.apps.split(",")) if args.apps else None
apps = []
for p in sorted(glob.glob(f"{APPS_DIR}/*/app.json")):
    d = os.path.basename(os.path.dirname(p))
    if want and d not in want:
        continue
    a = json.load(open(p))
    apps.append((d, a["name"], list(a["security_schemes"].keys())[0]))

# --- link one account per app ---
linked = 0
for d, name, scheme in apps:
    if scheme == "oauth2":
        st, resp = api("POST", "/v1/linked-accounts/oauth2-import",
                       {"app_name": name, "linked_account_owner_id": OWNER, "access_token": "faketoken",
                        "refresh_token": "fakerefresh", "connection_config": CC})
    else:
        api("POST", "/v1/app-configurations",
            {"app_name": name, "security_scheme": "api_key", "all_functions_enabled": True, "enabled_functions": []})
        st, resp = api("POST", "/v1/linked-accounts/api-key",
                       {"app_name": name, "linked_account_owner_id": OWNER, "api_key": "faketoken", "connection_config": CC})
    linked += st in (200, 201) or (isinstance(resp, dict) and "already exists" in str(resp.get("error", "")).lower())
print(f"linked accounts ready: {linked}/{len(apps)} apps", flush=True)

# --- load every tool + static check ---
tools = []
for d, name, scheme in apps:
    for fn in json.load(open(f"{APPS_DIR}/{d}/functions.json")):
        tools.append((d, fn))
print(f"catalog: {len(tools)} tools across {len(apps)} apps", flush=True)

# phase 1: static
static_bad = []
for d, fn in tools:
    e = static_check(fn)
    if e:
        static_bad.append({"app": d, "tool": fn["name"], "errs": e})

# phase 2+3: build input (self-validate) + fire, concurrently
def run_one(item):
    d, fn = item
    inp, vis = build_input(fn)
    self_valid = True
    if jsonschema:
        try:
            jsonschema.validate(inp, vis)
        except Exception:
            self_valid = False
    est, body = api("POST", f"/v1/functions/{fn['name']}/execute",
                    {"function_input": inp, "linked_account_owner_id": OWNER})
    cls, snip = classify(est, body)
    return {"app": d, "tool": fn["name"], "class": cls, "detail": snip, "input_self_valid": self_valid}

results = []
with ThreadPoolExecutor(max_workers=args.workers) as ex:
    for i, r in enumerate(ex.map(run_one, tools), 1):
        results.append(r)
        if i % 50 == 0:
            print(f"  fired {i}/{len(tools)}", flush=True)

json.dump({"results": results, "static_bad": static_bad}, open("/tmp/tool_sweep_results.json", "w"), indent=1)

# ================= summary =================
from collections import Counter
by_class = Counter(r["class"] for r in results)
wired = by_class.get("REACHED", 0) + by_class.get("WORKS_200", 0)
builder_bad = [r for r in results if not r["input_self_valid"]]
print("\n==================== TOOL SUITE SUMMARY ====================")
print(f"tools tested:            {len(tools)}")
print(f"[1] schema valid:        {len(tools)-len(static_bad)}/{len(tools)}   (LLM-callable)")
print(f"[2] input builder valid: {len(tools)-len(builder_bad)}/{len(tools)}   (harness builds a schema-valid request)")
print(f"[3] reached provider:    {wired}/{len(tools)}  ({100*wired//max(len(tools),1)}%)  [REACHED {by_class.get('REACHED',0)} + WORKS_200 {by_class.get('WORKS_200',0)}]")
print("\nexecution breakdown:")
for c, n in by_class.most_common():
    print(f"   {c:12} {n}")
if static_bad:
    print(f"\nSCHEMA PROBLEMS ({len(static_bad)}) — these tools may be uncallable:")
    for r in static_bad[:25]:
        print(f"   {r['tool']}: {r['errs'][0]}")
flagged = [r for r in results if r["class"] in ("URL_NETWORK", "TEMPLATE", "INPUT", "OTHER", "CLIENT_ERR")]
print(f"\nWIRING ISSUES TO REVIEW ({len(flagged)}):")
for r in flagged[:40]:
    print(f"   [{r['class']}] {r['tool']}: {r['detail']}")
