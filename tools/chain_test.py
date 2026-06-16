#!/usr/bin/env python3
"""Chained real-data test for servers that already have a working linked account
under owner='live'. Two things beyond the flat sweep:

  1. READ-CHAINING — fire every tool; from the successful responses harvest real
     ids (repo full_name, project_id, page_id, issue number, sha, ...) and RE-FIRE
     the tools that failed only because they were given a placeholder id.
  2. WRITE-LIFECYCLES — explicit create -> verify -> DELETE flows (auto clean-up)
     for the servers where that is safe (neon project + api-key, linear issue).

Keys are already linked in ACI (owner=live); no secrets live in this file.
Run: python tools/chain_test.py
"""
import json, copy, urllib.request, urllib.error
from collections import defaultdict

U = "http://localhost:8200"
KEY = open("/tmp/aci_api_key").read().strip()
OWNER = "live"
APPS = "/Users/vijay.krishna/Desktop/plivo-main/aci-mcp-server/apps"
READ_SERVERS = ["github", "notion", "neon", "supabase", "cal", "calendly"]


def api(method, path, body=None, timeout=40):
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


def execute(name, inp):
    st, body = api("POST", f"/v1/functions/{name}/execute",
                   {"function_input": inp, "linked_account_owner_id": OWNER})
    ok = st == 200 and isinstance(body, dict) and body.get("success") is True
    data = body.get("data") if isinstance(body, dict) else None
    err = "" if ok else (str(body.get("error"))[:120] if isinstance(body, dict) else str(body)[:120])
    return ok, data, err


def filter_visible(schema):
    def f(s):
        if not isinstance(s, dict) or s.get("type") != "object":
            return s
        out = {k: v for k, v in s.items() if k != "visible"}
        props = s.get("properties")
        if props is not None:
            out["properties"] = {k: f(v) for k, v in props.items() if k in s.get("visible", [])}
            if out.get("required") is not None:
                out["required"] = [k for k in out["required"] if k in s.get("visible", [])]
        return out
    return f(copy.deepcopy(schema))


def instance_for(s):
    if not isinstance(s, dict): return "test"
    if "const" in s: return s["const"]
    if "default" in s: return s["default"]
    if s.get("enum"): return s["enum"][0]
    t = s.get("type")
    if isinstance(t, list): t = next((x for x in t if x != "null"), "string")
    if t == "object" or "properties" in s:
        props = dict(s.get("properties", {})); req = list(s.get("required", []))
        for uk in ("oneOf", "anyOf"):
            if s.get(uk):
                br = s[uk][0] if isinstance(s[uk][0], dict) else {}
                props.update(br.get("properties", {})); req += [r for r in br.get("required", []) if r not in req]
                break
        return {k: instance_for(props.get(k, {"type": "string"})) for k in req}
    for k in ("oneOf", "anyOf"):
        if s.get(k): return instance_for(s[k][0])
    if t in ("integer", "number"): return 1
    if t == "boolean": return True
    if t == "array":
        items = s.get("items", {})
        return [instance_for(items)] if (s.get("minItems", 0) >= 1 or items) else []
    fmt = s.get("format", "")
    return {"date-time": "2026-01-01T00:00:00Z", "date": "2026-01-01", "email": "t@example.com",
            "uri": "https://example.com", "uuid": "00000000-0000-0000-0000-000000000000"}.get(fmt, "test")


def build_input(fn):
    vis = filter_visible(fn["parameters"]); inp = {}
    for loc, grp in vis.get("properties", {}).items():
        if isinstance(grp, dict) and grp.get("type") == "object" and grp.get("properties") is not None:
            built = instance_for(grp)
            if loc in vis.get("required", []) or built:
                inp[loc] = built
        elif loc in vis.get("required", []):
            inp[loc] = instance_for(grp)
    return inp


# ---- harvest real values from a response into a pool ----
ALIASES = {
    "owner": ["owner", "login", "organization", "org"], "repo": ["repo", "repository"],
    "project_id": ["project_id", "id"], "issue_number": ["issue_number", "number"],
    "pull_number": ["pull_number", "number"], "page_id": ["page_id", "id"], "database_id": ["database_id", "id"],
    "id": ["id"], "ref": ["sha", "ref"], "sha": ["sha"], "commit_sha": ["sha"], "ref_name": ["name"],
    "branch": ["branch", "name", "default_branch"], "key_id": ["key_id", "id"],
    "username": ["username", "login"], "team_id": ["team_id", "teamId", "id"], "org_id": ["org_id", "id"],
}


def harvest(data, pool, depth=0):
    if depth > 6: return
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (str, int)) and v not in (None, "") and k not in pool:
                pool[k] = v
            elif k == "owner" and isinstance(v, dict) and "login" in v:
                pool.setdefault("owner", v["login"])
            else:
                harvest(v, pool, depth + 1)
        if "full_name" in data and isinstance(data["full_name"], str) and "/" in data["full_name"]:
            o, r = data["full_name"].split("/", 1)
            pool.setdefault("owner", o); pool.setdefault("repo", r)
    elif isinstance(data, list) and data:
        harvest(data[0], pool, depth + 1)


def fill_required(fn, inp, pool):
    """Fill required leaf params (path/query/body) from the harvested pool by alias."""
    vis = filter_visible(fn["parameters"])
    filled_any = False
    for loc, grp in vis.get("properties", {}).items():
        if not (isinstance(grp, dict) and grp.get("type") == "object"):
            continue
        for pname in grp.get("required", []):
            if pname not in grp.get("properties", {}):  # filter_visible already pruned to visible
                continue
            for cand in ALIASES.get(pname, [pname]):
                if cand in pool:
                    inp.setdefault(loc, {})[pname] = pool[cand]
                    filled_any = True
                    break
    return filled_any


# ================= READ-CHAINING =================
print("================= READ-CHAINING (auto-discover ids) =================")
# fresh free accounts are mostly EMPTY, so seed a known real PUBLIC resource where
# the account has no data of its own — this lets us prove the get/detail tools work.
SEED = {"github": {"owner": "octocat", "repo": "Hello-World"}}
grand = defaultdict(lambda: [0, 0, 0])  # server -> [tools, base_200, chained_200]
for d in READ_SERVERS:
    fns = json.load(open(f"{APPS}/{d}/functions.json"))
    pool = dict(SEED.get(d, {}))
    status = {}
    # pass 1: fire all with placeholder input, harvest from successes
    for fn in fns:
        ok, data, err = execute(fn["name"], build_input(fn))
        status[fn["name"]] = ok
        if ok:
            harvest(data, pool)
    base_n = sum(status.values())
    # pass 2: multi-round chaining — fill required ids from the (seeded+harvested) pool
    newly = []
    for _ in range(3):
        improved = 0
        for fn in fns:
            if status[fn["name"]]:
                continue
            inp = build_input(fn)
            if fill_required(fn, inp, pool):
                ok, data, err = execute(fn["name"], inp)
                if ok:
                    status[fn["name"]] = True; improved += 1; newly.append(fn["name"].split("__")[1])
                    harvest(data, pool)
        if not improved:
            break
    chained_n = sum(status.values())
    grand[d] = [len(fns), base_n, chained_n]
    extra = f"  (+{chained_n - base_n}: {', '.join(newly[:8])}{'...' if len(newly) > 8 else ''})" if chained_n > base_n else "  (account empty — nothing to fetch)" if base_n <= 3 else ""
    print(f"  {d:10} {len(fns):3} tools | 200 baseline={base_n:2} -> chained={chained_n:2}{extra}")

# ================= WRITE-LIFECYCLES (create -> verify -> delete) =================
print("\n================= WRITE-LIFECYCLES (auto clean-up) =================")
life = []

# ---- NEON: create project -> get -> delete ; create api key -> revoke ----
ok, data, err = execute("NEON__CREATE_PROJECT", {"body": {"project": {"name": "aci-selftest-temp"}}})
if ok:
    pid = (data or {}).get("project", {}).get("id")
    gok, _, _ = execute("NEON__GET_PROJECT", {"path": {"project_id": pid}})
    dok, _, derr = execute("NEON__DELETE_PROJECT", {"path": {"project_id": pid}})
    life.append(("NEON  project create->get->delete", ok and gok and dok, f"id={pid} get={gok} delete={dok}"))
else:
    life.append(("NEON  project create", False, err))
ok, data, err = execute("NEON__CREATE_API_KEY", {"body": {"key_name": "aci-selftest-temp"}})
if ok:
    kid = (data or {}).get("id")
    rok, _, _ = execute("NEON__REVOKE_API_KEY", {"path": {"key_id": kid}})
    life.append(("NEON  api-key create->revoke", ok and rok, f"id={kid} revoke={rok}"))
else:
    life.append(("NEON  api-key create", False, err))

# ---- LINEAR (GraphQL): viewer -> teams -> create issue -> archive ----
ok, data, err = execute("LINEAR__GRAPHQL_QUERY", {"body": {"query": "{ viewer { id name } }"}})
life.append(("LINEAR viewer query", ok, "" if ok else err))
ok, data, err = execute("LINEAR__LIST_ISSUES", {"body": {"query": "{ issues(first: 3) { nodes { id identifier title } } }"}})
life.append(("LINEAR list issues", ok, "" if ok else err))
tok, tdata, _ = execute("LINEAR__GRAPHQL_QUERY", {"body": {"query": "{ teams(first: 1) { nodes { id name } } }"}})
team_id = None
try:
    team_id = tdata["data"]["teams"]["nodes"][0]["id"]
except Exception:
    pass
if team_id:
    mut = "mutation($i: IssueCreateInput!){ issueCreate(input: $i){ success issue { id identifier } } }"
    ok, data, err = execute("LINEAR__CREATE_ISSUE", {"body": {"query": mut, "variables": {"i": {"teamId": team_id, "title": "ACI self-test (auto-deleted)"}}}})
    iid = None
    try: iid = data["data"]["issueCreate"]["issue"]["id"]
    except Exception: pass
    if iid:
        amut = "mutation($id: String!){ issueArchive(id: $id){ success } }"
        aok, adata, _ = execute("LINEAR__GRAPHQL_QUERY", {"body": {"query": amut, "variables": {"id": iid}}})
        life.append(("LINEAR issue create->archive", ok and aok, f"id={iid} archived={aok}"))
    else:
        life.append(("LINEAR issue create", ok, err or str(data)[:100]))
else:
    life.append(("LINEAR create issue (need team)", False, "no team id"))

for label, ok, detail in life:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:34} {detail}")

# ================= SUMMARY =================
print("\n================= SUMMARY =================")
tot_tools = sum(v[0] for v in grand.values())
tot_base = sum(v[1] for v in grand.values())
tot_chain = sum(v[2] for v in grand.values())
print(f"read servers: {len(READ_SERVERS)} | tools {tot_tools} | real 200s: baseline {tot_base} -> after chaining {tot_chain}  (+{tot_chain-tot_base})")
print(f"write-lifecycles: {sum(1 for _,o,_ in life if o)}/{len(life)} passed (each created then cleaned up)")
print("note: openweathermap (3/3) + serpapi (5/5) already 100% in the flat run; not re-tested here.")
