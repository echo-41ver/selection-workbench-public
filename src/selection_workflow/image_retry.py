"""One durable retry schedule per source asset; never weaken transport validation."""
import json,math,random,time,uuid

class ImageRetryPending(RuntimeError):pass
class ImageNeedsAction(RuntimeError):pass
class ImageRetryCancelled(RuntimeError):pass

def classify_failure(error):
    response=getattr(error,'response',None)
    code=getattr(response,'status_code',getattr(error,'code',None))
    text=str(error).lower();kind=type(error).__name__.lower()
    reason=getattr(error,'reason',None)
    if getattr(error,'errno',None) in (104,110,111,10054,10060,10061) or getattr(reason,'errno',None) in (104,110,111,10054,10060,10061):return 'TRANSIENT_NETWORK'
    if code in (401,403):return 'ACTION_AUTH'
    if code in (404,410):return 'ACTION_MISSING'
    if code in (408,425,429) or isinstance(code,int) and 500<=code<=599:return 'TRANSIENT_NETWORK'
    if 'certificate_verify_failed' in text or 'certificate verify failed' in text:return 'ACTION_TLS'
    if 'unidentifiedimage' in kind or any(x in text for x in ('cannot identify image','image too small','broken data stream','truncated file','invalid image')):return 'ACTION_INVALID_IMAGE'
    if any(x in kind+' '+text for x in ('timeout','timed out','ssl eof','unexpected_eof','eof occurred','connection reset','connection aborted','connection refused','remoteprotocolerror','connecterror','readerror','temporary failure','name resolution','getaddrinfo','network is unreachable')):return 'TRANSIENT_NETWORK'
    if '404' in text or '410 gone' in text:return 'ACTION_MISSING'
    if '403 forbidden' in text or '401 unauthorized' in text:return 'ACTION_AUTH'
    return 'ACTION_UNKNOWN'

class ImageRetryQueue:
    def __init__(self,con,clock=time.time,jitter=random.random,base_delay=15,max_delay=300):
        self.con=con;self.clock=clock;self.jitter=jitter;self.base_delay=base_delay;self.max_delay=max_delay
        con.executescript('''CREATE TABLE IF NOT EXISTS image_retry_queue(
          image_key TEXT PRIMARY KEY,source_ref TEXT NOT NULL,product_json TEXT NOT NULL,
          status TEXT NOT NULL,attempts INTEGER NOT NULL,next_due REAL NOT NULL,
          lease_until REAL,last_classification TEXT,last_error TEXT,updated_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS idx_image_retry_due ON image_retry_queue(status,next_due);
          CREATE TABLE IF NOT EXISTS image_retry_history(
          event_id TEXT PRIMARY KEY,image_key TEXT NOT NULL,attempt INTEGER NOT NULL,
          event TEXT NOT NULL,classification TEXT,error TEXT,at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS image_pair_requeue_history(
          event_id TEXT PRIMARY KEY,image_key TEXT NOT NULL,path_key TEXT,asin TEXT,goods_id TEXT,
          pair_revision TEXT,old_status TEXT,old_retry_count INTEGER,old_error TEXT,at REAL NOT NULL);''')
    def get(self,key):return self.con.execute('SELECT * FROM image_retry_queue WHERE image_key=?',(key,)).fetchone()
    def _event(self,key,attempt,event,classification=None,error=None):
        self.con.execute('INSERT INTO image_retry_history VALUES(?,?,?,?,?,?,?)',
          (uuid.uuid4().hex,key,attempt,event,classification,error,self.clock()))
    def delay(self,attempt):
        base=min(self.max_delay,self.base_delay*2**min(max(0,attempt-1),20))
        return min(self.max_delay,max(1,base*(.85+.3*self.jitter())))
    def legacy(self,key,ref,product,attempts,error):
        if self.get(key):return
        classification=classify_failure(error);now=self.clock()
        status='RETRY' if classification=='TRANSIENT_NETWORK' else 'ACTION_REQUIRED'
        self.con.execute('INSERT INTO image_retry_queue VALUES(?,?,?,?,?,?,?,?,?,?)',
          (key,ref,json.dumps(product,ensure_ascii=False),status,attempts,now,None,classification,str(error),now))
        self._event(key,attempts,'LEGACY_FAILURE_IMPORTED',classification,str(error))
    def started(self,key,ref,product,prior_attempts=0):
        row=self.get(key);attempt=max(prior_attempts,row['attempts'] if row else 0)+1;now=self.clock()
        self.con.execute('''INSERT INTO image_retry_queue VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(image_key) DO UPDATE SET status='RUNNING',attempts=excluded.attempts,
          lease_until=excluded.lease_until,product_json=excluded.product_json,updated_at=excluded.updated_at''',
          (key,ref,json.dumps(product,ensure_ascii=False),'RUNNING',attempt,now,now+120,None,None,now))
        self._event(key,attempt,'DOWNLOAD_STARTED');return attempt
    def finish(self,key,error=None):
        row=self.get(key);attempt=row['attempts'];now=self.clock()
        classification=classify_failure(error) if error is not None else None
        status='RETRY' if classification=='TRANSIENT_NETWORK' else 'ACTION_REQUIRED' if error is not None else 'READY'
        due=now+self.delay(attempt) if status=='RETRY' else now
        self.con.execute('UPDATE image_retry_queue SET status=?,next_due=?,lease_until=NULL,last_classification=?,last_error=?,updated_at=? WHERE image_key=?',
          (status,due,classification,str(error) if error is not None else None,now,key))
        self._event(key,attempt,'DOWNLOAD_FAILED' if error is not None else 'DOWNLOAD_READY',classification,str(error) if error is not None else None)
    def due(self,limit):
        now=self.clock()
        self.con.execute("UPDATE image_retry_queue SET status='RETRY',lease_until=NULL,next_due=? WHERE status='RUNNING' AND lease_until<=?",(now,now))
        return self.con.execute("SELECT * FROM image_retry_queue WHERE status='RETRY' AND next_due<=? ORDER BY next_due,updated_at,image_key LIMIT ?",(now,limit)).fetchall()
    def cancel(self,key):
        row=self.get(key);now=self.clock()
        self.con.execute("UPDATE image_retry_queue SET status='RETRY',next_due=?,lease_until=NULL,updated_at=? WHERE image_key=?",(now,now,key))
        self._event(key,row['attempts'],'CANCELLED_BEFORE_SEND')
    def deferred(self,key):
        row=self.get(key)
        if row and row['status']=='ACTION_REQUIRED':return ImageNeedsAction(row['last_classification'] or 'image requires action')
        return ImageRetryPending('image scheduled for durable network retry')
