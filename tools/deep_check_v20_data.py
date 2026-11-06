from __future__ import annotations
import sys, sqlite3, time
from pathlib import Path

def resolve(p: Path)->Path:
    for c in [p,p/'data_v18_full',p/'data_v13']:
        if (c/'core'/'reflect_core_v13.db').exists() and (c/'billing_master_v18_2'/'reflect_billing_core_v18_2.db').exists(): return c.resolve()
    return p.resolve()

def main():
    if len(sys.argv)<2:
        print('Usage: python tools\\deep_check_v20_data.py <V18.2 data root>'); raise SystemExit(2)
    root=resolve(Path(sys.argv[1])); broot=root/'billing_master_v18_2'
    bmonths=sorted(x.stem.replace('reflect_billing_','') for x in (broot/'months').glob('reflect_billing_*.db'))
    if not bmonths: print('FAIL: no billing months'); raise SystemExit(1)
    latest=bmonths[-1]; db=broot/'months'/f'reflect_billing_{latest}.db'
    print('V20.1 DEEP DATA CHECK', flush=True)
    print('Database:',db, flush=True)
    t=time.time()
    with sqlite3.connect(db, timeout=60) as c:
        for table in ['patient_billing_month_summary','provider_billing_month_summary','patient_session_event','provider_activity_event','provider_activity_audit_event','provider_access_session']:
            print(f'Counting {table} ...', flush=True)
            n=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            print(f'  {n:,}', flush=True)
        print('Running PRAGMA quick_check (this can take a long time on the full V18.2 database)...', flush=True)
        check=c.execute('PRAGMA quick_check').fetchone()[0]
        print('SQLite quick_check:',check, flush=True)
    print(f'DEEP CHECK COMPLETE in {(time.time()-t)/60:.1f} min', flush=True)

if __name__=='__main__': main()
