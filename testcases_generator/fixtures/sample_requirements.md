# Sample requirements (for `--from-fixture` smoke runs)

Replace with real requirement text, or delete once GitHub access is configured.

## REQ-1 — User display name normalization
Display names are trimmed of leading/trailing whitespace and lowercased before storage.
Names longer than 64 characters are rejected with a validation error.

## REQ-2 — Session timeout
An idle session expires after 30 minutes. A request made at exactly 30 minutes is rejected;
a request at 29 minutes 59 seconds succeeds and resets the idle timer.

## REQ-3 — Device pairing codes
Pairing codes are 6 digits, valid for 5 minutes, single-use. Reusing a consumed code returns
`CODE_ALREADY_USED`. An expired code returns `CODE_EXPIRED`.
