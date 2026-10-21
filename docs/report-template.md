## Intent
<!-- The problem in plain words, the ticket, and what this change does not cover. -->
Customers who upgrade mid-month are charged the full new price today. Finance wants the charge prorated by remaining days, so an upgrade on day 20 of 30 costs one third of the difference.

**Out of scope:** downgrades (stay end-of-cycle), annual plans.

## Design
<!-- How it works in a few lines, plus one alternative you rejected and why. -->
- New `prorate(old, new, cycle_start, cycle_end, today)` in the pricing module. Pure function, no I/O.
- Called from the plan-change handler only when the new price is higher.
- Rejected: storing a proration row per change. The invoice line already carries the amount, a second table would drift.

## Edge cases
<!-- One row per case a reviewer might worry about: how it is handled and which check proves it. -->
| Case | Handled | Verified by |
| --- | --- | --- |
| Change on the last day of the cycle | Charged 1/30, not zero | unit test |
| Change on the first day | Full difference, no proration | unit test |
| Leap year February | Uses real cycle length, 29 days | unit test |
| Two changes in one day | Second change prorates from the first | integration test |
| Currency rounding | Round half-even to cents, once, at the end | golden file |

## Verification
<!-- Evidence, not claims: each check, how to reproduce it, and the result. -->
| Check | How to reproduce | Result |
| --- | --- | --- |
| Unit and integration tests | `pytest` ([CI job #8812](https://example.com/ci/8812)) | 212 passed |
| Golden invoices, 40 real customers | [notebook](https://example.com/notebooks/proration-golden) | 40/40 match the Finance sheet |
| Deployed to staging | [staging](https://example.com/staging/plan-change), upgraded test account #771 | invoice line 13.33 EUR |
| Error logs on staging, 2h | [log query](https://example.com/logs?q=plan-change) | none new |

## Agent reviews
<!-- What the agent reviewers found: what was fixed, and what was dismissed with the reason. -->
- Fixed: proration used a fixed 30-day month. It now uses the real cycle length.
- Fixed: the handler could prorate twice on a retried request. It is now idempotent per change id.
- Dismissed: "add a proration table". Rejected in Design, the invoice line is the source of truth.

## Judge this
<!-- The questions only a human can answer: product intent, trade-offs, risk you accept. -->
- Is "first day" the day the cycle starts, or the day after the previous invoice?
- Is rounding once at the end acceptable to Finance, or should each line round?

## Links
<!-- Ticket, deploy, notebook, then the code last. -->
- Ticket: [BILL-412](https://example.com/tickets/BILL-412)
- Staging: [plan change page](https://example.com/staging/plan-change)
- Notebook: [proration golden](https://example.com/notebooks/proration-golden)
- Code: [merge request !231](https://example.com/code/merge_requests/231)
