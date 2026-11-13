"""HTTP application. All state changes run in SQLite write transactions."""
import argparse
from contextlib import closing
from functools import wraps
from getpass import getpass
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
from tempfile import TemporaryDirectory
import time

from flask import Flask, abort, g, jsonify, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

import roster
import scheduling as sched
import tournament as t

ROOT = Path(__file__).parent


def create_app(db_path, secret, secure=False):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=secure, MAX_CONTENT_LENGTH=4_000_000)
    t.initialize(db_path)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store" if not request.path.startswith("/static/") else "public, max-age=300"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    @app.errorhandler(t.RuleError)
    def rejected(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(400)
    def bad_request(_):
        return jsonify(error="请求格式无效"), 400

    @app.errorhandler(403)
    def forbidden(_):
        return jsonify(error="权限不足或会话已过期"), 403

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="上传文件超过4 MB"), 413

    def api(write=False, admin=False):
        def decorate(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                if write:
                    csrf = request.headers.get("X-CSRF-Token", "")
                    if not session.get("csrf") or not hmac.compare_digest(csrf, session["csrf"]):
                        abort(403)
                if admin and not session.get("admin"):
                    abort(403)
                try:
                    with t.transaction(db_path, write) as db:
                        g.db = db
                        g.actor = session.get("admin", "")
                        if g.actor:
                            account = t.one(db, "SELECT password_hash FROM users WHERE username=?", (g.actor,))
                            if not account or not hmac.compare_digest(session.get("admin_key", ""),
                                                                     t.digest(account["password_hash"])):
                                session.pop("admin", None)
                                session.pop("admin_key", None)
                                g.actor = ""
                                if admin:
                                    abort(403)
                        return fn(*args, **kwargs)
                except t.RuleError:
                    raise
                except (ValueError, KeyError, TypeError, AttributeError):
                    abort(400)
                except sqlite3.IntegrityError:
                    raise t.RuleError("记录重复或数据关系无效")
            return wrapped
        return decorate

    def payload():
        data = request.get_json()
        if not isinstance(data, dict):
            abort(400)
        return data

    @app.get("/")
    @app.get("/r/<room>")
    def index(room=None):
        return render_template("index.html")

    @app.get("/api/session")
    @api()
    def whoami():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        rep = session.get("rep")
        if rep and rep.get("college") and not t.one(g.db, """SELECT 1 FROM participants p JOIN teams t ON t.room=p.room AND t.id=p.team
                WHERE p.room=? AND t.college=? AND p.name=?""", (rep["room"], rep["college"], rep["name"])):
            session.pop("rep", None)
        return jsonify(csrf=session["csrf"], admin=session.get("admin"),
                       rep=session.get("rep"), referee=session.get("referee"))

    @app.post("/api/login")
    @api(write=True)
    def login():
        data = payload()
        name = data["username"].strip()
        user = t.one(g.db, "SELECT * FROM users WHERE username=?", (name,))
        # Bound repeated guessing globally per configured operator name and client.
        key = (request.remote_addr, name)
        tick = time.monotonic()
        attempts = app.extensions.setdefault("login_attempts", {})
        if len(attempts) > 2048:
            attempts.clear()
        recent = [x for x in attempts.get(key, []) if tick - x < 300]
        t.need(len(recent) < 8, "尝试次数过多，请5分钟后再试")
        if not user or not check_password_hash(user["password_hash"], data["password"]):
            attempts[key] = recent + [tick]
            raise t.RuleError("账号或密码错误")
        attempts.pop(key, None)
        session["admin"] = name
        session["admin_key"] = t.digest(user["password_hash"])
        return jsonify(ok=True)

    @app.post("/api/logout")
    @api(write=True)
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/rooms")
    @api()
    def rooms():
        return jsonify(t.rows(g.db, "SELECT id,title,created FROM rooms ORDER BY created DESC,id"))

    @app.post("/api/rooms")
    @api(write=True, admin=True)
    def new_room():
        file = request.files.get("workbook")
        t.need(file and file.filename.lower().endswith(".xlsx"), "请选择报名工作簿 .xlsx")
        try:
            data = roster.parse_workbook(file.read())
        except Exception as error:
            if isinstance(error, ValueError):
                raise t.RuleError(str(error)) from error
            raise t.RuleError("无法读取报名工作簿") from error
        hard_errors = [err for err in data["errors"] if not err.endswith("待核验一男一女")]
        t.need(not hard_errors, "；".join(hard_errors))
        title = request.form.get("title", "").strip() or t.ROOM_TITLE
        t.need(len(title) <= 120, "房间标题过长")
        return jsonify(room=t.create_room(g.db, data, g.actor, title))

    @app.post("/api/join")
    @api(write=True)
    def join():
        data = payload()
        room = t.one(g.db, "SELECT id FROM rooms WHERE join_code=?", (data["room_code"].strip().upper(),))
        t.need(room, "邀请码无效")
        return jsonify(room=room["id"])

    @app.get("/api/r/<room>")
    @api()
    def snapshot(room):
        return jsonify(public_state(g.db, room))

    @app.get("/api/r/<room>/admin")
    @api(admin=True)
    def admin_state(room):
        r = t.get_room(g.db, room)
        teams = t.rows(g.db, "SELECT id,project,college,players,verified FROM teams WHERE room=? ORDER BY id", (room,))
        for team in teams:
            team["players"] = t.loads(team["players"])
        matches = t.rows(g.db, "SELECT code,pending_score FROM matches WHERE room=? AND pending_score IS NOT NULL", (room,))
        return jsonify(settings=r["settings"], join_code=r["join_code"], source=t.loads(r["roster_source"]),
                       teams=teams, draft=sched.assignments(g.db, room, "draft"),
                       officials=t.rows(g.db, "SELECT id,name,invite_code FROM officials WHERE room=? ORDER BY name", (room,)),
                       participants=t.rows(g.db, "SELECT team,name,created FROM participants WHERE room=?", (room,)),
                       pending={m["code"]: t.loads(m["pending_score"]) for m in matches})

    @app.get("/api/r/<room>/audit")
    @api(admin=True)
    def audit(room):
        result = t.rows(g.db, "SELECT * FROM audit WHERE room=? ORDER BY id DESC", (room,))
        for item in result:
            item["detail"] = t.loads(item["detail"])
        return jsonify(result)

    @app.post("/api/r/<room>/represent")
    @api(write=True)
    def represent(room):
        data = payload()
        college, name = data["college"].strip(), data["name"].strip()
        t.need(0 < len(name) <= 50, "请填写联系人姓名")
        teams = t.rows(g.db, "SELECT id FROM teams WHERE room=? AND college=? ORDER BY id", (room, college))
        t.need(teams, "请选择报名名单中的学院")
        existing = t.rows(g.db, """SELECT DISTINCT p.name FROM participants p JOIN teams t
                               ON t.room=p.room AND t.id=p.team WHERE p.room=? AND t.college=?""", (room, college))
        t.need(all(p["name"] == name for p in existing), "该学院已有登记，请使用原登记姓名或请主持人核对")
        for team in teams:
            g.db.execute("INSERT OR IGNORE INTO participants VALUES(?,?,?,?)", (room, team["id"], name, t.now()))
        if not existing:
            t.log(g.db, room, name, "登记学院代表", {"college": college, "teams": [team["id"] for team in teams]})
        session["rep"] = {"room": room, "college": college, "name": name}
        return jsonify(ok=True)

    @app.post("/api/r/<room>/draw")
    @api(write=True)
    def draw(room):
        rep = session.get("rep", {})
        if rep.get("room") != room or not rep.get("college"):
            abort(403)
        data = payload()
        participant = t.one(g.db, """SELECT 1 FROM participants p JOIN teams t ON t.room=p.room AND t.id=p.team
                            WHERE p.room=? AND t.college=? AND p.name=?""", (room, rep["college"], rep["name"]))
        if not participant:
            abort(403)
        return jsonify(position=t.take_college_position(g.db, room, rep["college"], rep["name"], data.get("revision")))

    @app.post("/api/r/<room>/admin/represent")
    @api(write=True, admin=True)
    def reset_rep(room):
        data = payload()
        college, reason = data["college"].strip(), data["reason"].strip()
        t.need(reason and len(reason) <= 300, "请填写登记重置原因")
        before = t.rows(g.db, """SELECT p.team,p.name,p.created FROM participants p JOIN teams t
                          ON t.room=p.room AND t.id=p.team WHERE p.room=? AND t.college=?""", (room, college))
        t.need(before, "该学院暂无登记")
        g.db.executemany("DELETE FROM participants WHERE room=? AND team=?", [(room,p["team"]) for p in before])
        t.log(g.db, room, g.actor, "重置联系人登记", {"college": college, "reason": reason, "before": before})
        return jsonify(ok=True)

    @app.post("/api/r/<room>/admin/draw")
    @api(write=True, admin=True)
    def host_draw(room):
        data = payload()
        t.host_action(g.db, room, data["action"], g.actor, data)
        return jsonify(ok=True)

    @app.post("/api/r/<room>/admin/roster")
    @api(write=True, admin=True)
    def edit_roster(room):
        data = payload()
        team = t.one(g.db, "SELECT * FROM teams WHERE room=? AND id=?", (room, data["team"]))
        t.need(team, "队伍不存在")
        t.need(t.project_state(g.db, room, team["project"])["state"] == "waiting", "请在抽签开放前核验名单")
        reason = data["reason"].strip()
        t.need(reason, "请填写更正或核验依据")
        before = t.loads(team["players"])
        corrections = {item["row"]: item for item in data["players"]}
        t.need(set(corrections) == {p["row"] for p in before}, "须核验原表全部队员行")
        after = []
        for old in before:
            change = corrections[old["row"]]
            name, role, sex = change["name"].strip(), change["role"], change.get("sex", "")
            t.need(0 < len(name) <= 60 and sex in ("", "男", "女"), "姓名或性别无效")
            after.append({**old, "name": name, "role": role, "sex": sex,
                          "sex_source": "管理员核验" if sex else ""})
        roles = [p["role"] for p in after]
        if team["project"] == "X":
            t.need(roles == [roster.MIXED, roster.MIXED] and {p["sex"] for p in after} == {"男", "女"},
                   "混双需确认两名选手及一男一女")
        else:
            t.need(roles.count(roster.FORMAL) == 3 and
                   roles.count(roster.RESERVE) == len(roles) - 3, "需要三名正式队员，其余为替补")
        t.need(len({p["name"] for p in after}) == len(after), "队内选手姓名不能重复")
        g.db.execute("UPDATE teams SET players=?,verified=1 WHERE room=? AND id=?", (t.dumps(after), room, team["id"]))
        t.log(g.db, room, g.actor, "名单核验或更正", {"team": team["id"], "reason": reason, "before": before, "after": after})
        return jsonify(ok=True)

    @app.post("/api/r/<room>/admin/settings")
    @api(write=True, admin=True)
    def settings(room):
        before = t.get_room(g.db, room)["settings"]
        data = payload()
        after = sched.config(data, before)
        if before["tie"] != after["tie"]:
            t.need(data.get("confirm_tie") is True and data.get("tie_basis", "").strip(), "请确认同分规则并填写依据")
        for project in t.PROJECTS:
            if before["formats"][project] != after["formats"][project]:
                t.need(not t.one(g.db, """SELECT 1 FROM matches WHERE room=? AND project=?
                                        AND (status!='pending' OR pending_score IS NOT NULL)""", (room, project)),
                       "已有赛况的项目不能改计分格式")
        g.db.execute("UPDATE rooms SET settings=? WHERE id=?", (t.dumps(after), room))
        if any(before[k] != after[k] for k in ("points", "tie")):
            g.db.execute("DELETE FROM tie_decisions WHERE room=?", (room,))
        for project in t.PROJECTS:
            t.refresh_rankings(g.db, room, project, after)
        t.log(g.db, room, g.actor, "更新赛事设置", {"before": before, "after": after,
                                              "tie_basis": data.get("tie_basis", "")})
        return jsonify(ok=True)

    @app.post("/api/r/<room>/admin/tie-decision")
    @api(write=True, admin=True)
    def tie_decision(room):
        data = payload()
        project, order, basis = data["project"], data["order"], data["basis"].strip()
        cfg = t.get_room(g.db, room)["settings"]
        existing = t.one(g.db, "SELECT ranking,basis FROM tie_decisions WHERE room=? AND project=?", (room, project))
        g.db.execute("DELETE FROM tie_decisions WHERE room=? AND project=?", (room, project))
        results, complete = t.standings(g.db, room, project, cfg)
        t.need(complete and any(r["rank"] is None for r in results), "无待裁定的同积分名次")
        allowed = {r["team"]: r["tie_group"] for r in results}
        t.need(isinstance(order, list) and len(order) == 4 and set(order) == set(allowed)
               and [allowed[x] for x in order] == sorted(allowed.values()),
               "只可调整积分及已确认规则仍并列的队伍")
        t.need(data.get("confirmed") is True and 0 < len(basis) <= 1000, "请明确确认并填写裁判组裁定依据")
        g.db.execute("INSERT INTO tie_decisions VALUES(?,?,?,?)", (room, project, t.dumps(order), basis))
        t.refresh_rankings(g.db, room, project, cfg)
        t.log(g.db, room, g.actor, "同分裁决", {"project": project, "before": existing, "order": order, "basis": basis})
        return jsonify(ok=True)

    @app.post("/api/r/<room>/admin/officials")
    @api(write=True, admin=True)
    def add_officials(room):
        data = payload()
        names = list(dict.fromkeys(n.strip() for n in data["names"] if n.strip()))
        t.need(1 <= len(names) <= 100 and all(len(x) <= 50 for x in names), "裁判名单无效")
        for name in names:
            if not t.one(g.db, "SELECT 1 FROM officials WHERE room=? AND name=?", (room, name)):
                invite = t.code()
                g.db.execute("INSERT INTO officials VALUES(?,?,?,?,?)",
                             (room, secrets.token_hex(5), name, t.digest(invite), invite))
        t.log(g.db, room, g.actor, "添加裁判", {"names": names})
        return jsonify(ok=True)

    @app.post("/api/r/<room>/referee")
    @api(write=True)
    def referee_login(room):
        official = t.one(g.db, "SELECT id,name FROM officials WHERE room=? AND invite_hash=?",
                         (room, t.digest(payload()["invite"].strip())))
        t.need(official, "裁判凭证无效")
        session["referee"] = {**official, "room": room}
        return jsonify(ok=True)

    @app.post("/api/r/<room>/admin/schedule")
    @api(write=True, admin=True)
    def schedule(room):
        data = payload()
        r = t.get_room(g.db, room)
        cfg = r["settings"]
        before = sched.assignments(g.db, room, "draft")
        action = data["action"]
        if action == "build":
            sched.build_schedule(g.db, room, cfg)
        elif action == "edit":
            sched.edit_draft(g.db, room, cfg, data["items"])
        elif action == "publish":
            t.need(data.get("confirmed") is True, "请明确确认发布")
            sched.publish_schedule(g.db, room, cfg)
            cfg["public_venue"] = cfg["venue"]
            g.db.execute("UPDATE rooms SET settings=? WHERE id=?", (t.dumps(cfg), room))
        else:
            abort(400)
        t.log(g.db, room, g.actor, "比赛日程", {"action": action, "before": before,
                                               "after": sched.assignments(g.db, room, "draft")})
        return jsonify(ok=True)

    @app.post("/api/r/<room>/match")
    @api(write=True)
    def match(room):
        data = payload()
        match = t.one(g.db, "SELECT * FROM matches WHERE room=? AND code=?", (room, data["code"]))
        t.need(match, "比赛不存在")
        assigned = t.one(g.db, """SELECT o.* FROM schedule s JOIN officials o ON o.room=s.room AND o.id=s.referee
                               WHERE s.room=? AND s.code=? AND s.kind='public'""", (room, match["code"]))
        ref = session.get("referee", {})
        if not g.actor and (not assigned or ref.get("room") != room or ref.get("id") != assigned["id"]):
            abort(403)
        actor = g.actor or assigned["name"]
        cfg = t.get_room(g.db, room)["settings"]
        action = data["action"]
        if action == "start":
            t.need(match["status"] == "pending", "比赛已开始或结束")
            sched.can_start(g.db, room, match, cfg)
            g.db.execute("UPDATE matches SET status='playing',started=? WHERE room=? AND code=?",
                         (t.now(), room, match["code"]))
        elif action in ("submit", "confirm"):
            t.need(match["status"] in ("playing", "done"), "请先登记开赛")
            if not g.actor and match["status"] == "done":
                abort(403)
            if action == "confirm":
                if not g.actor:
                    abort(403)
                score = t.loads(match["pending_score"])
                t.need(score, "没有待确认成绩")
            else:
                score = t.score_payload(data, match, cfg["formats"][match["project"]])
                score["referee"] = assigned["name"] if assigned else data.get("referee", "").strip()
            if action == "submit" and not (g.actor and data.get("confirmed") is True):
                t.need(match["status"] == "playing", "更正成绩须由管理员确认")
                g.db.execute("UPDATE matches SET pending_score=? WHERE room=? AND code=?",
                             (t.dumps(score), room, match["code"]))
            else:
                reason = data.get("reason", "").strip()
                t.need(match["status"] != "done" or reason, "请填写成绩更正原因")
                g.db.execute("""UPDATE matches SET status='done',winner=?,score=?,pending_score=NULL,finished=?
                              WHERE room=? AND code=?""",
                             (score["winner"], t.dumps(score), match["finished"] or t.now(), room, match["code"]))
                t.refresh_matches(g.db, room, match["project"])
                if match["winner"] and match["winner"] != score["winner"] and match["stage"] != "round":
                    context = sched.match_context(g.db, room)
                    for kind in ("draft", "public"):
                        sched.check(sched.assignments(g.db, room, kind), context, cfg["rest"])
                if match["stage"] == "round" and t.loads(match["score"]) != score:
                    g.db.execute("DELETE FROM tie_decisions WHERE room=? AND project=?", (room, match["project"]))
                t.refresh_rankings(g.db, room, match["project"], cfg)
        else:
            abort(400)
        after = t.one(g.db, "SELECT * FROM matches WHERE room=? AND code=?", (room, match["code"]))
        t.log(g.db, room, actor, "比赛成绩或状态", {"action": action, "reason": data.get("reason", ""),
                                               "before": match, "after": after})
        return jsonify(ok=True)

    @app.get("/api/r/<room>/export/<kind>/<filetype>")
    @api()
    def export(room, kind, filetype):
        from exports import make_export
        data, filename, media = make_export(public_state(g.db, room), kind, filetype)
        return send_file(data, mimetype=media, as_attachment=True, download_name=filename)

    @app.get("/api/backup")
    @api(admin=True)
    def backup():
        from io import BytesIO
        with TemporaryDirectory(dir=Path(db_path).parent) as tmp:
            path = Path(tmp) / "backup.sqlite3"
            make_backup(db_path, path)
            return send_file(BytesIO(path.read_bytes()), as_attachment=True, download_name="qingyou-backup.sqlite3",
                             mimetype="application/vnd.sqlite3")

    return app


def public_state(db, room):
    r = t.get_room(db, room)
    officials = {x["id"]: x["name"] for x in t.rows(db, "SELECT id,name FROM officials WHERE room=?", (room,))}
    teams = {x["id"]: x for x in t.rows(db, "SELECT id,college,project FROM teams WHERE room=? ORDER BY id", (room,))}
    matches = t.rows(db, """SELECT code,project,stage,a_source,b_source,a,b,winner,status,score
                            FROM matches WHERE room=? ORDER BY code""", (room,))
    assignments = {x["code"]: x for x in sched.assignments(db, room, "public")}
    for m in matches:
        m["score"] = t.loads(m["score"])
        if m["score"]:
            m["score"].pop("referee", None)
        m["next"] = [n["code"] + ("甲" if n["a_source"] == m["code"] else "乙") for n in matches
                     if m["code"] in (n["a_source"], n["b_source"])]
        m["schedule"] = assignments.get(m["code"])
        if m["schedule"]:
            m["schedule"]["official"] = officials.get(m["schedule"]["referee"])
    projects = {}
    for p in t.PROJECTS:
        item = t.project_state(db, room, p)
        draws = t.get_draws(db, item)
        standings, complete = t.standings(db, room, p, r["settings"])
        projects[p] = {"state": item["state"], "revision": item["revision"], "draws": draws, "byes": item["byes"],
                       "order": item["draw_order"], "standings": standings, "complete": complete,
                       "ranking": t.rows(db, "SELECT team,rank FROM rankings WHERE room=? AND project=? ORDER BY rank", (room, p))}
    last = t.one(db, "SELECT max(id) AS n FROM audit WHERE room=?", (room,))["n"]
    dates = sorted({x["start"][:10] for x in assignments.values()}) or ["2026-11-14", "2026-11-15"]
    return {"room": room, "title": r["title"], "dates": dates, "venue": r["settings"]["public_venue"] or t.DEFAULTS["venue"],
            "version": last, "teams": teams, "projects": projects, "matches": matches,
            "college_draw": t.unified_draw(db, room)}


def make_backup(source, target):
    target = Path(target)
    if target.exists():
        raise ValueError("目标文件已存在，请使用新路径")
    with closing(t.connect(source)) as db, closing(sqlite3.connect(target)) as out:
        db.backup(out)
        check_database(out)


def check_database(db):
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite 完整性检查失败")
    if db.execute("PRAGMA user_version").fetchone()[0] != 1 or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("数据库结构版本不兼容或存在外键错误")
    expected = {"users", "rooms", "teams", "draws", "matches", "schedule", "rankings", "audit"}
    if not expected <= {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        raise ValueError("不是本系统数据库")


def restore(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file() or target.exists() or any(Path(str(target) + ext).exists() for ext in ("-wal", "-shm")):
        raise ValueError("源备份须存在，恢复目标须为新文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        check_database(db)
        with closing(sqlite3.connect(target)) as out:
            db.backup(out)
            check_database(out)


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--db", type=Path, default=ROOT / "runtime" / "event.sqlite3")
    sub = cli.add_subparsers(dest="command", required=True)
    user = sub.add_parser("admin")
    user.add_argument("username")
    imp = sub.add_parser("import")
    imp.add_argument("workbook", type=Path)
    b = sub.add_parser("backup")
    b.add_argument("target", type=Path)
    r = sub.add_parser("restore")
    r.add_argument("source", type=Path)
    r.add_argument("target", type=Path)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--https", action="store_true", help="HTTPS反代，启用安全Cookie")
    args = cli.parse_args()
    if args.command == "restore":
        restore(args.source, args.target)
        print(args.target)
        return
    if args.command == "backup":
        if not args.db.is_file():
            cli.error("数据库不存在")
        make_backup(args.db, args.target)
        print(args.target)
        return
    t.initialize(args.db)
    if args.command == "admin":
        password = getpass("管理员密码（至少12字符）: ")
        if len(password) < 12 or password != getpass("再次输入: "):
            cli.error("密码过短或两次输入不一致")
        with t.transaction(args.db, True) as db:
            db.execute("""INSERT INTO users(username,password_hash) VALUES(?,?)
                       ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash""",
                       (args.username, generate_password_hash(password)))
    elif args.command == "import":
        data = roster.parse_workbook(args.workbook.read_bytes())
        t.need(not [e for e in data["errors"] if not e.endswith("待核验一男一女")], "源表包含结构错误")
        with t.transaction(args.db, True) as db:
            room = t.create_room(db, data, "本地导入")
        print(f"房间入口 /r/{room}")
    else:
        from waitress import serve as run
        secret = os.getenv("QINGYOU_SECRET")
        if not secret or len(secret) < 32:
            cli.error("请设置至少32字符的 QINGYOU_SECRET")
        run(create_app(args.db, secret, args.https), host=args.host, port=args.port, threads=12,
            max_request_body_size=4_000_000, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
