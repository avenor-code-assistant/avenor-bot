"""SQLite access ledger. All times are UTC Unix seconds; no credentials stored here."""
import sqlite3
import os
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        remote = os.getenv('TURSO_DATABASE_URL')
        if remote:
            import libsql
            self.db = RemoteConnection(libsql.connect(remote, auth_token=os.environ['TURSO_AUTH_TOKEN']))
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subscriptions(
          user_id INTEGER PRIMARY KEY, expires INTEGER NOT NULL,
          warned INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
          invite TEXT, invite_until INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS grants(
          action_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
          days INTEGER NOT NULL, reason TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices(
          payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
          amount INTEGER NOT NULL, days INTEGER NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS payments(
          charge TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL,
          payload TEXT NOT NULL, created INTEGER NOT NULL, refunded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY, created INTEGER NOT NULL, kind TEXT NOT NULL,
          user_id INTEGER, detail TEXT NOT NULL);
        ''')
        self.db.commit()

    def remember(self, uid, name):
        with self.db:
            self.db.execute('INSERT INTO users VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (uid, name))

    def known(self, uid):
        return self.db.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone() is not None

    def sub(self, uid):
        return self.db.execute('SELECT * FROM subscriptions WHERE user_id=?', (uid,)).fetchone()

    def active(self, uid, now=None):
        row = self.sub(uid)
        return bool(row and row['expires'] > (int(time.time()) if now is None else now))

    def _extend(self, uid, days, now):
        row = self.sub(uid)
        end = max(now, row['expires'] if row else now) + days * 86400
        self.db.execute('''INSERT INTO subscriptions(user_id,expires) VALUES(?,?)
        ON CONFLICT(user_id) DO UPDATE SET expires=excluded.expires,warned=0,removed=0''', (uid, end))
        return end

    def grant(self, uid, days, action_id, reason, now=None):
        now = int(time.time()) if now is None else now
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO grants VALUES(?,?,?,?,?)', (action_id, uid, days, reason, now))
            if cur.rowcount == 0:
                return self.sub(uid)['expires'], False
            end = self._extend(uid, days, now)
        return end, True

    def invoice(self, payload, uid, amount, days):
        with self.db:
            self.db.execute('INSERT INTO invoices VALUES(?,?,?,?,?)', (payload, uid, amount, days, int(time.time())))

    def get_invoice(self, payload):
        return self.db.execute('SELECT * FROM invoices WHERE payload=?', (payload,)).fetchone()

    def paid(self, charge, uid, amount, payload, now=None):
        now = int(time.time()) if now is None else now
        with self.db:
            prior = self.db.execute('SELECT * FROM payments WHERE charge=?', (charge,)).fetchone()
            if prior:
                if prior['user_id'] != uid or prior['payload'] != payload:
                    raise ValueError('Payment identity mismatch')
                return self.sub(uid)['expires'], False
            invoice = self.get_invoice(payload)
            if not invoice or invoice['user_id'] != uid or invoice['amount'] != amount:
                raise ValueError('Payment invoice mismatch')
            self.db.execute('INSERT INTO payments(charge,user_id,amount,payload,created) VALUES(?,?,?,?,?)', (charge, uid, amount, payload, now))
            return self._extend(uid, invoice['days'], now), True

    def payment(self, charge):
        return self.db.execute('SELECT * FROM payments WHERE charge=?', (charge,)).fetchone()

    def mark_refunded(self, charge):
        with self.db:
            self.db.execute('UPDATE payments SET refunded=1 WHERE charge=?', (charge,))

    def set_invite(self, uid, link, until):
        with self.db:
            self.db.execute('UPDATE subscriptions SET invite=?,invite_until=? WHERE user_id=?', (link, until, uid))

    def expire(self, uid):
        with self.db:
            self.db.execute('UPDATE subscriptions SET expires=0,warned=0,removed=0 WHERE user_id=?', (uid,))

    def flag(self, uid, field, value=1):
        if field not in {'warned', 'removed'}:
            raise ValueError('Unknown field')
        with self.db:
            self.db.execute(f'UPDATE subscriptions SET {field}=? WHERE user_id=?', (value, uid))

    def subscriptions(self):
        return self.db.execute('SELECT * FROM subscriptions ORDER BY expires DESC').fetchall()

    def event(self, kind, uid, detail):
        with self.db:
            self.db.execute('INSERT INTO events(created,kind,user_id,detail) VALUES(?,?,?,?)', (int(time.time()), kind, uid, detail))

    def backup(self, path):
        target = sqlite3.connect(path)
        try:
            self.db.backup(target)
        finally:
            target.close()


class RemoteCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
    def _row(self, row):
        return dict(zip([d[0] for d in self.cursor.description], row)) if row is not None else None
    def fetchone(self):
        return self._row(self.cursor.fetchone())
    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

class RemoteConnection:
    def __init__(self, connection):
        self.connection = connection
    def execute(self, *args):
        return RemoteCursor(self.connection.execute(*args))
    def executescript(self, script):
        return self.connection.executescript(script)
    def commit(self):
        self.connection.commit()
    def __enter__(self):
        self.connection.__enter__()
        return self
    def __exit__(self, *args):
        return self.connection.__exit__(*args)
