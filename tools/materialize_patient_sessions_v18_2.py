#!/usr/bin/env python3
import argparse, sqlite3, sys
from pathlib import Path
from build_billing_master_v18_2 import open_read, open_write, generate_sessions, iso_utc
from datetime import datetime, timezone


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('data_root');ap.add_argument('patient_id');ap.add_argument('billing_month')
    ap.add_argument('--output-name',default='billing_master_v18_2')
    a=ap.parse_args();root=Path(a.data_root).resolve();month=a.billing_month
    core=open_read(root/'core/reflect_core_v13.db')
    p=core.execute('select patient_key,provider_key,preferred_hour,primary_mobile_module_key,secondary_mobile_module_key,primary_xr_module_key,secondary_xr_module_key,monthly_seed from patients where patient_id=?',(a.patient_id,)).fetchone()
    if not p:sys.exit('Patient not found')
    m=open_read(root/'months'/f'reflect_month_{month}.db');r=m.execute('select * from patient_monthly_summary where patient_key=?',(p['patient_key'],)).fetchone()
    if not r:sys.exit('Patient-month not found')
    mods={'Mobile':[x[0] for x in core.execute("select module_key from module_catalog where platform='Mobile' order by module_key")], 'XR':[x[0] for x in core.execute("select module_key from module_catalog where platform='XR' order by module_key")]}
    outp=root/a.output_name/'months'/f'reflect_billing_{month}.db'
    if not outp.exists():sys.exit(f'Build V18.2 month first: {outp}')
    out=open_write(outp);created=iso_utc(datetime.now(timezone.utc))
    sess=generate_sessions(month,dict(r),dict(p),mods,created)
    out.execute('BEGIN');out.execute('delete from patient_session_event where patient_key=?',(p['patient_key'],))
    if sess:out.executemany('insert into patient_session_event values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',sess)
    out.execute('COMMIT');out.close();m.close();core.close()
    print(f"Materialized {len(sess)} session event(s) for {a.patient_id} / {month}")

if __name__=='__main__':main()
