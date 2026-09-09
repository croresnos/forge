"""Tests for FORGE.

The gate tests are the ones that matter. Everything else here is plumbing that
would announce itself the first time a server ran; the gate is the part that
fails silently and expensively, by letting an agent send a request nobody
approved. So it is tested by its failure modes, not its happy path.

    python -m pytest core/mcp/test_forge.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import forge
from forge import Gate, SpecError, build_request, load, preview

SPECS = Path(__file__).parent / "specs"

REST = {
    "name": "demo",
    "transport": {"kind": "rest", "url": "https://api.example.com/v1",
                  "auth": {"kind": "none"}, "headers": {"Accept": "application/json"}},
    "tools": [],
}
GQL = {
    "name": "demo-gql",
    "transport": {"kind": "graphql", "url": "https://api.example.com/graphql",
                  "auth": {"kind": "bearer", "env": "DEMO_TOKEN"}},
    "tools": [],
}

GET_CLIENT = {"name": "get_client", "description": "d",
              "params": [{"name": "client_id", "type": "string", "required": True}],
              "request": {"method": "GET", "path": "/clients/{client_id}"}}

CANCEL = {"name": "cancel", "description": "d", "write": True,
          "params": [{"name": "appt_id", "type": "string", "required": True},
                     {"name": "reason", "type": "string"}],
          "request": {"method": "POST", "path": "/appointments/{appt_id}/cancel",
                      "body": {"reason": "reason"}}}


# --------------------------------------------------------------------------
# request building
# --------------------------------------------------------------------------

def test_rest_path_and_body():
    r = build_request(REST, CANCEL, {"appt_id": "a1", "reason": "sick"})
    assert r["method"] == "POST"
    assert r["url"] == "https://api.example.com/v1/appointments/a1/cancel"
    assert r["json"] == {"reason": "sick"}


def test_omitted_optional_body_field_is_not_sent():
    # Sending {"reason": null} is not the same request as omitting it, and some
    # APIs treat the explicit null as "clear this field".
    r = build_request(REST, CANCEL, {"appt_id": "a1", "reason": None})
    assert r.get("json") == {}


def test_path_parameters_are_escaped():
    # An id carrying a slash must not be able to reach a different endpoint.
    r = build_request(REST, GET_CLIENT, {"client_id": "../../admin/keys"})
    assert r["url"] == "https://api.example.com/v1/clients/..%2F..%2Fadmin%2Fkeys"
    assert "/admin/keys" not in r["url"]


def test_query_params_use_the_spec_mapping():
    tool = {"name": "search", "description": "d",
            "params": [{"name": "q", "type": "string", "required": True}],
            "request": {"method": "GET", "path": "/search", "query": {"term": "q"}}}
    r = build_request(REST, tool, {"q": "hello"})
    assert r["params"] == {"term": "hello"}


def test_unknown_template_key_is_a_spec_error():
    bad = {"name": "x", "description": "d", "params": [],
           "request": {"method": "GET", "path": "/x/{nope}"}}
    with pytest.raises(SpecError, match="nope"):
        build_request(REST, bad, {})


def test_unknown_transport_kind_is_a_spec_error():
    with pytest.raises(SpecError, match="transport"):
        build_request({"name": "x", "transport": {"kind": "soap"}}, GET_CLIENT, {"client_id": "1"})


def test_graphql_sends_query_and_variables(monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "sekrit")
    tool = {"name": "q", "description": "d", "query": "query($id: ID!){ x(id:$id) }",
            "variables": ["id"], "params": [{"name": "id", "type": "string", "required": True}]}
    r = build_request(GQL, tool, {"id": "abc"})
    assert r["method"] == "POST"
    assert r["json"]["variables"] == {"id": "abc"}
    assert r["headers"]["Authorization"] == "Bearer sekrit"


def test_missing_credential_refuses_to_build(monkeypatch):
    # Better to fail at startup than to send an unauthenticated call and have
    # the vendor log a 401 against our name.
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    with pytest.raises(SpecError, match="DEMO_TOKEN"):
        build_request(GQL, {"name": "q", "description": "d", "query": "{x}"}, {})


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

def test_preview_never_prints_header_values(monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "sekrit")
    r = build_request(GQL, {"name": "q", "description": "d", "query": "{x}"}, {})
    text = preview(r, "tok")
    assert "Authorization" in text      # the caller should know it is set
    assert "sekrit" not in text         # and never see what it is
    assert "Bearer" not in text


def test_preview_says_nothing_was_sent():
    text = preview(build_request(REST, CANCEL, {"appt_id": "a1"}), "tok")
    assert "NOT been sent" in text
    assert 'confirm="tok"' in text


def test_matching_token_is_accepted_once():
    gate, req = Gate(), build_request(REST, CANCEL, {"appt_id": "a1"})
    tok = gate.offer(req)
    assert gate.accept(tok, req) == (True, "")
    # Replay must fail: an approval authorises one send, not a standing licence.
    ok, why = gate.accept(tok, req)
    assert not ok and "expired" in why


def test_changed_request_invalidates_the_token():
    """The whole point. Approving a cancellation of A must not be usable to
    cancel B, even though the tool and the caller are identical."""
    gate = Gate()
    approved = build_request(REST, CANCEL, {"appt_id": "appointment-A"})
    tok = gate.offer(approved)
    swapped = build_request(REST, CANCEL, {"appt_id": "appointment-B"})
    ok, why = gate.accept(tok, swapped)
    assert not ok
    assert "changed after it was approved" in why


def test_body_change_alone_invalidates_the_token():
    gate = Gate()
    a = build_request(REST, CANCEL, {"appt_id": "a1", "reason": "client asked"})
    tok = gate.offer(a)
    b = build_request(REST, CANCEL, {"appt_id": "a1", "reason": "no show"})
    assert gate.accept(tok, b)[0] is False


def test_unknown_token_is_refused():
    ok, why = Gate().accept("deadbeef", build_request(REST, CANCEL, {"appt_id": "a"}))
    assert not ok and "unknown or has expired" in why


def test_tokens_expire():
    gate = Gate(ttl=0)
    req = build_request(REST, CANCEL, {"appt_id": "a1"})
    tok = gate.offer(req)
    assert gate.accept(tok, req)[0] is False


def test_digest_is_stable_across_key_order():
    # The digest has to survive dict ordering or approvals break at random.
    assert Gate.digest({"a": 1, "b": 2}) == Gate.digest({"b": 2, "a": 1})


# --------------------------------------------------------------------------
# spec loading and schema generation
# --------------------------------------------------------------------------

def test_load_rejects_duplicate_tool_names(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"name": "x", "transport": {"kind": "rest"},
                             "tools": [{"name": "a"}, {"name": "a"}]}))
    with pytest.raises(SpecError, match="share a name"):
        load(p)


def test_load_rejects_missing_keys(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"name": "x"}))
    with pytest.raises(SpecError, match="transport"):
        load(p)


def test_shipped_specs_all_load():
    for p in sorted(SPECS.glob("*.json")):
        assert load(p)["tools"], f"{p.name} declares no tools"


# --------------------------------------------------------------------------
# spec validation
#
# The Boulevard spec cannot be smoke-tested - sandbox credentials come from
# their developer portal - so its first real call happens in front of the
# client. check() is what stands in for that call, which means check() itself
# has to be shown to fail on broken input, not just pass on good input.
# --------------------------------------------------------------------------

def _gql(query, variables, params):
    return {**GQL, "tools": [{"name": "t", "description": "d", "query": query,
                              "variables": variables, "params": params}]}


def test_check_passes_every_shipped_spec():
    for p in sorted(SPECS.glob("*.json")):
        assert forge.check(load(p)) == [], f"{p.name} has spec problems"


def test_check_catches_a_variable_used_but_not_declared():
    spec = _gql("query { cart(id: $cart_id) { id } }", ["cart_id"],
                [{"name": "cart_id", "type": "string"}])
    assert any("without declaring it" in p for p in forge.check(spec))


def test_check_catches_a_variable_declared_but_unused():
    spec = _gql("query Q($a: ID!, $b: ID) { cart(id: $a) { id } }", ["a", "b"],
                [{"name": "a"}, {"name": "b"}])
    assert any("never uses it" in p for p in forge.check(spec))


def test_check_catches_a_variable_that_is_not_a_tool_parameter():
    # The typo that costs the demo: query says $cartId, params say cart_id.
    spec = _gql("query Q($cartId: ID!) { cart(id: $cartId) { id } }", ["cartId"],
                [{"name": "cart_id", "type": "string"}])
    problems = forge.check(spec)
    assert any("not a parameter of the tool" in p for p in problems)


def test_check_finds_variables_nested_deep_in_a_mutation_input():
    # Boulevard's mutations bury variables inside input objects, which is
    # exactly where a naive scan of the top level would miss them.
    spec = _gql(
        "mutation M($id: ID!, $notes: String) "
        "{ cancelAppointment(input: {id: $id, notes: $notes}) { appointment { id } } }",
        ["id", "notes"], [{"name": "id"}, {"name": "notes"}])
    assert forge.check(spec) == []


def test_check_catches_unparseable_graphql():
    spec = _gql("query Q($a: ID!) { cart(id: $a) {", ["a"], [{"name": "a"}])
    assert any("does not parse" in p for p in forge.check(spec))


def test_check_catches_a_rest_path_placeholder_with_no_parameter():
    spec = {**REST, "tools": [{"name": "t", "description": "d",
                               "params": [{"name": "board"}],
                               "request": {"method": "GET", "path": "/b/{boardd}"}}]}
    assert any("not a parameter" in p for p in forge.check(spec))


def test_check_catches_a_rest_body_mapping_to_nothing():
    spec = {**REST, "tools": [{"name": "t", "description": "d",
                               "params": [{"name": "a"}],
                               "request": {"method": "POST", "path": "/x",
                                           "body": {"reason": "typo"}}}]}
    assert any("body.reason" in p for p in forge.check(spec))


@pytest.mark.anyio
async def test_schema_carries_types_descriptions_and_the_confirm_field():
    spec = {**REST, "tools": [GET_CLIENT, CANCEL]}
    spec["tools"][0] = {**GET_CLIENT, "params": [
        {"name": "client_id", "type": "string", "required": True,
         "description": "Boulevard client id"}]}
    tools = {t.name: t.input_schema for t in await forge.build_server(spec).list_tools()}

    props = tools["get_client"]["properties"]
    assert props["client_id"]["type"] == "string"
    assert props["client_id"]["description"] == "Boulevard client id"
    assert tools["get_client"]["required"] == ["client_id"]

    # Write tools gain confirm, and it must be optional or the model cannot
    # reach the preview that produces the token in the first place.
    assert "confirm" in tools["cancel"]["properties"]
    assert "confirm" not in tools["cancel"].get("required", [])


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------
# live, against a real API with no credentials
# --------------------------------------------------------------------------

def test_short_responses_are_returned_untouched():
    assert forge._cap('{"a": 1}') == '{"a": 1}'


def test_truncation_is_announced_and_not_passed_off_as_json():
    out = forge._cap("x" * 50, limit=10)
    assert out.startswith("x" * 10)
    assert "TRUNCATED" in out
    assert "not\nvalid JSON" in out or "not valid JSON" in out.replace("\n", " ")


@pytest.mark.anyio
@pytest.mark.skipif(os.environ.get("NO_NETWORK") == "1", reason="offline")
async def test_greenhouse_spec_returns_real_data():
    server = forge.build_server(load(SPECS / "greenhouse.json"))
    # The posting the whole Boulevard pitch is built on. If this ever stops
    # returning, the email's premise has expired and must not go out.
    result = await server.call_tool(
        "get_job", {"board": "boulevard", "job_id": "4683617006"})
    text = result.content[0].text
    assert "TRUNCATED" not in text
    body = json.loads(text)
    assert body["title"] == "Senior Product Manager, Developer Ecosystem"
    assert "MCP" in body["content"]


@pytest.mark.anyio
@pytest.mark.skipif(os.environ.get("NO_NETWORK") == "1", reason="offline")
async def test_vendor_errors_surface_rather_than_raise():
    server = forge.build_server(load(SPECS / "greenhouse.json"))
    result = await server.call_tool("list_departments", {"board": "not-a-real-board-xyz"})
    assert "HTTP 404" in result.content[0].text
