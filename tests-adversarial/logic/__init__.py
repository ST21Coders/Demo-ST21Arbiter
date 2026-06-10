"""Logic / state layer (Block F).

Covers compliance items:
  * #46 — Race conditions / TOCTOU on shared resources
  * #50 — Excessive data exposure in API responses
  * #61 — Workflow bypass on the action lifecycle state machine

The layer is sequential by nature (workflow probes consume + reset state on
the same action_id, the race probe owns its target for the duration of the
fan-out, and the field-exposure walker iterates per-persona per-route). A
modest 5 RPS throttle bounds the rate at which any probe touches the API so
back-to-back transitions don't accidentally trigger generic abuse heuristics
elsewhere in the harness.
"""
