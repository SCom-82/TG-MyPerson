# MAX frame fixtures

One JSON per inbound frame: `frame` = `{opcode, cmd, payload}` exactly as PyMax's
`InboundFrame` carries it; normalization fixtures add `expect` (the `max_messages`
row subset) and `expect_media`.

Source (see each file's `source`): the first set is synthesized from the PyMax
2.4.1 models (camelCase wire aliases). Only the FORWARD link block of
`n10_forward_empty_text.json` is a real payload fragment (PyMax docstring).
After the first live login (PR-6) dev-qa adds real frames from `max_raw_events`
next to these — tokens and phone numbers removed.
