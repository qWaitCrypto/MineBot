## Problem Boundary

<!-- What exact problem does this PR solve? -->

## Non-Goals

<!-- What does this PR deliberately not change? -->

## Architecture And Safety

<!--
Which layer owns this behavior? Does it affect Brain/Body boundaries, the
single-writer rule, transport, player-build protection, shared worlds, secrets,
or player data?
-->

## Authoritative Evidence

<!--
Show the positive result using world, inventory, server-event, or other terminal
facts. Model text and command acceptance are not completion evidence.
-->

## Negative / Regression Case

<!-- Show at least one inverse, failure, cancellation, or regression case. -->

## Verification

```text
# Exact commands and results
```

## Still Unproved

<!-- List environment, runtime, platform, or behavior that this PR did not prove. -->

## Checklist

- [ ] The change is bounded and contains no unrelated cleanup.
- [ ] I preserved Brain/Body ownership and terminal-truth semantics.
- [ ] I did not broaden mutation permission to make a scenario pass.
- [ ] I added or identified positive and negative evidence proportional to risk.
- [ ] I removed secrets, private world content, generated caches, and player data.
