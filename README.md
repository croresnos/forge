# FORGE

Turn an HTTP API into an MCP server by writing a JSON file instead of a Python
program.

You describe an API once: where it lives, how it authenticates, and which of
its operations an agent should be allowed to call. FORGE reads that file and
serves those operations as MCP tools. There is no per-vendor code. Adding a
second API means writing a second JSON file, not writing a second server.

REST and GraphQL are both supported.

## A whole server

This is a complete, working spec. Nothing else is needed to run it.

```json
{
  "name": "acme",
  "transport": {
    "kind": "rest",
    "url": "https://api.acme.com/v2",
    "auth": {"kind": "bearer", "env": "ACME_TOKEN"}
  },
  "tools": [
    {
      "name": "find_customer",
      "description": "Look up a customer by email address.",
      "params": [
        {"name": "email", "type": "string", "required": true,
         "description": "Full email address, not a name or an account number."}
      ],
      "request": {"method": "GET", "path": "/customers", "query": {"email": "email"}}
    }
  ]
}
```

```
python forge.py acme.json --check    # validate it
python forge.py acme.json --list     # show the tools it would serve
python forge.py acme.json            # serve over stdio
```

Point an MCP client at that last command and it has a `find_customer` tool.

## Why a spec rather than a server

Every hand-written MCP server re-implements the same handful of things: build
the auth headers, generate a JSON schema for each tool, render the request,
handle the response. That code is dull, it is nearly identical each time, and
it is not where anyone is looking when something goes wrong.

Putting it in a spec has two effects. Each new API becomes a description of
that API instead of another copy of the plumbing. And a fix to the request
builder is a fix to every server at once, rather than to whichever one you
remember to go back to.

Parameter descriptions are part of the spec and end up in the tool's JSON
schema. This matters more than it looks. The description is what the model
reads when it decides what to put in a field, and a schema of bare types is how
an agent ends up passing a business name where an id belongs.

## Writes are held until confirmed

This is the part that is not boilerplate, and it is the reason the project
exists.

Reads are cheap to get wrong. Writes are not. A tool marked `"write": true` is
never sent on the first call. FORGE builds the exact request, shows it, hashes
it, and stops:

```
This has NOT been sent yet.

POST https://api.acme.com/v2/customers/8891/cancel
  body:
    {
      "reason": "customer asked"
    }
  headers: Authorization, Content-Type, User-Agent

To send exactly this, call again with confirm="3f4ec46821fd2aa9".
Any change to the arguments invalidates the token.
```

Nothing reaches the API until that token comes back. The token is a hash of the
rendered request rather than of the arguments or the caller's intent, which is
what gives it its properties:

- approving a change to record A cannot be replayed against record B
- changing any field, including a free-text note, invalidates the token
- a token is single use, so an approval authorises one send and not a standing
  licence
- tokens expire, after five minutes by default

Header names appear in the preview. Header values never do. An `Authorization`
value in a transcript is a leaked credential.

## Writing a spec

**Transport.** `kind` is `rest` or `graphql`. `url` may contain `${ENV_VAR}`,
expanded at request time, so a tenant-specific endpoint does not have to be
committed to the file.

**Auth.** `none`, `bearer`, `basic`, or `header` (an arbitrary named header).
The credential is read from the environment by name, never stored in the spec.
A missing credential refuses to build the request rather than sending an
unauthenticated call and having the vendor log a 401.

**Tools.** Each has a `name`, a `description`, `params`, and either a `request`
block (REST) or a `query` string with a `variables` list (GraphQL). Add
`"write": true` to put it behind the gate.

**Params.** `string`, `integer`, `number`, `boolean`, `array`, `object`. Each
takes `required`, `default` and `description`.

For REST, `path` may contain `{param}` placeholders, and `query` and `body` map
API field names to parameter names. Path values are URL-escaped, so an id
containing a slash cannot walk out of its endpoint and reach a different one.

## Checking a spec

`--check` is the step before shipping. For GraphQL it parses each query and
proves that the variables the query *uses*, the variables it *declares*, and
the parameters exposed as tool arguments are all the same set. For REST it
proves every path placeholder and every query and body mapping resolves to a
real parameter.

These are the mistakes that otherwise surface on the first live call, which is
not always a call you get to make privately.

## The specs in this repo

Two are included so that FORGE can be run against something real without
writing a spec first. They are examples, not the subject of the project.

| Spec | Transport | Credentials | State |
|---|---|---|---|
| `specs/greenhouse.json` | REST | none | Works immediately. Two tests call it live. |
| `specs/boulevard.json` | GraphQL | env vars | Validated, never run against a live tenant. |

`greenhouse.json` reads any company's public Greenhouse job board. It needs no
credentials at all, which makes it the fastest way to confirm an install works.

`boulevard.json` describes the client-facing GraphQL API of a booking platform:
14 tools covering a cart booking flow through checkout, plus cancel and
reschedule. Every operation, argument and type in it was taken from Boulevard's
own MIT-licensed [`book-sdk`](https://github.com/Boulevard/book-sdk)
(`src/graph.d.ts`) rather than from guesswork.

**It has not been run against a live or sandbox tenant**, because those
credentials are issued through Boulevard's developer portal. `--check` passes
on it and nothing has smoke-tested it. Its own `instructions` field says so, so
a model loading the spec is not misled either.

## Limits

- stdio transport only, one spec per process
- no pagination or retry helpers; a tool returns what the API returned
- responses over 20,000 characters are truncated with an explicit notice rather
  than cut silently, because a model handed truncated JSON will usually invent
  the rest instead of reporting the problem
- 30 second request timeout

## Tests

```bash
pip install -r requirements.txt
python -m pytest test_forge.py -q
```

32 tests. Two of them call Greenhouse's live public API, so they need a network
connection. The rest are offline, and the gate is tested by feeding it altered
requests and expiring tokens rather than only working ones.
