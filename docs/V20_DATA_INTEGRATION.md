# V20 Data Integration

V20 expects the completed V18.2 root containing `core`, `months`, `billing_master_v18_2`, and `runtime`.

The patient workspace combines:

1. **Core profile** — patient/provider assignment and persistent synthetic traits.
2. **Monthly summary** — session counts, engagement, completion, platform mix, time-of-day totals and goal progress.
3. **V18.2 event detail** — exact patient sessions and exact provider review/communication/documentation activity.
4. **Runtime** — login/audit/report records created while demonstrating the dashboard.

Provider authorization is enforced on the server using the patient's assigned `provider_key`.

Billing-support evidence is one Provider + one Patient + one Billing Month. Patient engagement duration is never added to provider time.
