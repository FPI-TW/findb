-- Minimum core schema shared by supported pre-capacity Full runtimes.
CREATE TABLE full_quota (account TEXT, window TEXT, requests INTEGER NOT NULL, bytes INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0, PRIMARY KEY(account,window));
CREATE TABLE full_work (key TEXT PRIMARY KEY, provider TEXT NOT NULL, plan_id TEXT NOT NULL, member_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0, body BLOB, body_sha256 TEXT, receipt TEXT, reason TEXT, next_at REAL NOT NULL DEFAULT 0);
CREATE TABLE full_plan (plan_id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL, trade_date TEXT NOT NULL, body BLOB NOT NULL, complete INTEGER NOT NULL DEFAULT 0, late INTEGER NOT NULL DEFAULT 0);
CREATE TABLE full_cursor (provider TEXT, dataset TEXT, trade_date TEXT NOT NULL, PRIMARY KEY(provider,dataset));
INSERT INTO full_work (key,provider,plan_id,member_key,body) VALUES ('fp1:prepared','finlab','plan1','member1',X'7072657061726564');
INSERT INTO full_quota VALUES ('real-account','2026-10-01',10,250,0);
INSERT INTO full_cursor VALUES ('finlab','tw_equity_eod','2026-10-01');
