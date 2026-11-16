#!/usr/bin/env python3
"""Bounded historical archive repair through Mina's official block importer.

Archive SQL is read-only. No manual chain-status updates or payout operations.
Requires Python 3.9+, psql and mina-archive-blocks, all present in the archive image.
"""
import argparse
import base64
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request


class GuardianError(RuntimeError): pass
class BlockUnavailable(GuardianError): pass
class AlreadyRunning(GuardianError): pass


def log(message):
    print(time.strftime('[%H:%M:%S] ',time.gmtime())+message,flush=True)


def valid_hash(value):
    if not isinstance(value,str) or not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{45,60}',value):
        raise GuardianError('invalid block hash')
    return value


@dataclass(frozen=True)
class BlockRef:
    height: int
    state_hash: str
    def __post_init__(self):
        if type(self.height) is not int or self.height<1: raise GuardianError('invalid block height')
        valid_hash(self.state_hash)


@dataclass
class Block:
    ref: BlockRef
    parent_hash: str
    slot: int
    global_slot: int
    payload: bytes


@dataclass
class Limits:
    max_blocks: int=250
    max_roots: int=64
    max_seconds: int=240
    max_bytes: int=256*1024*1024
    max_file_bytes: int=64*1024*1024
    def __post_init__(self):
        if any(type(v) is not int or v<=0 for v in vars(self).values()):
            raise GuardianError('guardian limits must be positive integers')


def parse_block(payload,ref):
    try:
        # Yojson contains binary sok_digest strings. Keep original bytes for Mina.
        raw=json.loads(payload.decode('utf-8','surrogateescape'))
        state=raw.get('data',raw)['protocol_state']
        consensus=state['body']['consensus_state']
        height=consensus['blockchain_length']
        slot=consensus['curr_global_slot_since_hard_fork']['slot_number']
        global_slot=consensus['global_slot_since_genesis']
        if any(not re.fullmatch('[0-9]+',str(v)) for v in (height,slot,global_slot)):
            raise ValueError()
        if int(height)!=ref.height: raise ValueError()
        return Block(ref,valid_hash(state['previous_state_hash']),int(slot),int(global_slot),payload)
    except (ValueError,KeyError,TypeError,AttributeError,GuardianError):
        raise BlockUnavailable('downloaded block has invalid height or protocol fields') from None


class SqlArchive:
    def __init__(self,query): self.query=query
    def missing(self,refs):
        if not refs: return []
        hashes=','.join("'"+valid_hash(r.state_hash)+"'" for r in refs)
        rows=self.query('SELECT height,state_hash FROM blocks WHERE state_hash IN ('+hashes+')')
        found={r['state_hash']:r['height'] for r in rows}
        if len(found)!=len(rows) or any(r.state_hash in found and found[r.state_hash]!=r.height for r in refs):
            raise GuardianError('archive block identity mismatch')
        return [r for r in refs if r.state_hash not in found]
    def lookup(self,ref):
        rows=self.query("SELECT id,height,state_hash,parent_hash,parent_id,chain_status,global_slot_since_hard_fork,global_slot_since_genesis FROM blocks WHERE state_hash='"+valid_hash(ref.state_hash)+"'")
        if not rows: return None
        if len(rows)!=1 or rows[0]['height']!=ref.height: raise GuardianError('archive block identity mismatch')
        return rows[0]
    def missing_roots(self,skip=(),limit=64):
        excluded=','.join("'"+valid_hash(h)+"'" for h in skip)
        condition=' AND b.parent_hash NOT IN ('+excluded+')' if excluded else ''
        rows=self.query("""SELECT b.parent_hash AS state_hash,MIN(b.height)-1 AS height,
          MAX(b.height)-1 AS max_height,
          MIN(CASE WHEN b.chain_status='canonical' THEN 0 ELSE 1 END) AS priority
          FROM blocks b LEFT JOIN blocks p ON p.state_hash=b.parent_hash
          WHERE b.height>1 AND b.chain_status IN ('canonical','pending') AND p.id IS NULL"""+condition+
          ' GROUP BY b.parent_hash ORDER BY priority,height LIMIT '+str(int(limit)))
        if any(r['height']!=r['max_height'] for r in rows): raise GuardianError('inconsistent missing-parent heights')
        return [BlockRef(r['height'],r['state_hash']) for r in rows]
    def audit(self):
        canonical=self.query("""SELECT protocol_version_id,MIN(height) AS first,MAX(height) AS last,
          COUNT(*) AS n,COUNT(DISTINCT height) AS unique_heights FROM blocks
          WHERE chain_status='canonical' GROUP BY protocol_version_id""")
        def count(sql): return int(self.query(sql)[0]['n'])
        return {'canonical_rows':sum(r['n'] for r in canonical),
            'canonical_gaps':sum(r['last']-r['first']+1-r['unique_heights'] for r in canonical),
            'duplicate_canonical':sum(r['n']-r['unique_heights'] for r in canonical),
            'missing_parents':count("""SELECT COUNT(DISTINCT b.parent_hash) AS n FROM blocks b
              LEFT JOIN blocks p ON p.state_hash=b.parent_hash WHERE b.height>1
              AND b.chain_status IN ('canonical','pending') AND p.id IS NULL"""),
            'broken_links':count("""SELECT COUNT(*) AS n FROM blocks b LEFT JOIN blocks p ON p.id=b.parent_id
              WHERE b.chain_status='canonical' AND b.height>1 AND (p.id IS NULL OR p.height<>b.height-1
              OR p.state_hash<>b.parent_hash OR p.chain_status<>'canonical'
              OR (p.protocol_version_id=b.protocol_version_id AND p.global_slot_since_hard_fork>=b.global_slot_since_hard_fork))"""),
            'stale_pending':count("""SELECT COUNT(*) AS n FROM blocks b JOIN
              (SELECT protocol_version_id,MAX(height) AS h FROM blocks WHERE chain_status='canonical'
              GROUP BY protocol_version_id) c ON c.protocol_version_id=b.protocol_version_id
              WHERE b.chain_status='pending' AND b.height<=c.h""")}


class Psql:
    def __init__(self,uri): self.uri=uri
    def __call__(self,sql):
        env=dict(os.environ,PGCONNECT_TIMEOUT='10',PGAPPNAME='mina-guardian-audit',
                 PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=30000')
        try:
            r=subprocess.run(['psql','-X','-A','-t','-v','ON_ERROR_STOP=1','-d',self.uri,
                              '-c','SELECT row_to_json(guardian_row) FROM ('+sql+') guardian_row'],
                             env=env,capture_output=True,timeout=40)
            if r.returncode: raise GuardianError('archive query failed; no absence inferred')
            return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
        except (OSError,subprocess.TimeoutExpired,ValueError):
            raise GuardianError('archive query unavailable or invalid; no absence inferred') from None


class RetryState:
    def __init__(self,path,binding,base=600,maximum=21600):
        self.path=Path(path); self.binding=binding; self.base=base; self.maximum=maximum; self.failures={}
        if base<=0 or maximum<base: raise GuardianError('invalid retry intervals')
        if self.path.exists():
            try:
                data=json.loads(self.path.read_text())
                if data['version']!=1 or not isinstance(data['failures'],dict): raise ValueError()
                if data['binding']==binding:
                    self.failures=data['failures']
                    for key,value in self.failures.items():
                        valid_hash(key)
                        if type(value['attempts']) is not int or value['attempts']<1: raise ValueError()
                        if type(value['retry_at']) not in (int,float) or not 0<=value['retry_at']<1e12: raise ValueError()
            except (ValueError,KeyError,TypeError,GuardianError):
                raise GuardianError('guardian retry state is invalid; inspect it before restarting') from None
    def active(self,now): return {h for h,v in self.failures.items() if v['retry_at']>now}
    def fail(self,ref,now):
        n=self.failures.get(ref.state_hash,{}).get('attempts',0)+1
        self.failures[ref.state_hash]={'attempts':n,'retry_at':now+min(self.maximum,self.base*2**min(n-1,20))}
        self.save()
    def clear(self,ref):
        if self.failures.pop(ref.state_hash,None) is not None: self.save()
    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,name=tempfile.mkstemp(prefix='.retry-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as f:
                json.dump({'version':1,'binding':self.binding,'failures':self.failures},f)
                f.flush(); os.fsync(f.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name): os.unlink(name)


@contextlib.contextmanager
def iteration_lock(directory):
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (directory/'run.lock').open('a') as f:
        try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise AlreadyRunning('another guardian iteration is running') from None
        try: yield
        finally: fcntl.flock(f,fcntl.LOCK_UN)


class HttpFetcher:
    DEFAULT_SOURCES=('https://storage.googleapis.com/mina_network_block_data',
                     'https://673156464838-mina-precomputed-blocks.s3.us-west-2.amazonaws.com/mainnet')
    def __init__(self,sources=None):
        self.bytes_received=0
        self.sources=tuple(sources or self.DEFAULT_SOURCES)
        if not self.sources or any(not s.startswith('https://') for s in self.sources):
            raise GuardianError('block sources must use HTTPS')
    def __call__(self,ref,byte_limit,timeout):
        deadline=time.monotonic()+timeout
        initial_bytes=self.bytes_received
        for source in self.sources:
            remaining=deadline-time.monotonic()
            available=byte_limit-(self.bytes_received-initial_bytes)
            if remaining<=0 or available<=0: break
            try:
                request=urllib.request.Request(source.rstrip('/')+f'/mainnet-{ref.height}-{ref.state_hash}.json',headers={'Accept-Encoding':'identity'})
                with urllib.request.urlopen(request,timeout=min(25,remaining)) as r:
                    if int(r.headers.get('Content-Length','0'))>available: raise BlockUnavailable('block exceeds download limit')
                    chunks=[]; size=0
                    while True:
                        if time.monotonic()>=deadline: raise BlockUnavailable('download deadline exceeded')
                        chunk=r.read(min(1024*1024,available-size+1))
                        if not chunk: break
                        size+=len(chunk)
                        self.bytes_received+=len(chunk)
                        if size>available: raise BlockUnavailable('block exceeds download limit')
                        chunks.append(chunk)
                    payload=b''.join(chunks)
                    checksum=re.search(r'(?:^|,\s*)md5=([^,]+)',r.headers.get('x-goog-hash',''))
                    if checksum and base64.b64decode(checksum.group(1))!=hashlib.md5(payload).digest():
                        raise BlockUnavailable('download checksum mismatch')
                parse_block(payload,ref)
                return payload
            except (OSError,ValueError,BlockUnavailable): continue
        raise BlockUnavailable('block unavailable or invalid at configured sources')


class MinaImporter:
    def __init__(self,uri): self.uri=uri
    def __call__(self,blocks,timeout):
        with tempfile.TemporaryDirectory(prefix='guardian-import-') as directory:
            directory=Path(directory); paths={}
            for block in blocks:
                path=directory/f'mainnet-{block.ref.height}-{block.ref.state_hash}.json'
                path.write_bytes(block.payload); paths[str(path)]=block.ref.state_hash
            success=directory/'success.txt'; failed=directory/'failed.txt'
            env=dict(os.environ,PGCONNECT_TIMEOUT='10',PGAPPNAME='mina-historical-guardian',
                     PGOPTIONS='-c statement_timeout=30000 -c lock_timeout=5000')
            try:
                with (directory/'import.log').open('wb') as output:
                    r=subprocess.run(['mina-archive-blocks','--precomputed','--archive-uri',self.uri,
                        '--successful-files',str(success),'--failed-files',str(failed),*paths],
                        env=env,stdout=output,stderr=subprocess.STDOUT,timeout=timeout)
                if r.returncode: return set()
            except (OSError,subprocess.TimeoutExpired): return set()
            successes=set(success.read_text().splitlines()) if success.exists() else set()
            failures=set(failed.read_text().splitlines()) if failed.exists() else set()
            return {paths[p] for p in successes-failures if p in paths}


class Guardian:
    def __init__(self,archive,fetch,importer,state,limits=None,clock=time.monotonic,wall_clock=time.time):
        self.archive=archive; self.fetch=fetch; self.importer=importer; self.state=state
        self.limits=limits or Limits(); self.clock=clock; self.wall_clock=wall_clock
    def run(self,recent,check=False):
        start=self.clock(); remaining=lambda:max(0,self.limits.max_seconds-(self.clock()-start))
        # A slow download walk must leave time to persist its bounded progress.
        import_reserve=min(120,self.limits.max_seconds/2)
        download_remaining=lambda:max(0,remaining()-import_reserve)
        result={'imported':0,'failed':0,'deferred':0,'limited':False,'audit':self.archive.audit()}
        recent_missing=self.archive.missing(recent)
        result['recent_missing']=len(recent_missing)
        if check: return result
        excluded=self.state.active(self.wall_clock())
        seeds=self.archive.missing_roots(excluded,self.limits.max_roots)+sorted(recent_missing,key=lambda r:-r.height)
        attempts=used_bytes=0; visited=set()
        for seed in seeds:
            if attempts>=self.limits.max_blocks or download_remaining()<=0: result['limited']=True; break
            ref=seed; collected=[]; child_slot=None
            while True:
                known=self.archive.lookup(ref)
                if known is not None:
                    if child_slot is not None and known['global_slot_since_genesis']>=child_slot:
                        result['failed']+=1; collected=[]; self.state.fail(seed,self.wall_clock())
                    break
                if ref.state_hash in visited: break
                if ref.state_hash in self.state.active(self.wall_clock()): result['deferred']+=1; break
                if attempts>=self.limits.max_blocks or download_remaining()<=0 or used_bytes>=self.limits.max_bytes:
                    result['limited']=True; break
                attempts+=1; visited.add(ref.state_hash)
                byte_limit=min(self.limits.max_file_bytes,self.limits.max_bytes-used_bytes)
                received_before=getattr(self.fetch,'bytes_received',None)
                try:
                    raw=self.fetch(ref,byte_limit,min(30,download_remaining()))
                    if len(raw)>byte_limit: raise BlockUnavailable('download limit exceeded')
                    if received_before is None: used_bytes+=len(raw)
                    block=parse_block(raw,ref)
                    if child_slot is None:
                        # The existing child is an independent height/hash anchor.
                        children=self.archive.query("SELECT global_slot_since_genesis FROM blocks WHERE parent_hash='"+ref.state_hash+"'")
                        if children: child_slot=min(r['global_slot_since_genesis'] for r in children)
                    if child_slot is not None and block.global_slot>=child_slot: raise BlockUnavailable('non-decreasing parent slot')
                except BlockUnavailable:
                    self.state.fail(ref,self.wall_clock()); result['failed']+=1
                    log(f'Block {ref.height}: unavailable/invalid; retry scheduled')
                    break
                finally:
                    if received_before is not None:
                        used_bytes+=self.fetch.bytes_received-received_before
                collected.append(block); child_slot=block.global_slot
                if ref.height==1: break
                ref=BlockRef(ref.height-1,block.parent_hash)
            if not collected: continue
            if remaining()<=0: result['limited']=True; break
            ordered=list(reversed(collected))
            # Recheck DB availability before invoking any writer.
            self.archive.lookup(ordered[0].ref)
            receipts=self.importer(ordered,min(120,remaining()))
            for block in ordered:
                row=self.archive.lookup(block.ref)
                exact=row is not None and row['parent_hash']==block.parent_hash and row['global_slot_since_hard_fork']==block.slot and row['global_slot_since_genesis']==block.global_slot
                if block.ref.state_hash in receipts and exact:
                    result['imported']+=1; self.state.clear(block.ref)
                else:
                    result['failed']+=1; self.state.fail(block.ref,self.wall_clock())
            log(f'Imported/verified {result["imported"]} blocks; failed {result["failed"]}')
        result['audit']=self.archive.audit()
        result['recent_missing']=len(self.archive.missing(recent))
        result['cooldown_blocks']=len(self.state.active(self.wall_clock()))
        return result


def daemon_chain(url,lookback,expected_chain=None):
    query='{ syncStatus daemonStatus { chainId } bestChain(maxLength: '+str(lookback)+') { stateHash protocolState { consensusState { blockchainLength } } } }'
    request=urllib.request.Request(url,data=json.dumps({'query':query}).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=20) as r: raw=r.read(4*1024*1024+1)
        if len(raw)>4*1024*1024: raise ValueError()
        response=json.loads(raw); data=response['data']
        if response.get('errors') or data['syncStatus']!='SYNCED': raise ValueError()
        if expected_chain and data['daemonStatus']['chainId']!=expected_chain: raise ValueError()
        refs=[BlockRef(int(b['protocolState']['consensusState']['blockchainLength']),b['stateHash']) for b in data['bestChain']]
        if not refs: raise ValueError()
        return refs
    except (OSError,ValueError,KeyError,TypeError): raise GuardianError('daemon evidence unavailable or invalid') from None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--check',action='store_true')
    args=parser.parse_args(argv)
    try:
        uri=os.environ['PG_CONN']
        if os.environ.get('MINA_NETWORK','mainnet')!='mainnet': raise GuardianError('this block-source configuration requires mainnet')
        directory=Path(os.environ.get('GUARDIAN_STATE_DIR','/tmp/blocks-guardian'))
        def number(name,default):
            n=int(os.environ.get(name,default))
            if n<=0: raise GuardianError('guardian settings must be positive')
            return n
        limits=Limits(number('GUARDIAN_MAX_BLOCKS',250),number('GUARDIAN_MAX_ROOTS',64),
                      number('GUARDIAN_MAX_SECONDS',240),number('GUARDIAN_MAX_BYTES',256*1024*1024),
                      number('GUARDIAN_MAX_FILE_BYTES',64*1024*1024))
        with iteration_lock(directory):
            recent=daemon_chain(os.environ.get('DAEMON_URL','http://mina_node:3085/graphql'),
                                number('GUARDIAN_LOOKBACK',290),os.environ.get('GUARDIAN_EXPECTED_CHAIN_ID'))
            state=RetryState(directory/'retry.json',hashlib.sha256(uri.encode()).hexdigest(),
                             number('GUARDIAN_RETRY_SECONDS',600),number('GUARDIAN_MAX_RETRY_SECONDS',21600))
            sources=os.environ.get('GUARDIAN_SOURCES')
            worker=Guardian(SqlArchive(Psql(uri)),HttpFetcher(sources.split(',') if sources else None),MinaImporter(uri),state,limits)
            result=worker.run(recent,check=args.check)
            log(json.dumps(result,sort_keys=True))
            audit=result['audit']
            unresolved=not audit['canonical_rows'] or any(v for k,v in audit.items() if k!='canonical_rows')
            return 2 if unresolved or result['failed'] or result['recent_missing'] else 0
    except AlreadyRunning:
        log('Another guardian iteration is running; skipped'); return 0
    except (GuardianError,OSError,ValueError,KeyError):
        log('Guardian stopped: invalid input/state or unavailable service; no successful repair claimed')
        return 1


if __name__=='__main__': raise SystemExit(main())
