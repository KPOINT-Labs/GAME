# `gd_add_bonus` - Add bonus

Add a named bonus on top of the points of the `assign_points` that ends
the run. The `assign_points` value becomes the **base** and every bonus
becomes its own component of the award's **points breakdown**, which is
stored with the award (`userpoints.breakdown`) and returned by the points
detail endpoints and by simulate.

## AST

```json
{
  "type": "add_bonus",
  "key": "streak",
  "value": { "type": "literal", "value": 5 }
}
```

## Example

> 10 base points for a completed task, +5 for a week-long streak, plus a
> quality bonus sent by the app:

```
rule 1: when user.streak_days >= 7
        then add_bonus "streak" = 5

rule 2: when data.completed == true
        then add_bonus "quality" = data.quality_bonus
             assign 10 points  caseName="TaskCompleted"
```

With `user.streak_days = 9` and `data.quality_bonus = 2` the award is
17 points, stored as:

```json
{
  "version": 1,
  "components": [
    { "key": "base", "points": 10 },
    { "key": "streak", "points": 5 },
    { "key": "quality", "points": 2 }
  ]
}
```

## Notes

* `add_bonus` does **not** halt. Put it before `assign_points` in the same
  rule, or alone in an earlier rule: a matched rule without an
  `assign_points` falls through to the next rule, and its bonuses carry
  over.
* Bonuses are added to whichever `assign_points` ends the run, including
  the program's `default`. If no `assign_points` runs (nothing matched and
  there is no default, or a `return` fired first), the award is 0 points
  and the bonuses are discarded.
* The same key added more than once is one component holding the sum.
* A negative value works as a penalty.
* `value` must evaluate to a number - `DSL_ADD_BONUS_NOT_NUMBER`
  otherwise.
* `key` must be 1-64 characters of `[A-Za-z0-9_]` and cannot be `base`,
  which is reserved for the `assign_points` value.
* Allowed only in main `rules`. It is rejected in `default`, `pre_rules`
  and `post_rules`.
* Every award from a custom (`DSL_FULL`) strategy stores a breakdown; with
  no bonuses it has the single `base` component. `DSL_EXTEND` strategies
  store none.
