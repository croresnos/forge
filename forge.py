"""FORGE - turn an HTTP API into an MCP server from one JSON spec.

A spec says where an API lives, how it authenticates, and which of its
operations to expose. FORGE serves those as MCP tools. Adding an API means
writing another JSON file rather than another Python server. REST and GraphQL.

    python forge.py specs/greenhouse.json --check   # validate the spec
    python forge.py specs/greenhouse.json --list    # show the tools
    python forge.py specs/greenhouse.json           # serve over stdio

Tools marked "write": true are held. The first call renders the request and
returns it together with a hash of it, and sends nothing. The API is not
touched until the caller passes that hash back. The token is bound to the
rendered request rather than to the arguments or the caller's intent, so an
approval cannot be replayed against a different record and editing any field
invalidates it.

See README.md for the spec format.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import graphql
import httpx
from mcp.server import MCPServer
from pydantic import Field

TYPES = {"string": str, "integer": int, "number": float, "boolean": bool,
         "array": list, "object": dict}
TIMEOUT = 30
CONFIRM_TTL = 300  # seconds a confirmation token stays good


class SpecError(ValueError):
    """The spec is wrong. Say which key and stop - a half-built server that
    404s at call time wastes the client's evaluation, not ours."""


# --------------------------------------------------------------------------
# the confirmation gate
# --------------------------------------------------------------------------

class Gate:
    """Binds an executed request to an approved one by hash.

    Holding the *rendered request* rather than the tool arguments is the point.
    If a model asks to cancel appointment A, is shown A, and then sends the
    token along with appointment B, the digest will not match and the call is
    refused. Approving one sentence cannot authorise a different one.
    """

    def __init__(self, ttl: int = CONFIRM_TTL):
        self.ttl = ttl
        self._pending: dict[str, tuple[float, dict]] = {}

    @staticmethod
    def digest(request: dict) -> str:
        blob = json.dumps(request, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def offer(self, request: dict) -> str:
        tok = self.digest(request)
        self._pending[tok] = (time.time(), request)
        self._sweep()
        return tok

    def accept(self, token: str, request: dict) -> tuple[bool, str]:
        self._sweep()
        entry = self._pending.get(token)
        if entry is None:
            return False, ("That confirmation token is unknown or has expired. "
                           "Call again without a token to get a fresh preview.")
        if self.digest(request) != token:
            return False, ("The request changed after it was approved, so it was "
                           "not sent. Call again without a token to preview the "
                           "new request.")
        self._pending.pop(token, None)
        return True, ""

    def _sweep(self) -> None:
        # Age >= ttl is expired, so a ttl of 0 means "no validity at all"
        # rather than "valid for one clock tick".
        cut = time.time() - self.ttl
        for k in [k for k, (t, _) in self._pending.items() if t <= cut]:
            self._pending.pop(k, None)


# --------------------------------------------------------------------------
# spec -> request
# --------------------------------------------------------------------------

def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SpecError(
            f"environment variable {name} is not set. This server refuses to "
            f"start unauthenticated rather than fail on the first call."
        )
    return v


def auth_headers(auth: dict | None) -> dict[str, str]:
    if not auth:
        return {}
    kind = auth.get("kind", "none")
    if kind == "none":
        return {}
    if kind == "bearer":
        return {"Authorization": f"Bearer {_env(auth['env'])}"}
    if kind == "basic":
        import base64
        raw = f"{_env(auth['env'])}:{auth.get('password', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    if kind == "header":
        return {auth["header"]: _env(auth["env"])}
    raise SpecError(f"unknown auth kind: {kind!r}")


def expand_env(url: str) -> str:
    """Expand ${VAR} in a transport URL.

    Several vendors put the tenant in the path rather than in a header -
    Boulevard's is /api/2020-01/{businessID}/client - so the base URL is
    per-install configuration, not part of the spec.
    """
    return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: _env(m.group(1)), url)


def _fill(template: str, args: dict) -> str:
    """Substitute {name} from args. Path segments are escaped; a client id
    containing a slash must not be able to reach a different endpoint."""
    from urllib.parse import quote

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in args:
            raise SpecError(f"template refers to {{{key}}} which is not a parameter")
        return quote(str(args[key]), safe="")

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, template)


def build_request(spec: dict, tool: dict, args: dict) -> dict:
    """Render the exact HTTP request. Pure: no network, no clock, no globals -
    which is what makes the gate's digest reproducible and testable."""
    t = spec["transport"]
    headers = {"User-Agent": spec.get("user_agent", "core-forge/0.1"),
               **t.get("headers", {}), **auth_headers(t.get("auth"))}

    if t["kind"] == "graphql":
        return {"method": "POST", "url": expand_env(t["url"]), "headers": headers,
                "json": {"query": tool["query"],
                         "variables": {k: v for k, v in args.items()
                                       if k in tool.get("variables", args)}}}

    if t["kind"] == "rest":
        op = tool["request"]
        url = expand_env(t["url"]).rstrip("/") + _fill(op["path"], args)
        req = {"method": op.get("method", "GET"), "url": url, "headers": headers}
        if op.get("query"):
            req["params"] = {k: args[v] for k, v in op["query"].items()
                             if args.get(v) is not None}
        if op.get("body"):
            req["json"] = {k: args[v] for k, v in op["body"].items()
                           if args.get(v) is not None}
        return req

    raise SpecError(f"unknown transport kind: {t['kind']!r}")


def _cap(text: str, limit: int = 20000) -> str:
    """Truncate a vendor response, loudly.

    A JSON document cut at a byte boundary is not shorter JSON, it is broken
    JSON, and a model handed broken JSON will usually invent the rest of it
    rather than report the problem. So say what happened and what to do.
    """
    if len(text) <= limit:
        return text
    return (text[:limit] + f"\n\n[TRUNCATED. The full response was {len(text)} "
            f"characters and only the first {limit} are shown, so this is not "
            f"valid JSON and must not be parsed as a complete document. Narrow "
            f"the request or fetch a single record instead.]")


def preview(request: dict, token: str) -> str:
    """What the caller sees before anything is sent. Headers are named but never
    printed - an Authorization value in a transcript is a leaked credential."""
    lines = [f"{request['method']} {request['url']}"]
    if request.get("params"):
        lines.append(f"  query: {json.dumps(request['params'])}")
    if request.get("json"):
        body = json.dumps(request["json"], indent=2)
        lines.append("  body:")
        lines += [f"    {ln}" for ln in body.splitlines()]
    lines.append(f"  headers: {', '.join(sorted(request['headers']))}")
    return ("This has NOT been sent yet.\n\n" + "\n".join(lines) +
            f"\n\nTo send exactly this, call again with confirm=\"{token}\".\n"
            "Any change to the arguments invalidates the token.")


# --------------------------------------------------------------------------
# spec -> MCP tools
# --------------------------------------------------------------------------

def _signature(params: list[dict], write: bool) -> inspect.Signature:
    """Build the signature the SDK reads to generate the tool's JSON schema.

    Parameter descriptions ride along in Annotated metadata rather than being
    dropped, because the description is what the model reads to decide what to
    put in the field. A schema of bare types is how you get an agent passing a
    business name where an id belongs.
    """
    out = []
    for p in sorted(params, key=lambda p: "default" in p or not p.get("required")):
        ann = TYPES.get(p.get("type", "string"), str)
        if p.get("required") and "default" not in p:
            default = inspect.Parameter.empty
        else:
            default = p.get("default")
            ann = ann | None
        if p.get("description"):
            ann = Annotated[ann, Field(description=p["description"])]
        out.append(inspect.Parameter(p["name"], inspect.Parameter.KEYWORD_ONLY,
                                     default=default, annotation=ann))
    if write:
        out.append(inspect.Parameter(
            "confirm", inspect.Parameter.KEYWORD_ONLY, default="",
            annotation=Annotated[str, Field(
                description="Leave empty to preview. Pass the token from the "
                            "preview to actually send the request.")]))
    return inspect.Signature(out)


def make_tool(spec: dict, tool: dict, gate: Gate, client: httpx.AsyncClient):
    write = bool(tool.get("write"))
    params = tool.get("params", [])

    async def call(**args: Any) -> str:
        token = args.pop("confirm", "")
        request = build_request(spec, tool, args)

        if write:
            if not token:
                return preview(request, gate.offer(request))
            ok, why = gate.accept(token, request)
            if not ok:
                return why

        r = await client.request(**request, timeout=TIMEOUT)
        if r.status_code >= 400:
            # The vendor's own error text is more useful to the caller than
            # ours, but cap it: some APIs return a full HTML error page.
            return f"HTTP {r.status_code} from {spec['name']}: {r.text[:600]}"
        try:
            body = r.json()
        except ValueError:
            return _cap(r.text)
        if isinstance(body, dict) and body.get("errors"):
            return f"{spec['name']} returned errors: {json.dumps(body['errors'])[:600]}"
        return _cap(json.dumps(body, indent=2))

    doc = tool["description"]
    if write:
        doc += ("\n\nThis writes to the live system. The first call returns a "
                "preview and a confirmation token and sends nothing; call again "
                "with confirm=<token> to actually send it.")
    call.__name__ = tool["name"]
    call.__doc__ = doc
    call.__signature__ = _signature(params, write)
    call.__annotations__ = {p.name: p.annotation
                            for p in call.__signature__.parameters.values()}
    return call


def check(spec: dict) -> list[str]:
    """Find the mistakes a spec-driven server would otherwise reveal at call time.

    This exists because the Boulevard spec cannot be smoke-tested: sandbox
    credentials are issued through their developer portal, so the first real
    call happens in front of the client. A GraphQL variable named in the query
    but missing from the parameter list is a 400 on that first call, and this
    catches it on the bench instead.
    """
    problems: list[str] = []
    kind = spec.get("transport", {}).get("kind")

    for tool in spec.get("tools", []):
        where = f"{spec.get('name')}.{tool.get('name')}"
        declared = {p["name"] for p in tool.get("params", [])}

        if kind == "graphql":
            try:
                doc = graphql.parse(tool["query"])
            except graphql.GraphQLSyntaxError as e:
                problems.append(f"{where}: query does not parse: {e.message}")
                continue
            ops = [d for d in doc.definitions
                   if isinstance(d, graphql.OperationDefinitionNode)]
            if len(ops) != 1:
                problems.append(f"{where}: expected exactly one operation, found {len(ops)}")
                continue
            sig = {v.variable.name.value for v in (ops[0].variable_definitions or ())}
            # Walk the body only. Walking the whole operation would count each
            # variable's own declaration as a use, and every unused variable
            # would then look used.
            used = {n.name.value
                    for part in (ops[0].selection_set, *(ops[0].directives or ()))
                    for n in _walk_variables(part)}
            listed = set(tool.get("variables", []))

            for name in sorted(used - sig):
                problems.append(f"{where}: uses ${name} without declaring it")
            for name in sorted(sig - used):
                problems.append(f"{where}: declares ${name} but never uses it")
            for name in sorted(sig - declared):
                problems.append(f"{where}: ${name} is not a parameter of the tool")
            for name in sorted(listed - declared):
                problems.append(f"{where}: variables lists {name!r}, which is not a parameter")
            if sig and listed and sig != listed:
                problems.append(f"{where}: variables {sorted(listed)} "
                                f"does not match the query's {sorted(sig)}")

        elif kind == "rest":
            op = tool.get("request", {})
            for name in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", op.get("path", "")):
                if name not in declared:
                    problems.append(f"{where}: path uses {{{name}}}, which is not a parameter")
            for section in ("query", "body"):
                for key, ref in (op.get(section) or {}).items():
                    if ref not in declared:
                        problems.append(f"{where}: {section}.{key} maps to {ref!r}, "
                                        f"which is not a parameter")
    return problems


def _walk_variables(node) -> list:
    """Every $variable used anywhere in an operation, at any nesting depth."""
    found = []

    def visit(n):
        if isinstance(n, graphql.VariableNode):
            found.append(n)
        for child in (getattr(n, k) for k in n.keys if k != "loc"):
            if isinstance(child, graphql.Node):
                visit(child)
            elif isinstance(child, (list, tuple)):
                for c in child:
                    if isinstance(c, graphql.Node):
                        visit(c)

    visit(node)
    return found


def load(path: Path) -> dict:
    spec = json.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "transport", "tools"):
        if key not in spec:
            raise SpecError(f"spec is missing required key: {key!r}")
    names = [t["name"] for t in spec["tools"]]
    if len(names) != len(set(names)):
        raise SpecError("two tools share a name")
    return spec


def build_server(spec: dict, client: httpx.AsyncClient | None = None) -> MCPServer:
    client = client or httpx.AsyncClient(follow_redirects=True)
    gate = Gate()
    server = MCPServer(name=spec["name"],
                       instructions=spec.get("instructions", ""),
                       version=spec.get("version", "0.1.0"))
    for tool in spec["tools"]:
        server.add_tool(make_tool(spec, tool, gate, client),
                        name=tool["name"], description=tool["description"])
    return server


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path)
    ap.add_argument("--list", action="store_true", help="print the tools and exit")
    ap.add_argument("--check", action="store_true",
                    help="validate the spec without serving; exits 1 on problems")
    a = ap.parse_args(argv)

    spec = load(a.spec)
    if a.check:
        problems = check(spec)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        n = len(spec["tools"])
        print(f"{spec['name']}: {n} tools, {len(problems)} problem(s)")
        return 1 if problems else 0
    if a.list:
        print(f"{spec['name']}  ({spec['transport']['kind']} -> "
              f"{spec['transport'].get('url', '?')})")
        for t in spec["tools"]:
            mark = "WRITE (gated)" if t.get("write") else "read"
            args = ", ".join(p["name"] for p in t.get("params", []))
            print(f"  {t['name']}({args})  [{mark}]")
            print(f"      {t['description']}")
        return 0

    build_server(spec).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
