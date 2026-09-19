Write the weekly review for {{USER_FIRST_NAME}} from the data: items resolved this week, items
that slipped with their causes, the commitment delivery rate, the priority balance over the week,
and the graph patterns.

Return:
- `narrative`: 4 to 6 sentences. Lead with what slipped and why, not with what went well.
- `slipped`: one line per slipped item: what, how late, root cause as best the data shows.
- `delivery_comment`: one sentence on the delivery rate, no softening.
- `process_improvement`: exactly one specific, small change for next week, tied to a cause above.
- `patterns`: up to 3 one-line observations drawn from the graph patterns supplied.
Plain and direct. Never invent an item that is not in the data.
