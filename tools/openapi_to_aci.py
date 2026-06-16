#!/usr/bin/env python3
"""
OpenAPI 3.x  ->  ACI app definition (app.json + functions.json) generator.

Each OpenAPI *operation* becomes ONE thin ACI REST function (1 tool = 1 endpoint,
no business logic). OpenAPI parameters are grouped by location into ACI's
path / query / header / cookie groups, and requestBody becomes the `body` group.
Every object node is emitted with the 5 keys ACI's validator requires
(type, properties, required, visible, additionalProperties).

Usage:
  python openapi_to_aci.py --spec petstore.json --app PETSTORE --out apps/petstore \
      [--server-url "https://{subdomain}.example.com"] \
      [--auth-name Authorization --auth-prefix Bearer] \
      [--include /pets,/store] [--methods get,post] [--limit 15] \
      [--provider "Pet Store"] [--category "Productivity"]

If --server-url contains {placeholders}, they are filled per-connection from the
linked account's connection_config at execute time (ACI Gap-1 templating).
"""
import argparse
import json
import re
import sys

try:
    import yaml  # optional; only needed for .yaml/.yml specs
except Exception:
    yaml = None

PRIMITIVES = {"string", "integer", "number", "boolean"}


def load_spec(path: str) -> dict:
    with open(path) as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        if not yaml:
            sys.exit("PyYAML not installed; convert the spec to JSON or `pip install pyyaml`.")
        return yaml.safe_load(text)
    return json.loads(text)


def resolve_ref(spec: dict, ref: str) -> dict:
    # only local refs like "#/components/schemas/Foo"
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def sanitize_schema(schema: dict, spec: dict, depth: int = 0, seen=None) -> dict:
    """Turn an OpenAPI schema into an ACI-friendly JSON-schema node.
    Guarantees object nodes carry type/properties/required/visible/additionalProperties.
    Conservative: unknown constructs collapse to a permissive object/string.
    """
    seen = seen or set()
    if not isinstance(schema, dict):
        return {"type": "string"}
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen or depth > 6:
            return _free_object()
        return sanitize_schema(resolve_ref(spec, ref), spec, depth + 1, seen | {ref})
    # allOf -> shallow merge of object members
    if "allOf" in schema:
        merged = {"type": "object", "properties": {}, "required": []}
        for sub in schema["allOf"]:
            s = sanitize_schema(sub, spec, depth + 1, seen)
            if s.get("type") == "object":
                merged["properties"].update(s.get("properties", {}))
                merged["required"].extend(s.get("required", []))
        return _finish_object(merged["properties"], merged["required"])
    if "oneOf" in schema or "anyOf" in schema:
        return _free_object()

    t = schema.get("type")
    if isinstance(t, list):  # e.g. ["string","null"]
        t = next((x for x in t if x != "null"), "string")

    if t == "object" or ("properties" in schema and not t):
        ap = schema.get("additionalProperties")
        props = {}
        for name, sub in (schema.get("properties") or {}).items():
            if depth > 6:
                break
            props[name] = sanitize_schema(sub, spec, depth + 1, seen)
        required = [r for r in (schema.get("required") or []) if r in props]
        node = _finish_object(props, required)
        # a typed map ({additionalProperties: <schema>}) or an explicit free map, or an
        # object with no usable properties, becomes a free-form object so it can still be
        # marked visible (ACI rejects a visible object whose children are all non-visible).
        if ap is True or isinstance(ap, dict) or not props:
            node["additionalProperties"] = True
        if schema.get("description"):
            node["description"] = schema["description"]
        return node
    if t == "array":
        return {
            "type": "array",
            **({"description": schema["description"]} if schema.get("description") else {}),
            "items": sanitize_schema(schema.get("items", {}), spec, depth + 1, seen),
        }
    if t in PRIMITIVES:
        node = {"type": t}
        for k in ("description", "enum", "default"):
            if k in schema:
                node[k] = schema[k]
        return node
    # unknown/empty -> permissive string
    return {"type": "string", **({"description": schema["description"]} if schema.get("description") else {})}


def _worthy(node: dict) -> bool:
    """ACI won't let an object property be `visible` if none of its own children are
    visible. A node is 'worthy' of being visible if it's a primitive/array, or an
    object that either has visible children or accepts arbitrary keys."""
    if isinstance(node, dict) and node.get("type") == "object":
        return bool(node.get("visible")) or node.get("additionalProperties") is True
    return True


def _finish_object(props: dict, required: list) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "visible": [k for k, v in props.items() if _worthy(v)],
        "additionalProperties": False,
    }


def _free_object() -> dict:
    # an object that accepts arbitrary keys (used when the real schema is too complex)
    return {"type": "object", "properties": {}, "required": [], "visible": [], "additionalProperties": True}


_VERB = {"get": "GET", "post": "CREATE", "put": "UPDATE", "patch": "UPDATE", "delete": "DELETE"}


def func_name(app: str, method: str, path: str, op: dict) -> str:
    oid = (op.get("operationId") or "").strip()
    # use the operationId only when it's clean (no embedded path, reasonable length);
    # many specs (e.g. HubSpot) bake the whole path into operationId -> ugly names.
    if oid and "/" not in oid and len(oid) <= 40:
        base = oid
    else:
        segs = [s for s in path.split("/") if s and not s.startswith("{")]
        tail = "_".join(segs[-2:]) if segs else "root"
        base = f"{_VERB.get(method.lower(), method.upper())}_{tail}"
    base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").upper()
    return f"{app}__{base}"


def operation_to_function(app, method, path, op, server_url, spec) -> dict:
    groups = {}  # location -> {props, required}
    for p in op.get("parameters", []):
        if "$ref" in p:
            p = resolve_ref(spec, p["$ref"])
        loc = p.get("in")
        if loc not in ("path", "query", "header", "cookie"):
            continue
        g = groups.setdefault(loc, {"props": {}, "required": []})
        g["props"][p["name"]] = sanitize_schema(p.get("schema", {"type": "string"}), spec)
        if p.get("description") and "description" not in g["props"][p["name"]]:
            g["props"][p["name"]]["description"] = p["description"]
        if p.get("required"):
            g["required"].append(p["name"])

    # requestBody -> body group (prefer application/json)
    rb = op.get("requestBody")
    if rb:
        if "$ref" in rb:
            rb = resolve_ref(spec, rb["$ref"])
        content = (rb.get("content") or {})
        media = content.get("application/json") or next(iter(content.values()), {})
        body_schema = sanitize_schema(media.get("schema", {}), spec)
        if body_schema.get("type") != "object":
            body_schema = _finish_object({"value": body_schema}, [])
        groups["body"] = {
            "props": body_schema.get("properties", {}),
            "required": body_schema.get("required", []),
            "_extra_additional": body_schema.get("additionalProperties", False),
        }

    properties = {}
    for loc, g in groups.items():
        node = _finish_object(g["props"], g["required"])
        if g.get("_extra_additional") is True and not g["props"]:
            node["additionalProperties"] = True
        node["description"] = {
            "path": "Path parameters", "query": "Query parameters",
            "header": "Header parameters", "cookie": "Cookie parameters",
            "body": "Request body",
        }[loc]
        properties[loc] = node

    # H-4: a location group must be top-level required if it carries any required
    # params, else required query/header/cookie params are silently droppable.
    top_required = [
        loc
        for loc in ("path", "query", "header", "cookie", "body")
        if loc in properties
        and (loc in ("path", "body") or properties[loc].get("required"))
    ]
    desc = (op.get("summary") or op.get("description") or f"{method.upper()} {path}").strip()
    return {
        "name": func_name(app, method, path, op),
        "description": desc[:512],
        "tags": op.get("tags", []),
        "visibility": "public",
        "active": True,
        "protocol": "rest",
        "protocol_data": {"method": method.upper(), "path": path, "server_url": server_url},
        "parameters": {
            "type": "object",
            "required": top_required,
            "visible": [k for k in properties if _worthy(properties[k])],
            "additionalProperties": False,
            "properties": properties,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--app", required=True, help="ACI app name, UPPERCASE")
    ap.add_argument("--out", required=True, help="output dir for app.json + functions.json")
    ap.add_argument("--server-url", default=None, help="override base url (may contain {placeholders})")
    ap.add_argument("--auth-name", default="Authorization")
    ap.add_argument("--auth-prefix", default="Bearer")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--category", default="Productivity")
    ap.add_argument("--include", default=None, help="comma-separated path substrings to keep")
    ap.add_argument("--methods", default="get,post,put,patch,delete")
    ap.add_argument("--limit", type=int, default=0, help="cap number of tools (0 = all)")
    args = ap.parse_args()

    spec = load_spec(args.spec)
    servers = spec.get("servers") or [{}]
    server_url = args.server_url or servers[0].get("url", "")
    server_url = server_url.rstrip("/")
    methods = {m.strip().lower() for m in args.methods.split(",")}
    includes = [s.strip() for s in args.include.split(",")] if args.include else None

    functions = []
    used: dict = {}
    for path, item in (spec.get("paths") or {}).items():
        if includes and not any(inc in path for inc in includes):
            continue
        for method, op in item.items():
            if method.lower() not in methods or not isinstance(op, dict):
                continue
            fn = operation_to_function(args.app, method, path, op, server_url, spec)
            # H-5: increment until the suffixed name is itself free, and register
            # every emitted final name so a later literal "<base>_<n>" can't collide.
            base = fn["name"]
            nm = base
            while nm in used:
                used[base] = used.get(base, 0) + 1
                nm = f"{base}_{used[base]}"
            used[nm] = used.get(nm, 0)
            fn["name"] = nm
            functions.append(fn)
            if args.limit and len(functions) >= args.limit:
                break
        if args.limit and len(functions) >= args.limit:
            break

    info = spec.get("info", {})
    app_json = {
        "name": args.app,
        "display_name": info.get("title", args.app.title()),
        "logo": f"https://raw.githubusercontent.com/aipotheosis-labs/aipolabs-icons/refs/heads/main/apps/{args.app.lower()}.svg",
        "provider": args.provider or info.get("title", args.app.title()),
        "version": "1.0",
        "description": (info.get("description") or f"{args.app} API (generated from OpenAPI).")[:300],
        "security_schemes": {"api_key": {"location": "header", "name": args.auth_name, "prefix": args.auth_prefix}},
        "default_security_credentials_by_scheme": {},
        "categories": [args.category],
        "visibility": "public",
        "active": True,
    }

    import os
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "app.json"), "w") as f:
        json.dump(app_json, f, indent=2)
    with open(os.path.join(args.out, "functions.json"), "w") as f:
        json.dump(functions, f, indent=2)
    print(f"OK: {args.app} -> {len(functions)} tools (server_url={server_url})")
    print("  sample:", ", ".join(fn["name"] for fn in functions[:8]))


if __name__ == "__main__":
    main()
