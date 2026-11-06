from __future__ import annotations
import sys, sqlite3
from pathlib import Path

def out(*a):
    print(*a, flush=True)

def resolve(p: Path)->Path:
    candidates=[p,p/'data_v18_full',p/'data_v13']
    for c in candidates:
        if (c/'core'/'reflect_core_v13.db').exists() and (c/'billing_master_v18_2'/'reflect_billing_core_v18_2.db').exists():
            return c.resolve()
    return p.resolve()

def main():
    if len(sys.argv)<2:
        out('Usage: python tools\\check_v20_data.py <V18.2 data root>'); raise SystemExit(2)
    root=resolve(Path(sys.argv[1]))
    core=root/'core'/'reflect_core_v13.db'
    broot=root/'billing_master_v18_2'; bcore=broot/'reflect_billing_core_v18_2.db'
    out('V20.1 / V18.2 FAST STARTUP CHECK')
    out('Root:',root)
    if not core.exists(): out('FAIL: missing',core); raise SystemExit(1)
    if not bcore.exists(): out('FAIL: missing',bcore); raise SystemExit(1)

    out('[1/4] Checking core database...')
    with sqlite3.connect(core, timeout=10) as c:
        pats=c.execute('SELECT COUNT(*) FROM patients WHERE is_deleted=0').fetchone()[0]
        prov=c.execute('SELECT COUNT(*) FROM providers WHERE is_deleted=0').fetchone()[0]
        mods=dict(c.execute('SELECT platform,COUNT(*) FROM module_catalog WHERE is_active=1 GROUP BY platform').fetchall())
    out(f'      Patients: {pats:,}')
    out(f'      Providers: {prov:,}')
    out('      Modules:',mods)

    out('[2/4] Checking month files...')
    months=sorted(x.stem.replace('reflect_month_','') for x in (root/'months').glob('reflect_month_*.db'))
    bmonths=sorted(x.stem.replace('reflect_billing_','') for x in (broot/'months').glob('reflect_billing_*.db'))
    out(f'      Base months: {len(months)}', (months[0]+' to '+months[-1]) if months else 'NONE')
    out(f'      Billing months: {len(bmonths)}', (bmonths[0]+' to '+bmonths[-1]) if bmonths else 'NONE')
    if not months or not bmonths: out('FAIL: monthly data missing'); raise SystemExit(1)
    missing=sorted(set(months)-set(bmonths))
    if missing: out('WARNING: billing master missing months:',', '.join(missing))

    latest=bmonths[-1]; db=broot/'months'/f'reflect_billing_{latest}.db'
    out(f'[3/4] Checking latest billing schema ({latest})...')
    with sqlite3.connect(db, timeout=10) as c:
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required={'patient_session_event','provider_activity_event','provider_activity_audit_event','provider_access_session','patient_billing_month_summary','provider_billing_month_summary','metadata'}
        miss=required-names
        if miss: out('FAIL: missing V18.2 tables:',sorted(miss)); raise SystemExit(1)
        meta=dict(c.execute('SELECT key,value FROM metadata').fetchall())
        status=meta.get('build_status','UNKNOWN')
        month_meta=meta.get('billing_month','UNKNOWN')
        # Fast B-tree/sample checks only. No multi-million-row COUNT(*) and no PRAGMA quick_check here.
        max_patient=c.execute('SELECT MAX(patient_key) FROM patient_billing_month_summary').fetchone()[0]
        provider_rows=c.execute('SELECT COUNT(*) FROM provider_billing_month_summary').fetchone()[0]
        sample_summary=c.execute('SELECT patient_key,provider_key,total_provider_minutes FROM patient_billing_month_summary LIMIT 1').fetchone()
        sample_session=c.execute('SELECT 1 FROM patient_session_event LIMIT 1').fetchone()
        sample_provider_event=c.execute('SELECT 1 FROM provider_activity_event LIMIT 1').fetchone()
        provider_minutes=c.execute('SELECT COALESCE(SUM(total_provider_minutes),0) FROM provider_billing_month_summary').fetchone()[0]
    out(f'      metadata month: {month_meta}')
    out(f'      build status: {status}')
    out(f'      highest patient key: {max_patient:,}' if max_patient is not None else '      highest patient key: NONE')
    out(f'      provider summaries: {provider_rows:,}')
    out(f'      total provider minutes: {provider_minutes:,.1f}')
    if status != 'COMPLETE': out('FAIL: latest billing month is not marked COMPLETE'); raise SystemExit(1)
    if provider_rows != prov: out(f'WARNING: provider summary count {provider_rows:,} != core providers {prov:,}')
    if max_patient is None or sample_summary is None: out('FAIL: patient billing summary is empty'); raise SystemExit(1)
    if sample_session is None: out('WARNING: patient_session_event has no rows in latest month')
    if sample_provider_event is None: out('WARNING: provider_activity_event has no rows in latest month')

    out('[4/4] Fast startup validation complete.')
    out('PASS: V18.2 structure is compatible with Reflect Provider Dashboard V20.1.')
    out('NOTE: Full COUNT(*) and SQLite quick_check are intentionally excluded from startup.')
    out('      Run DEEP_CHECK_V20_DATA.bat separately when you want a full integrity scan.')

if __name__=='__main__': main()
