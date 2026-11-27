# `gd_reject` - Reject with reason

Refuse the event. When a `reject` runs:

* The run stops. Statements and rules after it never run, and bonuses or
  callback data gathered before it are dropped.
* The award request fails with **HTTP 422** and error code `DSL_REJECTED`.
  The designer's reason is in `params.reason`.
* **Nothing is recorded**: no award, no wallet change, and the event's
  idempotency key (`eventId`) stays unused. A later event with the same key
  that the rules accept still earns its points.

Use it when an event must not count *yet*, for example a video milestone
that the watched time doesn't cover. Awarding 0 points instead would record
the event and use up its idempotency key, so the real milestone could never
be awarded.

## AST

```json
{
  "type": "reject",
  "id": "rej_25",
  "reason": "MILESTONE_NOT_REACHED"
}
```

`reason` is required: 1-64 characters of `A-Z`, `0-9` and `_`, starting with
a letter. Codes starting with `DSL_` are reserved for GAME's own errors.

## Response

```http
HTTP/1.1 422 Unprocessable Entity
```

```json
{
  "detail": {
    "code": "DSL_REJECTED",
    "params": {"reason": "MILESTONE_NOT_REACHED", "nodeId": "rej_25"},
    "message": "Event rejected by strategy: MILESTONE_NOT_REACHED"
  }
}
```

Clients should branch on `detail.code == "DSL_REJECTED"` and then on
`detail.params.reason`. Status 422 alone isn't enough: it's also used for
request validation errors. Don't retry a rejected event as is; send it again
only when its data has changed.

## Example

> Award a 25 % milestone only when the user really watched a quarter of
> the video:

```
when data.watchedSeconds < data.durationSeconds * 0.25
then reject with reason MILESTONE_NOT_REACHED
when true
then assign 10 points  caseName="Milestone25"
```

A floating `reject` block (outside any rule) becomes the program's
`default`, refusing every event no rule handled:

```
reject with reason NO_RULE_MATCHED
```

## Notes

* Allowed in main rules (including `else if` / `else` branches) and as the
  `default`. Not allowed in `pre_rules` / `post_rules`; DSL_EXTEND
  pre-rules have [`gd_veto`](veto.md), which records a 0-point result.
* The editor's **Test** button shows the rejection as a warning, with the
  reason, and selects the rejecting block.
* Strategy observability logs these runs with status `rejected` and the
  reason as the error code. They don't count as errors: they're left out
  of the error rate, the "top errors" list, the
  `dsl_execution_errors_total` metric and its alert.
* The user record for an unknown `externalUserId` is still created even
  when the event is rejected, because users are looked up before scoring.
  It holds no points.
* Idempotency is checked after scoring. Retrying an event that was
  already awarded runs the strategy again; if the retry's data now hits a
  `reject`, the caller gets the 422 instead of the stored award.
