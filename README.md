# Reflect Provider Dashboard V20.1

V20.1 is the dashboard layer for the V18.2 Billing Master synthetic dataset. It does not regenerate the 5M-patient data.

## Main corrections from V17

- Practical centered login page with dataset-linked Provider, Admin and Billing demo accounts.
- Patient profiles open as full workspace pages instead of side drawers.
- Explorer, Engagement, Modules and Platform all use the restored `visual_analytics` API layer.
- Admin dashboard combines engagement, platform, cohort, provider workload and billing-context analytics.
- Billing Reviewer has separate **Patients** and **Providers** tabs.
- Provider time is sourced from V18.2 timestamped `DATA_REVIEW`, `COMMUNICATION` and `DOCUMENTATION` events.
- Patient engagement minutes remain separate from provider billing-support time.
- Monthly PDF evidence is generated per patient.
- Optional synthetic demo amount is provider-time based and clearly marked as non-reimbursement demo data.

## Data source

V20.1 reads these V18.2 tables directly:

- `patient_session_event`
- `provider_activity_event`
- `provider_activity_audit_event`
- `provider_access_session`
- `patient_billing_month_summary`
- `provider_billing_month_summary`
- `provider_activity_profile`

The base V13 core/month summaries remain the fast source for 5M-scale population and engagement analytics.

## Performance model

- Patient directory is paged server-side.
- Admin totals use 10K-provider monthly summaries where possible.
- Large population distributions use deterministic cached/sampled aggregation.
- Detailed patient events are queried only for the selected patient/month.
- Billing queues use the indexed V18.2 monthly billing databases.

See `START_HERE.txt` for setup.


## V20.1 startup performance fix
The normal launcher now performs a fast schema/metadata validation only. It does not COUNT the multi-million-row event tables and does not run SQLite PRAGMA quick_check at startup. Use `DEEP_CHECK_V20_DATA.bat` separately for a full integrity scan.
