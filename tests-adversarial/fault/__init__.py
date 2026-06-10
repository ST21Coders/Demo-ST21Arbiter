"""Fault-injection layer (Block H).

Covers checklist items #43 (fail-open logic), #45 (swallowed errors),
#47 (inconsistent state after partial failure), #53 (unsafe consumption
of third-party APIs), and #74 (LLM insecure output handling).

True fault injection (killing a Lambda mid-request, swapping a downstream
response) requires AWS Fault Injection Simulator or Lambda extensions —
beyond a black-box harness. This layer takes the pragmatic approach:
client-side probes that simulate downstream failure conditions and
verify the API's response shape.
"""
