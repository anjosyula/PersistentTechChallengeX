# V20 Verification

Validated against the bundled V18.2 sample dataset (500 patients, 20 providers, October 2026).

Passed checks:

- V18.2 data-structure validation and SQLite `quick_check`.
- Admin login and Organization Overview.
- Provider login and assigned-patient directory.
- Billing Reviewer login.
- Explorer, Engagement, Modules, and Platform analytics API routes.
- Full-page patient workspace with V18.2 session events.
- V18.2 provider activity timeline and patient-specific provider minutes.
- Provider Time analytics.
- Patient billing queue.
- Provider billing queue and provider drill-down.
- Synthetic demo amount calculation and labeling.
- Monthly branded PDF generation.
- Server and frontend syntax validation.

Sample billing totals used during verification:

- 222 patient billing-support records with provider activity.
- 3,791.01 total provider minutes at the patient-summary level.
- 2,326.78 data-review minutes.
- 385.78 communication minutes.
- 1,078.31 documentation minutes.

Small rounding differences can appear between patient and provider summary tables because the synthetic generator stores event-level decimal minutes and provider aggregates independently at display precision.
