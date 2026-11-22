# `gd_add_bonus` - Add bonus

Add a named bonus on top of the points a rule assigns. The rule's
`assign_points` value becomes the **base** and every bonus becomes its own
component of the award's **points breakdown**, which is stored with the
award (`userpoints.breakdown`) and returned by the points detail endpoints
and by simulate.

A bonus is either:

* **fixed** (`points` in the block, the default) - the value is added as
  points, or
* a **multiplier** (`× base points` in the block, `"mode": "multiplier"`) -
  the value is a factor of the base points: `2` means double points, so the
  bonus is the base once more.

## AST

```json
{
  "type": "add_bonus",
  "key": "streak",
  "mode": "multiplier",
  "value": { "type": "literal", "value": 2 }
}
```

`mode` is optional: omit it (or use `"fixed"`) for a fixed bonus.

## Example

> A video share is worth 10 points. With a streak of 5 days or more the
> share earns double points, and the app can send an extra quality bonus:

```
rule 1: when user.streak_days >= 5
        then add_bonus "streak" = ×2 base points

rule 2: when data.shared == true
        then add_bonus "quality" = data.quality_bonus      (points)
             assign 10 points  caseName="VideoShared"
```

With `user.streak_days = 6` and `data.quality_bonus = 2` the award is
22 points, stored as:

```json
{
  "version": 1,
  "components": [
    { "key": "base", "points": 10 },
    { "key": "streak", "points": 10 },
    { "key": "quality", "points": 2 }
  ]
}
```

Without the streak the same share is 12 points (`base` 10, `quality` 2).

## Notes

* `add_bonus` does **not** halt. Put it before `assign_points` in the same
  rule, or alone in an earlier rule: a matched rule without an
  `assign_points` falls through to the next rule, and its bonuses carry
  over.
* Bonuses are only applied when a **rule's** `assign_points` awards the
  points. If the run falls through to the program's `default` (no rule
  assigned points), the default's value is awarded alone and the bonuses
  are discarded; the same applies when no `assign_points` runs at all.
* Multipliers apply to the base only, never to other bonuses, so they
  stack by adding: ×2 and ×1.5 on a base of 10 add 10 and 5 (25 in total).
  A factor below 1 reduces the award (×0.5 halves it) and ×0 cancels the
  base.
* Award points are whole numbers, so a multiplier's bonus is rounded to
  whole points, .5 upwards (×1.5 on 5 points adds 3, for 8 in total; ×0.5
  on 5 points gives 3). Fixed bonuses are used as given.
* The same key added more than once is one component holding the sum.
* A negative fixed value works as a penalty.
* `value` must evaluate to a number - `DSL_ADD_BONUS_NOT_NUMBER`
  otherwise.
* `key` must be 1-64 characters of `[A-Za-z0-9_]` and cannot be `base`,
  which is reserved for the `assign_points` value.
* Allowed only in main `rules`. It is rejected in `default`, `pre_rules`
  and `post_rules`.
* Every award from a custom (`DSL_FULL`) strategy stores a breakdown; with
  no bonuses it has the single `base` component. `DSL_EXTEND` strategies
  store none.
