from __future__ import annotations
import json, os, sqlite3, hashlib, secrets, time, random, math, uuid, calendar, io, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"

def _resolve_data_root():
    env=os.environ.get("REFLECT_V18_2_DATA") or os.environ.get("REFLECT_DATA_ROOT")
    candidates=[]
    if env: candidates.append(Path(env).resolve())
    candidates += [
        (ROOT/"data_v18_2").resolve(),
        (ROOT/"data_v18_2"/"data_v18_full").resolve(),
        (ROOT/"data_v18_2"/"data_v13").resolve(),
        (ROOT/"data_v18_full").resolve(),
    ]
    for p in candidates:
        if (p/"core"/"reflect_core_v13.db").exists() and (p/"billing_master_v18_2"/"reflect_billing_core_v18_2.db").exists():
            return p
    return (ROOT/"data_sample_v18_2").resolve()

DATA_ROOT=_resolve_data_root()
CORE_DB = DATA_ROOT / "core" / "reflect_core_v13.db"
RUNTIME_DB = DATA_ROOT / "runtime" / "reflect_dashboard_v20.db"
BILLING_ROOT = DATA_ROOT / "billing_master_v18_2"
BILLING_CORE_DB = BILLING_ROOT / "reflect_billing_core_v18_2.db"
PORT = int(os.environ.get("PORT", "8020"))
SESSION_IDLE = 30 * 60
SESSION_MAX = 8 * 60 * 60
EASTERN = ZoneInfo("America/New_York")
SESSIONS: dict[str, dict] = {}
FAILED: dict[str, dict] = {}

if not CORE_DB.exists():
    raise SystemExit("No V18 dataset found. Put the generated V18.2 data root in data_v18_2/ (core, months, billing_master_v18_2) or set REFLECT_V18_2_DATA.")

def connect(path: Path):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c

def core(): return connect(CORE_DB)
def runtime():
    RUNTIME_DB.parent.mkdir(parents=True, exist_ok=True)
    return connect(RUNTIME_DB)
def month_path(month: str): return DATA_ROOT / "months" / f"reflect_month_{month}.db"
def monthdb(month: str):
    p = month_path(month)
    if not p.exists(): raise FileNotFoundError(f"Billing month not available: {month}")
    return connect(p)
def billing_month_path(month: str): return BILLING_ROOT / "months" / f"reflect_billing_{month}.db"
def billingdb(month: str):
    p=billing_month_path(month)
    if not p.exists(): raise FileNotFoundError(f"V18 billing month not available: {month}")
    return connect(p)
def billing_core(): return connect(BILLING_CORE_DB)
def has_v18_month(month: str): return BILLING_CORE_DB.exists() and billing_month_path(month).exists()
def now(): return datetime.now(timezone.utc)
def now_iso(): return now().isoformat()
def stable_seed(*parts): return int.from_bytes(hashlib.sha256("|".join(map(str,parts)).encode()).digest()[:8], "big")
def pw_hash(pw, salt): return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 180000).hex()
def jbytes(obj): return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
def row(r): return dict(r) if r else None

def available_months():
    return sorted([p.stem.replace("reflect_month_","") for p in (DATA_ROOT/"months").glob("reflect_month_*.db")])

def metadata():
    with core() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM metadata")}

def us_dt(s):
    if not s: return "—"
    try:
        d=datetime.fromisoformat(str(s).replace("Z","+00:00"))
        if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
        d=d.astimezone(EASTERN)
        return f"{d.month}/{d.day}/{d.year} {d.strftime('%-I:%M %p') if os.name!='nt' else d.strftime('%I:%M %p').lstrip('0')}"
    except: return str(s)

def ensure_runtime_schema():
    c=runtime()
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS auth_users(
      user_id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,
      role TEXT NOT NULL,provider_key INTEGER,provider_id TEXT,password_salt TEXT NOT NULL,
      password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS provider_notes(
      note_id TEXT PRIMARY KEY,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,created_at_utc TEXT NOT NULL,note_text TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_events(
      audit_id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,role TEXT,provider_id TEXT,
      action TEXT NOT NULL,patient_key INTEGER,billing_month TEXT,event_time_utc TEXT NOT NULL,detail TEXT
    );
    CREATE TABLE IF NOT EXISTS billing_reports(
      report_id TEXT PRIMARY KEY,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,generated_by TEXT NOT NULL,generated_at_utc TEXT NOT NULL,
      provider_review_minutes REAL NOT NULL,communication_minutes REAL NOT NULL,
      real_time_communication_present INTEGER NOT NULL,remote_sessions INTEGER NOT NULL,
      remote_active_days INTEGER NOT NULL,status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS session_events(
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,platform TEXT NOT NULL,module_key INTEGER NOT NULL,session_start_utc TEXT NOT NULL,
      session_end_utc TEXT NOT NULL,duration_minutes REAL NOT NULL,remote_or_clinic TEXT NOT NULL,
      completed INTEGER NOT NULL,event_count INTEGER NOT NULL,image_used INTEGER NOT NULL,
      UNIQUE(patient_key,billing_month,session_start_utc,platform,module_key)
    );
    CREATE TABLE IF NOT EXISTS provider_reviews(
      review_id INTEGER PRIMARY KEY AUTOINCREMENT,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,review_start_utc TEXT NOT NULL,review_end_utc TEXT NOT NULL,
      duration_minutes REAL NOT NULL,review_type TEXT NOT NULL,documentation_completed INTEGER NOT NULL,
      UNIQUE(patient_key,billing_month,review_start_utc)
    );
    CREATE TABLE IF NOT EXISTS communications(
      communication_id INTEGER PRIMARY KEY AUTOINCREMENT,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,occurred_utc TEXT NOT NULL,channel TEXT NOT NULL,communication_mode TEXT NOT NULL,
      duration_minutes REAL NOT NULL,summary TEXT NOT NULL,
      UNIQUE(patient_key,billing_month,occurred_utc,channel)
    );
    CREATE TABLE IF NOT EXISTS analytics_cache_v15(
      cache_key TEXT PRIMARY KEY,payload_json TEXT NOT NULL,generated_at_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS analytics_cache_v17(
      cache_key TEXT PRIMARY KEY,payload_json TEXT NOT NULL,generated_at_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS provider_notes_v17(
      note_id TEXT PRIMARY KEY,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,created_at_utc TEXT NOT NULL,note_text TEXT NOT NULL,
      documentation_minutes REAL NOT NULL DEFAULT 0,source TEXT NOT NULL DEFAULT 'USER_RECORDED'
    );
    CREATE TABLE IF NOT EXISTS billing_reports_v17(
      report_id TEXT PRIMARY KEY,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,generated_by TEXT NOT NULL,generated_at_utc TEXT NOT NULL,
      data_review_minutes REAL NOT NULL,communication_minutes REAL NOT NULL,documentation_minutes REAL NOT NULL,
      total_provider_minutes REAL NOT NULL,real_time_communication_present INTEGER NOT NULL,
      remote_sessions INTEGER NOT NULL,remote_active_days INTEGER NOT NULL,status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runtime_schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_notes17_patient_month ON provider_notes_v17(patient_key,billing_month);
    CREATE INDEX IF NOT EXISTS ix_audit_time ON audit_events(event_time_utc DESC);
    CREATE INDEX IF NOT EXISTS ix_sessions_patient_month ON session_events(patient_key,billing_month);
    CREATE INDEX IF NOT EXISTS ix_reviews_patient_month ON provider_reviews(patient_key,billing_month);
    CREATE INDEX IF NOT EXISTS ix_comms_patient_month ON communications(patient_key,billing_month);
    INSERT OR REPLACE INTO runtime_schema_meta(key,value) VALUES('runtime_schema_version','20.0');
    """)
    # add demo users mapped to actual top providers in whichever dataset is present
    with core() as cc:
        top=list(cc.execute("""SELECT p.provider_key,p.provider_id,p.provider_display_name,s.patient_count
          FROM providers p JOIN provider_summary s USING(provider_key)
          ORDER BY s.patient_count DESC LIMIT 5"""))
    users=[]
    for i,p in enumerate(top,1):
        users.append((f"USR-P{i}",f"provider{i}@reflect.demo",p["provider_display_name"],"Provider",p["provider_key"],p["provider_id"],"ProviderDemo2026!"))
    users += [
      ("USR-ADMIN","admin@reflect.demo","Reflect Administrator","Admin",None,None,"AdminDemo2026!"),
      ("USR-BILLING","billing@reflect.demo","Billing Reviewer","Billing",None,None,"BillingDemo2026!"),
    ]
    for uid,email,name,role,pk,pid,pw in users:
        salt=hashlib.sha256((email+"|reflect-v20").encode()).hexdigest()[:32]
        c.execute("""INSERT OR REPLACE INTO auth_users(user_id,email,display_name,role,provider_key,provider_id,password_salt,password_hash,active,created_at_utc)
          VALUES(?,?,?,?,?,?,?,?,1,COALESCE((SELECT created_at_utc FROM auth_users WHERE user_id=?),?))""",
          (uid,email,name,role,pk,pid,salt,pw_hash(pw,salt),uid,now_iso()))
    c.commit(); c.close()

ensure_runtime_schema()

# ---------- V16.1/V20 provider-time layer ----------
def _object_exists(conn, name, kinds=("table","view")):
    marks=",".join("?" for _ in kinds)
    return bool(conn.execute(f"SELECT 1 FROM sqlite_master WHERE type IN ({marks}) AND name=? LIMIT 1", (*kinds,name)).fetchone())

def has_v16_1_month(month: str) -> bool:
    try:
        with monthdb(month) as c:
            return _object_exists(c,"patient_provider_time_v16_1") and _object_exists(c,"provider_time_summary_v16_1",("table",))
    except Exception:
        return False

def materialize_provider_timeline(patient_id: str, month: str):
    """Create the deterministic V16.1 event timeline for one patient/month once."""
    with core() as c:
        p=c.execute("SELECT patient_key,provider_key FROM patients WHERE patient_id=? AND is_deleted=0",(patient_id,)).fetchone()
    if not p: return
    r=runtime()
    exists=False
    try:
        exists=bool(r.execute("SELECT 1 FROM provider_patient_activity_v16_1 WHERE patient_key=? AND provider_key=? AND billing_month=? LIMIT 1",(p["patient_key"],p["provider_key"],month)).fetchone())
    except sqlite3.OperationalError:
        pass
    r.close()
    if exists or not has_v16_1_month(month): return
    try:
        with monthdb(month) as m:
            pt=m.execute("SELECT total_provider_minutes FROM patient_provider_time_v16_1 WHERE patient_key=?",(p["patient_key"],)).fetchone()
            if not pt or float(pt[0] or 0)<=0: return
    except Exception:
        return
    tool=ROOT/"tools"/"materialize_provider_patient_timeline_v16_1.py"
    if not tool.exists(): return
    try:
        subprocess.run([sys.executable,str(tool),str(DATA_ROOT),patient_id,month],cwd=str(ROOT),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=120,check=True)
    except Exception as exc:
        # Patient analytics should still open even if the optional detailed provider timeline cannot be prepared.
        print(f"V19 provider timeline materialization warning for {patient_id} {month}: {exc}")

def provider_month_time(patient_key: int, month: str):
    """Monthly patient-specific provider time. Runtime details override monthly synthetic summary when present."""
    r=runtime()
    try:
        if _object_exists(r,"provider_patient_monthly_runtime_summary_v16_1",("table",)):
            x=r.execute("SELECT * FROM provider_patient_monthly_runtime_summary_v16_1 WHERE patient_key=? AND billing_month=?",(patient_key,month)).fetchone()
            if x:
                d=dict(x); r.close(); return d
    except sqlite3.OperationalError:
        pass
    r.close()
    try:
        with monthdb(month) as m:
            if _object_exists(m,"patient_provider_time_v16_1"):
                x=m.execute("SELECT * FROM patient_provider_time_v16_1 WHERE patient_key=?",(patient_key,)).fetchone()
                if x: return dict(x)
    except Exception:
        pass
    return {}

def refresh_provider_runtime_summary(patient_key: int, provider_key: int, month: str):
    """Reconcile detailed provider activities into daily/monthly runtime summaries."""
    r=runtime()
    if not _object_exists(r,"provider_patient_activity_v16_1",("table",)):
        r.close(); return
    rows=[dict(x) for x in r.execute("SELECT activity_date,activity_type,duration_minutes,interaction_mode,activity_start_utc FROM provider_patient_activity_v16_1 WHERE patient_key=? AND provider_key=? AND billing_month=? ORDER BY activity_start_utc",(patient_key,provider_key,month))]
    r.execute("DELETE FROM provider_patient_daily_summary_v16_1 WHERE patient_key=? AND provider_key=? AND billing_month=?",(patient_key,provider_key,month))
    byday={}
    for x in rows:
        d=byday.setdefault(x['activity_date'],{'DATA_REVIEW':[0,0.0],'COMMUNICATION':[0,0.0],'DOCUMENTATION':[0,0.0]})
        typ=x['activity_type']; d[typ][0]+=1; d[typ][1]+=float(x['duration_minutes'] or 0)
    for day,d in byday.items():
        r.execute("""INSERT OR REPLACE INTO provider_patient_daily_summary_v16_1(patient_key,provider_key,activity_date,billing_month,data_review_count,data_review_minutes,communication_count,communication_minutes,documentation_count,documentation_minutes,total_provider_minutes) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (patient_key,provider_key,day,month,d['DATA_REVIEW'][0],round(d['DATA_REVIEW'][1],2),d['COMMUNICATION'][0],round(d['COMMUNICATION'][1],2),d['DOCUMENTATION'][0],round(d['DOCUMENTATION'][1],2),round(sum(v[1] for v in d.values()),2)))
    sums={'DATA_REVIEW':[0,0.0],'COMMUNICATION':[0,0.0],'DOCUMENTATION':[0,0.0]}; realtime=0
    for x in rows:
        sums[x['activity_type']][0]+=1; sums[x['activity_type']][1]+=float(x['duration_minutes'] or 0)
        if x['activity_type']=='COMMUNICATION' and str(x.get('interaction_mode','')).lower().startswith('real-time'): realtime+=1
    total=round(sum(v[1] for v in sums.values()),2)
    first=rows[0]['activity_start_utc'] if rows else ''
    last=rows[-1]['activity_start_utc'] if rows else ''
    dominant=max(sums.items(),key=lambda kv:kv[1][1])[0] if rows else ''
    r.execute("""INSERT OR REPLACE INTO provider_patient_monthly_runtime_summary_v16_1(patient_key,provider_key,billing_month,first_provider_activity_utc,last_provider_activity_utc,activity_days,data_review_count,data_review_minutes,communication_count,communication_minutes,documentation_count,documentation_minutes,total_provider_minutes,real_time_communication_count,dominant_activity_type,generated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (patient_key,provider_key,month,first,last,len(byday),sums['DATA_REVIEW'][0],round(sums['DATA_REVIEW'][1],2),sums['COMMUNICATION'][0],round(sums['COMMUNICATION'][1],2),sums['DOCUMENTATION'][0],round(sums['DOCUMENTATION'][1],2),total,realtime,dominant,now_iso()))
    r.commit(); r.close()

def insert_provider_activity(patient_key:int, provider_key:int, month:str, activity_type:str, start_dt:datetime, end_dt:datetime,
                             subtype:str, channel:str='', interaction_mode:str='', note_text:str='', documentation_completed:int=0,
                             reason_code:str='USER_RECORDED', outcome_code:str='DOCUMENTED', source:str='USER_RECORDED_V20'):
    dur=max(0.0,(end_dt-start_dt).total_seconds()/60)
    aid='ACT-'+uuid.uuid4().hex[:16].upper(); gid='GRP-'+uuid.uuid4().hex[:12].upper()
    related_platform=''; related_module=0
    r=runtime()
    r.execute("""INSERT INTO provider_patient_activity_v16_1(activity_id,activity_group_id,parent_activity_id,patient_key,provider_key,billing_month,activity_date,activity_type,activity_subtype,activity_status,activity_start_utc,activity_end_utc,duration_seconds,duration_minutes,communication_channel,interaction_mode,review_focus,reason_code,outcome_code,related_session_event_id,related_module_key,related_platform,provider_workstation,documentation_completed,documentation_support_flag,billing_relevance,note_text,metadata_json,source,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (aid,gid,'',patient_key,provider_key,month,start_dt.date().isoformat(),activity_type,subtype,'COMPLETED',start_dt.isoformat(),end_dt.isoformat(),int(round(dur*60)),round(dur,2),channel,interaction_mode,'',reason_code,outcome_code,0,related_module,related_platform,'REFLECT_DASHBOARD',int(documentation_completed),1,'DOCUMENTATION_SUPPORT_ONLY',note_text,'{}',source,now_iso()))
    for ev,ts,detail in [('CREATED',start_dt,'Provider activity record created'),('STARTED',start_dt,'Provider activity started'),('COMPLETED',end_dt,f'Provider activity completed in {dur:.2f} minutes')]:
        r.execute("INSERT INTO provider_activity_audit_v16_1(activity_id,activity_group_id,patient_key,provider_key,billing_month,event_type,event_utc,change_summary,source) VALUES(?,?,?,?,?,?,?,?,?)",(aid,gid,patient_key,provider_key,month,ev,ts.isoformat(),detail,source))
    r.commit(); r.close(); refresh_provider_runtime_summary(patient_key,provider_key,month)
    return {'activity_id':aid,'duration_minutes':round(dur,2)}

def provider_time_history(patient_key:int):
    out=[]
    for mon in available_months():
        d={}
        try:
            with monthdb(mon) as m:
                if _object_exists(m,"patient_provider_time_v16_1"):
                    x=m.execute("SELECT data_review_count,data_review_minutes,communication_count,communication_minutes,documentation_count,documentation_minutes,total_provider_minutes,real_time_communication_count FROM patient_provider_time_v16_1 WHERE patient_key=?",(patient_key,)).fetchone()
                    if x: d=dict(x)
        except Exception: pass
        if d: out.append({'month':mon,**d})
    return out

# ---------- detail materialization ----------
def choose_weighted_module(rng, platform, primary, secondary):
    with core() as c:
        keys=[r["module_key"] for r in c.execute("SELECT module_key FROM module_catalog WHERE platform=? AND is_active=1 ORDER BY module_key",(platform,))]
    weights=[]
    for k in keys:
        w=1.0
        if k==primary: w=7.0
        elif k==secondary: w=3.5
        weights.append(w*rng.lognormvariate(0,0.25))
    return rng.choices(keys,weights=weights,k=1)[0]

def materialize(patient_id, month):
    rc=runtime()
    with core() as cc:
        p=cc.execute("SELECT * FROM patients WHERE patient_id=?",(patient_id,)).fetchone()
    if not p: rc.close(); return None
    pk=p["patient_key"]
    existing=rc.execute("SELECT COUNT(*) n FROM session_events WHERE patient_key=? AND billing_month=?",(pk,month)).fetchone()["n"]
    if existing: rc.close(); return p
    with monthdb(month) as mc:
        m=mc.execute("SELECT * FROM patient_monthly_summary WHERE patient_key=?",(pk,)).fetchone()
    if not m: rc.close(); return p
    rc.executescript("""
    CREATE TABLE IF NOT EXISTS session_events(
      event_id INTEGER PRIMARY KEY AUTOINCREMENT,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,platform TEXT NOT NULL,module_key INTEGER NOT NULL,session_start_utc TEXT NOT NULL,
      session_end_utc TEXT NOT NULL,duration_minutes REAL NOT NULL,remote_or_clinic TEXT NOT NULL,
      completed INTEGER NOT NULL,event_count INTEGER NOT NULL,image_used INTEGER NOT NULL,
      UNIQUE(patient_key,billing_month,session_start_utc,platform,module_key));
    CREATE TABLE IF NOT EXISTS provider_reviews(
      review_id INTEGER PRIMARY KEY AUTOINCREMENT,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,review_start_utc TEXT NOT NULL,review_end_utc TEXT NOT NULL,
      duration_minutes REAL NOT NULL,review_type TEXT NOT NULL,documentation_completed INTEGER NOT NULL,
      UNIQUE(patient_key,billing_month,review_start_utc));
    CREATE TABLE IF NOT EXISTS communications(
      communication_id INTEGER PRIMARY KEY AUTOINCREMENT,patient_key INTEGER NOT NULL,provider_key INTEGER NOT NULL,
      billing_month TEXT NOT NULL,occurred_utc TEXT NOT NULL,channel TEXT NOT NULL,communication_mode TEXT NOT NULL,
      duration_minutes REAL NOT NULL,summary TEXT NOT NULL,
      UNIQUE(patient_key,billing_month,occurred_utc,channel));
    CREATE INDEX IF NOT EXISTS ix_sessions_patient_month ON session_events(patient_key,billing_month);
    CREATE INDEX IF NOT EXISTS ix_reviews_patient_month ON provider_reviews(patient_key,billing_month);
    CREATE INDEX IF NOT EXISTS ix_comms_patient_month ON communications(patient_key,billing_month);
    """)
    y,mo=map(int,month.split("-")); days=calendar.monthrange(y,mo)[1]
    rng=random.Random(int(m["month_seed"]))
    session_rows=[]
    for _ in range(int(m["session_count"])):
        platform="XR" if rng.random()<float(p["xr_probability"]) else "Mobile"
        if platform=="XR": module=choose_weighted_module(rng,"XR",p["primary_xr_module_key"],p["secondary_xr_module_key"])
        else: module=choose_weighted_module(rng,"Mobile",p["primary_mobile_module_key"],p["secondary_mobile_module_key"])
        day=rng.randint(1,days); local_hour=max(0,min(23,round(rng.gauss(int(p["preferred_hour"]),2))))
        minute=rng.randint(0,59); local=datetime(y,mo,day,local_hour,minute,tzinfo=EASTERN); st=local.astimezone(timezone.utc)
        dur=max(5,min(75,rng.gauss(float(m["avg_duration_min"] or 22),6))); en=st+timedelta(minutes=dur)
        context="Remote" if rng.random()<float(p["remote_probability"]) else "In Clinic"
        completed=1 if rng.random()<float(m["completion_rate"] or 0)/100 else 0
        events=max(3,min(120,round(rng.gauss(35,12)))); image=1 if platform=="Mobile" and rng.random()<0.35 else 0
        session_rows.append((pk,p["provider_key"],month,platform,module,st.isoformat(),en.isoformat(),round(dur,2),context,completed,events,image))
    rc.executemany("""INSERT OR IGNORE INTO session_events(patient_key,provider_key,billing_month,platform,module_key,session_start_utc,session_end_utc,duration_minutes,remote_or_clinic,completed,event_count,image_used)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",session_rows)
    # pre-existing synthetic provider reviews and communication evidence from monthly summary
    rcount=int(m["provider_review_count"] or 0); rmins=int(m["provider_review_minutes"] or 0)
    if rcount:
        base=rmins//rcount; rem=rmins%rcount
        for i in range(rcount):
            d=base+(1 if i<rem else 0); local=datetime(y,mo,rng.randint(1,days),rng.randint(8,18),rng.randint(0,59),tzinfo=EASTERN); st=local.astimezone(timezone.utc); en=st+timedelta(minutes=d)
            rc.execute("INSERT OR IGNORE INTO provider_reviews(patient_key,provider_key,billing_month,review_start_utc,review_end_utc,duration_minutes,review_type,documentation_completed) VALUES(?,?,?,?,?,?,?,?)",
                       (pk,p["provider_key"],month,st.isoformat(),en.isoformat(),d,"Patient engagement review",1 if i<int(m["documented_review_count"] or 0) else 0))
    ccount=int(m["communication_count"] or 0); cmins=int(m["communication_minutes"] or 0); realtime=int(m["real_time_communication_count"] or 0)
    if ccount:
        base=cmins//ccount; rem=cmins%ccount
        channels=["Portal/Message","Phone","Video","Email","Outside Reflect"]
        for i in range(ccount):
            d=base+(1 if i<rem else 0); local=datetime(y,mo,rng.randint(1,days),rng.randint(8,19),rng.randint(0,59),tzinfo=EASTERN); ts=local.astimezone(timezone.utc)
            rc.execute("INSERT OR IGNORE INTO communications(patient_key,provider_key,billing_month,occurred_utc,channel,communication_mode,duration_minutes,summary) VALUES(?,?,?,?,?,?,?,?)",
                       (pk,p["provider_key"],month,ts.isoformat(),rng.choice(channels),"Real-time interactive" if i<realtime else "Asynchronous",d,"Synthetic provider-patient communication record"))
    rc.commit(); rc.close(); return p

# ---------- queries ----------
def provider_key_for_id(pid):
    with core() as c:
        r=c.execute("SELECT provider_key FROM providers WHERE provider_id=?",(pid,)).fetchone(); return r["provider_key"] if r else None

def authorize_patient(user, patient_id):
    with core() as c:
        p=c.execute("SELECT * FROM patients WHERE patient_id=? AND is_deleted=0",(patient_id,)).fetchone()
        if not p: return None
        if user["role"]=="Provider" and p["provider_key"]!=user["provider_key"]: return None
        if user["role"]=="Billing": return None
        return p

def provider_overview(user, month):
    pk=user["provider_key"]
    with monthdb(month) as m:
        s=m.execute("SELECT * FROM provider_monthly_summary WHERE provider_key=?",(pk,)).fetchone()
        pt=m.execute("SELECT * FROM provider_time_summary_v16_1 WHERE provider_key=?",(pk,)).fetchone() if _object_exists(m,"provider_time_summary_v16_1",("table",)) else None
        q=list(m.execute("""SELECT patient_key,session_count,active_days,total_minutes,completion_rate,engagement_index,last_activity_utc,review_suggested,treatment_goal_progress
          FROM patient_monthly_summary WHERE provider_key=? ORDER BY review_suggested DESC, engagement_index ASC LIMIT 8""",(pk,)))
        tod=m.execute("""SELECT SUM(morning_sessions) morning,SUM(afternoon_sessions) afternoon,SUM(evening_sessions) evening,SUM(night_sessions) night
          FROM patient_monthly_summary WHERE provider_key=?""",(pk,)).fetchone()
        m.execute("ATTACH DATABASE ? AS coredb",(str(CORE_DB),))
        platform=[dict(r) for r in m.execute("""SELECT p.platform_preference label,COUNT(*) value FROM patient_monthly_summary pm
          JOIN coredb.patients p USING(patient_key) WHERE pm.provider_key=? GROUP BY p.platform_preference ORDER BY value DESC""",(pk,))]
        patterns=[dict(r) for r in m.execute("""SELECT p.trend_pattern label,COUNT(*) value FROM patient_monthly_summary pm
          JOIN coredb.patients p USING(patient_key) WHERE pm.provider_key=? GROUP BY p.trend_pattern ORDER BY value DESC""",(pk,))]
    keys=[x["patient_key"] for x in q]; pmap={}
    if keys:
        with core() as c:
            for r in c.execute(f"SELECT patient_key,patient_id,platform_preference,trend_pattern,preferred_time_band,treatment_goal_type FROM patients WHERE patient_key IN ({','.join('?'*len(keys))})",keys): pmap[r["patient_key"]]=dict(r)
    queue=[]
    for r in q:
        d=dict(r); d.update(pmap.get(r["patient_key"],{})); queue.append(d)
    trend=[]
    for mon in available_months():
        with monthdb(mon) as md:
            r=md.execute("SELECT active_patients,total_sessions,total_minutes,avg_completion_rate,avg_engagement_index,avg_session_quality,provider_review_minutes,communication_minutes FROM provider_monthly_summary WHERE provider_key=?",(pk,)).fetchone()
            if r:
                item={"month":mon,**dict(r)}
                if _object_exists(md,"provider_time_summary_v16_1",("table",)):
                    ptx=md.execute("SELECT data_review_minutes,communication_minutes AS provider_communication_minutes,documentation_minutes,total_provider_minutes,patients_with_provider_activity FROM provider_time_summary_v16_1 WHERE provider_key=?",(pk,)).fetchone()
                    if ptx:item.update(dict(ptx))
                trend.append(item)
    with core() as c:
        pr=c.execute("SELECT p.provider_id,p.provider_display_name,p.region_code,s.patient_count FROM providers p JOIN provider_summary s USING(provider_key) WHERE p.provider_key=?",(pk,)).fetchone()
    month_data=dict(s) if s else {}
    if pt: month_data.update({f"provider_time_{k}":v for k,v in dict(pt).items() if k!='provider_key'})
    active=month_data.get('active_patients',0) or 0
    month_data['active_rate']=round(active/max(month_data.get('patient_count',1) or 1,1)*100,1)
    month_data['sessions_per_active']=round((month_data.get('total_sessions',0) or 0)/max(active,1),1)
    return {"provider":dict(pr),"month":month_data,"queue":queue,"trend":trend,"platform_mix":platform,"engagement_patterns":patterns,
            "time_of_day":{"Morning":tod['morning'] or 0,"Afternoon":tod['afternoon'] or 0,"Evening":tod['evening'] or 0,"Night":tod['night'] or 0}}

def admin_overview(month):
    meta=metadata()
    with monthdb(month) as m:
        g=m.execute("""SELECT COUNT(*) providers,SUM(patient_count) patients,SUM(active_patients) active_patients,SUM(review_suggested_patients) review_suggested,
          SUM(total_sessions) sessions,SUM(total_minutes) minutes,AVG(avg_completion_rate) completion,AVG(avg_engagement_index) engagement,AVG(avg_session_quality) quality,
          SUM(provider_review_minutes) provider_review_minutes,SUM(communication_minutes) communication_minutes FROM provider_monthly_summary""").fetchone()
        ptg=m.execute("""SELECT SUM(patients_with_provider_activity) patients_with_provider_activity,SUM(data_review_minutes) data_review_minutes,SUM(communication_minutes) provider_communication_minutes,SUM(documentation_minutes) documentation_minutes,SUM(total_provider_minutes) total_provider_minutes,SUM(data_review_count) data_review_count,SUM(communication_count) provider_communication_count,SUM(documentation_count) documentation_count FROM provider_time_summary_v16_1""").fetchone() if _object_exists(m,"provider_time_summary_v16_1",("table",)) else None
        top=list(m.execute("SELECT provider_key,patient_count,active_patients,review_suggested_patients,total_sessions,total_minutes,avg_completion_rate,avg_engagement_index,avg_session_quality,provider_review_minutes,communication_minutes FROM provider_monthly_summary ORDER BY patient_count DESC LIMIT 12"))
        ptmap={r['provider_key']:dict(r) for r in m.execute("SELECT provider_key,patients_with_provider_activity,data_review_minutes,communication_minutes AS provider_communication_minutes,documentation_minutes,total_provider_minutes FROM provider_time_summary_v16_1") } if _object_exists(m,"provider_time_summary_v16_1",("table",)) else {}
        engagement_bands=[dict(r) for r in m.execute("""SELECT CASE WHEN avg_engagement_index<40 THEN 'Below 40' WHEN avg_engagement_index<60 THEN '40-59' WHEN avg_engagement_index<80 THEN '60-79' ELSE '80+' END label,COUNT(*) value FROM provider_monthly_summary GROUP BY label ORDER BY MIN(avg_engagement_index)""")]
        panel_bands=[dict(r) for r in m.execute("""SELECT CASE WHEN patient_count<250 THEN '<250' WHEN patient_count<500 THEN '250-499' WHEN patient_count<750 THEN '500-749' ELSE '750+' END label,COUNT(*) value FROM provider_monthly_summary GROUP BY label ORDER BY MIN(patient_count)""")]
        workload_bands=[dict(r) for r in m.execute("""SELECT CASE WHEN review_suggested_patients=0 THEN 'No queue' WHEN review_suggested_patients<25 THEN '1-24' WHEN review_suggested_patients<75 THEN '25-74' ELSE '75+' END label,COUNT(*) value FROM provider_monthly_summary GROUP BY label ORDER BY MIN(review_suggested_patients)""")]
        m.execute("ATTACH DATABASE ? AS coredb",(str(CORE_DB),))
        regions=[dict(r) for r in m.execute("""SELECT p.region_code region,COUNT(*) providers,SUM(pm.patient_count) patients,SUM(pm.active_patients) active_patients,SUM(pm.review_suggested_patients) review_suggested,SUM(pm.total_sessions) sessions,AVG(pm.avg_engagement_index) engagement FROM provider_monthly_summary pm JOIN coredb.providers p USING(provider_key) GROUP BY p.region_code ORDER BY patients DESC""")]
    ids={}
    if top:
        with core() as c:
            for r in c.execute("SELECT provider_key,provider_id,provider_display_name,region_code FROM providers WHERE provider_key IN (%s)"%(','.join('?'*len(top))),[x['provider_key'] for x in top]): ids[r['provider_key']]=dict(r)
    trend=[]
    for mon in available_months():
        with monthdb(mon) as md:
            r=md.execute("""SELECT SUM(patient_count) patients,SUM(active_patients) active_patients,SUM(review_suggested_patients) review_suggested,SUM(total_sessions) sessions,SUM(total_minutes) minutes,AVG(avg_completion_rate) completion,AVG(avg_engagement_index) engagement,AVG(avg_session_quality) quality,SUM(provider_review_minutes) provider_review_minutes,SUM(communication_minutes) communication_minutes FROM provider_monthly_summary""").fetchone()
            item={"month":mon,**dict(r)}
            if _object_exists(md,"provider_time_summary_v16_1",("table",)):
                x=md.execute("SELECT SUM(data_review_minutes) data_review_minutes,SUM(communication_minutes) provider_communication_minutes,SUM(documentation_minutes) documentation_minutes,SUM(total_provider_minutes) total_provider_minutes FROM provider_time_summary_v16_1").fetchone(); item.update(dict(x))
            trend.append(item)
    month_data=dict(g)
    if ptg: month_data.update(dict(ptg))
    active=month_data.get('active_patients',0) or 0; patients=month_data.get('patients',0) or 0
    month_data['active_rate']=round(active/max(patients,1)*100,1)
    month_data['sessions_per_active']=round((month_data.get('sessions',0) or 0)/max(active,1),2)
    month_data['review_hours']=round((month_data.get('data_review_minutes',month_data.get('provider_review_minutes',0)) or 0)/60,1)
    month_data['communication_hours']=round((month_data.get('provider_communication_minutes',month_data.get('communication_minutes',0)) or 0)/60,1)
    month_data['documentation_hours']=round((month_data.get('documentation_minutes',0) or 0)/60,1)
    month_data['provider_time_hours']=round((month_data.get('total_provider_minutes',0) or 0)/60,1)
    month_data['review_rate']=round((month_data.get('review_suggested',0) or 0)/max(active,1)*100,1)
    top_rows=[]
    for r in top:
        d={**dict(r),**ids.get(r['provider_key'],{}),**ptmap.get(r['provider_key'],{})}; top_rows.append(d)
    return {"metadata":meta,"month":month_data,"top_providers":top_rows,"trend":trend,"regions":regions,"engagement_bands":engagement_bands,"panel_bands":panel_bands,"workload_bands":workload_bands}

def provider_time_analytics(user, month):
    """V20 provider-time analytics sourced from V18.2 billing master when available."""
    if user['role']=='Billing': return {'error':'Billing role uses the billing review workspace.'}
    if has_v18_month(month):
        with billingdb(month) as b:
            if user['role']=='Provider':
                pk=user['provider_key']
                row=b.execute('SELECT * FROM provider_billing_month_summary WHERE provider_key=?',(pk,)).fetchone(); k=dict(row) if row else {}
                top=[dict(x) for x in b.execute("SELECT * FROM patient_billing_month_summary WHERE provider_key=? AND total_provider_minutes>0 ORDER BY total_provider_minutes DESC LIMIT 20",(pk,))]
                keys=[x['patient_key'] for x in top]
                if keys:
                    marks=','.join('?'*len(keys))
                    with core() as c:names={x['patient_key']:x['patient_id'] for x in c.execute(f'SELECT patient_key,patient_id FROM patients WHERE patient_key IN ({marks})',keys)}
                    for x in top:x['patient_id']=names.get(x['patient_key'],str(x['patient_key']))
                intensity=[]
                for label,lo,hi in [('Light',0,15),('Moderate',15,45),('High',45,90),('Very High',90,1e12)]:
                    n=b.execute('SELECT COUNT(*) FROM patient_billing_month_summary WHERE provider_key=? AND total_provider_minutes>? AND total_provider_minutes<=?',(pk,lo,hi)).fetchone()[0]
                    intensity.append({'label':label,'value':n})
                top_providers=[]
            else:
                row=b.execute('SELECT COUNT(*) providers,SUM(assigned_patient_count) patient_count,SUM(patients_with_provider_activity) patients_with_provider_activity,SUM(data_review_count) data_review_count,SUM(data_review_minutes) data_review_minutes,SUM(communication_count) communication_count,SUM(communication_minutes) communication_minutes,SUM(documentation_count) documentation_count,SUM(documentation_minutes) documentation_minutes,SUM(total_provider_minutes) total_provider_minutes,SUM(real_time_communication_count) realtime_communication_count FROM provider_billing_month_summary').fetchone(); k=dict(row)
                top_providers=[dict(x) for x in b.execute('SELECT * FROM provider_billing_month_summary ORDER BY total_provider_minutes DESC LIMIT 20')]
                pks=[x['provider_key'] for x in top_providers]
                if pks:
                    marks=','.join('?'*len(pks))
                    with core() as c:names={x['provider_key']:dict(x) for x in c.execute(f'SELECT provider_key,provider_id,provider_display_name,region_code FROM providers WHERE provider_key IN ({marks})',pks)}
                    for x in top_providers:x.update(names.get(x['provider_key'],{}))
                top=[]
                intensity=[]
                for label,lo,hi in [('<2 hr',0,120),('2-5 hr',120,300),('5-10 hr',300,600),('10+ hr',600,1e12)]:
                    n=b.execute('SELECT COUNT(*) FROM provider_billing_month_summary WHERE total_provider_minutes>? AND total_provider_minutes<=?',(lo,hi)).fetchone()[0]
                    intensity.append({'label':label,'value':n})
        trend=[]
        for mon in available_months():
            if not has_v18_month(mon): continue
            with billingdb(mon) as bb:
                if user['role']=='Provider':x=bb.execute('SELECT data_review_minutes,communication_minutes,documentation_minutes,total_provider_minutes,patients_with_provider_activity FROM provider_billing_month_summary WHERE provider_key=?',(user['provider_key'],)).fetchone()
                else:x=bb.execute('SELECT SUM(data_review_minutes) data_review_minutes,SUM(communication_minutes) communication_minutes,SUM(documentation_minutes) documentation_minutes,SUM(total_provider_minutes) total_provider_minutes,SUM(patients_with_provider_activity) patients_with_provider_activity FROM provider_billing_month_summary').fetchone()
            if x:trend.append({'month':mon,**dict(x)})
        return {'available':True,'source':'V18','month':month,'kpis':k,'activity_mix':[{'label':'Data review','value':float(k.get('data_review_minutes') or 0)},{'label':'Communication','value':float(k.get('communication_minutes') or 0)},{'label':'Documentation','value':float(k.get('documentation_minutes') or 0)}],'trend':trend,'intensity':intensity,'top_patients':top,'top_providers':top_providers}
    return {'available':False,'month':month}

def list_patients(user, month, page=1, size=25, search="", review="", platform=""):
    """Fast patient directory for multi-million-row datasets.

    Avoids a 5M-row cross-database JOIN for the common directory path. Monthly
    keys are paged first using month indexes; only the visible patient keys are
    then resolved against the core database. Search/platform filters are resolved
    from the indexed core table first.
    """
    size=max(10,min(size,100)); page=max(page,1); off=(page-1)*size
    search=(search or "").strip(); platform=(platform or "").strip()
    review = review if review in {"1","0"} else ""

    # Core-filtered path (patient-id prefix and/or platform). This uses core indexes
    # and only asks the monthly DB for the small page of patient keys.
    if search or platform:
        cwh=["is_deleted=0"]; cparams=[]
        if user["role"]=="Provider": cwh.append("provider_key=?"); cparams.append(user["provider_key"])
        if search:
            # Prefix search keeps the UNIQUE patient_id index usable.
            cwh.append("patient_id LIKE ?"); cparams.append(search + "%")
        if platform: cwh.append("platform_preference=?"); cparams.append(platform)
        cwhere=" AND ".join(cwh)
        with core() as c:
            total_core=c.execute(f"SELECT COUNT(*) n FROM patients WHERE {cwhere}",cparams).fetchone()["n"]
            # For review filtering, pull a bounded candidate window and apply the
            # monthly review flag. Searches are usually narrow; platform-only review
            # filtering is intentionally capped per page to keep latency bounded.
            candidate_limit = size if not review else max(size*20,500)
            cand=[dict(r) for r in c.execute(f"""SELECT patient_key,patient_id,platform_preference,trend_pattern,preferred_time_band,treatment_goal_type
              FROM patients WHERE {cwhere} ORDER BY patient_key LIMIT ? OFFSET ?""",cparams+[candidate_limit, off if not review else 0])]
        if not cand: return {"page":page,"page_size":size,"total":0 if review else total_core,"rows":[]}
        keys=[x["patient_key"] for x in cand]; marks=','.join('?' for _ in keys)
        with monthdb(month) as m:
            mrows={r["patient_key"]:dict(r) for r in m.execute(f"""SELECT patient_key,session_count,active_days,total_minutes,completion_rate,engagement_index,last_activity_utc,review_suggested,treatment_goal_progress
              FROM patient_monthly_summary WHERE patient_key IN ({marks})""",keys)}
            ptrows={}
            if _object_exists(m,"patient_provider_time_v16_1"):
                ptrows={r["patient_key"]:dict(r) for r in m.execute(f"SELECT patient_key,total_provider_minutes,data_review_minutes,communication_minutes,documentation_minutes FROM patient_provider_time_v16_1 WHERE patient_key IN ({marks})",keys)}
        rows=[]
        for c in cand:
            mr=mrows.get(c["patient_key"]);
            if not mr: continue
            if review and str(mr.get("review_suggested",0))!=review: continue
            rows.append({**c,**mr,**ptrows.get(c["patient_key"],{})})
            if len(rows)>=size: break
        total = len(rows) if review and search else (total_core if not review else max(len(rows),0))
        return {"page":page,"page_size":size,"total":total,"rows":rows,"bounded_filter":bool(review and platform and not search)}

    # Fast monthly path: no cross-database join. Counts and page keys are resolved
    # entirely in the monthly DB, then the visible 25 core rows are fetched by PK.
    mwh=[]; params=[]
    if user["role"]=="Provider": mwh.append("provider_key=?"); params.append(user["provider_key"])
    if review: mwh.append("review_suggested=?"); params.append(int(review))
    where=(" WHERE "+" AND ".join(mwh)) if mwh else ""
    with monthdb(month) as m:
        if not mwh and not review:
            # All five million patient-month rows exist by design; use core metadata
            # instead of scanning the monthly table for COUNT(*).
            total=int(metadata().get("patient_count",0))
        else:
            total=m.execute(f"SELECT COUNT(*) n FROM patient_monthly_summary{where}",params).fetchone()["n"]
        mpage=[dict(r) for r in m.execute(f"""SELECT patient_key,session_count,active_days,total_minutes,completion_rate,engagement_index,last_activity_utc,review_suggested,treatment_goal_progress
          FROM patient_monthly_summary{where}
          ORDER BY review_suggested DESC, patient_key LIMIT ? OFFSET ?""",params+[size,off])]
    if not mpage: return {"page":page,"page_size":size,"total":total,"rows":[]}
    keys=[x["patient_key"] for x in mpage]; marks=','.join('?' for _ in keys)
    with core() as c:
        cmap={r["patient_key"]:dict(r) for r in c.execute(f"""SELECT patient_key,patient_id,platform_preference,trend_pattern,preferred_time_band,treatment_goal_type
          FROM patients WHERE patient_key IN ({marks}) AND is_deleted=0""",keys)}
    ptmap={}
    with monthdb(month) as m:
        if _object_exists(m,"patient_provider_time_v16_1"):
            ptmap={r["patient_key"]:dict(r) for r in m.execute(f"SELECT patient_key,total_provider_minutes,data_review_minutes,communication_minutes,documentation_minutes FROM patient_provider_time_v16_1 WHERE patient_key IN ({marks})",keys)}
    rows=[{**cmap.get(x["patient_key"],{}),**x,**ptmap.get(x["patient_key"],{})} for x in mpage if x["patient_key"] in cmap]
    return {"page":page,"page_size":size,"total":total,"rows":rows}

# ---------- V18 billing-master integration ----------
def v18_provider_profile(provider_key):
    if not BILLING_CORE_DB.exists(): return {}
    with billing_core() as c:
        x=c.execute("SELECT * FROM provider_activity_profile WHERE provider_key=?",(provider_key,)).fetchone()
    if not x: return {}
    d=dict(x); d["preferred_work_window"]=f"{int(d.get('work_start_hour',0)):02d}:00-{int(d.get('work_end_hour',0)):02d}:00"; return d

def v18_patient_month(patient_key, month):
    if not has_v18_month(month): return None
    with billingdb(month) as c:
        summary=c.execute("SELECT * FROM patient_billing_month_summary WHERE patient_key=?",(patient_key,)).fetchone()
        acts=[dict(x) for x in c.execute("SELECT * FROM provider_activity_event WHERE patient_key=? ORDER BY activity_start_utc DESC",(patient_key,))]
        sessions=[dict(x) for x in c.execute("SELECT * FROM patient_session_event WHERE patient_key=? ORDER BY session_start_utc DESC",(patient_key,))]
        audits=[dict(x) for x in c.execute("SELECT * FROM provider_activity_audit_event WHERE patient_key=? ORDER BY event_utc DESC LIMIT 160",(patient_key,))]
    return {"summary":dict(summary) if summary else {},"activities":acts,"sessions":sessions,"audits":audits}

def v18_provider_access(provider_key, month):
    if not has_v18_month(month): return []
    with billingdb(month) as c:
        return [dict(x) for x in c.execute("SELECT * FROM provider_access_session WHERE provider_key=? ORDER BY login_utc DESC LIMIT 80",(provider_key,))]

def v18_patient_history(patient_key):
    out=[]
    for mon in available_months():
        if not has_v18_month(mon): continue
        with billingdb(mon) as c:
            x=c.execute("SELECT * FROM patient_billing_month_summary WHERE patient_key=?",(patient_key,)).fetchone()
        if x: out.append({"month":mon,**dict(x)})
    return out

def v18_billing_patient_queue(month,page=1,size=25,provider_key=None):
    size=max(10,min(size,100)); off=(max(page,1)-1)*size
    if not has_v18_month(month): return {"page":page,"page_size":size,"total":0,"rows":[]}
    with billingdb(month) as b:
        wh=' WHERE total_provider_minutes>0'; params=[]
        if provider_key is not None: wh+=' AND provider_key=?'; params.append(provider_key)
        total=b.execute(f"SELECT COUNT(*) FROM patient_billing_month_summary{wh}",params).fetchone()[0]
        rows=[dict(x) for x in b.execute(f"SELECT * FROM patient_billing_month_summary{wh} ORDER BY total_provider_minutes DESC,patient_key LIMIT ? OFFSET ?",params+[size,off])]
    if not rows:return {"page":page,"page_size":size,"total":total,"rows":[]}
    pkeys=[x['patient_key'] for x in rows]; marks=','.join('?'*len(pkeys))
    with core() as c:
        meta={x['patient_key']:dict(x) for x in c.execute(f"SELECT p.patient_key,p.patient_id,p.provider_key,pr.provider_id,pr.provider_display_name FROM patients p JOIN providers pr ON pr.provider_key=p.provider_key WHERE p.patient_key IN ({marks})",pkeys)}
    return {"page":page,"page_size":size,"total":total,"rows":[{**meta.get(x['patient_key'],{}),**x} for x in rows]}

def v18_billing_provider_queue(month,page=1,size=25):
    size=max(10,min(size,100)); off=(max(page,1)-1)*size
    if not has_v18_month(month): return {"page":page,"page_size":size,"total":0,"rows":[]}
    with billingdb(month) as b:
        total=b.execute("SELECT COUNT(*) FROM provider_billing_month_summary").fetchone()[0]
        rows=[dict(x) for x in b.execute("SELECT * FROM provider_billing_month_summary ORDER BY total_provider_minutes DESC LIMIT ? OFFSET ?",(size,off))]
    pks=[x['provider_key'] for x in rows]
    if pks:
        marks=','.join('?'*len(pks))
        with core() as c: meta={x['provider_key']:dict(x) for x in c.execute(f"SELECT provider_key,provider_id,provider_display_name,region_code FROM providers WHERE provider_key IN ({marks})",pks)}
    else: meta={}
    return {"page":page,"page_size":size,"total":total,"rows":[{**meta.get(x['provider_key'],{}),**x} for x in rows]}

def v18_billing_provider_detail(provider_id,month):
    with core() as c:
        p=c.execute("SELECT provider_key,provider_id,provider_display_name,region_code FROM providers WHERE provider_id=?",(provider_id,)).fetchone()
    if not p or not has_v18_month(month): return None
    with billingdb(month) as b:
        s=b.execute("SELECT * FROM provider_billing_month_summary WHERE provider_key=?",(p['provider_key'],)).fetchone()
        patients=[dict(x) for x in b.execute("SELECT * FROM patient_billing_month_summary WHERE provider_key=? AND total_provider_minutes>0 ORDER BY total_provider_minutes DESC LIMIT 250",(p['provider_key'],))]
        access=[dict(x) for x in b.execute("SELECT * FROM provider_access_session WHERE provider_key=? ORDER BY login_utc DESC LIMIT 100",(p['provider_key'],))]
    if patients:
        keys=[x['patient_key'] for x in patients]; marks=','.join('?'*len(keys))
        with core() as c: names={x['patient_key']:x['patient_id'] for x in c.execute(f"SELECT patient_key,patient_id FROM patients WHERE patient_key IN ({marks})",keys)}
        for x in patients:x['patient_id']=names.get(x['patient_key'],str(x['patient_key']))
    return {"provider":dict(p),"summary":dict(s) if s else {},"patients":patients,"access_sessions":access,"profile":v18_provider_profile(p['provider_key'])}

def patient_payload(user, patient_id, month):
    p=authorize_patient(user,patient_id)
    if not p: return None
    # Patient engagement detail and provider activity detail are materialized independently.
    materialize(patient_id,month)
    materialize_provider_timeline(patient_id,month)
    with monthdb(month) as m:
        ms=m.execute("SELECT * FROM patient_monthly_summary WHERE patient_key=?",(p["patient_key"],)).fetchone()
    history=[]
    for mon in available_months():
        with monthdb(mon) as md:
            h=md.execute("""SELECT session_count,mobile_sessions,xr_sessions,remote_sessions,clinic_sessions,active_days,total_minutes,avg_duration_min,completion_rate,engagement_index,session_quality,morning_sessions,afternoon_sessions,evening_sessions,night_sessions,last_activity_utc,provider_review_count,provider_review_minutes,communication_count,communication_minutes,treatment_goal_progress,review_suggested FROM patient_monthly_summary WHERE patient_key=?""",(p["patient_key"],)).fetchone()
            item={"month":mon,**dict(h)} if h else {"month":mon}
            if _object_exists(md,"patient_provider_time_v16_1"):
                pt=md.execute("SELECT data_review_count,data_review_minutes,communication_count AS provider_communication_count,communication_minutes AS provider_communication_minutes,documentation_count,documentation_minutes,total_provider_minutes,real_time_communication_count FROM patient_provider_time_v16_1 WHERE patient_key=?",(p["patient_key"],)).fetchone()
                if pt: item.update(dict(pt))
            history.append(item)
    with core() as c:
        modules={r["module_key"]:dict(r) for r in c.execute("SELECT * FROM module_catalog WHERE is_active=1")}
        prov=c.execute("SELECT provider_id,provider_display_name,region_code FROM providers WHERE provider_key=?",(p["provider_key"],)).fetchone()
    r=runtime()
    events=[dict(x) for x in r.execute("SELECT * FROM session_events WHERE patient_key=? AND billing_month=? ORDER BY session_start_utc DESC",(p["patient_key"],month))]
    reviews=[dict(x) for x in r.execute("SELECT * FROM provider_reviews WHERE patient_key=? AND billing_month=? ORDER BY review_start_utc DESC",(p["patient_key"],month))]
    comms=[dict(x) for x in r.execute("SELECT * FROM communications WHERE patient_key=? AND billing_month=? ORDER BY occurred_utc DESC",(p["patient_key"],month))]
    notes=[dict(x) for x in r.execute("SELECT * FROM provider_notes_v17 WHERE patient_key=? AND billing_month=? ORDER BY created_at_utc DESC",(p["patient_key"],month))]
    activities=[]; daily=[]; provider_profile={}; activity_audit=[]
    if _object_exists(r,"provider_patient_activity_v16_1",("table",)):
        activities=[dict(x) for x in r.execute("SELECT * FROM provider_patient_activity_v16_1 WHERE patient_key=? AND provider_key=? AND billing_month=? ORDER BY activity_start_utc DESC",(p["patient_key"],p["provider_key"],month))]
        for x in activities:
            x['display_start']=us_dt(x.get('activity_start_utc')); x['display_end']=us_dt(x.get('activity_end_utc'))
            try: x['metadata']=json.loads(x.get('metadata_json') or '{}')
            except: x['metadata']={}
    if _object_exists(r,"provider_patient_daily_summary_v16_1",("table",)):
        daily=[dict(x) for x in r.execute("SELECT * FROM provider_patient_daily_summary_v16_1 WHERE patient_key=? AND provider_key=? AND billing_month=? ORDER BY activity_date",(p["patient_key"],p["provider_key"],month))]
    if _object_exists(r,"provider_activity_profiles",("table",)):
        q=r.execute("SELECT * FROM provider_activity_profiles WHERE provider_key=?",(p["provider_key"],)).fetchone(); provider_profile=dict(q) if q else {}
    if _object_exists(r,"provider_activity_audit_v16_1",("table",)):
        activity_audit=[dict(x) for x in r.execute("SELECT * FROM provider_activity_audit_v16_1 WHERE patient_key=? AND provider_key=? AND billing_month=? ORDER BY event_utc DESC LIMIT 120",(p["patient_key"],p["provider_key"],month))]
    r.close()
    byplat={"Mobile":{},"XR":{}}; hours=[0]*24; periods={"Morning":0,"Afternoon":0,"Evening":0,"Night":0}
    active=set(); remote_active=set()
    for e in events:
        mod=modules.get(e["module_key"],{"module_name":"Unknown","program_group":""}); e["module_name"]=mod["module_name"]; e["program_group"]=mod["program_group"]; e["display_start"]=us_dt(e["session_start_utc"])
        d=datetime.fromisoformat(e["session_start_utc"]).astimezone(EASTERN); h=d.hour; hours[h]+=1; active.add(d.date().isoformat())
        if e["remote_or_clinic"]=="Remote": remote_active.add(d.date().isoformat())
        band="Morning" if 5<=h<12 else "Afternoon" if 12<=h<17 else "Evening" if 17<=h<22 else "Night"; periods[band]+=1
        bm=byplat[e["platform"]].setdefault(e["module_key"],{"module_key":e["module_key"],"module_name":mod["module_name"],"program_group":mod["program_group"],"sessions":0,"minutes":0.0,"completed":0,"last_activity":e["session_start_utc"]})
        bm["sessions"]+=1; bm["minutes"]+=float(e["duration_minutes"]); bm["completed"]+=int(e["completed"]); bm["last_activity"]=max(bm["last_activity"],e["session_start_utc"])
    for plat in byplat:
        for v in byplat[plat].values():
            v["minutes"]=round(v["minutes"],1); v["avg_duration"]=round(v["minutes"]/max(v["sessions"],1),1); v["completion_rate"]=round(v["completed"]/max(v["sessions"],1)*100,1); v["last_activity_display"]=us_dt(v["last_activity"])
    provider_time=provider_month_time(p["patient_key"],month)
    # Runtime detailed timeline becomes authoritative for the selected patient/month after materialization.
    if activities:
        review_acts=[x for x in activities if x['activity_type']=='DATA_REVIEW']; comm_acts=[x for x in activities if x['activity_type']=='COMMUNICATION']; doc_acts=[x for x in activities if x['activity_type']=='DOCUMENTATION']
        provider_time.update({
            'data_review_count':len(review_acts),'data_review_minutes':round(sum(float(x['duration_minutes'] or 0) for x in review_acts),2),
            'communication_count':len(comm_acts),'communication_minutes':round(sum(float(x['duration_minutes'] or 0) for x in comm_acts),2),
            'documentation_count':len(doc_acts),'documentation_minutes':round(sum(float(x['duration_minutes'] or 0) for x in doc_acts),2),
            'total_provider_minutes':round(sum(float(x['duration_minutes'] or 0) for x in activities),2),
            'real_time_communication_count':sum(1 for x in comm_acts if str(x.get('interaction_mode','')).lower().startswith('real-time')),
            'activity_days':len({x.get('activity_date') for x in activities}),
        })
    provider_time.setdefault('data_review_count',0); provider_time.setdefault('data_review_minutes',0)
    provider_time.setdefault('communication_count',0); provider_time.setdefault('communication_minutes',0)
    provider_time.setdefault('documentation_count',0); provider_time.setdefault('documentation_minutes',0)
    provider_time.setdefault('total_provider_minutes',round(float(provider_time.get('data_review_minutes',0))+float(provider_time.get('communication_minutes',0))+float(provider_time.get('documentation_minutes',0)),2))
    provider_time.setdefault('real_time_communication_count',0)
    billing={
        "data_review_count":int(provider_time.get('data_review_count') or 0),"data_review_minutes":round(float(provider_time.get('data_review_minutes') or 0),2),
        "communication_count":int(provider_time.get('communication_count') or 0),"communication_minutes":round(float(provider_time.get('communication_minutes') or 0),2),
        "documentation_count":int(provider_time.get('documentation_count') or 0),"documentation_minutes":round(float(provider_time.get('documentation_minutes') or 0),2),
        "total_provider_minutes":round(float(provider_time.get('total_provider_minutes') or 0),2),
        "real_time_communication_count":int(provider_time.get('real_time_communication_count') or 0),
        "real_time_communication_present":int(provider_time.get('real_time_communication_count') or 0)>0,
        "remote_sessions":sum(e["remote_or_clinic"]=="Remote" for e in events),"remote_active_days":len(remote_active),"status":"REQUIRES_REVIEW"
    }
    insights={}
    if history:
        cur=next((x for x in history if x['month']==month),history[-1]); idx=history.index(cur); prev=history[idx-1] if idx>0 else None
        insights={"engagement_change":round(float(cur.get('engagement_index') or 0)-float(prev.get('engagement_index') or 0),1) if prev else None,
                  "session_change":int(cur.get('session_count') or 0)-int(prev.get('session_count') or 0) if prev else None,
                  "completion_change":round(float(cur.get('completion_rate') or 0)-float(prev.get('completion_rate') or 0),1) if prev else None,
                  "provider_time_change":round(float(cur.get('total_provider_minutes') or 0)-float(prev.get('total_provider_minutes') or 0),1) if prev else None}
    # V18.2 billing master is the authoritative detailed source when present.
    v18=v18_patient_month(p["patient_key"],month) if has_v18_month(month) else None
    provider_access=[]
    if v18:
        vs=v18.get("summary",{})
        if vs:
            provider_time={"data_review_count":vs.get("data_review_count",0),"data_review_minutes":vs.get("data_review_minutes",0),"communication_count":vs.get("communication_count",0),"communication_minutes":vs.get("communication_minutes",0),"documentation_count":vs.get("documentation_count",0),"documentation_minutes":vs.get("documentation_minutes",0),"total_provider_minutes":vs.get("total_provider_minutes",0),"real_time_communication_count":vs.get("real_time_communication_count",0),"activity_days":vs.get("provider_activity_days",0),"first_provider_activity_utc":vs.get("first_provider_activity_utc"),"last_provider_activity_utc":vs.get("last_provider_activity_utc")}
            billing={**provider_time,"real_time_communication_present":int(vs.get("real_time_communication_count",0) or 0)>0,"remote_sessions":vs.get("patient_remote_sessions",0),"remote_active_days":vs.get("patient_active_days",0),"status":vs.get("billing_status","REQUIRES_REVIEW")}
        activities=v18.get("activities",[])
        for x in activities:
            x["display_start"]=x.get("activity_start_eastern") or us_dt(x.get("activity_start_utc")); x["display_end"]=x.get("activity_end_eastern") or us_dt(x.get("activity_end_utc"))
        activity_audit=v18.get("audits",[])
        provider_profile=v18_provider_profile(p["provider_key"])
        provider_access=v18_provider_access(p["provider_key"],month)
        # Replace session detail with the fully materialized V18 master events.
        ve=[]
        for x in v18.get("sessions",[]):
            mod=modules.get(x.get("module_key"),{"module_name":"Unknown","program_group":""})
            ve.append({"event_id":x.get("patient_session_id"),"patient_key":x.get("patient_key"),"provider_key":x.get("provider_key"),"billing_month":month,"platform":x.get("platform"),"module_key":x.get("module_key"),"module_name":mod.get("module_name"),"program_group":mod.get("program_group"),"session_start_utc":x.get("session_start_utc"),"session_end_utc":x.get("session_end_utc"),"display_start":x.get("session_start_eastern") or us_dt(x.get("session_start_utc")),"duration_minutes":x.get("duration_minutes"),"remote_or_clinic":x.get("remote_or_clinic"),"completed":x.get("completed"),"event_count":x.get("event_count"),"image_used":x.get("image_used")})
        events=ve
        # rebuild module/time detail from V18 sessions
        byplat={"Mobile":{},"XR":{}}; hours=[0]*24; periods={"Morning":0,"Afternoon":0,"Evening":0,"Night":0}; remote_active=set()
        for e in events:
            try:d=datetime.fromisoformat(e["session_start_utc"]).astimezone(EASTERN)
            except:continue
            hours[d.hour]+=1; band="Morning" if 5<=d.hour<12 else "Afternoon" if d.hour<17 else "Evening" if d.hour<22 else "Night"; periods[band]+=1
            if e.get("remote_or_clinic")=="Remote":remote_active.add(d.date().isoformat())
            bm=byplat[e["platform"]].setdefault(e["module_key"],{"module_key":e["module_key"],"module_name":e["module_name"],"program_group":e["program_group"],"sessions":0,"minutes":0.0,"completed":0,"last_activity":e["session_start_utc"]})
            bm["sessions"]+=1; bm["minutes"]+=float(e.get("duration_minutes") or 0); bm["completed"]+=int(e.get("completed") or 0); bm["last_activity"]=max(bm["last_activity"],e["session_start_utc"])
        for plat in byplat:
            for v in byplat[plat].values():v["minutes"]=round(v["minutes"],1);v["avg_duration"]=round(v["minutes"]/max(v["sessions"],1),1);v["completion_rate"]=round(v["completed"]/max(v["sessions"],1)*100,1);v["last_activity_display"]=us_dt(v["last_activity"])
        # derived daily provider totals
        dmap={}
        for a in activities:
            day=(a.get("activity_start_eastern") or a.get("activity_start_utc") or "")[:10]; z=dmap.setdefault(day,{"activity_date":day,"data_review_minutes":0,"communication_minutes":0,"documentation_minutes":0,"total_provider_minutes":0})
            mins=float(a.get("duration_minutes") or 0); z["total_provider_minutes"]+=mins
            if a.get("activity_type")=="DATA_REVIEW":z["data_review_minutes"]+=mins
            elif a.get("activity_type")=="COMMUNICATION":z["communication_minutes"]+=mins
            elif a.get("activity_type")=="DOCUMENTATION":z["documentation_minutes"]+=mins
        daily=sorted(dmap.values(),key=lambda x:x["activity_date"])
        # overlay V18 monthly provider-time history
        vh={x["month"]:x for x in v18_patient_history(p["patient_key"])}
        for item in history:
            z=vh.get(item["month"]);
            if z:item.update({"data_review_count":z.get("data_review_count",0),"data_review_minutes":z.get("data_review_minutes",0),"provider_communication_count":z.get("communication_count",0),"provider_communication_minutes":z.get("communication_minutes",0),"documentation_count":z.get("documentation_count",0),"documentation_minutes":z.get("documentation_minutes",0),"total_provider_minutes":z.get("total_provider_minutes",0)})
    return {"patient":dict(p),"provider":dict(prov),"month_summary":dict(ms) if ms else {},"history":history,"mobile_modules":sorted(byplat["Mobile"].values(),key=lambda x:x["sessions"],reverse=True),"xr_modules":sorted(byplat["XR"].values(),key=lambda x:x["sessions"],reverse=True),"time_of_day":periods,"hourly":hours,"events":events[:250],"reviews":reviews,"communications":comms,"notes":notes,"provider_time":provider_time,"provider_activities":activities,"provider_daily":daily,"provider_profile":provider_profile,"provider_access":provider_access,"provider_activity_audit":activity_audit,"billing":billing,"goal":{"title":p["treatment_goal_type"],"progress_percent":round(float(ms["treatment_goal_progress"] or 0),1) if ms else 0},"insights":insights,"billing_master":"V18" if v18 else "runtime"}

def billing_queue(month, page=1, size=25):
    return v18_billing_patient_queue(month,page,size)

def billing_evidence(patient_id, month):
    with core() as c:
        p=c.execute("SELECT p.patient_key,p.patient_id,p.provider_key,pr.provider_id,pr.provider_display_name FROM patients p JOIN providers pr ON pr.provider_key=p.provider_key WHERE p.patient_id=? AND p.is_deleted=0",(patient_id,)).fetchone()
    if not p:return None
    v=v18_patient_month(p['patient_key'],month) or {}
    return {"patient":dict(p),"summary":v.get('summary',{}),"timeline":v.get('activities',[]),"sessions":v.get('sessions',[]),"audit":v.get('audits',[]),"profile":v18_provider_profile(p['provider_key'])}

# ---------- PDF ----------
def branded_pdf(payload, month, report_title="Monthly RTM Documentation Support"):
    """Branded patient-specific report with provider time as the primary documentation evidence."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    buf=io.BytesIO()
    navy=colors.HexColor('#1F305E'); coral=colors.HexColor('#FF8282'); slate=colors.HexColor('#586F8A'); cool=colors.HexColor('#E6E8EB'); off=colors.HexColor('#FAFAFB'); amber=colors.HexColor('#E8A33D'); white=colors.white
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='V19Title',parent=styles['Title'],fontName='Helvetica-Bold',fontSize=20,leading=24,textColor=navy,spaceAfter=4))
    styles.add(ParagraphStyle(name='V19Sub',parent=styles['BodyText'],fontName='Helvetica',fontSize=8.5,leading=12,textColor=slate))
    styles.add(ParagraphStyle(name='V19H2',parent=styles['Heading2'],fontName='Helvetica-Bold',fontSize=12,leading=15,textColor=navy,spaceBefore=8,spaceAfter=6))
    styles.add(ParagraphStyle(name='V19Body',parent=styles['BodyText'],fontSize=8.5,leading=12,textColor=navy))
    styles.add(ParagraphStyle(name='V19Small',parent=styles['BodyText'],fontSize=7.3,leading=9.5,textColor=slate))
    doc=SimpleDocTemplate(buf,pagesize=letter,rightMargin=.55*inch,leftMargin=.55*inch,topMargin=.72*inch,bottomMargin=.55*inch,title=f"Reflect {report_title}")
    def page(canvas,doc):
        canvas.saveState(); w,h=letter
        canvas.setFillColor(navy); canvas.rect(0,h-34,w,34,fill=1,stroke=0)
        canvas.setFillColor(coral); canvas.circle(28,h-17,7,fill=1,stroke=0)
        canvas.setFillColor(white); canvas.setFont('Helvetica-Bold',9); canvas.drawString(42,h-20,'REFLECT | PROVIDER DASHBOARD V19')
        canvas.setFillColor(slate); canvas.setFont('Helvetica',7); canvas.drawRightString(w-30,22,f"Synthetic documentation support | Page {doc.page}")
        canvas.setStrokeColor(cool); canvas.line(30,30,w-30,30); canvas.restoreState()
    p=payload['patient']; prov=payload['provider']; m=payload.get('month_summary',{}); b=payload.get('billing',{}); goal=payload.get('goal',{})
    acts=payload.get('provider_activities',[]); hist=payload.get('history',[]); prof=payload.get('provider_profile',{})
    story=[Paragraph(report_title,styles['V19Title']),Paragraph('Patient-specific provider activity and patient engagement evidence for the selected monthly period.',styles['V19Sub']),Spacer(1,9)]
    ident=Table([[Paragraph('<b>Patient</b><br/>'+str(p.get('patient_id','')),styles['V19Body']),Paragraph('<b>Provider</b><br/>'+str(prov.get('provider_display_name',''))+' ('+str(prov.get('provider_id',''))+')',styles['V19Body']),Paragraph('<b>Period</b><br/>'+month,styles['V19Body']),Paragraph('<b>Status</b><br/>REQUIRES REVIEW',styles['V19Body'])]],colWidths=[1.25*inch,2.2*inch,1.15*inch,1.5*inch])
    ident.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),off),('BOX',(0,0),(-1,-1),.6,cool),('INNERGRID',(0,0),(-1,-1),.35,cool),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)])); story += [ident,Spacer(1,10)]
    story += [Paragraph('Provider time for this patient',styles['V19H2']),Paragraph('Provider time is tracked independently from patient application engagement. The values below are documentation support only.',styles['V19Small'])]
    pt=[['Provider activity','Count','Minutes'],['Data review',b.get('data_review_count',0),b.get('data_review_minutes',0)],['Communication',b.get('communication_count',0),b.get('communication_minutes',0)],['Documentation',b.get('documentation_count',0),b.get('documentation_minutes',0)],['TOTAL PROVIDER TIME','',b.get('total_provider_minutes',0)]]
    tab=Table(pt,colWidths=[3.2*inch,1.1*inch,1.8*inch]); tab.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),navy),('TEXTCOLOR',(0,0),(-1,0),white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('BACKGROUND',(0,-1),(-1,-1),colors.HexColor('#FFF3F1')),('FONTNAME',(0,-1),(-1,-1),'Helvetica-Bold'),('TEXTCOLOR',(0,-1),(-1,-1),navy),('ROWBACKGROUNDS',(0,1),(-1,-2),[white,off]),('GRID',(0,0),(-1,-1),.35,cool),('FONTSIZE',(0,0),(-1,-1),8.5),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)])); story += [tab,Spacer(1,9)]
    profile_text=f"Synthetic provider work profile: {prof.get('review_style','—')} review style; {prof.get('preferred_work_window','—')} work window; {prof.get('documentation_cadence','—')} documentation cadence; {prof.get('communication_preference','—')} communication preference."
    story += [Table([[Paragraph('<b>Provider work context</b>',styles['V19Body']),Paragraph(profile_text,styles['V19Small'])]],colWidths=[1.5*inch,4.6*inch],style=TableStyle([('BACKGROUND',(0,0),(-1,-1),off),('BOX',(0,0),(-1,-1),.5,cool),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)])),Spacer(1,10)]
    story += [Paragraph('Patient engagement being monitored',styles['V19H2'])]
    eng=[['Measure','Recorded value'],['Sessions',m.get('session_count',0)],['Reflect Mobile sessions',m.get('mobile_sessions',0)],['Reflect XR sessions',m.get('xr_sessions',0)],['Remote sessions',m.get('remote_sessions',0)],['In-clinic sessions',m.get('clinic_sessions',0)],['Patient engagement minutes',m.get('total_minutes',0)],['Active days',m.get('active_days',0)],['Completion',f"{float(m.get('completion_rate',0) or 0):.1f}%"],['Engagement index',f"{float(m.get('engagement_index',0) or 0):.1f}"],['Goal progress',f"{float(goal.get('progress_percent',0) or 0):.1f}%"]]
    et=Table(eng,colWidths=[3.2*inch,2.9*inch]); et.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),slate),('TEXTCOLOR',(0,0),(-1,0),white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,off]),('GRID',(0,0),(-1,-1),.3,cool),('FONTSIZE',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)])); story += [et,PageBreak()]

    story += [Paragraph('Provider activity timeline',styles['V19Title']),Paragraph('Each activity is tied to one provider, one patient, and one billing month. Patient engagement duration is not included in provider time.',styles['V19Sub']),Spacer(1,8)]
    data=[['Date/time (Eastern)','Activity','Subtype / context','Minutes']]
    for x in acts[:40]:
        context=x.get('activity_subtype','').replace('_',' ').title()
        if x.get('activity_type')=='COMMUNICATION' and x.get('communication_channel'): context += ' · '+str(x.get('communication_channel'))
        data.append([x.get('display_start') or us_dt(x.get('activity_start_utc')),x.get('activity_type','').replace('_',' ').title(),context,f"{float(x.get('duration_minutes',0) or 0):.1f}"])
    if len(data)==1:data.append(['—','No detailed provider activities materialized','—','0'])
    tt=Table(data,colWidths=[1.55*inch,1.25*inch,2.7*inch,.65*inch],repeatRows=1); tt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),navy),('TEXTCOLOR',(0,0),(-1,0),white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,off]),('GRID',(0,0),(-1,-1),.3,cool),('VALIGN',(0,0),(-1,-1),'TOP'),('FONTSIZE',(0,0),(-1,-1),7.2),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)])); story += [tt,Spacer(1,10)]
    story += [Paragraph('Recent monthly comparison',styles['V19H2'])]
    hdata=[['Month','Patient sessions','Mobile','XR','Provider review min','Communication min','Documentation min','Total provider min']]
    for x in hist[-6:]: hdata.append([x.get('month',''),x.get('session_count',0),x.get('mobile_sessions',0),x.get('xr_sessions',0),x.get('data_review_minutes',0),x.get('provider_communication_minutes',x.get('communication_minutes',0)),x.get('documentation_minutes',0),x.get('total_provider_minutes',0)])
    ht=Table(hdata,colWidths=[.68*inch,.74*inch,.52*inch,.48*inch,.88*inch,.9*inch,.9*inch,.92*inch],repeatRows=1); ht.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),coral),('TEXTCOLOR',(0,0),(-1,0),white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,off]),('GRID',(0,0),(-1,-1),.25,cool),('FONTSIZE',(0,0),(-1,-1),6.5),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)])); story += [ht,PageBreak()]

    story += [Paragraph('Module and documentation detail',styles['V19Title']),Paragraph('Reflect Mobile and Reflect XR are separated so the patient activity being reviewed can be interpreted clearly.',styles['V19Sub']),Spacer(1,8)]
    def module_table(title, rows):
        d=[[title,'Program','Sessions','Minutes','Completion']]
        for x in rows[:10]: d.append([x.get('module_name',''),x.get('program_group',''),x.get('sessions',0),f"{float(x.get('minutes',0) or 0):.1f}",f"{float(x.get('completion_rate',0) or 0):.1f}%"])
        if len(d)==1:d.append(['No activity','','0','0','—'])
        tab=Table(d,colWidths=[2.05*inch,1.45*inch,.7*inch,.72*inch,.88*inch],repeatRows=1); tab.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),navy),('TEXTCOLOR',(0,0),(-1,0),white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,off]),('GRID',(0,0),(-1,-1),.3,cool),('FONTSIZE',(0,0),(-1,-1),7.2),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)])); return tab
    story += [module_table('Reflect Mobile',payload.get('mobile_modules',[])),Spacer(1,8),module_table('Reflect XR',payload.get('xr_modules',[])),Spacer(1,10)]
    story += [Paragraph('Documentation notes',styles['V19H2'])]
    notes=payload.get('notes',[])
    nd=[['Date/time','Documentation minutes','Note']]
    for n in notes[:12]:nd.append([us_dt(n.get('created_at_utc')),f"{float(n.get('documentation_minutes',0) or 0):.1f}",n.get('note_text','')])
    if len(nd)==1:nd.append(['—','0','No provider notes recorded in V20.'])
    nt=Table(nd,colWidths=[1.45*inch,1.15*inch,3.55*inch],repeatRows=1); nt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),slate),('TEXTCOLOR',(0,0),(-1,0),white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,off]),('GRID',(0,0),(-1,-1),.3,cool),('VALIGN',(0,0),(-1,-1),'TOP'),('FONTSIZE',(0,0),(-1,-1),7.2)])); story += [nt,Spacer(1,12)]
    boundary=Table([[Paragraph('<b>REQUIRES REVIEW</b>',styles['V19Body']),Paragraph('This synthetic report supports provider documentation and human billing/compliance review only. It does not determine diagnosis, treatment, CPT eligibility, coverage, medical necessity, or reimbursement. Provider time is kept separate from patient application engagement time.',styles['V19Small'])]],colWidths=[1.45*inch,4.7*inch]); boundary.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#FFF7E9')),('BOX',(0,0),(-1,-1),.7,amber),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)])); story += [boundary]
    doc.build(story,onFirstPage=page,onLaterPages=page); return buf.getvalue()


# ---------- V15 visual analytics ----------
def cache_get(key):
    with runtime() as r:
        x=r.execute("SELECT payload_json FROM analytics_cache_v15 WHERE cache_key=?",(key,)).fetchone()
    if not x: return None
    try: return json.loads(x["payload_json"])
    except: return None

def cache_set(key,payload):
    with runtime() as r:
        r.execute("INSERT OR REPLACE INTO analytics_cache_v15(cache_key,payload_json,generated_at_utc) VALUES(?,?,?)",(key,json.dumps(payload,separators=(',',':')),now_iso()))
        r.commit()
    return payload

def _module_names():
    with core() as c:
        return {x['module_key']:dict(x) for x in c.execute("SELECT module_key,module_id,platform,program_group,module_name FROM module_catalog WHERE is_active=1")}

def _org_core_distributions():
    key='v19:core-distributions-fast'
    hit=cache_get(key)
    if hit: return hit
    total=int(metadata().get('patient_count',0) or 0); sample_n=min(total,100000) if total else 100000
    factor=(total/sample_n) if sample_n else 1.0
    with core() as c:
        platform=[dict(x) for x in c.execute("SELECT platform_preference label,COUNT(*) value FROM patients WHERE is_deleted=0 AND patient_key<=? GROUP BY platform_preference ORDER BY value DESC",(sample_n,))]
        patterns=[dict(x) for x in c.execute("SELECT trend_pattern label,COUNT(*) value FROM patients WHERE is_deleted=0 AND patient_key<=? GROUP BY trend_pattern ORDER BY value DESC",(sample_n,))]
        mobile=[dict(x) for x in c.execute("""SELECT mc.module_name label,COUNT(*) value FROM patients p JOIN module_catalog mc ON mc.module_key=p.primary_mobile_module_key WHERE p.is_deleted=0 AND p.patient_key<=? GROUP BY p.primary_mobile_module_key ORDER BY value DESC""",(sample_n,))]
        xr=[dict(x) for x in c.execute("""SELECT mc.module_name label,COUNT(*) value FROM patients p JOIN module_catalog mc ON mc.module_key=p.primary_xr_module_key WHERE p.is_deleted=0 AND p.patient_key<=? GROUP BY p.primary_xr_module_key ORDER BY value DESC""",(sample_n,))]
    for arr in (platform,patterns,mobile,xr):
        for x in arr: x['value']=int(round(x['value']*factor))
    return cache_set(key,{'platform_mix':platform,'patterns':patterns,'mobile_primary':mobile,'xr_primary':xr,'distribution_basis':f'Estimated from {sample_n:,} deterministic synthetic patients; KPI totals remain exact.'})

def _month_population_analytics(month):
    key=f'v19:month-fast:{month}'
    hit=cache_get(key)
    if hit: return hit
    mods=_module_names(); total=int(metadata().get('patient_count',0) or 0); sample_n=min(total,100000) if total else 100000; factor=(total/sample_n) if sample_n else 1.0
    with monthdb(month) as m:
        # Exact organization KPIs from only 10k provider summary rows.
        pr=m.execute("""SELECT SUM(patient_count) cohort_patients,SUM(total_sessions) sessions,SUM(total_minutes) minutes,SUM(review_suggested_patients) review_suggested,SUM(active_patients) active_patients,
          SUM(avg_completion_rate*patient_count)/NULLIF(SUM(patient_count),0) completion,
          SUM(avg_engagement_index*patient_count)/NULLIF(SUM(patient_count),0) engagement
          FROM provider_monthly_summary""").fetchone(); agg=dict(pr)
        agg['avg_duration']=(float(agg.get('minutes') or 0)/max(float(agg.get('sessions') or 0),1))
        # Distribution visuals use a deterministic 100k-row slice so the first page
        # renders quickly on 5M datasets; displayed counts are scaled estimates.
        activity=[dict(x) for x in m.execute("""SELECT CASE WHEN session_count=0 THEN 'Inactive' WHEN session_count<=2 THEN 'Low Recent Activity' WHEN session_count<=8 THEN 'Active' ELSE 'Highly Active' END label,COUNT(*) value FROM patient_monthly_summary WHERE patient_key<=? GROUP BY label""",(sample_n,))]
        engagement=[dict(x) for x in m.execute("""SELECT CASE WHEN engagement_index<50 THEN 'Low' WHEN engagement_index<75 THEN 'Moderate' WHEN engagement_index<90 THEN 'High' ELSE 'Very High' END label,COUNT(*) value FROM patient_monthly_summary WHERE patient_key<=? GROUP BY label""",(sample_n,))]
        monthly_platform=[dict(x) for x in m.execute("""SELECT CASE WHEN session_count=0 THEN 'No activity' WHEN mobile_sessions>xr_sessions*1.25 THEN 'Mobile Heavy' WHEN xr_sessions>mobile_sessions*1.25 THEN 'XR Heavy' ELSE 'Mixed' END label,COUNT(*) value FROM patient_monthly_summary WHERE patient_key<=? GROUP BY label""",(sample_n,))]
        matrix=[dict(x) for x in m.execute("""SELECT CASE WHEN engagement_index<50 THEN 'Low' WHEN engagement_index<75 THEN 'Moderate' WHEN engagement_index<90 THEN 'High' ELSE 'Very High' END engagement_band,CASE WHEN completion_rate<50 THEN 'Low <50%' WHEN completion_rate<75 THEN 'Moderate 50-74%' WHEN completion_rate<90 THEN 'High 75-89%' ELSE 'Very High 90%+' END completion_band,COUNT(*) value FROM patient_monthly_summary WHERE patient_key<=? GROUP BY engagement_band,completion_band""",(sample_n,))]
        mob=[dict(x) for x in m.execute("SELECT top_mobile_module_key module_key,COUNT(*) value FROM patient_monthly_summary WHERE patient_key<=? AND top_mobile_module_key>0 GROUP BY top_mobile_module_key ORDER BY value DESC LIMIT 14",(sample_n,))]
        xr=[dict(x) for x in m.execute("SELECT top_xr_module_key module_key,COUNT(*) value FROM patient_monthly_summary WHERE patient_key<=? AND top_xr_module_key>0 GROUP BY top_xr_module_key ORDER BY value DESC LIMIT 17",(sample_n,))]
        tod=dict(m.execute("SELECT SUM(morning_sessions) morning,SUM(afternoon_sessions) afternoon,SUM(evening_sessions) evening,SUM(night_sessions) night FROM patient_monthly_summary WHERE patient_key<=?",(sample_n,)).fetchone())
    for arr in (activity,engagement,monthly_platform,matrix,mob,xr):
        for x in arr: x['value']=int(round((x.get('value') or 0)*factor))
    for arr in (mob,xr):
        for x in arr:
            info=mods.get(x['module_key'],{}); x['label']=info.get('module_name',f"Module {x['module_key']}"); x['platform']=info.get('platform','')
    payload={'kpis':agg,'activity':activity,'engagement_profile':engagement,'monthly_platform':monthly_platform,'matrix':matrix,'mobile_modules':mob,'xr_modules':xr,'time_of_day':[{'label':'Morning','value':int(round((tod.get('morning') or 0)*factor))},{'label':'Afternoon','value':int(round((tod.get('afternoon') or 0)*factor))},{'label':'Evening','value':int(round((tod.get('evening') or 0)*factor))},{'label':'Night','value':int(round((tod.get('night') or 0)*factor))}],'distribution_basis':f'Estimated distributions from {sample_n:,} deterministic synthetic patient-month rows; KPI totals are exact.'}
    return cache_set(key,payload)

def _provider_analytics(user,month):
    pk=user['provider_key']; mods=_module_names()
    with monthdb(month) as m:
        k=dict(m.execute("""SELECT COUNT(*) cohort_patients,SUM(session_count) sessions,SUM(total_minutes) minutes,AVG(avg_duration_min) avg_duration,AVG(completion_rate) completion,AVG(engagement_index) engagement,SUM(review_suggested) review_suggested,SUM(active_in_month) active_patients,SUM(morning_sessions) morning,SUM(afternoon_sessions) afternoon,SUM(evening_sessions) evening,SUM(night_sessions) night FROM patient_monthly_summary WHERE provider_key=?""",(pk,)).fetchone())
        activity=[dict(x) for x in m.execute("""SELECT CASE WHEN session_count=0 THEN 'Inactive' WHEN session_count<=2 THEN 'Low Recent Activity' WHEN session_count<=8 THEN 'Active' ELSE 'Highly Active' END label,COUNT(*) value FROM patient_monthly_summary WHERE provider_key=? GROUP BY label""",(pk,))]
        engagement=[dict(x) for x in m.execute("""SELECT CASE WHEN engagement_index<50 THEN 'Low' WHEN engagement_index<75 THEN 'Moderate' WHEN engagement_index<90 THEN 'High' ELSE 'Very High' END label,COUNT(*) value FROM patient_monthly_summary WHERE provider_key=? GROUP BY label""",(pk,))]
        monthly_platform=[dict(x) for x in m.execute("""SELECT CASE WHEN session_count=0 THEN 'No activity' WHEN mobile_sessions>xr_sessions*1.25 THEN 'Mobile Heavy' WHEN xr_sessions>mobile_sessions*1.25 THEN 'XR Heavy' ELSE 'Mixed' END label,COUNT(*) value FROM patient_monthly_summary WHERE provider_key=? GROUP BY label""",(pk,))]
        matrix=[dict(x) for x in m.execute("""SELECT CASE WHEN engagement_index<50 THEN 'Low' WHEN engagement_index<75 THEN 'Moderate' WHEN engagement_index<90 THEN 'High' ELSE 'Very High' END engagement_band,CASE WHEN completion_rate<50 THEN 'Low <50%' WHEN completion_rate<75 THEN 'Moderate 50-74%' WHEN completion_rate<90 THEN 'High 75-89%' ELSE 'Very High 90%+' END completion_band,COUNT(*) value FROM patient_monthly_summary WHERE provider_key=? GROUP BY engagement_band,completion_band""",(pk,))]
        mob=[dict(x) for x in m.execute("SELECT top_mobile_module_key module_key,COUNT(*) value FROM patient_monthly_summary WHERE provider_key=? AND top_mobile_module_key>0 GROUP BY top_mobile_module_key ORDER BY value DESC LIMIT 14",(pk,))]
        xr=[dict(x) for x in m.execute("SELECT top_xr_module_key module_key,COUNT(*) value FROM patient_monthly_summary WHERE provider_key=? AND top_xr_module_key>0 GROUP BY top_xr_module_key ORDER BY value DESC LIMIT 17",(pk,))]
    with core() as c:
        platform=[dict(x) for x in c.execute("SELECT platform_preference label,COUNT(*) value FROM patients WHERE provider_key=? AND is_deleted=0 GROUP BY platform_preference ORDER BY value DESC",(pk,))]
        patterns=[dict(x) for x in c.execute("SELECT trend_pattern label,COUNT(*) value FROM patients WHERE provider_key=? AND is_deleted=0 GROUP BY trend_pattern ORDER BY value DESC",(pk,))]
    for arr in (mob,xr):
        for x in arr:
            info=mods.get(x['module_key'],{}); x['label']=info.get('module_name',f"Module {x['module_key']}"); x['platform']=info.get('platform','')
    return {'kpis':k,'platform_mix':platform,'patterns':patterns,'activity':activity,'engagement_profile':engagement,'monthly_platform':monthly_platform,'matrix':matrix,'mobile_modules':mob,'xr_modules':xr,'time_of_day':[{'label':'Morning','value':k.get('morning') or 0},{'label':'Afternoon','value':k.get('afternoon') or 0},{'label':'Evening','value':k.get('evening') or 0},{'label':'Night','value':k.get('night') or 0}]}

def visual_analytics(user,month):
    if user['role']=='Provider': base=_provider_analytics(user,month)
    else:
        base=_month_population_analytics(month); base.update(_org_core_distributions())
    # month trend uses the 10k-row provider summaries and stays fast even at 5M patients
    trend=[]
    for mon in available_months():
        with monthdb(mon) as md:
            if user['role']=='Provider':
                r=md.execute("SELECT patient_count patients,active_patients,total_sessions sessions,total_minutes minutes,avg_completion_rate completion,avg_engagement_index engagement,provider_review_minutes,communication_minutes FROM provider_monthly_summary WHERE provider_key=?",(user['provider_key'],)).fetchone()
            else:
                r=md.execute("SELECT SUM(patient_count) patients,SUM(active_patients) active_patients,SUM(total_sessions) sessions,SUM(total_minutes) minutes,AVG(avg_completion_rate) completion,AVG(avg_engagement_index) engagement,SUM(provider_review_minutes) provider_review_minutes,SUM(communication_minutes) communication_minutes FROM provider_monthly_summary").fetchone()
            if r: trend.append({'month':mon,**dict(r)})
    base['trend']=trend; base['scope']='provider' if user['role']=='Provider' else 'organization'; return base

def explorer_analytics(user,month,provider_id='',platform='',pattern='',activity=''):
    # Default cohort uses the cached/scoped analytics. Filters are intentionally limited to indexed/synthetic dimensions.
    if not any([provider_id,platform,pattern,activity]): return visual_analytics(user,month)
    forced_pk=user.get('provider_key') if user['role']=='Provider' else None
    if provider_id and user['role']=='Admin': forced_pk=provider_key_for_id(provider_id)
    m=monthdb(month); m.execute("ATTACH DATABASE ? AS coredb",(str(CORE_DB),))
    wh=['p.is_deleted=0']; params=[]
    if forced_pk is not None: wh.append('pm.provider_key=?'); params.append(forced_pk)
    if platform: wh.append('p.platform_preference=?'); params.append(platform)
    if pattern: wh.append('p.trend_pattern=?'); params.append(pattern)
    if activity=='active': wh.append('pm.active_in_month=1')
    elif activity=='inactive': wh.append('pm.active_in_month=0')
    where=' AND '.join(wh)
    k=dict(m.execute(f"""SELECT COUNT(*) cohort_patients,SUM(pm.session_count) sessions,SUM(pm.total_minutes) minutes,AVG(pm.avg_duration_min) avg_duration,AVG(pm.completion_rate) completion,AVG(pm.engagement_index) engagement,SUM(pm.review_suggested) review_suggested,SUM(pm.active_in_month) active_patients FROM patient_monthly_summary pm JOIN coredb.patients p USING(patient_key) WHERE {where}""",params).fetchone())
    platforms=[dict(x) for x in m.execute(f"SELECT p.platform_preference label,COUNT(*) value FROM patient_monthly_summary pm JOIN coredb.patients p USING(patient_key) WHERE {where} GROUP BY p.platform_preference ORDER BY value DESC",params)]
    patterns=[dict(x) for x in m.execute(f"SELECT p.trend_pattern label,COUNT(*) value FROM patient_monthly_summary pm JOIN coredb.patients p USING(patient_key) WHERE {where} GROUP BY p.trend_pattern ORDER BY value DESC",params)]
    act=[dict(x) for x in m.execute(f"SELECT CASE WHEN pm.session_count=0 THEN 'Inactive' WHEN pm.session_count<=2 THEN 'Low Recent Activity' WHEN pm.session_count<=8 THEN 'Active' ELSE 'Highly Active' END label,COUNT(*) value FROM patient_monthly_summary pm JOIN coredb.patients p USING(patient_key) WHERE {where} GROUP BY label",params)]
    m.close()
    return {'scope':'filtered','kpis':k,'platform_mix':platforms,'patterns':patterns,'activity':act,'filters':{'provider_id':provider_id,'platform':platform,'pattern':pattern,'activity':activity}}


# ---------- V20 V18.2-native query layer ----------
DEMO_HOURLY_RATE = float(os.environ.get("REFLECT_DEMO_HOURLY_RATE", "120"))

def demo_amount(minutes):
    return round(float(minutes or 0) / 60.0 * DEMO_HOURLY_RATE, 2)

def _billing_summary_for_keys(month, keys):
    if not keys or not has_v18_month(month): return {}
    marks=','.join('?' for _ in keys)
    with billingdb(month) as b:
        return {r['patient_key']:dict(r) for r in b.execute(
            f"SELECT * FROM patient_billing_month_summary WHERE patient_key IN ({marks})", keys)}

def provider_overview(user, month):
    pk=user['provider_key']
    with monthdb(month) as m:
        s=m.execute("SELECT * FROM provider_monthly_summary WHERE provider_key=?",(pk,)).fetchone()
        q=list(m.execute("""SELECT patient_key,session_count,active_days,total_minutes,completion_rate,engagement_index,last_activity_utc,review_suggested,treatment_goal_progress
          FROM patient_monthly_summary WHERE provider_key=? ORDER BY review_suggested DESC, engagement_index ASC LIMIT 8""",(pk,)))
        tod=m.execute("SELECT SUM(morning_sessions) morning,SUM(afternoon_sessions) afternoon,SUM(evening_sessions) evening,SUM(night_sessions) night FROM patient_monthly_summary WHERE provider_key=?",(pk,)).fetchone()
    keys=[x['patient_key'] for x in q]
    pmap={}; bmap=_billing_summary_for_keys(month,keys)
    if keys:
        marks=','.join('?' for _ in keys)
        with core() as c:
            pmap={r['patient_key']:dict(r) for r in c.execute(f"SELECT patient_key,patient_id,platform_preference,trend_pattern,preferred_time_band,treatment_goal_type FROM patients WHERE patient_key IN ({marks})",keys)}
    queue=[]
    for r in q:
        d={**dict(r),**pmap.get(r['patient_key'],{}),**bmap.get(r['patient_key'],{})}; queue.append(d)
    trend=[]
    for mon in available_months():
        with monthdb(mon) as md:
            r=md.execute("SELECT active_patients,total_sessions,total_minutes,avg_completion_rate,avg_engagement_index,avg_session_quality FROM provider_monthly_summary WHERE provider_key=?",(pk,)).fetchone()
        if not r: continue
        item={'month':mon,**dict(r)}
        if has_v18_month(mon):
            with billingdb(mon) as b:
                x=b.execute("SELECT data_review_minutes,communication_minutes,documentation_minutes,total_provider_minutes,patients_with_provider_activity FROM provider_billing_month_summary WHERE provider_key=?",(pk,)).fetchone()
            if x:item.update(dict(x))
        trend.append(item)
    with core() as c:
        pr=c.execute("SELECT p.provider_id,p.provider_display_name,p.region_code,s.patient_count FROM providers p JOIN provider_summary s USING(provider_key) WHERE p.provider_key=?",(pk,)).fetchone()
        platform=[dict(r) for r in c.execute("SELECT platform_preference label,COUNT(*) value FROM patients WHERE provider_key=? AND is_deleted=0 GROUP BY platform_preference ORDER BY value DESC",(pk,))]
        patterns=[dict(r) for r in c.execute("SELECT trend_pattern label,COUNT(*) value FROM patients WHERE provider_key=? AND is_deleted=0 GROUP BY trend_pattern ORDER BY value DESC",(pk,))]
    month_data=dict(s) if s else {}
    if has_v18_month(month):
        with billingdb(month) as b:
            bt=b.execute("SELECT * FROM provider_billing_month_summary WHERE provider_key=?",(pk,)).fetchone()
        if bt: month_data.update(dict(bt))
    active=month_data.get('active_patients',0) or 0
    month_data['active_rate']=round(active/max(month_data.get('patient_count',1) or 1,1)*100,1)
    month_data['sessions_per_active']=round((month_data.get('total_sessions',0) or 0)/max(active,1),1)
    return {'provider':dict(pr),'month':month_data,'queue':queue,'trend':trend,'platform_mix':platform,'engagement_patterns':patterns,
            'time_of_day':{'Morning':tod['morning'] or 0,'Afternoon':tod['afternoon'] or 0,'Evening':tod['evening'] or 0,'Night':tod['night'] or 0}}

def admin_overview(month):
    meta=metadata()
    with monthdb(month) as m:
        g=m.execute("""SELECT COUNT(*) providers,SUM(patient_count) patients,SUM(active_patients) active_patients,SUM(review_suggested_patients) review_suggested,
          SUM(total_sessions) sessions,SUM(total_minutes) minutes,AVG(avg_completion_rate) completion,AVG(avg_engagement_index) engagement,AVG(avg_session_quality) quality
          FROM provider_monthly_summary""").fetchone()
        top=[dict(x) for x in m.execute("SELECT provider_key,patient_count,active_patients,review_suggested_patients,total_sessions,total_minutes,avg_completion_rate,avg_engagement_index,avg_session_quality FROM provider_monthly_summary ORDER BY patient_count DESC LIMIT 12")]
        engagement_bands=[dict(r) for r in m.execute("SELECT CASE WHEN avg_engagement_index<40 THEN 'Below 40' WHEN avg_engagement_index<60 THEN '40-59' WHEN avg_engagement_index<80 THEN '60-79' ELSE '80+' END label,COUNT(*) value FROM provider_monthly_summary GROUP BY label ORDER BY MIN(avg_engagement_index)")]
        panel_bands=[dict(r) for r in m.execute("SELECT CASE WHEN patient_count<250 THEN '<250' WHEN patient_count<500 THEN '250-499' WHEN patient_count<750 THEN '500-749' ELSE '750+' END label,COUNT(*) value FROM provider_monthly_summary GROUP BY label ORDER BY MIN(patient_count)")]
        workload_bands=[dict(r) for r in m.execute("SELECT CASE WHEN review_suggested_patients=0 THEN 'No queue' WHEN review_suggested_patients<25 THEN '1-24' WHEN review_suggested_patients<75 THEN '25-74' ELSE '75+' END label,COUNT(*) value FROM provider_monthly_summary GROUP BY label ORDER BY MIN(review_suggested_patients)")]
    ids={}; ptmap={}; billing_totals={}
    if top:
        pks=[x['provider_key'] for x in top]; marks=','.join('?'*len(pks))
        with core() as c: ids={r['provider_key']:dict(r) for r in c.execute(f"SELECT provider_key,provider_id,provider_display_name,region_code FROM providers WHERE provider_key IN ({marks})",pks)}
    if has_v18_month(month):
        with billingdb(month) as b:
            bt=b.execute("SELECT SUM(patients_with_provider_activity) patients_with_provider_activity,SUM(data_review_minutes) data_review_minutes,SUM(communication_minutes) communication_minutes,SUM(documentation_minutes) documentation_minutes,SUM(total_provider_minutes) total_provider_minutes,SUM(data_review_count) data_review_count,SUM(communication_count) communication_count,SUM(documentation_count) documentation_count FROM provider_billing_month_summary").fetchone()
            billing_totals=dict(bt) if bt else {}
            if top:
                ptmap={r['provider_key']:dict(r) for r in b.execute(f"SELECT * FROM provider_billing_month_summary WHERE provider_key IN ({marks})",pks)}
    month_data={**dict(g),**billing_totals}
    active=month_data.get('active_patients',0) or 0; patients=month_data.get('patients',0) or 0
    month_data['active_rate']=round(active/max(patients,1)*100,1); month_data['sessions_per_active']=round((month_data.get('sessions',0) or 0)/max(active,1),2)
    month_data['provider_time_hours']=round((month_data.get('total_provider_minutes',0) or 0)/60,1)
    month_data['review_hours']=round((month_data.get('data_review_minutes',0) or 0)/60,1); month_data['communication_hours']=round((month_data.get('communication_minutes',0) or 0)/60,1); month_data['documentation_hours']=round((month_data.get('documentation_minutes',0) or 0)/60,1)
    trend=[]
    for mon in available_months():
        with monthdb(mon) as md:
            r=md.execute("SELECT SUM(patient_count) patients,SUM(active_patients) active_patients,SUM(total_sessions) sessions,SUM(total_minutes) minutes,AVG(avg_completion_rate) completion,AVG(avg_engagement_index) engagement FROM provider_monthly_summary").fetchone()
        item={'month':mon,**dict(r)}
        if has_v18_month(mon):
            with billingdb(mon) as b:x=b.execute("SELECT SUM(data_review_minutes) data_review_minutes,SUM(communication_minutes) communication_minutes,SUM(documentation_minutes) documentation_minutes,SUM(total_provider_minutes) total_provider_minutes FROM provider_billing_month_summary").fetchone()
            if x:item.update(dict(x))
        trend.append(item)
    regions=[]
    with core() as c:
        for region in c.execute("SELECT region_code,COUNT(*) providers FROM providers WHERE is_deleted=0 GROUP BY region_code ORDER BY providers DESC"):
            regions.append({'region':region['region_code'],'providers':region['providers']})
    top_rows=[{**x,**ids.get(x['provider_key'],{}),**ptmap.get(x['provider_key'],{})} for x in top]
    return {'metadata':meta,'month':month_data,'top_providers':top_rows,'trend':trend,'regions':regions,'engagement_bands':engagement_bands,'panel_bands':panel_bands,'workload_bands':workload_bands}

def list_patients(user, month, page=1, size=25, search='', review='', platform=''):
    size=max(10,min(size,100)); page=max(page,1); off=(page-1)*size
    search=(search or '').strip(); platform=(platform or '').strip(); review=review if review in {'1','0'} else ''
    cwh=['is_deleted=0']; cparams=[]
    if user['role']=='Provider': cwh.append('provider_key=?'); cparams.append(user['provider_key'])
    if search: cwh.append('patient_id LIKE ?'); cparams.append(search+'%')
    if platform: cwh.append('platform_preference=?'); cparams.append(platform)
    # Fast path for unfiltered listing: page keys from monthly DB first.
    if not search and not platform:
        mwh=[]; mparams=[]
        if user['role']=='Provider': mwh.append('provider_key=?'); mparams.append(user['provider_key'])
        if review: mwh.append('review_suggested=?'); mparams.append(int(review))
        where=' WHERE '+' AND '.join(mwh) if mwh else ''
        with monthdb(month) as m:
            total=(m.execute(f"SELECT COUNT(*) n FROM patient_monthly_summary{where}",mparams).fetchone()['n'] if mwh else int(metadata().get('patient_count',0)))
            mpage=[dict(r) for r in m.execute(f"SELECT patient_key,session_count,active_days,total_minutes,completion_rate,engagement_index,last_activity_utc,review_suggested,treatment_goal_progress FROM patient_monthly_summary{where} ORDER BY review_suggested DESC,patient_key LIMIT ? OFFSET ?",mparams+[size,off])]
        keys=[x['patient_key'] for x in mpage]
        if not keys:return {'page':page,'page_size':size,'total':total,'rows':[]}
        marks=','.join('?'*len(keys))
        with core() as c:cmap={r['patient_key']:dict(r) for r in c.execute(f"SELECT patient_key,patient_id,platform_preference,trend_pattern,preferred_time_band,treatment_goal_type FROM patients WHERE patient_key IN ({marks})",keys)}
        bmap=_billing_summary_for_keys(month,keys)
        return {'page':page,'page_size':size,'total':total,'rows':[{**cmap.get(x['patient_key'],{}),**x,**bmap.get(x['patient_key'],{})} for x in mpage if x['patient_key'] in cmap]}
    # Filtered core path.
    cwhere=' AND '.join(cwh)
    with core() as c:
        total_core=c.execute(f"SELECT COUNT(*) n FROM patients WHERE {cwhere}",cparams).fetchone()['n']
        candidate_limit=max(size*20,500) if review else size
        cand=[dict(r) for r in c.execute(f"SELECT patient_key,patient_id,platform_preference,trend_pattern,preferred_time_band,treatment_goal_type FROM patients WHERE {cwhere} ORDER BY patient_key LIMIT ? OFFSET ?",cparams+[candidate_limit,0 if review else off])]
    if not cand:return {'page':page,'page_size':size,'total':0 if review else total_core,'rows':[]}
    keys=[x['patient_key'] for x in cand]; marks=','.join('?'*len(keys))
    with monthdb(month) as m:mrows={r['patient_key']:dict(r) for r in m.execute(f"SELECT patient_key,session_count,active_days,total_minutes,completion_rate,engagement_index,last_activity_utc,review_suggested,treatment_goal_progress FROM patient_monthly_summary WHERE patient_key IN ({marks})",keys)}
    bmap=_billing_summary_for_keys(month,keys); rows=[]
    for c in cand:
        mr=mrows.get(c['patient_key']);
        if not mr: continue
        if review and str(mr.get('review_suggested',0))!=review: continue
        rows.append({**c,**mr,**bmap.get(c['patient_key'],{})})
        if len(rows)>=size: break
    return {'page':page,'page_size':size,'total':len(rows) if review else total_core,'rows':rows}

def _materialize_v18_2_sessions(patient_id, month):
    """Best-effort on-demand session detail for billing-mode V18.2 datasets."""
    try:
        tool=ROOT/'tools'/'materialize_patient_sessions_v18_2.py'
        if not tool.exists(): return False
        subprocess.run([sys.executable,str(tool),str(DATA_ROOT),patient_id,month],cwd=str(ROOT),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45,check=False)
        return True
    except Exception:
        return False

def patient_payload(user, patient_id, month):
    p=authorize_patient(user,patient_id)
    if not p:return None
    with monthdb(month) as m: ms=m.execute('SELECT * FROM patient_monthly_summary WHERE patient_key=?',(p['patient_key'],)).fetchone()
    history=[]
    for mon in available_months():
        with monthdb(mon) as md:
            h=md.execute('SELECT * FROM patient_monthly_summary WHERE patient_key=?',(p['patient_key'],)).fetchone()
        item={'month':mon,**dict(h)} if h else {'month':mon}
        if has_v18_month(mon):
            with billingdb(mon) as b:bt=b.execute('SELECT * FROM patient_billing_month_summary WHERE patient_key=?',(p['patient_key'],)).fetchone()
            if bt:item.update({'data_review_count':bt['data_review_count'],'data_review_minutes':bt['data_review_minutes'],'provider_communication_count':bt['communication_count'],'provider_communication_minutes':bt['communication_minutes'],'documentation_count':bt['documentation_count'],'documentation_minutes':bt['documentation_minutes'],'total_provider_minutes':bt['total_provider_minutes']})
        history.append(item)
    with core() as c:
        modules={r['module_key']:dict(r) for r in c.execute('SELECT * FROM module_catalog WHERE is_active=1')}
        prov=c.execute('SELECT provider_id,provider_display_name,region_code FROM providers WHERE provider_key=?',(p['provider_key'],)).fetchone()
    v18=v18_patient_month(p['patient_key'],month) if has_v18_month(month) else None
    if v18 and not v18.get('sessions') and ms and int(ms['session_count'] or 0)>0:
        if _materialize_v18_2_sessions(patient_id,month): v18=v18_patient_month(p['patient_key'],month)
    acts=(v18 or {}).get('activities',[]); events=(v18 or {}).get('sessions',[]); audits=(v18 or {}).get('audits',[])
    for x in acts:
        x['display_start']=x.get('activity_start_eastern') or us_dt(x.get('activity_start_utc')); x['display_end']=x.get('activity_end_eastern') or us_dt(x.get('activity_end_utc'))
    # enrich session events and analytics
    byplat={'Mobile':{},'XR':{}}; periods={'Morning':0,'Afternoon':0,'Evening':0,'Night':0}; hours=[0]*24; remote_days=set(); out_events=[]
    for x in events:
        mod=modules.get(x.get('module_key'),{'module_name':'Unknown','program_group':''})
        e={**x,'module_name':mod.get('module_name'),'program_group':mod.get('program_group'),'display_start':x.get('session_start_eastern') or us_dt(x.get('session_start_utc'))}; out_events.append(e)
        try:d=datetime.fromisoformat(str(x.get('session_start_utc')).replace('Z','+00:00')).astimezone(EASTERN)
        except:continue
        hours[d.hour]+=1; band='Morning' if 5<=d.hour<12 else 'Afternoon' if d.hour<17 else 'Evening' if d.hour<22 else 'Night'; periods[band]+=1
        if x.get('remote_or_clinic')=='Remote':remote_days.add(d.date().isoformat())
        plat=x.get('platform') or 'Mobile'; mk=x.get('module_key'); bm=byplat.setdefault(plat,{}).setdefault(mk,{'module_key':mk,'module_name':mod.get('module_name'),'program_group':mod.get('program_group'),'sessions':0,'minutes':0.0,'completed':0,'last_activity':x.get('session_start_utc')})
        bm['sessions']+=1; bm['minutes']+=float(x.get('duration_minutes') or 0); bm['completed']+=int(x.get('completed') or 0); bm['last_activity']=max(bm['last_activity'],x.get('session_start_utc'))
    for plat in byplat:
        for v in byplat[plat].values():
            v['minutes']=round(v['minutes'],1); v['avg_duration']=round(v['minutes']/max(v['sessions'],1),1); v['completion_rate']=round(v['completed']/max(v['sessions'],1)*100,1); v['last_activity_display']=us_dt(v['last_activity'])
    vs=(v18 or {}).get('summary',{}) or {}
    provider_time={'data_review_count':vs.get('data_review_count',0),'data_review_minutes':vs.get('data_review_minutes',0),'communication_count':vs.get('communication_count',0),'communication_minutes':vs.get('communication_minutes',0),'documentation_count':vs.get('documentation_count',0),'documentation_minutes':vs.get('documentation_minutes',0),'total_provider_minutes':vs.get('total_provider_minutes',0),'real_time_communication_count':vs.get('real_time_communication_count',0),'activity_days':vs.get('provider_activity_days',0),'first_provider_activity_utc':vs.get('first_provider_activity_utc'),'last_provider_activity_utc':vs.get('last_provider_activity_utc')}
    billing={**provider_time,'real_time_communication_present':bool(provider_time['real_time_communication_count']),'remote_sessions':vs.get('patient_remote_sessions',ms['remote_sessions'] if ms else 0),'remote_active_days':vs.get('patient_active_days',ms['active_days'] if ms else 0),'status':vs.get('billing_status','REQUIRES_REVIEW'),'synthetic_demo_amount':demo_amount(provider_time['total_provider_minutes']),'demo_hourly_rate':DEMO_HOURLY_RATE}
    dmap={}
    for a in acts:
        day=(a.get('activity_start_eastern') or a.get('activity_start_utc') or '')[:10]; z=dmap.setdefault(day,{'activity_date':day,'data_review_minutes':0,'communication_minutes':0,'documentation_minutes':0,'total_provider_minutes':0})
        mins=float(a.get('duration_minutes') or 0); z['total_provider_minutes']+=mins
        if a.get('activity_type')=='DATA_REVIEW':z['data_review_minutes']+=mins
        elif a.get('activity_type')=='COMMUNICATION':z['communication_minutes']+=mins
        elif a.get('activity_type')=='DOCUMENTATION':z['documentation_minutes']+=mins
    daily=sorted(dmap.values(),key=lambda x:x['activity_date'])
    r=runtime(); notes=[dict(x) for x in r.execute('SELECT * FROM provider_notes_v17 WHERE patient_key=? AND billing_month=? ORDER BY created_at_utc DESC',(p['patient_key'],month))] if _object_exists(r,'provider_notes_v17',('table',)) else []; r.close()
    insights={}
    if history:
        cur=next((x for x in history if x['month']==month),history[-1]); idx=history.index(cur); prev=history[idx-1] if idx>0 else None
        if prev: insights={'engagement_change':round(float(cur.get('engagement_index') or 0)-float(prev.get('engagement_index') or 0),1),'session_change':int(cur.get('session_count') or 0)-int(prev.get('session_count') or 0),'completion_change':round(float(cur.get('completion_rate') or 0)-float(prev.get('completion_rate') or 0),1),'provider_time_change':round(float(cur.get('total_provider_minutes') or 0)-float(prev.get('total_provider_minutes') or 0),1)}
    return {'patient':dict(p),'provider':dict(prov),'month_summary':dict(ms) if ms else {},'history':history,'mobile_modules':sorted(byplat.get('Mobile',{}).values(),key=lambda x:x['sessions'],reverse=True),'xr_modules':sorted(byplat.get('XR',{}).values(),key=lambda x:x['sessions'],reverse=True),'time_of_day':periods,'hourly':hours,'events':out_events[:500],'reviews':[],'communications':[],'notes':notes,'provider_time':provider_time,'provider_activities':acts[:1000],'provider_daily':daily,'provider_profile':v18_provider_profile(p['provider_key']),'provider_access':v18_provider_access(p['provider_key'],month),'provider_activity_audit':audits[:300],'billing':billing,'goal':{'title':p['treatment_goal_type'],'progress_percent':round(float(ms['treatment_goal_progress'] or 0),1) if ms else 0},'insights':insights,'billing_master':'V18.2'}

# Preserve original queue functions before redefining amount wrappers.
_v20_base_patient_queue = v18_billing_patient_queue if 'v18_billing_patient_queue' in globals() else None
_v20_base_provider_queue = v18_billing_provider_queue if 'v18_billing_provider_queue' in globals() else None
_v20_base_provider_detail = v18_billing_provider_detail if 'v18_billing_provider_detail' in globals() else None

def v20_billing_patient_queue(month,page=1,size=25,provider_key=None):
    out=_v20_base_patient_queue(month,page,size,provider_key)
    for r in out.get('rows',[]): r['synthetic_demo_amount']=demo_amount(r.get('total_provider_minutes'))
    totals={'records':0,'total_provider_minutes':0,'data_review_minutes':0,'communication_minutes':0,'documentation_minutes':0}
    if has_v18_month(month):
        with billingdb(month) as b:
            wh=' WHERE total_provider_minutes>0'; params=[]
            if provider_key is not None: wh+=' AND provider_key=?'; params.append(provider_key)
            x=b.execute(f"SELECT COUNT(*) records,SUM(total_provider_minutes) total_provider_minutes,SUM(data_review_minutes) data_review_minutes,SUM(communication_minutes) communication_minutes,SUM(documentation_minutes) documentation_minutes FROM patient_billing_month_summary{wh}",params).fetchone()
            if x: totals={k:(x[k] or 0) for k in totals}
    totals['synthetic_demo_amount']=demo_amount(totals['total_provider_minutes'])
    out['totals']=totals; out['demo_hourly_rate']=DEMO_HOURLY_RATE; return out

def v20_billing_provider_queue(month,page=1,size=25):
    out=_v20_base_provider_queue(month,page,size)
    for r in out.get('rows',[]): r['synthetic_demo_amount']=demo_amount(r.get('total_provider_minutes'))
    totals={'records':0,'total_provider_minutes':0,'data_review_minutes':0,'communication_minutes':0,'documentation_minutes':0,'patients_with_provider_activity':0}
    if has_v18_month(month):
        with billingdb(month) as b:
            x=b.execute("SELECT COUNT(*) records,SUM(total_provider_minutes) total_provider_minutes,SUM(data_review_minutes) data_review_minutes,SUM(communication_minutes) communication_minutes,SUM(documentation_minutes) documentation_minutes,SUM(patients_with_provider_activity) patients_with_provider_activity FROM provider_billing_month_summary").fetchone()
            if x: totals={k:(x[k] or 0) for k in totals}
    totals['synthetic_demo_amount']=demo_amount(totals['total_provider_minutes'])
    out['totals']=totals; out['demo_hourly_rate']=DEMO_HOURLY_RATE; return out

def v20_billing_provider_detail(provider_id,month):
    out=_v20_base_provider_detail(provider_id,month)
    if not out:return out
    out['summary']['synthetic_demo_amount']=demo_amount(out['summary'].get('total_provider_minutes'))
    out['summary']['demo_hourly_rate']=DEMO_HOURLY_RATE
    for r in out.get('patients',[]): r['synthetic_demo_amount']=demo_amount(r.get('total_provider_minutes'))
    return out

v18_billing_patient_queue=v20_billing_patient_queue
v18_billing_provider_queue=v20_billing_provider_queue
v18_billing_provider_detail=v20_billing_provider_detail

class H(SimpleHTTPRequestHandler):
    def log_message(self,fmt,*args): print(f"[{self.log_date_time_string()}] {fmt%args}")
    def _json(self,obj,status=200,headers=None):
        b=jbytes(obj); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(b))); self.send_header("Cache-Control","no-store"); self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'")
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers()
        try: self.wfile.write(b)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError): return
    def _body(self):
        n=int(self.headers.get("Content-Length","0") or 0); return json.loads(self.rfile.read(n).decode()) if n else {}
    def _cookie(self):
        for p in self.headers.get("Cookie","").split(";"):
            k,_,v=p.strip().partition("=")
            if k=="reflect_v20_session": return v
    def _session(self):
        t=self._cookie(); s=SESSIONS.get(t)
        if not s:return None
        z=time.time()
        if z-s["last"]>SESSION_IDLE or z-s["created"]>SESSION_MAX: SESSIONS.pop(t,None); return None
        s["last"]=z; return s
    def _require(self,roles=None):
        s=self._session()
        if not s: self._json({"error":"Authentication required"},401); return None
        if roles and s["role"] not in roles: self._json({"error":"Access denied for this role"},403); return None
        return s
    def _csrf(self,s): return self.headers.get("X-CSRF-Token","")==s.get("csrf")
    def _audit(self,s,action,patient_key=None,month=None,detail=""):
        with runtime() as r: r.execute("INSERT INTO audit_events(user_id,role,provider_id,action,patient_key,billing_month,event_time_utc,detail) VALUES(?,?,?,?,?,?,?,?)",(s.get("user_id"),s.get("role"),s.get("provider_id"),action,patient_key,month,now_iso(),detail)); r.commit()
    def do_GET(self):
        u=urlparse(self.path); path=u.path; q=parse_qs(u.query)
        # Static UI routes (/, /index.html, /app.js, /app.css, SPA fallbacks)
        # must remain public so an unauthenticated user can reach the login page.
        # Authentication is enforced only for protected /api/* endpoints below.
        if not path.startswith("/api/"):
            return super().do_GET()
        try:
            if path=="/api/health":
                md=metadata(); return self._json({"ok":True,"version":"V20","dataset":"sample-v18.2" if DATA_ROOT.name=="data_sample_v18_2" else "full-v18.2","billing_master":BILLING_CORE_DB.exists(),"patients":int(md.get("patient_count",0)),"providers":int(md.get("provider_count",0)),"months":available_months(),"demo_hourly_rate":DEMO_HOURLY_RATE})
            if path=="/api/demo-accounts":
                r=runtime(); rows=[dict(x) for x in r.execute("SELECT email,display_name,role,provider_id FROM auth_users WHERE active=1 ORDER BY CASE role WHEN 'Provider' THEN 1 WHEN 'Admin' THEN 2 ELSE 3 END,email")]; r.close()
                if rows:
                    with core() as c:
                        counts={x["provider_id"]:x["patient_count"] for x in c.execute("SELECT p.provider_id,s.patient_count FROM providers p JOIN provider_summary s USING(provider_key)") if x["provider_id"] in {z['provider_id'] for z in rows if z['provider_id']}}
                    for x in rows: x["patient_count"]=counts.get(x["provider_id"])
                return self._json({"accounts":rows,"provider_password":"ProviderDemo2026!","admin_password":"AdminDemo2026!","billing_password":"BillingDemo2026!"})
            if path=="/api/session":
                s=self._session(); return self._json({"authenticated":bool(s),"user":{k:s.get(k) for k in ["user_id","email","display_name","role","provider_id","csrf"]} if s else None})
            s=self._require()
            if not s:return
            months=available_months(); mon=(q.get("month") or [months[-1]])[0]
            if path=="/api/months": return self._json({"months":months,"default":months[-1]})
            if path=="/api/overview":
                if s["role"]=="Provider": return self._json(provider_overview(s,mon))
                if s["role"]=="Admin": return self._json(admin_overview(mon))
                # billing landing
                ao=admin_overview(mon); return self._json({"month":ao["month"],"top_providers":ao["top_providers"][:8]})
            if path=="/api/analytics":
                if s["role"]=="Billing": return self._json({"error":"Billing role does not have analytics workspace access"},403)
                return self._json(visual_analytics(s,mon))
            if path=="/api/analytics/explorer":
                if s["role"]=="Billing": return self._json({"error":"Billing role does not have analytics workspace access"},403)
                return self._json(explorer_analytics(s,mon,(q.get('provider')or[''])[0],(q.get('platform')or[''])[0],(q.get('pattern')or[''])[0],(q.get('activity')or[''])[0]))
            if path=="/api/provider-time":
                if s["role"]=="Billing": return self._json({"error":"Billing role uses the patient-specific evidence queue"},403)
                return self._json(provider_time_analytics(s,mon))
            if path=="/api/patients":
                if s["role"]=="Billing": return self._json({"error":"Billing role does not have full patient analytics access"},403)
                return self._json(list_patients(s,mon,int((q.get('page')or['1'])[0]),int((q.get('page_size')or['25'])[0]),(q.get('search')or[''])[0],(q.get('review')or[''])[0],(q.get('platform')or[''])[0]))
            if path.startswith("/api/patients/"):
                if s["role"]=="Billing": return self._json({"error":"Billing role does not have patient analytics access"},403)
                pid=unquote(path.split("/")[-1]); payload=patient_payload(s,pid,mon)
                if not payload:return self._json({"error":"Patient not found or not authorized"},404)
                self._audit(s,"VIEW_PATIENT",payload["patient"]["patient_key"],mon); return self._json(payload)
            if path=="/api/billing/queue":
                if s["role"] not in {"Billing","Admin"}: return self._json({"error":"Billing or Admin access required"},403)
                view=(q.get("view")or["patients"])[0]
                if view=="providers": return self._json(v18_billing_provider_queue(mon,int((q.get('page')or['1'])[0]),25))
                return self._json(v18_billing_patient_queue(mon,int((q.get('page')or['1'])[0]),25))
            if path.startswith("/api/billing/provider/"):
                if s["role"] not in {"Billing","Admin"}: return self._json({"error":"Billing or Admin access required"},403)
                pid=unquote(path.split("/")[-1]); out=v18_billing_provider_detail(pid,mon)
                return self._json(out) if out else self._json({"error":"Provider not found"},404)
            if path.startswith("/api/billing/patient/"):
                if s["role"] not in {"Billing","Admin"}: return self._json({"error":"Billing or Admin access required"},403)
                pid=unquote(path.split("/")[-1]); ev=billing_evidence(pid,mon)
                if not ev:return self._json({"error":"Patient not found"},404)
                return self._json(ev)
            if path=="/api/providers":
                if s["role"]!="Admin": return self._json({"error":"Admin access required"},403)
                page=int((q.get('page')or['1'])[0]); size=25; off=(page-1)*size
                with core() as c:
                    total=c.execute("SELECT COUNT(*) n FROM providers WHERE is_deleted=0").fetchone()["n"]
                    rows=[dict(x) for x in c.execute("""SELECT p.provider_id,p.provider_display_name,p.region_code,s.* FROM providers p JOIN provider_summary s USING(provider_key) WHERE p.is_deleted=0 ORDER BY s.patient_count DESC LIMIT ? OFFSET ?""",(size,off))]
                return self._json({"page":page,"page_size":size,"total":total,"rows":rows})
            if path=="/api/audit":
                if s["role"]!="Admin": return self._json({"error":"Admin access required"},403)
                with runtime() as r: rows=[dict(x) for x in r.execute("SELECT * FROM audit_events ORDER BY audit_id DESC LIMIT 100")]
                return self._json({"rows":rows})
            if path=="/api/billing/report":
                pid=(q.get("patient_id")or[""])[0]
                if s["role"]=="Provider":
                    payload=patient_payload(s,pid,mon)
                    if not payload:return self._json({"error":"Patient not found or not authorized"},404)
                elif s["role"] in {"Billing","Admin"}:
                    fake={"role":"Admin","provider_key":None}
                    payload=patient_payload(fake,pid,mon)
                    if not payload:return self._json({"error":"Patient not found"},404)
                else:return self._json({"error":"Report access denied"},403)
                b=payload['billing']; patient_key=payload['patient']['patient_key']; provider_key=payload['patient']['provider_key']
                pdf=branded_pdf(payload,mon,"Monthly RTM Documentation Support")
                rid='BR-'+uuid.uuid4().hex[:10].upper()
                with runtime() as r:
                    r.execute("INSERT INTO billing_reports_v17(report_id,patient_key,provider_key,billing_month,generated_by,generated_at_utc,data_review_minutes,communication_minutes,documentation_minutes,total_provider_minutes,real_time_communication_present,remote_sessions,remote_active_days,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,patient_key,provider_key,mon,s['user_id'],now_iso(),b['data_review_minutes'],b['communication_minutes'],b['documentation_minutes'],b['total_provider_minutes'],1 if b['real_time_communication_present'] else 0,b['remote_sessions'],b['remote_active_days'],'REQUIRES_REVIEW')); r.commit()
                self.send_response(200); self.send_header("Content-Type","application/pdf"); self.send_header("Content-Disposition",f'attachment; filename="Reflect_RTM_{pid}_{mon}.pdf"'); self.send_header("Content-Length",str(len(pdf))); self.end_headers(); self.wfile.write(pdf); return
            return self._json({"error":"Unknown endpoint"},404)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as e:
            return self._json({"error":str(e)},500)
    def do_POST(self):
        u=urlparse(self.path); path=u.path
        try: data=self._body()
        except: return self._json({"error":"Invalid JSON"},400)
        if path=="/api/login":
            email=(data.get("email")or"").strip().lower(); password=data.get("password")or""; z=time.time(); lock=FAILED.get(email,{})
            if lock.get("until",0)>z:return self._json({"error":"Too many failed attempts. Try again shortly."},429)
            with runtime() as r:
                urow=r.execute("SELECT * FROM auth_users WHERE email=? AND active=1",(email,)).fetchone()
            ok=bool(urow and pw_hash(password,urow["password_salt"])==urow["password_hash"])
            if not ok:
                ent=FAILED.setdefault(email,{"count":0,"until":0}); ent["count"]+=1
                if ent["count"]>=5: ent["count"]=0; ent["until"]=z+30
                return self._json({"error":"Invalid email or password"},401)
            FAILED.pop(email,None); token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24); sess={"user_id":urow["user_id"],"email":urow["email"],"display_name":urow["display_name"],"role":urow["role"],"provider_key":urow["provider_key"],"provider_id":urow["provider_id"],"csrf":csrf,"created":z,"last":z}; SESSIONS[token]=sess; self._audit(sess,"LOGIN")
            return self._json({"ok":True,"user":{k:sess.get(k) for k in ["user_id","email","display_name","role","provider_id","csrf"]}},200,{"Set-Cookie":f"reflect_v20_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_MAX}"})
        if path=="/api/logout":
            t=self._cookie(); SESSIONS.pop(t,None); return self._json({"ok":True},200,{"Set-Cookie":"reflect_v20_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        s=self._require()
        if not s:return
        if not self._csrf(s):return self._json({"error":"Invalid CSRF token"},403)
        if path=="/api/reviews/start":
            if s["role"]!="Provider":return self._json({"error":"Provider role required"},403)
            pid=data.get("patient_id",""); p=authorize_patient(s,pid)
            if not p:return self._json({"error":"Patient not found or not authorized"},404)
            mon=data.get("month") or available_months()[-1]; materialize(pid,mon); st=now(); r=runtime(); cur=r.execute("INSERT INTO provider_reviews(patient_key,provider_key,billing_month,review_start_utc,review_end_utc,duration_minutes,review_type,documentation_completed) VALUES(?,?,?,?,?,?,?,?)",(p["patient_key"],p["provider_key"],mon,st.isoformat(),st.isoformat(),0,"Live dashboard review",0)); rid=cur.lastrowid; r.commit(); r.close(); self._audit(s,"START_REVIEW",p["patient_key"],mon); return self._json({"review_id":rid,"started_at":st.isoformat()},201)
        if path=="/api/reviews/end":
            if s["role"]!="Provider":return self._json({"error":"Provider role required"},403)
            rid=int(data.get("review_id",0)); r=runtime(); rv=r.execute("SELECT * FROM provider_reviews WHERE review_id=?",(rid,)).fetchone()
            if not rv or rv["provider_key"]!=s["provider_key"]:r.close();return self._json({"error":"Review not found or not authorized"},404)
            st=datetime.fromisoformat(rv["review_start_utc"]); en=now(); mins=max(.1,(en-st).total_seconds()/60); r.execute("UPDATE provider_reviews SET review_end_utc=?,duration_minutes=?,documentation_completed=? WHERE review_id=?",(en.isoformat(),round(mins,2),1 if data.get("documentation_completed",True) else 0,rid)); r.commit(); r.close();
            act=insert_provider_activity(rv["patient_key"],rv["provider_key"],rv["billing_month"],"DATA_REVIEW",st,en,"LIVE_DASHBOARD_REVIEW",note_text="Live provider dashboard review",documentation_completed=1 if data.get("documentation_completed",True) else 0,reason_code="LIVE_PATIENT_REVIEW",outcome_code="PROVIDER_REVIEW_COMPLETED")
            self._audit(s,"END_REVIEW",rv["patient_key"],rv["billing_month"],f"{mins:.2f} minutes"); return self._json({"review_id":rid,"activity_id":act["activity_id"],"duration_minutes":round(mins,2)})
        if path=="/api/notes":
            if s["role"]!="Provider":return self._json({"error":"Provider role required"},403)
            pid=data.get("patient_id",""); p=authorize_patient(s,pid); txt=(data.get("note_text")or"").strip(); mon=data.get("month") or available_months()[-1]
            if not p or not txt:return self._json({"error":"Authorized patient and note text are required"},400)
            mins=max(0.1,float(data.get("duration_minutes",3) or 3)); nid='NOTE-'+uuid.uuid4().hex[:10].upper(); end=now(); start=end-timedelta(minutes=mins); r=runtime(); r.execute("INSERT INTO provider_notes_v17(note_id,patient_key,provider_key,billing_month,created_at_utc,note_text,documentation_minutes,source) VALUES(?,?,?,?,?,?,?,?)",(nid,p["patient_key"],p["provider_key"],mon,end.isoformat(),txt,round(mins,2),"USER_RECORDED_V20")); r.commit(); r.close(); act=insert_provider_activity(p["patient_key"],p["provider_key"],mon,"DOCUMENTATION",start,end,"PROVIDER_NOTE",note_text=txt,documentation_completed=1,reason_code="DOCUMENTATION",outcome_code="DOCUMENTED"); self._audit(s,"ADD_NOTE",p["patient_key"],mon,f"{mins:.2f} documentation minutes"); return self._json({"note_id":nid,"activity_id":act["activity_id"],"duration_minutes":round(mins,2)},201)
        if path=="/api/communications":
            if s["role"]!="Provider":return self._json({"error":"Provider role required"},403)
            pid=data.get("patient_id",""); p=authorize_patient(s,pid); mon=data.get("month") or available_months()[-1]
            if not p:return self._json({"error":"Patient not found or not authorized"},404)
            mins=max(0.1,float(data.get("duration_minutes",0) or 0.1)); end=now(); start=end-timedelta(minutes=mins); channel=data.get("channel","Portal/Message"); mode=data.get("communication_mode","Asynchronous"); summary=(data.get("summary")or"Documented communication"); r=runtime(); ts=end.isoformat(); r.execute("INSERT INTO communications(patient_key,provider_key,billing_month,occurred_utc,channel,communication_mode,duration_minutes,summary) VALUES(?,?,?,?,?,?,?,?)",(p["patient_key"],p["provider_key"],mon,ts,channel,mode,round(mins,2),summary)); r.commit(); r.close(); act=insert_provider_activity(p["patient_key"],p["provider_key"],mon,"COMMUNICATION",start,end,"PATIENT_COMMUNICATION",channel=channel,interaction_mode=mode,note_text=summary,documentation_completed=1,reason_code="PATIENT_COMMUNICATION",outcome_code="DOCUMENTED"); self._audit(s,"RECORD_COMMUNICATION",p["patient_key"],mon,f"{mins:.2f} communication minutes"); return self._json({"ok":True,"activity_id":act["activity_id"],"duration_minutes":round(mins,2)},201)
        if path=="/api/chat":
            if s["role"]!="Provider":return self._json({"error":"Ask Reflect is available only in provider patient context in this demo"},403)
            pid=(data.get("patient_id")or"").strip(); mon=data.get("month") or available_months()[-1]; raw=(data.get("message")or"").strip(); msg=raw.lower()
            if not pid:return self._json({"answer":"Open one of your assigned patients first. Ask Reflect is intentionally patient-specific in V20.","disclaimer":"Descriptive synthetic analytics only."})
            p=patient_payload(s,pid,mon)
            if not p:return self._json({"error":"Patient not found or not authorized"},404)
            ms=p["month_summary"]; b=p["billing"]; ins=p.get('insights',{}); hist=p.get('history',[])
            if "trend" in msg or "change" in msg or "month" in msg:
                ec=ins.get('engagement_change'); sc=ins.get('session_change'); cc=ins.get('completion_change');
                parts=[f"For {pid} in {mon}, the engagement index is {float(ms.get('engagement_index',0) or 0):.1f} and session count is {ms.get('session_count',0)}."]
                if ec is not None: parts.append(f"Compared with the previous available month, engagement changed by {ec:+.1f} points and sessions changed by {sc:+d}.")
                if cc is not None: parts.append(f"Completion changed by {cc:+.1f} percentage points.")
                ans=' '.join(parts)+" These are descriptive usage changes, not clinical interpretation."
            elif "module" in msg or "xr" in msg or "mobile" in msg:
                mob=', '.join(f"{x['module_name']} ({x['sessions']} sessions, {x['minutes']} min)" for x in p['mobile_modules'][:4]) or 'no Mobile activity'; xr=', '.join(f"{x['module_name']} ({x['sessions']} sessions, {x['minutes']} min)" for x in p['xr_modules'][:4]) or 'no XR activity'; ans=f"For {mon}, {pid} used Mobile modules: {mob}. XR modules: {xr}."
            elif "time" in msg or "hour" in msg:
                best=max(p['time_of_day'],key=p['time_of_day'].get) if p['time_of_day'] else '—'; ans=f"The most active period for {pid} in {mon} is {best}, with {p['time_of_day'].get(best,0)} sessions. The core synthetic profile preference is {p['patient']['preferred_time_band']}."
            elif "billing" in msg or "provider" in msg or "review" in msg or "evidence" in msg or "time spent" in msg:
                ans=f"For {pid} in {mon}, total provider time is {b['total_provider_minutes']} minutes: {b['data_review_minutes']} minutes of data review, {b['communication_minutes']} minutes of communication, and {b['documentation_minutes']} minutes of documentation. There are {b['data_review_count']} review activities, {b['communication_count']} communication activities, and {b['documentation_count']} documentation activities. Patient engagement time is tracked separately and is not counted as provider time. Status is REQUIRES_REVIEW; this does not determine billing eligibility."
            elif "goal" in msg or "progress" in msg:
                ans=f"The synthetic monitoring goal is '{p['goal']['title']}' with {p['goal']['progress_percent']}% generated progress for {mon}. The engagement index is {float(ms.get('engagement_index',0) or 0):.1f}. These values support provider review but are not clinical outcomes."
            elif "remote" in msg or "clinic" in msg:
                total=max(int(ms.get('session_count',0) or 0),1); remote=int(ms.get('remote_sessions',0) or 0); clinic=int(ms.get('clinic_sessions',0) or 0); ans=f"In {mon}, {pid} has {remote} remote sessions ({remote/total*100:.1f}%) and {clinic} in-clinic sessions ({clinic/total*100:.1f}%)."
            else:
                peak=max(p['time_of_day'],key=p['time_of_day'].get) if p['time_of_day'] else '—'; top=sorted(p['mobile_modules']+p['xr_modules'],key=lambda x:x['sessions'],reverse=True); topmod=top[0]['module_name'] if top else 'none'; ans=f"{pid} had {ms.get('session_count',0)} sessions in {mon}, {ms.get('total_minutes',0)} engagement minutes, {float(ms.get('completion_rate',0)or 0):.1f}% completion, and an engagement index of {float(ms.get('engagement_index',0)or 0):.1f}. Most activity occurred in the {peak}; the most-used module was {topmod}. The synthetic longitudinal pattern is {p['patient']['trend_pattern']}."
            return self._json({"answer":ans,"disclaimer":"Patient-specific descriptive synthetic analytics only. Provider time is documentation support, not an automatic billing determination."})
        return self._json({"error":"Unknown endpoint"},404)
    def do_HEAD(self): return super().do_HEAD()
    def translate_path(self,path):
        parsed=urlparse(path).path
        if parsed.startswith('/api/'): return str(FRONTEND/'index.html')
        rel=parsed.lstrip('/') or 'index.html'; target=(FRONTEND/rel).resolve()
        if FRONTEND.resolve() not in target.parents and target!=FRONTEND.resolve(): return str(FRONTEND/'index.html')
        if not target.exists() or target.is_dir(): return str(FRONTEND/'index.html')
        return str(target)
    def end_headers(self):
        if not self.path.startswith('/api/'):
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'")
            self.send_header("X-Content-Type-Options","nosniff")
        super().end_headers()

if __name__=='__main__':
    md=metadata(); print('='*72); print('Reflect Provider Dashboard V20'); print('V18.2 data root:',DATA_ROOT); print('Billing master:', 'READY' if BILLING_CORE_DB.exists() else 'MISSING'); print('Patients:',md.get('patient_count'),'Providers:',md.get('provider_count'),'Months:',', '.join(available_months())); print(f'Open http://127.0.0.1:{PORT}'); print('='*72)
    ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
