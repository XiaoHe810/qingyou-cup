PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, join_code TEXT NOT NULL UNIQUE,
  created TEXT NOT NULL, roster_source TEXT NOT NULL, settings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  room TEXT NOT NULL REFERENCES rooms(id), code TEXT NOT NULL CHECK(code IN ('M','W','X')),
  state TEXT NOT NULL DEFAULT 'waiting' CHECK(state IN ('waiting','open','paused','locked')),
  revision INTEGER NOT NULL DEFAULT 1, draw_order TEXT NOT NULL, byes TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(room,code)
);
CREATE TABLE IF NOT EXISTS teams (
  room TEXT NOT NULL, id TEXT NOT NULL, project TEXT NOT NULL, college TEXT NOT NULL,
  players TEXT NOT NULL, verified INTEGER NOT NULL, invite_hash TEXT, invite_code TEXT,
  PRIMARY KEY(room,id), UNIQUE(room,project,college),
  FOREIGN KEY(room,project) REFERENCES projects(room,code)
);
CREATE TABLE IF NOT EXISTS participants (
  room TEXT NOT NULL, team TEXT NOT NULL, name TEXT NOT NULL, created TEXT NOT NULL,
  PRIMARY KEY(room,team), FOREIGN KEY(room,team) REFERENCES teams(room,id)
);
CREATE TABLE IF NOT EXISTS draws (
  room TEXT NOT NULL, project TEXT NOT NULL, revision INTEGER NOT NULL,
  team TEXT NOT NULL, slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 16),
  seq INTEGER NOT NULL, drawn_at TEXT NOT NULL, actor TEXT NOT NULL,
  PRIMARY KEY(room,project,revision,team), UNIQUE(room,project,revision,slot),
  UNIQUE(room,project,revision,seq), FOREIGN KEY(room,team) REFERENCES teams(room,id),
  FOREIGN KEY(room,project) REFERENCES projects(room,code)
);
CREATE TABLE IF NOT EXISTS matches (
  room TEXT NOT NULL, code TEXT NOT NULL, project TEXT NOT NULL, stage TEXT NOT NULL,
  a_source TEXT NOT NULL, b_source TEXT NOT NULL, a TEXT, b TEXT, winner TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','playing','done')),
  score TEXT, pending_score TEXT, started TEXT, finished TEXT,
  PRIMARY KEY(room,code), FOREIGN KEY(room,project) REFERENCES projects(room,code)
);
CREATE TABLE IF NOT EXISTS officials (
  room TEXT NOT NULL REFERENCES rooms(id), id TEXT NOT NULL, name TEXT NOT NULL,
  invite_hash TEXT NOT NULL, invite_code TEXT NOT NULL, PRIMARY KEY(room,id), UNIQUE(room,name)
);
CREATE TABLE IF NOT EXISTS schedule (
  room TEXT NOT NULL, code TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('draft','public')),
  start TEXT NOT NULL, end TEXT NOT NULL, court INTEGER NOT NULL CHECK(court > 0), referee TEXT,
  PRIMARY KEY(room,code,kind), FOREIGN KEY(room,code) REFERENCES matches(room,code) ON DELETE CASCADE,
  FOREIGN KEY(room,referee) REFERENCES officials(room,id)
);
CREATE TABLE IF NOT EXISTS rankings (
  room TEXT NOT NULL, project TEXT NOT NULL, team TEXT NOT NULL, rank INTEGER NOT NULL,
  PRIMARY KEY(room,project,team), UNIQUE(room,project,rank),
  FOREIGN KEY(room,team) REFERENCES teams(room,id), FOREIGN KEY(room,project) REFERENCES projects(room,code)
);
CREATE TABLE IF NOT EXISTS tie_decisions (
  room TEXT NOT NULL, project TEXT NOT NULL, ranking TEXT NOT NULL, basis TEXT NOT NULL,
  PRIMARY KEY(room,project), FOREIGN KEY(room,project) REFERENCES projects(room,code)
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL REFERENCES rooms(id),
  at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_room ON audit(room,id);
PRAGMA user_version = 1;
