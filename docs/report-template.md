## Intent
> The problem in plain words, the ticket, and what this change does not cover.

Customers who upgrade mid-month are charged the full new price today. Finance wants the charge prorated by remaining days, so an upgrade on day 20 of 30 costs one third of the difference.

**Out of scope:** downgrades (stay end-of-cycle), annual plans.

## Judge this
> The questions only a human can answer: product intent, trade-offs, risk you accept.

- Is "first day" the day the cycle starts, or the day after the previous invoice?
- Is rounding once at the end acceptable to Finance, or should each line round?

## Design
> How it works in a few lines, plus one alternative you rejected and why.

- New `prorate(old, new, cycle_start, cycle_end, today)` in the pricing module. Pure function, no I/O.
- Called from the plan-change handler only when the new price is higher.
- Rejected: storing a proration row per change. The invoice line already carries the amount, a second table would drift.

## Edge cases
> One row per case a reviewer might worry about: how it is handled and which check proves it.

| Case | Handled | Verified by |
| --- | --- | --- |
| Change on the last day of the cycle | Charged 1/30, not zero | unit test |
| Change on the first day | Full difference, no proration | unit test |
| Leap year February | Uses real cycle length, 29 days | unit test |
| Two changes in one day | Second change prorates from the first | integration test |
| Currency rounding | Round half-even to cents, once, at the end | golden file |

## Verification
> Evidence, not claims: what each check proves, the command, the result (✅ passed, ⚠️ not run or partial, ❌ failed; give a reason for anything but ✅), a link to the run, and whether it fails if the change is reverted.

| Proves | Command | Result | Link | Fails on revert |
| --- | --- | --- | --- | --- |
| Proration math for all edge cases | `pytest tests/test_pricing.py` | ✅ 212 passed | [CI job #8812](https://example.com/ci/8812) | yes |
| Retried request does not prorate twice | `pytest tests/test_plan_change.py::test_retry` | ✅ passed | [CI job #8812](https://example.com/ci/8812) | yes |
| 40 real customers match the Finance sheet | notebook, run all cells | ✅ 40/40 match | [notebook](https://example.com/notebooks/proration-golden) | no, it reads past invoices |
| Upgrade works on staging | upgrade test account #771 in the UI | ✅ invoice line 13.33 EUR | [staging](https://example.com/staging/plan-change) | yes |
| No new errors after deploy | log query, last 2h | ⚠️ 40 minutes only, staging was redeployed | [log query](https://example.com/logs?q=plan-change) | no |

## Not verified / assumptions
> What you did not check and what you took on trust, so the reviewer can decide whether to trust it too.

- Annual plans were not tested. We assume they never reach the plan-change handler.
- Not run against production data volumes. The function is pure, so we expect no change in speed.
- Assumes the billing cycle dates in the database are always in the customer's local time zone.

## Rollback
> How to undo this change and what it leaves behind.

Revert merge request !231 and redeploy. No schema change and no data migration. Invoices already issued with a prorated line stay as they are.

## Security impact
> What the change touches that matters for security, or "None" with the reason.

None. The change only reads prices and dates that the handler already loads. It adds no input from users, no new endpoint and no new permission.

## Agent reviews
> What the agent reviewers found and what you did with each finding.

| Finding | Severity | Disposition | Reason |
| --- | --- | --- | --- |
| Proration used a fixed 30-day month | high | fixed | Now uses the real cycle length |
| Handler could prorate twice on a retried request | high | fixed | Now idempotent per change id |
| "Add a proration table" | low | dismissed | Rejected in Design, the invoice line is the source of truth |

## Links
> Ticket, deploy, notebook, then the code last.

- Ticket: [BILL-412](https://example.com/tickets/BILL-412)
- Staging: [plan change page](https://example.com/staging/plan-change)
- Notebook: [proration golden](https://example.com/notebooks/proration-golden)
- Code: [merge request !231](https://gitlab.example.com/billing/billing-service/-/merge_requests/231)
