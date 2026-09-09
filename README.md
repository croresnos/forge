# FORGE

Build an MCP server for a vendor's API from one JSON spec, with a confirmation
gate on every write.

```bash
pip install -r requirements.txt

python forge.py specs/greenhouse.json --check   # validate the spec
python forge.py specs/greenhouse.json --list    # see the tools
python forge.py specs/greenhouse.json           # serve over stdio
python -m pytest test_forge.py -q               # 32 tests
```

`specs/greenhouse.json` needs no credentials, so the fastest way to judge this
is to run the tests. Two of them call Greenhouse's live public API.

## Why a spec instead of a server per vendor

Vertical SaaS APIs are all the same shape: documented REST or GraphQL, a bearer
or basic token, a dozen operations worth handing to an agent. Writing bespoke
Python per vendor means rewriting auth, schema generation, pagination and error
handling every time, and every rewrite is a fresh chance to get one of them
wrong. A spec file makes the second vendor an afternoon.

A spec declares its transport, its auth, and its tools. Parameter types and
descriptions become the tool's JSON schema, which is what the model reads to
decide what to put in each field. A schema of bare types is how you get an
agent passing a business name where an id belongs.

## The confirmation gate

Any tool marked `"write": true` is held. The first call **does not touch the
vendor**. It renders the exact HTTP request, hashes it, and returns both:

Actual output from `cancel_appointment` on the Boulevard spec:

```
This has NOT been sent yet.

POST https://sandbox.joinblvd.com/api/2020-01/urn:blvd:Business:demo/client
  body:
    {
      "query": "mutation($appointment_id: ID!, $notes: String) { cancelAppointment(input: {id: $appointment_id, notes: $notes}) { appointment { id cancelled } } }",
      "variables": {
        "appointment_id": "appt_8891",
        "notes": "client asked"
      }
    }
  headers: Authorization, Content-Type, User-Agent

To send exactly this, call again with confirm="3f4ec46821fd2aa9".
Any change to the arguments invalidates the token.
```

Nothing goes out until that token comes back. Because the token is a hash of
the **rendered request** and not of the tool arguments or the caller's intent:

- approving a cancellation of appointment A cannot be replayed to cancel B
- changing any field, even the cancellation note, invalidates the token
- a token is single use, so an approval authorises one send, not a standing licence
- tokens expire, by default after five minutes

Header *names* appear in the preview so the caller can see auth is configured.
Header *values* never do. An `Authorization` value in a transcript is a leaked
credential.

This is the part worth buying. A booking platform's objection to agent access
has never been "can you call our API". It is "what happens when the model is
wrong about which appointment it is looking at."

## Writing a spec

```jsonc
{
  "name": "vendor",
  "transport": {
    "kind": "graphql",              // or "rest"
    "url": "https://api.vendor.com/graphql",   // ${ENV_VAR} is expanded
    "auth": {"kind": "bearer", "env": "VENDOR_TOKEN"}
  },
  "tools": [{
    "name": "cancel_appointment",
    "description": "What it does, and what it costs if it is wrong.",
    "write": true,
    "params": [
      {"name": "appointment_id", "type": "string", "required": true,
       "description": "Read by the model to pick the value. Be specific."}
    ],
    "query": "mutation($appointment_id: ID!) { ... }",
    "variables": ["appointment_id"]
  }]
}
```

Auth kinds: `none`, `bearer`, `basic`, `header`. Credentials are read from the
environment at request time and a missing one refuses to build the request,
rather than sending an unauthenticated call and having the vendor log a 401.

Then run `--check` before shipping. It parses each GraphQL query and proves the
variables it *uses*, *declares* and exposes as *tool parameters* are the same
set, and that every REST path placeholder and body mapping resolves to a real
parameter. These are the mistakes that otherwise surface on the first live
call, which is not always a call you get to make privately.

## Specs included

| Spec | Transport | Credentials | State |
|---|---|---|---|
| `greenhouse.json` | REST | none | Live. Covered by tests that hit the real API. |
| `boulevard.json` | GraphQL | `BLVD_API_URL`, `BLVD_API_KEY` | Validated, not yet run. See below. |

### On the Boulevard spec

Every operation, argument and type in it was taken from Boulevard's own
MIT-licensed [`book-sdk`](https://github.com/Boulevard/book-sdk)
(`src/graph.d.ts`), not from guesswork. It covers the real cart booking flow
(`create_cart`, `add_service`, `bookable_dates`, `bookable_times`,
`reserve_time`, `checkout`) plus `cancel_appointment` and
`reschedule_appointment`.

**It has not been run against a live or sandbox tenant.** Boulevard issues
sandbox credentials through their developer portal to Enterprise accounts.
Until those exist it is a reviewed design against a published schema, and it
says so in its own `instructions` field so that a model loading it is not
misled either. `--check` is what stands in for the smoke test.
