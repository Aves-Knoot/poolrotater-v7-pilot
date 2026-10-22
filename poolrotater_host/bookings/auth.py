"""Separate identify-only browser sessions; host OAuth credentials never enter."""
import hashlib
import hmac
import secrets
import time
from ..diagnostic_logs.common import private_database,transaction
from ..network.contracts import NetworkError,require,integer,text


def digest(value):
    require(type(value)is str and 32<=len(value)<=128 and all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-'for c in value),'booking_signin_required',401)
    return hashlib.sha256(value.encode('ascii')).hexdigest()


class Sessions:
    def __init__(self,root,*,clock=time.time):
        self.clock=clock;self.path=private_database(root,'booking-browser.sqlite3')
        with transaction(self.path)as db:
            db.execute('CREATE TABLE IF NOT EXISTS oauth_states(state TEXT PRIMARY KEY,browser TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS browser_sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL,username TEXT NOT NULL,csrf TEXT NOT NULL,expires REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS request_limits(user_id INTEGER NOT NULL,bucket TEXT NOT NULL,at REAL NOT NULL,PRIMARY KEY(user_id,bucket))')

    def begin(self):
        state=secrets.token_urlsafe(32);browser=secrets.token_urlsafe(32);now=self.clock()
        with transaction(self.path)as db:
            db.execute('DELETE FROM oauth_states WHERE expires<?',(now,))
            require(db.execute('SELECT count(*)FROM oauth_states').fetchone()[0]<10000,'booking_signin_busy',429)
            db.execute('INSERT INTO oauth_states VALUES(?,?,?,0)',(digest(state),digest(browser),now+600))
        return {'state':state,'browser':browser}

    def consume(self,state,browser):
        now=self.clock()
        try:key=digest(state);browser=digest(browser)
        except NetworkError:raise NetworkError('booking_oauth_state_refused',403)from None
        with transaction(self.path)as db:
            row=db.execute('SELECT * FROM oauth_states WHERE state=?',(key,)).fetchone()
            require(row is not None and not row['used']and row['expires']>=now and hmac.compare_digest(row['browser'],browser),'booking_oauth_state_refused',403)
            db.execute('UPDATE oauth_states SET used=1 WHERE state=?',(key,))

    def create(self,user):
        integer(user.get('id'),1,2**63-1);text(user.get('username'),80)
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);now=self.clock()
        with transaction(self.path)as db:
            db.execute('DELETE FROM browser_sessions WHERE expires<?',(now,))
            require(db.execute('SELECT count(*)FROM browser_sessions').fetchone()[0]<100000,'booking_signin_busy',429)
            db.execute('INSERT INTO browser_sessions VALUES(?,?,?,?,?)',(digest(token),user['id'],user['username'],csrf,now+86400))
        return token

    def get(self,token):
        try:key=digest(token)
        except Exception:return None
        with transaction(self.path)as db:row=db.execute('SELECT * FROM browser_sessions WHERE token=?',(key,)).fetchone()
        if row is None or not self.clock()<=row['expires']:return None
        return {'user':{'id':row['user_id'],'username':row['username']},'csrf_token':row['csrf'],'expires':row['expires']}

    def check(self,token,csrf):
        session=self.get(token)
        require(session is not None,'booking_signin_required',401)
        require(type(csrf)is str and hmac.compare_digest(session['csrf_token'],csrf),'booking_csrf_refused',403)
        return session

    def logout(self,token):
        with transaction(self.path)as db:db.execute('DELETE FROM browser_sessions WHERE token=?',(digest(token),))

    def admit(self,user_id,bucket,interval):
        """Bound expensive browser work independently from host control pacing."""
        require(bucket in ('preview','invite'),'booking_request_refused')
        now=self.clock()
        with transaction(self.path)as db:
            db.execute('DELETE FROM request_limits WHERE at<?',(now-86400,))
            old=db.execute('SELECT at FROM request_limits WHERE user_id=?AND bucket=?',(user_id,bucket)).fetchone()
            require(old is None or now-old['at']>=interval,'booking_request_rate_limited',429)
            require(db.execute('SELECT count(*)FROM request_limits').fetchone()[0]<100000,'booking_signin_busy',429)
            db.execute('INSERT OR REPLACE INTO request_limits VALUES(?,?,?)',(user_id,bucket,now))
