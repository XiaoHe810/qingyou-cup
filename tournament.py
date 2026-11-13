"""Tournament state, persistence and match rules."""
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3

from roster import EXPECTED, PROJECTS

TZ = timezone(timedelta(hours=8))
STAGES = {"pre": "附加淘汰赛", "quarter": "八强淘汰赛", "round": "四强循环赛"}
STATUSES = {"pending": "待比赛", "playing": "进行中", "done": "已结束"}
ROOM_TITLE = "东北石油大学第四届“青油杯”研究生乒乓球比赛"
SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULTS = {
    "venue": "东北石油大学乒乓球馆（体育场旁）", "courts": 4, "minutes": 35, "rest": 5,
    "sessions": [{"date": date, "start": a, "end": b} for date in ("2026-11-14", "2026-11-15")
                 for a, b in (("08:00", "12:00"), ("13:30", "18:00"))],
    "points": {"win": 2, "loss": 1, "forfeit": 0}, "tie": [],
    "formats": {"M": {"rubbers": 5, "games": 3}, "W": {"rubbers": 5, "games": 3},
                "X": {"rubbers": 1, "games": 5}},
    "public_venue": None, "final_day": True,
}
RNG = secrets.SystemRandom()


class RuleError(ValueError):
    pass


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value):
    return json.loads(value) if value is not None else None


def code():
    return secrets.token_urlsafe(18)


def digest(value):
    from hashlib import sha256
    return sha256(value.encode()).hexdigest()


def need(condition, message):
    if not condition:
        raise RuleError(message)


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA.read_text(encoding="utf-8-sig"))


@contextmanager
def transaction(path, write=False):
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def rows(db, sql, params=()):
    return [dict(row) for row in db.execute(sql, params)]


def one(db, sql, params=()):
    row = db.execute(sql, params).fetchone()
    return dict(row) if row else None


def log(db, room, actor, action, detail):
    db.execute("INSERT INTO audit(room,at,actor,action,detail) VALUES(?,?,?,?,?)",
               (room, now(), actor, action, dumps(detail)))


def create_room(db, source, actor, title=ROOM_TITLE):
    room = secrets.token_hex(6)
    db.execute("INSERT INTO rooms VALUES(?,?,?,?,?,?)",
               (room, title, secrets.token_hex(4).upper(), now(),
                dumps({k: v for k, v in source.items() if k != "teams"}), dumps(DEFAULTS)))
    for project in PROJECTS:
        teams = [t for t in source["teams"] if t["project"] == project]
        db.execute("INSERT INTO projects(room,code,draw_order) VALUES(?,?,?)",
                   (room, project, dumps([t["id"] for t in teams])))
        for t in teams:
            verified = project != "X" or {p["sex"] for p in t["players"]} == {"男", "女"}
            db.execute("INSERT INTO teams VALUES(?,?,?,?,?,?,?,?)",
                       (room, t["id"], project, t["college"], dumps(t["players"]), int(verified), None, None))
    log(db, room, actor, "创建房间并导入", {"sha256": source["sha256"], "issues": source["errors"]})
    return room


def get_room(db, room):
    data = one(db, "SELECT * FROM rooms WHERE id=?", (room,))
    need(data, "找不到抽签房间")
    data["settings"] = loads(data["settings"])
    return data


def project_state(db, room, project):
    p = one(db, "SELECT * FROM projects WHERE room=? AND code=?", (room, project))
    need(p, "未知项目")
    p["draw_order"], p["byes"] = loads(p["draw_order"]), loads(p["byes"])
    return p


def get_draws(db, p):
    return rows(db, """SELECT d.team,d.slot,d.seq,d.drawn_at,t.college FROM draws d
                JOIN teams t ON t.room=d.room AND t.id=d.team
                WHERE d.room=? AND d.project=? AND d.revision=? ORDER BY d.seq""",
                (p["room"], p["code"], p["revision"]))


def college_order(db, room):
    return [r["college"] for r in rows(db, "SELECT college FROM teams WHERE room=? GROUP BY college ORDER BY min(id)", (room,))]


def college_draws(db, room, revision):
    return rows(db, """SELECT college,position,seq,drawn_at FROM college_draws
                       WHERE room=? AND revision=? ORDER BY position""", (room, revision))


def unified_draw(db, room):
    projects = [project_state(db, room, p) for p in PROJECTS]
    lead = projects[0]
    history = college_draws(db, room, lead["revision"])
    # Previously generated project draws remain readable and can be archived via reset.
    legacy = any(p["revision"] != lead["revision"] or p["state"] != lead["state"] or
                 (not history and get_draws(db, p)) for p in projects)
    settings = get_room(db, room)["settings"]
    return {"state": lead["state"], "revision": lead["revision"], "legacy": legacy,
            "draws": history, "order": settings.get("college_order") or college_order(db, room),
            "ordered": bool(settings.get("college_order")), "preview": bool(settings.get("college_preview"))}


def reset_draw(db, room, actor, reason, confirm):
    need(confirm == "恢复初始状态" and reason.strip(), "请输入原因，并输入「恢复初始状态」")
    settings = get_room(db, room)["settings"]
    need(settings.get("college_preview") or
         not one(db, "SELECT 1 FROM matches WHERE room=? AND (status!='pending' OR pending_score IS NOT NULL)", (room,)),
         "正式比赛已有开赛或成绩记录，无法重置抽签")
    projects = [project_state(db, room, p) for p in PROJECTS]
    log(db, room, actor, "统一抽签重置归档",
        {"reason": reason.strip(), "preview": bool(settings.get("college_preview")),
         "projects": [{**p, "draws": get_draws(db, p)} for p in projects],
         "college_draws": college_draws(db, room, projects[0]["revision"]),
         "matches": rows(db, "SELECT * FROM matches WHERE room=?", (room,)),
         "schedule": rows(db, "SELECT * FROM schedule WHERE room=?", (room,)),
         "participants": rows(db, "SELECT * FROM participants WHERE room=?", (room,))})
    for table in ("matches", "participants", "rankings", "tie_decisions"):
        db.execute(f"DELETE FROM {table} WHERE room=?", (room,))
    revision = max(p["revision"] for p in projects) + 1
    for project in PROJECTS:
        order = [r["id"] for r in rows(db, "SELECT id FROM teams WHERE room=? AND project=? ORDER BY id", (room, project))]
        db.execute("UPDATE projects SET state='waiting',revision=?,byes='[]',draw_order=? WHERE room=? AND code=?",
                   (revision, dumps(order), room, project))
    for key in ("college_order", "college_preview", "ordered_projects"):
        settings.pop(key, None)
    settings["public_venue"] = None
    db.execute("UPDATE rooms SET settings=? WHERE id=?", (dumps(settings), room))


def host_action(db, room, action, actor, data):
    if action == "reset":
        reset_draw(db, room, actor, data.get("reason", ""), data.get("confirm", ""))
        return
    state = unified_draw(db, room)
    need(not state["legacy"], "旧版签位请先归档并恢复初始状态")
    status = state["state"]
    settings = get_room(db, room)["settings"]
    if action == "order":
        order = data["order"]
        need(status == "waiting", "仅可在开放前设置学院操作顺序")
        need(isinstance(order, list) and all(isinstance(x, str) for x in order) and
             sorted(order) == sorted(state["order"]), "顺序须包含全部学院，各出现一次")
        settings["college_order"] = order if data.get("ordered", True) else []
        db.execute("UPDATE rooms SET settings=? WHERE id=?", (dumps(settings), room))
    elif action in ("open", "preview"):
        need(status in (("waiting",) if action == "preview" else ("waiting", "paused")), "当前无法开放抽签")
        counts = {r["project"]: r["n"] for r in rows(db, "SELECT project,count(*) AS n FROM teams WHERE room=? GROUP BY project", (room,))}
        need(counts == EXPECTED, "报名队数与赛制不符")
        db.execute("UPDATE projects SET state='open' WHERE room=?", (room,))
        if action == "preview":
            settings["college_preview"] = True
            db.execute("UPDATE rooms SET settings=? WHERE id=?", (dumps(settings), room))
            for college in state["order"]:
                take_college_position(db, room, college, actor, state["revision"])
    elif action == "pause":
        need(status == "open", "当前无法暂停")
        db.execute("UPDATE projects SET state='paused' WHERE room=?", (room,))
    else:
        raise RuleError("未知抽签操作")
    log(db, room, actor, "统一抽签操作", {"action": action, "revision": state["revision"],
                                       "order": settings.get("college_order", [])})


def take_college_position(db, room, college, actor, revision):
    state = unified_draw(db, room)
    need(revision == state["revision"], "抽签轮次已更新，请刷新")
    need(not state["legacy"], "旧版签位请先归档并恢复初始状态")
    need(college in state["order"], "该学院没有报名")
    previous = state["draws"]
    need(not any(d["college"] == college for d in previous), "该学院本轮已完成抽签")
    need(state["state"] == "open", "主持人尚未开放抽签或已暂停")
    if state["ordered"]:
        need(state["order"][len(previous)] == college, "请等待本学院抽签顺序")
    position = RNG.choice(sorted(set(range(1, len(state["order"]) + 1)) - {d["position"] for d in previous}))
    at = now()
    db.execute("INSERT INTO college_draws VALUES(?,?,?,?,?,?,?)",
               (room, revision, college, position, len(previous) + 1, at, actor))
    log(db, room, actor, "学院抽签", {"college": college, "position": position,
                                  "revision": revision, "seq": len(previous) + 1})
    if len(previous) + 1 == len(state["order"]):
        finalize_college_draw(db, room, revision)
    return position


def finalize_college_draw(db, room, revision):
    drawn = rows(db, "SELECT * FROM college_draws WHERE room=? AND revision=? ORDER BY position", (room, revision))
    byes_by_count = {}
    for project, count in EXPECTED.items():
        teams = {r["college"]: r["id"] for r in rows(db, "SELECT id,college FROM teams WHERE room=? AND project=?", (room, project))}
        eligible = [d for d in drawn if d["college"] in teams]
        need(len(eligible) == count, "项目报名不完整，无法生成签位")
        if count not in byes_by_count:
            byes_by_count[count] = sorted(2 * pair + RNG.choice((1, 2)) for pair in RNG.sample(range(8), 16 - count))
        byes = byes_by_count[count]
        slots = sorted(set(range(1, 17)) - set(byes))
        result = [{"slot": slot, "team": teams[d["college"]]} for slot, d in zip(slots, eligible)]
        # Use a compact per-project sequence preserving the relative order of the shared draw.
        for seq, (d, item) in enumerate(zip(eligible, result), 1):
            db.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?,?)",
                       (room, project, revision, item["team"], item["slot"], seq, d["drawn_at"], d["actor"]))
        create_matches(db, room, project, result, byes)
        db.execute("UPDATE projects SET state='locked',byes=? WHERE room=? AND code=?", (dumps(byes), room, project))
    log(db, room, "系统", "统一抽签锁定并生成三个项目对阵", {"revision": revision})


def create_matches(db, room, project, draws, byes):
    slots = {d["slot"]: d["team"] for d in draws}
    need(len(slots) == EXPECTED[project] and set(slots).isdisjoint(byes)
         and set(slots) | set(byes) == set(range(1, 17)), "签位不完整")
    current = []
    quarter_inputs = []

    def add(stage, a, b):
        identifier = f"{project}{len(current) + 1:02}"
        current.append((room, identifier, project, stage, a, b))
        return identifier

    for first in range(1, 17, 2):
        a, b = slots.get(first), slots.get(first + 1)
        need(a or b, "同一个首轮对位不能有两个空签")
        quarter_inputs.append(add("pre", a, b) if a and b else a or b)
    quarters = [add("quarter", quarter_inputs[i], quarter_inputs[i + 1]) for i in range(0, 8, 2)]
    # Three rounds of two disjoint pairings for a four-team round robin.
    for a, b in ((0, 3), (1, 2), (0, 2), (3, 1), (0, 1), (2, 3)):
        add("round", quarters[a], quarters[b])
    db.executemany("INSERT INTO matches(room,code,project,stage,a_source,b_source) VALUES(?,?,?,?,?,?)", current)
    refresh_matches(db, room, project)


def refresh_matches(db, room, project):
    found = rows(db, "SELECT * FROM matches WHERE room=? AND project=? ORDER BY code", (room, project))
    winners = {}
    for m in found:
        a, b = [src if src.startswith("T") else winners.get(src) for src in (m["a_source"], m["b_source"])]
        if (a, b) != (m["a"], m["b"]):
            need(m["status"] == "pending" and not m["pending_score"], f'{m["code"]} 已有赛况，无法更改对阵')
            db.execute("UPDATE matches SET a=?,b=? WHERE room=? AND code=?", (a, b, room, m["code"]))
        winners[m["code"]] = m["winner"]


def score_payload(payload, match, fmt):
    if payload.get("forfeit"):
        forfeiter = payload["forfeit"]
        need(forfeiter in ("a", "b"), "请选择弃权方")
        return {"forfeit": forfeiter, "winner": match["b" if forfeiter == "a" else "a"],
                "rubbers": [], "total": [0, 0], "games": [0, 0], "points": [0, 0]}
    rubbers = payload.get("rubbers")
    need(isinstance(rubbers, list) and 1 <= len(rubbers) <= fmt["rubbers"], "单项比分场数不符")
    total, games, points = [0, 0], [0, 0], [0, 0]
    for rubber in rubbers:
        need(max(total) < fmt["rubbers"] // 2 + 1, "已经分出胜负，不能继续填入单项比分")
        need(isinstance(rubber, list) and 1 <= len(rubber) <= fmt["games"], "单项局数不符")
        result = [0, 0]
        for game in rubber:
            need(max(result) < fmt["games"] // 2 + 1, "单项已分出胜负，不能继续填局")
            need(isinstance(game, list) and len(game) == 2 and
                 all(type(x) is int and 0 <= x <= 999 for x in game), "每局需要两个有效分数")
            high, low = max(game), min(game)
            need((high == 11 and low <= 9) or (low >= 10 and high - low == 2), "每局11分，10平后须净胜两分")
            side = int(game[1] > game[0])
            result[side] += 1
            games[side] += 1
            for i in (0, 1):
                points[i] += game[i]
        need(max(result) == fmt["games"] // 2 + 1, "单项尚未分出胜负")
        total[int(result[1] > result[0])] += 1
    need(max(total) == fmt["rubbers"] // 2 + 1, "比赛尚未分出胜负")
    return {"forfeit": "", "winner": match["b" if total[1] > total[0] else "a"],
            "rubbers": rubbers, "total": total, "games": games, "points": points}


def standings(db, room, project, settings):
    matches = rows(db, "SELECT * FROM matches WHERE room=? AND project=? AND stage='round' ORDER BY code",
                   (room, project))
    result = {team: {"team": team, "played": 0, "wins": 0, "losses": 0, "forfeits": 0, "points": 0,
                     "rubber_diff": 0, "game_diff": 0, "point_diff": 0, "rank": None}
              for m in matches for team in (m["a"], m["b"]) if team}
    for m in matches:
        if m["status"] != "done":
            continue
        score = loads(m["score"])
        for i, team in enumerate((m["a"], m["b"])):
            item = result[team]
            win = team == m["winner"]
            forfeited = score["forfeit"] == ("a", "b")[i]
            item["played"] += 1
            item["wins"] += win
            item["losses"] += not win
            item["forfeits"] += forfeited
            item["points"] += settings["points"]["forfeit" if forfeited else "win" if win else "loss"]
            for k, key in (("rubber_diff", "total"), ("game_diff", "games"), ("point_diff", "points")):
                item[k] += score[key][i] - score[key][1 - i]
    ordered = []
    by_points = defaultdict(list)
    for item in result.values():
        by_points[item["points"]].append(item)

    def split_ties(group, rules):
        if len(group) < 2 or not rules:
            return [group]
        rule = rules[0]
        buckets = defaultdict(list)
        for item in group:
            value = item.get(rule, 0)
            if rule == "head_to_head" and len(group) == 2:
                pair = {g["team"] for g in group}
                value = sum(m["winner"] == item["team"] for m in matches if {m["a"], m["b"]} == pair)
            buckets[value].append(item)
        return [sub for key in sorted(buckets, reverse=True) for sub in split_ties(buckets[key], rules[1:])]

    groups = [g for points in sorted(by_points, reverse=True)
              for g in split_ties(by_points[points], settings["tie"])]
    complete = len(matches) == 6 and all(m["status"] == "done" for m in matches)
    decision = one(db, "SELECT ranking FROM tie_decisions WHERE room=? AND project=?", (room, project))
    decided = {team: i + 1 for i, team in enumerate(loads(decision["ranking"]))} if decision else {}
    for g in groups:
        if complete:
            for item in g:
                item["tie_group"] = len(ordered) + 1
                item["rank"] = decided.get(item["team"]) if decided else (len(ordered) + 1 if len(g) == 1 else None)
        ordered.extend(sorted(g, key=lambda item: item["rank"] or 0))
    return ordered, complete


def refresh_rankings(db, room, project, settings):
    ranks, complete = standings(db, room, project, settings)
    db.execute("DELETE FROM rankings WHERE room=? AND project=?", (room, project))
    if complete and all(r["rank"] for r in ranks):
        db.executemany("INSERT INTO rankings VALUES(?,?,?,?)",
                       [(room, project, r["team"], r["rank"]) for r in ranks])
