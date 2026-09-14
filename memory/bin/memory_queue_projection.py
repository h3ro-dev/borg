"""Bounded metadata extension for the existing memory-health publisher only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from borg_config import CONFIG, BorgConfigError

LOCAL_MACHINE = os.environ.get('BORG_QUEUE_LOCAL_MACHINE', 'local')
REMOTE_HOSTS = tuple(filter(None, (value.strip() for value in os.environ.get('BORG_QUEUE_REMOTE_HOSTS', '').split(','))))
HOSTS = (LOCAL_MACHINE, *REMOTE_HOSTS)
REMOTE_HOME = os.environ.get('BORG_QUEUE_REMOTE_HOME')
HELPER_SHA256 = '444bfe61d81643f6d13f611966d101423d87001db274f9876ab32d286d32b830'
MAX_BYTES = 1024 * 1024
HOST_TIMEOUT = 28
COUNT_FIELDS = ('queued_files','unique_file_ids','cohort_size','cohort_locally_accepted',
                'cohort_still_queued','cohort_missing_without_receipt','cursor_rows')
ATTEMPTS = ('PASS','PASS_EMPTY','SKIP_DUP','BUSY','FAIL_OPEN','DEFERRED','KILL_SWITCH','CONTINUATION_PENDING','OTHER')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def valid_stamp(value):
    try:
        return isinstance(value,str) and len(value)<=40 and datetime.fromisoformat(value.replace('Z','+00:00')).tzinfo is not None
    except ValueError:
        return False


def number(value):
    return value if type(value) is int and 0<=value<=1000000000 else None


def unavailable(machine, error):
    return {'machine_id':machine,'state':'UNAVAILABLE','observed_at':None,
            'checked_at':stamp(),'error':error,**{key:None for key in COUNT_FIELDS},
            'oldest_age_s':None,'age_basis':None,'attempts_last_hour':None,
            'scan_error_count':None,'cohort_sha256':None}


def sanitize(machine, raw):
    if not isinstance(raw,dict) or raw.get('schema')!='capture-queue-metadata/v2' or raw.get('state') not in ('OK','PARTIAL','UNAVAILABLE') or not valid_stamp(raw.get('observed_at')):
        return unavailable(machine,'queue_source_invalid')
    row=unavailable(machine,'queue_source_unavailable')
    row.update(state=raw['state'],observed_at=raw['observed_at'])
    if raw['state']=='UNAVAILABLE':
        if raw.get('reason')=='deadline_exceeded':row['error']='queue_timeout'
        elif raw.get('reason')=='invalid_or_unreadable_source':row['error']='queue_source_invalid'
        return row
    row.update({key:number(raw.get(key)) for key in COUNT_FIELDS})
    age=raw.get('oldest_age_s')
    row['oldest_age_s']=age if type(age) in (int,float) and math.isfinite(age) and age>=0 else None
    row['age_basis']=raw.get('age_basis') if raw.get('age_basis') in ('birthtime','mtime_retry_rotated') else None
    attempts=raw.get('attempts_last_hour')
    row['attempts_last_hour']={key:number(attempts.get(key,0)) for key in ATTEMPTS} if isinstance(attempts,dict) else None
    errors=raw.get('scan_errors')
    row['scan_error_count']=sum(errors.values()) if isinstance(errors,dict) and all(number(v) is not None for v in errors.values()) else None
    cohort=raw.get('cohort')
    if isinstance(cohort,list) and len(cohort)<=32 and all(isinstance(v,str) and len(v)==64 and all(c in '0123456789abcdef' for c in v) for v in cohort):
        row['cohort_sha256']=hashlib.sha256(json.dumps(sorted(cohort),separators=(',',':')).encode()).hexdigest()
    if isinstance(errors,dict):
        if any(number(errors.get(k,0)) not in (0,None) for k in ('log_parse','log_read')):row['attempts_last_hour']=None
        if number(errors.get('queue_stat',0)) not in (0,None):
            for key in ('queued_files','unique_file_ids','oldest_age_s','age_basis','cohort_still_queued','cohort_missing_without_receipt'):row[key]=None
        if number(errors.get('cursor_read',0)) not in (0,None):row['cursor_rows']=None
    if raw['state']=='OK' and (any(row[k] is None for k in COUNT_FIELDS) or row['scan_error_count']!=0):row['state']='PARTIAL'
    row['error']=None if row['state']=='OK' else 'queue_scan_partial'
    return row


# Fixed program verifies the reviewed executable bytes before running. No
# caller path, host, environment or source text is forwarded to this program.
PROGRAM = """import hashlib,os,pathlib,sys
p=pathlib.Path(os.environ['BORG_HOME'])/'mem0/bin/mem0-queue-metrics'
if hashlib.sha256(p.read_bytes()).hexdigest()!=%r:raise SystemExit(3)
os.execv(sys.executable,[sys.executable,'-B',str(p),'--timeout-seconds','25'])
""" % HELPER_SHA256


def command(machine):
    if machine==LOCAL_MACHINE:return ['/usr/bin/env',f'BORG_HOME={CONFIG.home}','/usr/bin/python3','-B','-c',PROGRAM]
    if machine not in HOSTS:raise ValueError('queue_host_not_allowed')
    if not REMOTE_HOME:raise BorgConfigError('BORG_QUEUE_REMOTE_HOME is required for remote queue hosts')
    import shlex
    return ['/usr/bin/ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=3',machine,
            shlex.join(['env',f'BORG_HOME={REMOTE_HOME}','python3','-B','-c',PROGRAM])]


def run_host(machine):
    process=None
    try:
        process=subprocess.Popen(command(machine),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
        os.set_blocking(process.stdout.fileno(),False)
        data=bytearray();deadline=time.monotonic()+HOST_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:return unavailable(machine,'queue_timeout')
                ready=selector.select(min(remaining,.2))
                if not ready:continue
                chunk=os.read(process.stdout.fileno(),65536)
                if not chunk:break
                data.extend(chunk)
                if len(data)>MAX_BYTES:return unavailable(machine,'queue_output_limit')
        code=process.wait(timeout=max(.01,deadline-time.monotonic()))
        if code not in (0,2):return unavailable(machine,'queue_helper_unavailable')
        return sanitize(machine,json.loads(data))
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return unavailable(machine,'queue_read_failed')
    finally:
        if process:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            process.wait(timeout=1)
            if process.stdout:process.stdout.close()


def collect():
    started=stamp()
    # Three independent existing collectors only. Per-host deadlines bound the
    # whole addition to ~29 seconds, without serial 3x timeout accumulation.
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        machines=list(pool.map(run_host,HOSTS))
    return {'schema':'memory-queue-projection/v1','published_at':stamp(),'started_at':started,
            'host_coverage':{'observed':sum(r['state']=='OK' for r in machines),'denominator':len(HOSTS),'attempted':len(HOSTS)},
            'max_age_seconds':900,'helper_sha256':HELPER_SHA256,'machines':machines,
            'semantics':'Three selected local metadata scans; local cohort receipts are not native accepted source coverage. No persisted prior cohort is supplied.'}
